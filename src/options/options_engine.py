"""Options trading engine with entry, lifecycle exits, and persistence-friendly state."""

import asyncio
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from config import settings


class OptionsEngine:
    """Handles options contract selection, order placement, and lifecycle management."""

    def __init__(self, api_key: str, secret_key: str, base_url: str = "https://paper-api.alpaca.markets"):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "APCA-API-KEY-ID": api_key,
            "APCA-API-SECRET-KEY": secret_key,
        }
        # Keyed by contract symbol.
        self.positions: Dict[str, Dict] = {}
        self._quote_cache: Dict[str, Dict] = {}
        self._quote_cache_ttl = 10
        logger.info("Options engine initialized")

    @staticmethod
    def _now_utc() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _now_eastern() -> datetime:
        try:
            import zoneinfo

            return datetime.now(zoneinfo.ZoneInfo("US/Eastern"))
        except Exception:
            try:
                import pytz

                return datetime.now(pytz.timezone("US/Eastern"))
            except Exception:
                return datetime.now(timezone.utc)

    def _get_target_expiry_range(self) -> Tuple[str, str]:
        """Get target expiry range from settings (defaults: 7-21 DTE)."""
        now = self._now_utc()
        min_days = max(1, int(getattr(settings, "OPTIONS_CONTRACT_DTE_MIN", 7)))
        max_days = max(min_days, int(getattr(settings, "OPTIONS_CONTRACT_DTE_MAX", 21)))
        return (
            (now + timedelta(days=min_days)).strftime("%Y-%m-%d"),
            (now + timedelta(days=max_days)).strftime("%Y-%m-%d"),
        )

    @staticmethod
    def _parse_expiry(expiry: str) -> Optional[datetime]:
        if not expiry:
            return None
        try:
            return datetime.strptime(expiry, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _days_to_expiry(self, expiry: str) -> int:
        exp = self._parse_expiry(expiry)
        if not exp:
            return 0
        delta = exp - self._now_utc()
        return max(0, int(math.ceil(delta.total_seconds() / 86400.0)))

    def _is_expiry_day_cleanup_window(self, expiry: str) -> bool:
        """
        Rule 6 guardrail:
        On Friday after 3:30 PM ET, close any contracts expiring the same day.
        """
        if not expiry:
            return False
        try:
            expiry_date = datetime.strptime(expiry, "%Y-%m-%d").date()
        except Exception:
            return False
        now_et = self._now_eastern()
        now_min = now_et.hour * 60 + now_et.minute
        return now_et.weekday() == 4 and expiry_date == now_et.date() and now_min >= (15 * 60 + 30)

    @staticmethod
    def _infer_underlying_from_contract(contract_symbol: str) -> str:
        # OCC symbology starts with underlying letters before first digit.
        letters = []
        for ch in contract_symbol.upper():
            if ch.isdigit():
                break
            letters.append(ch)
        return "".join(letters) or contract_symbol

    @staticmethod
    def _is_weekly(expiry: str) -> bool:
        try:
            return datetime.strptime(expiry, "%Y-%m-%d").weekday() == 4  # Friday
        except Exception:
            return False

    @staticmethod
    def _normal_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    @staticmethod
    def _estimate_sigma(change_pct: float, volume_spike: float) -> float:
        # Lightweight realized-vol proxy from candidate tape.
        base = 0.45
        move_adj = min(0.35, abs(change_pct) / 40.0)
        vol_adj = min(0.20, max(0.0, volume_spike - 1.0) / 20.0)
        return min(1.25, max(0.20, base + move_adj + vol_adj))

    def estimate_delta(
        self,
        underlying_price: float,
        strike: float,
        days_to_expiry: int,
        option_type: str,
        change_pct: float = 0.0,
        volume_spike: float = 1.0,
    ) -> float:
        """
        Approximate option delta without full Greeks feed.
        Returns signed delta (calls positive, puts negative).
        """
        if underlying_price <= 0 or strike <= 0 or days_to_expiry <= 0:
            return 0.0

        t = max(days_to_expiry / 365.0, 1.0 / 365.0)
        sigma = self._estimate_sigma(change_pct, volume_spike)
        denom = sigma * math.sqrt(t)
        if denom <= 0:
            return 0.0

        # Black-Scholes-ish d1 term with r~0.
        d1 = (math.log(underlying_price / strike) + 0.5 * sigma * sigma * t) / denom
        call_delta = max(0.01, min(0.99, self._normal_cdf(d1)))
        if option_type == "put":
            return call_delta - 1.0
        return call_delta

    @staticmethod
    def _spread_threshold_for_underlying(price: float) -> float:
        if price < 20:
            return 0.30
        if price < 50:
            return 0.20
        return 0.15

    @staticmethod
    def _quote_to_prices(quote: Optional[Dict]) -> Tuple[float, float, float]:
        if not quote:
            return 0.0, 0.0, 0.0
        bid = float(quote.get("bp", 0) or 0)
        ask = float(quote.get("ap", 0) or 0)
        if bid > 0 and ask > 0:
            mid = (bid + ask) / 2.0
        else:
            mid = ask or bid or 0.0
        return bid, ask, mid

    def _derive_exit_limit_price(self, contract_symbol: str) -> Tuple[Optional[float], str]:
        """
        Build a marketable limit price for option exits to reduce spread bleed.
        Preference:
          1) bid-based price
          2) mid-based discounted fallback
        """
        quote = self.get_option_quote(contract_symbol, force_refresh=True)
        bid, _, mid = self._quote_to_prices(quote)
        bid_discount_pct = max(0.0, float(getattr(settings, "OPTIONS_EXIT_BID_DISCOUNT_PCT", 0.25)))
        mid_discount_pct = max(0.0, float(getattr(settings, "OPTIONS_EXIT_MID_DISCOUNT_PCT", 2.0)))

        if bid > 0:
            px = max(0.01, bid * (1.0 - bid_discount_pct / 100.0))
            return round(px, 2), "bid"
        if mid > 0:
            px = max(0.01, mid * (1.0 - mid_discount_pct / 100.0))
            return round(px, 2), "mid"
        return None, "none"

    def _fetch_contract_chain(self, symbol: str, option_type: str, min_expiry: str, max_expiry: str) -> List[Dict]:
        resp = httpx.get(
            f"{self.base_url}/v2/options/contracts",
            headers=self._headers,
            params={
                "underlying_symbols": symbol,
                "type": option_type,
                "expiration_date_gte": min_expiry,
                "expiration_date_lte": max_expiry,
                "limit": 100,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.warning(f"Options chain lookup failed for {symbol}: {resp.status_code} {resp.text[:200]}")
            return []
        return resp.json().get("option_contracts", [])

    def find_contract(
        self,
        symbol: str,
        price: float,
        direction: str = "BUY",
        sentiment_data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """
        Find best contract with:
          - target DTE window
          - weekly preference near target DTE
          - delta target around 0.40 abs
          - open-interest floor
          - spread sanity filter by underlying price bucket
        """
        sentiment_data = sentiment_data or {}
        option_type = "call" if direction == "BUY" else "put"
        min_expiry, max_expiry = self._get_target_expiry_range()
        min_oi = int(getattr(settings, "OPTIONS_MIN_OPEN_INTEREST", 10))
        target_dte = int(getattr(settings, "OPTIONS_TARGET_DTE", 12))
        delta_target = float(getattr(settings, "OPTIONS_TARGET_DELTA", 0.40))
        spread_threshold = self._spread_threshold_for_underlying(price)
        change_pct = float(sentiment_data.get("change_pct", 0.0) or 0.0)
        volume_spike = float(sentiment_data.get("volume_spike", 1.0) or 1.0)

        try:
            contracts = self._fetch_contract_chain(symbol, option_type, min_expiry, max_expiry)
            if not contracts:
                logger.info(f"No {option_type} contracts found for {symbol} in {min_expiry}..{max_expiry}")
                return None

            staged: List[Tuple[float, Dict]] = []
            for contract in contracts:
                if not contract.get("tradable"):
                    continue
                strike = float(contract.get("strike_price", 0) or 0)
                if strike <= 0:
                    continue
                expiry = contract.get("expiration_date", "")
                dte = self._days_to_expiry(expiry)
                if dte <= 0:
                    continue

                oi = int(contract.get("open_interest", 0) or 0)
                if oi < min_oi:
                    continue

                delta = self.estimate_delta(
                    underlying_price=price,
                    strike=strike,
                    days_to_expiry=dte,
                    option_type=option_type,
                    change_pct=change_pct,
                    volume_spike=volume_spike,
                )
                abs_delta = abs(delta)

                # Keep around 0.30-0.50 but allow a wider guardrail for sparse chains.
                if not (0.20 <= abs_delta <= 0.70):
                    continue

                delta_pen = abs(abs_delta - delta_target) * 4.0
                dte_pen = abs(dte - target_dte) / max(1.0, target_dte)
                strike_pen = abs(strike - price) / max(price, 1.0)
                weekly_bonus = -0.10 if self._is_weekly(expiry) else 0.0
                oi_bonus = -min(0.20, oi / 2000.0)
                pre_score = delta_pen + dte_pen + strike_pen + weekly_bonus + oi_bonus

                candidate = dict(contract)
                candidate["_estimated_delta"] = round(delta, 4)
                candidate["_pre_score"] = pre_score
                staged.append((pre_score, candidate))

            if not staged:
                logger.info(f"No liquid delta-targeted contracts for {symbol} {option_type}")
                return None

            # Quote only the best preliminary candidates to control latency.
            staged.sort(key=lambda x: x[0])
            best: Optional[Dict] = None
            best_score = float("inf")
            for _, contract in staged[:25]:
                contract_symbol = contract.get("symbol", "")
                quote = self.get_option_quote(contract_symbol)
                bid, ask, mid = self._quote_to_prices(quote)
                if mid <= 0:
                    continue
                spread_pct = ((ask - bid) / mid) if (bid > 0 and ask > 0) else 0.0
                if spread_pct > spread_threshold:
                    continue

                # Final ranking includes spread and premium sanity.
                spread_pen = spread_pct * 2.5
                premium_pen = 0.15 if mid < 0.10 else 0.0
                score = float(contract.get("_pre_score", 0)) + spread_pen + premium_pen
                if score < best_score:
                    best_score = score
                    best = dict(contract)
                    best["_bid"] = bid
                    best["_ask"] = ask
                    best["_mid"] = mid
                    best["_spread_pct"] = round(spread_pct * 100.0, 2)

            if not best:
                logger.info(f"All {symbol} contracts failed spread/liquidity checks")
                return None

            logger.info(
                f"📋 Options contract: {best.get('symbol')} strike=${best.get('strike_price')} "
                f"exp={best.get('expiration_date')} delta≈{best.get('_estimated_delta')} "
                f"spread={best.get('_spread_pct', 0):.2f}%"
            )
            return best
        except Exception as exc:
            logger.error(f"Options chain error for {symbol}: {exc}")
            return None

    def get_option_quote(self, contract_symbol: str, force_refresh: bool = False) -> Optional[Dict]:
        """Get latest option quote (cached for a few seconds)."""
        now_ts = time.time()
        if not force_refresh:
            cached = self._quote_cache.get(contract_symbol)
            if cached and (now_ts - cached.get("ts", 0)) <= self._quote_cache_ttl:
                return cached.get("quote")

        try:
            resp = httpx.get(
                "https://data.alpaca.markets/v1beta1/options/quotes/latest",
                headers=self._headers,
                params={"symbols": contract_symbol, "feed": "indicative"},
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            quote = resp.json().get("quotes", {}).get(contract_symbol)
            self._quote_cache[contract_symbol] = {"quote": quote, "ts": now_ts}
            return quote
        except Exception as exc:
            logger.error(f"Options quote error for {contract_symbol}: {exc}")
            return None

    def get_current_premium(self, contract_symbol: str, force_refresh: bool = False) -> float:
        quote = self.get_option_quote(contract_symbol, force_refresh=force_refresh)
        _, _, mid = self._quote_to_prices(quote)
        return mid

    def place_option_order(
        self,
        contract_symbol: str,
        qty: int = 1,
        side: str = "buy",
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> Optional[Dict]:
        """Place an option order via Alpaca REST."""
        qty = int(qty or 0)
        if qty < 1:
            return None

        order_data = {
            "symbol": contract_symbol,
            "qty": str(qty),
            "side": side,
            "type": order_type,
            "time_in_force": "day",
            "extended_hours": False,
        }
        if order_type == "limit" and limit_price:
            order_data["limit_price"] = str(round(float(limit_price), 2))

        try:
            resp = httpx.post(
                f"{self.base_url}/v2/orders",
                headers=self._headers,
                json=order_data,
                timeout=10,
            )
            if resp.status_code not in (200, 201):
                logger.error(f"Options order failed: {resp.status_code} {resp.text[:200]}")
                return None
            order = resp.json()
            logger.success(
                f"🎯 Options order: {side.upper()} {qty}x {contract_symbol} -> {order.get('id', '')[:8]}"
            )
            return order
        except Exception as exc:
            logger.error(f"Options order error for {contract_symbol}: {exc}")
            return None

    @staticmethod
    def calculate_contract_qty(budget: float, premium_per_contract: float) -> int:
        if budget <= 0 or premium_per_contract <= 0:
            return 0
        max_contracts = int(budget / premium_per_contract)
        return max(0, min(max_contracts, 5))

    async def execute_option_trade(
        self,
        symbol: str,
        price: float,
        direction: str,
        budget: float,
        sentiment_data: Optional[Dict] = None,
    ) -> Optional[Dict]:
        """
        Entry flow:
          1. Select contract.
          2. Price premium from live quote.
          3. Size in whole contracts.
          4. Place buy order and track position state.
        """
        sentiment_data = sentiment_data or {}
        if budget <= 0 or price <= 0:
            return None

        from functools import partial

        loop = asyncio.get_event_loop()
        contract = await loop.run_in_executor(
            None,
            partial(self.find_contract, symbol, price, direction, sentiment_data=sentiment_data),
        )
        if not contract:
            logger.info(f"No suitable options contract for {symbol} {direction}")
            return None

        contract_symbol = contract.get("symbol", "")
        if not contract_symbol:
            return None

        bid = float(contract.get("_bid", 0) or 0)
        ask = float(contract.get("_ask", 0) or 0)
        mid = float(contract.get("_mid", 0) or 0)
        if ask <= 0:
            # Fall back to quote lookup if selection path did not hydrate.
            quote = await loop.run_in_executor(
                None, partial(self.get_option_quote, contract_symbol, True)
            )
            bid, ask, mid = self._quote_to_prices(quote)

        premium = ask or mid
        if premium <= 0:
            # Conservative fallback estimate if quote is missing.
            premium = max(0.10, price * 0.04)

        premium_per_contract = premium * 100.0
        qty = self.calculate_contract_qty(budget, premium_per_contract)
        if qty < 1:
            logger.info(
                f"Budget ${budget:.0f} insufficient for {contract_symbol} "
                f"(premium ~${premium_per_contract:.0f}/contract)"
            )
            return None

        order = await loop.run_in_executor(
            None,
            partial(
                self.place_option_order,
                contract_symbol=contract_symbol,
                qty=qty,
                side="buy",
                order_type="limit",
                limit_price=round(premium * 1.02, 2),  # small price-improvement buffer
            ),
        )
        if not order:
            return None

        signal_sources = sentiment_data.get("signal_sources", ["unknown"])
        if isinstance(signal_sources, str):
            signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
        if not isinstance(signal_sources, list) or not signal_sources:
            signal_sources = ["unknown"]

        entry_ts = time.time()
        position = {
            "asset_type": "option",
            "underlying": symbol,
            "contract_symbol": contract_symbol,
            "symbol": contract_symbol,
            "direction": direction,
            "option_type": "call" if direction == "BUY" else "put",
            "strike": float(contract.get("strike_price", 0) or 0),
            "expiry": contract.get("expiration_date", ""),
            "qty": int(qty),
            "entry_premium": float(premium),
            "current_premium": float(mid or premium),
            "premium_hwm": float(mid or premium),
            "premium_trail_pct": float(getattr(settings, "OPTIONS_PREMIUM_TRAIL_PCT", 35.0)),
            "premium_stop_loss_pct": float(getattr(settings, "OPTIONS_PREMIUM_STOP_LOSS_PCT", 50.0)),
            "premium_profit_target_pct": float(getattr(settings, "OPTIONS_PREMIUM_PROFIT_TARGET_PCT", 100.0)),
            "premium_triple_target_mult": float(getattr(settings, "OPTIONS_PREMIUM_TRIPLE_TARGET_MULT", 3.0)),
            "partial_exit_done": False,
            "triple_partial_exit_done": False,
            "delta_at_entry": float(contract.get("_estimated_delta", 0.0) or 0.0),
            "underlying_entry_price": float(price),
            "underlying_last_price": float(price),
            "total_cost": round(qty * premium * 100.0, 2),
            "entry_time": entry_ts,
            "order_id": order.get("id"),
            "status": "open",
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "signal_sources": signal_sources,
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "signal_price": sentiment_data.get("signal_price", price),
            "decision_price": sentiment_data.get("decision_price", price),
        }
        self.positions[contract_symbol] = position

        logger.success(
            f"🎯 OPTIONS ENTRY: {qty}x {contract_symbol} "
            f"({position['option_type']} ${position['strike']}, exp {position['expiry']}) "
            f"@ ~${premium:.2f} = ${position['total_cost']:.0f}"
        )
        return dict(position)

    def load_positions(self, positions: Dict[str, Dict]):
        """Load persisted options positions into engine memory."""
        self.positions = {}
        for contract_symbol, pos in (positions or {}).items():
            if not isinstance(pos, dict):
                continue
            normalized = dict(pos)
            normalized.setdefault("asset_type", "option")
            normalized.setdefault("symbol", contract_symbol)
            normalized.setdefault("contract_symbol", contract_symbol)
            normalized.setdefault("qty", int(normalized.get("quantity", 0) or 0))
            normalized["qty"] = int(normalized.get("qty", 0) or 0)
            if normalized["qty"] <= 0:
                continue
            normalized.setdefault("entry_premium", float(normalized.get("entry_price", 0) or 0))
            normalized.setdefault("current_premium", normalized["entry_premium"])
            if "premium_hwm" not in normalized and "peak_premium" in normalized:
                normalized["premium_hwm"] = float(normalized.get("peak_premium", normalized["entry_premium"]) or normalized["entry_premium"])
            normalized.setdefault("premium_hwm", normalized["entry_premium"])
            normalized.setdefault("premium_trail_pct", float(getattr(settings, "OPTIONS_PREMIUM_TRAIL_PCT", 35.0)))
            normalized.setdefault("premium_stop_loss_pct", float(getattr(settings, "OPTIONS_PREMIUM_STOP_LOSS_PCT", 50.0)))
            normalized.setdefault("premium_profit_target_pct", float(getattr(settings, "OPTIONS_PREMIUM_PROFIT_TARGET_PCT", 100.0)))
            normalized.setdefault("premium_triple_target_mult", float(getattr(settings, "OPTIONS_PREMIUM_TRIPLE_TARGET_MULT", 3.0)))
            normalized.setdefault("partial_exit_done", False)
            normalized.setdefault("triple_partial_exit_done", False)
            normalized.setdefault("strategy_tag", "unknown")
            normalized.setdefault("signal_sources", ["unknown"])
            normalized.setdefault("decision_confidence", 0)
            normalized.setdefault("provider_used", "")
            normalized.setdefault("signal_price", normalized.get("underlying_entry_price", 0))
            normalized.setdefault("decision_price", normalized.get("underlying_entry_price", 0))
            normalized.setdefault("status", "open")
            self.positions[contract_symbol] = normalized

    def get_options_positions(self) -> List[Dict]:
        return list(self.positions.values())

    def get_positions_by_underlying(self, symbol: str) -> List[Dict]:
        return [p for p in self.positions.values() if p.get("underlying") == symbol and int(p.get("qty", 0) or 0) > 0]

    def get_open_premium_exposure(self) -> float:
        exposure = 0.0
        for pos in self.positions.values():
            qty = int(pos.get("qty", 0) or 0)
            if qty <= 0:
                continue
            premium = float(pos.get("current_premium", pos.get("entry_premium", 0)) or 0)
            if premium <= 0:
                premium = float(pos.get("entry_premium", 0) or 0)
            exposure += qty * premium * 100.0
        return round(exposure, 2)

    def get_underlying_price(self, symbol: str) -> float:
        """Best-effort latest stock price for underlying move attribution."""
        if not symbol:
            return 0.0
        try:
            resp = httpx.get(
                "https://data.alpaca.markets/v2/stocks/trades/latest",
                headers=self._headers,
                params={"symbols": symbol, "feed": "iex"},
                timeout=8,
            )
            if resp.status_code != 200:
                return 0.0
            data = resp.json().get("trades", {}).get(symbol, {})
            return float(data.get("p", 0) or 0)
        except Exception:
            return 0.0

    def check_exit_rules(self, contract_symbol: str) -> Optional[Dict]:
        """
        Evaluate premium-based exits.
        Priority:
          1) Time decay (DTE threshold)
          2) Hard stop loss
          3) Profit target (partial or tighten)
          4) Premium trailing stop
        """
        pos = self.positions.get(contract_symbol)
        if not pos:
            return None

        qty = int(pos.get("qty", 0) or 0)
        if qty < 1 or pos.get("status") in ("closing", "closed"):
            return None

        current_premium = self.get_current_premium(contract_symbol, force_refresh=True)
        if current_premium <= 0:
            return None

        pos["current_premium"] = current_premium
        pos["premium_hwm"] = max(float(pos.get("premium_hwm", current_premium) or current_premium), current_premium)
        pos["last_quote_time"] = time.time()

        underlying_symbol = pos.get("underlying", "")
        underlying_price = self.get_underlying_price(underlying_symbol)
        if underlying_price > 0:
            pos["underlying_last_price"] = underlying_price

        entry = float(pos.get("entry_premium", 0) or 0)
        if entry <= 0:
            return None

        # 0) Friday 3:30 PM ET expiry-day cleanup.
        if self._is_expiry_day_cleanup_window(pos.get("expiry", "")):
            return {
                "action": "close",
                "qty": qty,
                "reason": "expiry_day_cleanup",
                "current_premium": current_premium,
                "days_to_expiry": self._days_to_expiry(pos.get("expiry", "")),
            }

        # 1) Time-decay guard.
        min_dte = int(getattr(settings, "OPTIONS_MIN_DAYS_TO_EXPIRY", 2))
        dte = self._days_to_expiry(pos.get("expiry", ""))
        if dte <= min_dte:
            return {
                "action": "close",
                "qty": qty,
                "reason": "time_decay_exit",
                "current_premium": current_premium,
                "days_to_expiry": dte,
            }

        # 2) Hard stop loss on premium.
        stop_loss_pct = float(pos.get("premium_stop_loss_pct", 50.0) or 50.0)
        stop_loss_premium = entry * (1.0 - stop_loss_pct / 100.0)
        if current_premium <= stop_loss_premium:
            return {
                "action": "close",
                "qty": qty,
                "reason": "premium_stop_loss",
                "current_premium": current_premium,
                "stop_loss_premium": stop_loss_premium,
            }

        # 3) Profit target with partial and tighter trailing.
        profit_target_pct = float(pos.get("premium_profit_target_pct", 100.0) or 100.0)
        profit_target_premium = entry * (1.0 + profit_target_pct / 100.0)
        if current_premium >= profit_target_premium and not bool(pos.get("partial_exit_done", False)):
            tight_trail = float(getattr(settings, "OPTIONS_PREMIUM_TIGHT_TRAIL_PCT", 20.0))
            pos["premium_trail_pct"] = min(float(pos.get("premium_trail_pct", 35.0) or 35.0), tight_trail)
            pos["partial_exit_done"] = True
            if qty >= 2:
                return {
                    "action": "partial_take_profit",
                    "qty": max(1, qty // 2),
                    "reason": "premium_profit_target_partial",
                    "current_premium": current_premium,
                    "profit_target_premium": profit_target_premium,
                }
            return {
                "action": "tighten_trail",
                "qty": 0,
                "reason": "premium_profit_target_tighten",
                "current_premium": current_premium,
                "profit_target_premium": profit_target_premium,
            }

        # 3b) Explicit 3x premium rule: sell half of remaining after first target.
        triple_target_mult = float(
            pos.get("premium_triple_target_mult", getattr(settings, "OPTIONS_PREMIUM_TRIPLE_TARGET_MULT", 3.0)) or 3.0
        )
        triple_target_mult = max(1.0, triple_target_mult)
        triple_target_premium = entry * triple_target_mult
        if (
            bool(pos.get("partial_exit_done", False))
            and not bool(pos.get("triple_partial_exit_done", False))
            and current_premium >= triple_target_premium
        ):
            tight_trail = float(getattr(settings, "OPTIONS_PREMIUM_TIGHT_TRAIL_PCT", 20.0))
            pos["premium_trail_pct"] = min(float(pos.get("premium_trail_pct", 35.0) or 35.0), tight_trail)
            pos["triple_partial_exit_done"] = True
            if qty >= 2:
                return {
                    "action": "partial_take_profit",
                    "qty": max(1, qty // 2),
                    "reason": "premium_triple_target_partial",
                    "current_premium": current_premium,
                    "triple_target_premium": triple_target_premium,
                }
            return {
                "action": "tighten_trail",
                "qty": 0,
                "reason": "premium_triple_target_tighten",
                "current_premium": current_premium,
                "triple_target_premium": triple_target_premium,
            }

        # 4) Trailing stop on premium giveback.
        trail_pct = float(pos.get("premium_trail_pct", 35.0) or 35.0)
        hwm = float(pos.get("premium_hwm", entry) or entry)
        trail_trigger = hwm * (1.0 - trail_pct / 100.0)
        if hwm > entry and current_premium <= trail_trigger:
            return {
                "action": "close",
                "qty": qty,
                "reason": "premium_trailing_stop",
                "current_premium": current_premium,
                "trail_trigger_premium": trail_trigger,
            }

        return None

    def close_option_position(self, contract_symbol: str, qty: Optional[int] = None, reason: str = "manual") -> Optional[Dict]:
        """Place sell order for an option position."""
        pos = self.positions.get(contract_symbol)
        if not pos:
            return None
        if pos.get("status") == "closing":
            return None

        held_qty = int(pos.get("qty", 0) or 0)
        if held_qty < 1:
            return None
        sell_qty = min(held_qty, int(qty if qty is not None else held_qty))
        if sell_qty < 1:
            return None

        use_limit_exit = bool(getattr(settings, "OPTIONS_EXIT_BID_AWARE_LIMIT", True))
        market_fallback = bool(getattr(settings, "OPTIONS_EXIT_MARKET_FALLBACK", True))

        order = None
        limit_source = ""
        limit_price = None
        if use_limit_exit:
            limit_price, limit_source = self._derive_exit_limit_price(contract_symbol)
            if limit_price and limit_price > 0:
                order = self.place_option_order(
                    contract_symbol=contract_symbol,
                    qty=sell_qty,
                    side="sell",
                    order_type="limit",
                    limit_price=limit_price,
                )

        if not order and not market_fallback:
            logger.warning(
                f"Options exit skipped for {contract_symbol}: no limit quote and market fallback disabled"
            )
            return None

        if not order:
            order = self.place_option_order(
                contract_symbol=contract_symbol,
                qty=sell_qty,
                side="sell",
                order_type="market",
            )
        if not order:
            return None

        if limit_price and order.get("type") == "limit":
            order["exit_limit_price"] = limit_price
            order["exit_price_source"] = limit_source
            order["exit_order_type"] = "limit"
        else:
            order["exit_order_type"] = "market"

        pos["status"] = "closing"
        pos["close_order_id"] = order.get("id")
        pos["close_order_type"] = order.get("exit_order_type", order.get("type", "market"))
        pos["pending_close_qty"] = sell_qty
        pos["last_exit_reason"] = reason
        return order

    def finalize_exit(self, contract_symbol: str, qty: int, exit_premium: float, reason: str) -> Optional[Dict]:
        """Finalize an option exit into a trade record and mutate position state."""
        pos = self.positions.get(contract_symbol)
        if not pos:
            return None

        held_qty = int(pos.get("qty", 0) or 0)
        exit_qty = min(held_qty, int(qty or 0))
        if exit_qty < 1:
            return None

        if exit_premium <= 0:
            exit_premium = self.get_current_premium(contract_symbol, force_refresh=True)
        if exit_premium <= 0:
            exit_premium = float(pos.get("entry_premium", 0) or 0)

        entry_premium = float(pos.get("entry_premium", 0) or 0)
        pnl = (exit_premium - entry_premium) * exit_qty * 100.0
        pnl_pct = ((exit_premium - entry_premium) / entry_premium * 100.0) if entry_premium > 0 else 0.0

        underlying_entry = float(pos.get("underlying_entry_price", 0) or 0)
        underlying_last = float(pos.get("underlying_last_price", underlying_entry) or underlying_entry)
        underlying_move_pct = (
            ((underlying_last - underlying_entry) / underlying_entry) * 100.0 if underlying_entry > 0 else 0.0
        )

        now_ts = time.time()
        trade_record = {
            "asset_type": "option",
            "symbol": contract_symbol,
            "contract_symbol": contract_symbol,
            "underlying": pos.get("underlying", self._infer_underlying_from_contract(contract_symbol)),
            "option_type": pos.get("option_type", ""),
            "strike": float(pos.get("strike", 0) or 0),
            "expiry": pos.get("expiry", ""),
            "side": "sell",
            "entry_price": entry_premium,
            "exit_price": exit_premium,
            "entry_premium": entry_premium,
            "exit_premium": exit_premium,
            "quantity": exit_qty,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "hold_seconds": now_ts - float(pos.get("entry_time", now_ts) or now_ts),
            "entry_time": float(pos.get("entry_time", now_ts) or now_ts),
            "exit_time": now_ts,
            "delta_at_entry": float(pos.get("delta_at_entry", 0.0) or 0.0),
            "underlying_move_pct": underlying_move_pct,
            "strategy_tag": pos.get("strategy_tag", "unknown"),
            "signal_sources": pos.get("signal_sources", ["unknown"]),
            "decision_confidence": pos.get("decision_confidence", 0),
            "provider_used": pos.get("provider_used", ""),
            "signal_price": pos.get("signal_price", underlying_entry),
            "decision_price": pos.get("decision_price", underlying_entry),
            "fill_price": exit_premium,
            "slippage_bps": 0.0,
        }

        remaining = held_qty - exit_qty
        if remaining <= 0:
            self.positions.pop(contract_symbol, None)
        else:
            pos["qty"] = remaining
            pos["status"] = "open"
            pos["pending_close_qty"] = 0
            pos["total_cost"] = round(remaining * entry_premium * 100.0, 2)
            self.positions[contract_symbol] = pos

        return trade_record

    def build_external_close_trade(self, pos: Dict, reason: str = "options_reconcile_closed") -> Optional[Dict]:
        """Build a trade record for a position that disappeared externally (manual close/broker action)."""
        if not pos:
            return None
        qty = int(pos.get("qty", 0) or 0)
        if qty < 1:
            return None
        contract_symbol = pos.get("contract_symbol") or pos.get("symbol")
        if not contract_symbol:
            return None

        entry_premium = float(pos.get("entry_premium", 0) or 0)
        exit_premium = float(pos.get("current_premium", entry_premium) or entry_premium)
        pnl = (exit_premium - entry_premium) * qty * 100.0
        pnl_pct = ((exit_premium - entry_premium) / entry_premium * 100.0) if entry_premium > 0 else 0.0
        now_ts = time.time()
        underlying_entry = float(pos.get("underlying_entry_price", 0) or 0)
        underlying_last = float(pos.get("underlying_last_price", underlying_entry) or underlying_entry)
        underlying_move_pct = (
            ((underlying_last - underlying_entry) / underlying_entry) * 100.0 if underlying_entry > 0 else 0.0
        )

        return {
            "asset_type": "option",
            "symbol": contract_symbol,
            "contract_symbol": contract_symbol,
            "underlying": pos.get("underlying", self._infer_underlying_from_contract(contract_symbol)),
            "option_type": pos.get("option_type", ""),
            "strike": float(pos.get("strike", 0) or 0),
            "expiry": pos.get("expiry", ""),
            "side": "sell",
            "entry_price": entry_premium,
            "exit_price": exit_premium,
            "entry_premium": entry_premium,
            "exit_premium": exit_premium,
            "quantity": qty,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "hold_seconds": now_ts - float(pos.get("entry_time", now_ts) or now_ts),
            "entry_time": float(pos.get("entry_time", now_ts) or now_ts),
            "exit_time": now_ts,
            "delta_at_entry": float(pos.get("delta_at_entry", 0.0) or 0.0),
            "underlying_move_pct": underlying_move_pct,
            "strategy_tag": pos.get("strategy_tag", "unknown"),
            "signal_sources": pos.get("signal_sources", ["unknown"]),
            "decision_confidence": pos.get("decision_confidence", 0),
            "provider_used": pos.get("provider_used", ""),
            "signal_price": pos.get("signal_price", underlying_entry),
            "decision_price": pos.get("decision_price", underlying_entry),
            "fill_price": exit_premium,
            "slippage_bps": 0.0,
        }

    def close_paired_options(self, underlying_symbol: str, reason: str = "underlying_exit") -> List[Dict]:
        """Close all option positions linked to an underlying symbol."""
        exits: List[Dict] = []
        for pos in list(self.get_positions_by_underlying(underlying_symbol)):
            contract_symbol = pos.get("contract_symbol")
            qty = int(pos.get("qty", 0) or 0)
            if not contract_symbol or qty < 1:
                continue
            premium = self.get_current_premium(contract_symbol, force_refresh=True)
            order = self.close_option_position(contract_symbol, qty=qty, reason=reason)
            if not order:
                continue
            trade = self.finalize_exit(contract_symbol, qty=qty, exit_premium=premium, reason=reason)
            if trade:
                exits.append(trade)
        return exits

    def reconcile_with_broker(self) -> Dict[str, object]:
        """
        Best-effort reconciliation against Alpaca /v2/positions.
        Treats the REST snapshot as source of truth for currently open option symbols.
        """
        removed = 0
        added = 0
        removed_positions: List[Dict] = []
        try:
            resp = httpx.get(f"{self.base_url}/v2/positions", headers=self._headers, timeout=10)
            if resp.status_code != 200:
                return {"removed": 0, "added": 0, "removed_positions": []}
            rows = resp.json() if isinstance(resp.json(), list) else []
            broker_open = {}
            for row in rows:
                symbol = str(row.get("symbol", "")).upper().strip()
                asset_class = str(row.get("asset_class", "")).lower()
                if not symbol:
                    continue
                if asset_class not in ("us_option", "option"):
                    continue
                broker_open[symbol] = row

            local_symbols = set(self.positions.keys())
            broker_symbols = set(broker_open.keys())

            for symbol in list(local_symbols - broker_symbols):
                removed_pos = self.positions.pop(symbol, None)
                if removed_pos:
                    removed_positions.append(removed_pos)
                removed += 1

            for symbol in sorted(broker_symbols - local_symbols):
                row = broker_open[symbol]
                qty = int(abs(float(row.get("qty", 0) or 0)))
                if qty < 1:
                    continue
                avg = float(row.get("avg_entry_price", 0) or 0)
                underlying = row.get("underlying_symbol") or self._infer_underlying_from_contract(symbol)
                self.positions[symbol] = {
                    "asset_type": "option",
                    "symbol": symbol,
                    "contract_symbol": symbol,
                    "underlying": underlying,
                    "option_type": "call" if "C" in symbol else "put",
                    "strike": float(row.get("strike_price", 0) or 0),
                    "expiry": row.get("expiration_date", ""),
                    "qty": qty,
                    "entry_premium": avg,
                    "current_premium": avg,
                    "premium_hwm": avg,
                    "premium_trail_pct": float(getattr(settings, "OPTIONS_PREMIUM_TRAIL_PCT", 35.0)),
                    "premium_stop_loss_pct": float(getattr(settings, "OPTIONS_PREMIUM_STOP_LOSS_PCT", 50.0)),
                    "premium_profit_target_pct": float(getattr(settings, "OPTIONS_PREMIUM_PROFIT_TARGET_PCT", 100.0)),
                    "premium_triple_target_mult": float(getattr(settings, "OPTIONS_PREMIUM_TRIPLE_TARGET_MULT", 3.0)),
                    "partial_exit_done": False,
                    "triple_partial_exit_done": False,
                    "delta_at_entry": 0.0,
                    "underlying_entry_price": 0.0,
                    "underlying_last_price": 0.0,
                    "total_cost": round(qty * avg * 100.0, 2),
                    "entry_time": time.time(),
                    "status": "open",
                    "strategy_tag": "broker_sync_option",
                    "signal_sources": ["broker_sync"],
                    "decision_confidence": 0,
                    "provider_used": "",
                    "signal_price": 0.0,
                    "decision_price": 0.0,
                }
                added += 1
        except Exception as exc:
            logger.debug(f"Options reconcile error: {exc}")
        return {"removed": removed, "added": added, "removed_positions": removed_positions}

    def get_positions_snapshot(self, refresh_quotes: bool = False) -> List[Dict]:
        """Return dashboard-friendly options positions with live metrics."""
        snapshot = []
        for contract_symbol, pos in list(self.positions.items()):
            qty = int(pos.get("qty", 0) or 0)
            if qty < 1:
                continue
            quote = self.get_option_quote(contract_symbol, force_refresh=refresh_quotes)
            bid, ask, mid = self._quote_to_prices(quote)
            premium = mid
            if premium > 0:
                pos["current_premium"] = premium
                pos["premium_hwm"] = max(float(pos.get("premium_hwm", premium) or premium), premium)
            current = float(pos.get("current_premium", pos.get("entry_premium", 0)) or 0)
            entry = float(pos.get("entry_premium", 0) or 0)
            pnl = (current - entry) * qty * 100.0
            pnl_pct = ((current - entry) / entry * 100.0) if entry > 0 else 0.0
            hwm = float(pos.get("premium_hwm", entry) or entry)
            trail_pct = float(pos.get("premium_trail_pct", getattr(settings, "OPTIONS_PREMIUM_TRAIL_PCT", 35.0)) or 35.0)
            stop_loss_pct = float(pos.get("premium_stop_loss_pct", getattr(settings, "OPTIONS_PREMIUM_STOP_LOSS_PCT", 50.0)) or 50.0)
            profit_target_pct = float(pos.get("premium_profit_target_pct", getattr(settings, "OPTIONS_PREMIUM_PROFIT_TARGET_PCT", 100.0)) or 100.0)
            triple_target_mult = float(pos.get("premium_triple_target_mult", getattr(settings, "OPTIONS_PREMIUM_TRIPLE_TARGET_MULT", 3.0)) or 3.0)
            snapshot.append(
                {
                    "contract_symbol": contract_symbol,
                    "underlying": pos.get("underlying", ""),
                    "option_type": pos.get("option_type", ""),
                    "strike": float(pos.get("strike", 0) or 0),
                    "expiry": pos.get("expiry", ""),
                    "days_to_expiry": self._days_to_expiry(pos.get("expiry", "")),
                    "qty": qty,
                    "entry_premium": round(entry, 4),
                    "current_premium": round(current, 4),
                    "bid": round(bid, 4) if bid > 0 else 0.0,
                    "ask": round(ask, 4) if ask > 0 else 0.0,
                    "premium_hwm": round(hwm, 4),
                    "peak_premium": round(hwm, 4),
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "status": pos.get("status", "open"),
                    "strategy_tag": pos.get("strategy_tag", "unknown"),
                    "signal_sources": pos.get("signal_sources", ["unknown"]),
                    "decision_confidence": pos.get("decision_confidence", 0),
                    "provider_used": pos.get("provider_used", ""),
                    "trail_trigger_premium": round(hwm * (1.0 - trail_pct / 100.0), 4),
                    "stop_loss_premium": round(entry * (1.0 - stop_loss_pct / 100.0), 4),
                    "profit_target_premium": round(entry * (1.0 + profit_target_pct / 100.0), 4),
                    "triple_target_premium": round(entry * triple_target_mult, 4),
                }
            )
        return snapshot
