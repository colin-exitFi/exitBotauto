"""
Entry Manager - Validate conditions, size positions, execute via Alpaca.
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from loguru import logger

from config import settings
from src.data import strategy_controls


class EntryManager:
    """
    Entry flow:
      1. Validate: sentiment > threshold, risk approves, market open
      2. Size: % of buying power
      3. Execute: Alpaca limit order, 30s timeout, up to 3 retries
    """

    def __init__(self, alpaca_client=None, polygon_client=None, risk_manager=None):
        self.broker = alpaca_client
        self.polygon = polygon_client
        self.risk = risk_manager

        self.min_sentiment = settings.MIN_ENTRY_SENTIMENT
        self.max_retries = 3
        self.order_timeout = 30  # seconds

        self.max_chase_pct = settings.MAX_PRICE_CHASE_PCT

        # Track active positions (symbol -> position dict)
        self.positions: Dict[str, Dict] = {}
        self.last_gate: Dict[str, str] = {}
        self.last_order_error: str = ""
        self._halted_symbols = set()
        # Load existing brokerage positions on init
        self._load_brokerage_positions()
        logger.info("Entry manager initialized")

    def is_market_open(self) -> bool:
        """Check if US stock market is open including extended hours (4:00 AM - 8:00 PM ET)."""
        from pytz import timezone as tz
        try:
            et = tz("US/Eastern")
        except Exception:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")

        now = datetime.now(et)

        # Weekday check (0=Mon, 6=Sun)
        if now.weekday() >= 5:
            return False
        extended_open = now.replace(hour=4, minute=0, second=0, microsecond=0)
        extended_close = now.replace(hour=20, minute=0, second=0, microsecond=0)
        return extended_open <= now <= extended_close

    def is_extended_hours(self) -> bool:
        """Check if market is in extended hours (before 9:30 AM or after 4:00 PM ET)."""
        from pytz import timezone as tz
        try:
            et = tz("US/Eastern")
        except Exception:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")

        now = datetime.now(et)
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return now < market_open or now >= market_close

    @staticmethod
    def _parse_iso_ts(value) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _carryover_fallback_entry_time() -> float:
        try:
            import zoneinfo

            et = zoneinfo.ZoneInfo("US/Eastern")
        except Exception:
            from pytz import timezone as tz

            et = tz("US/Eastern")

        now_et = datetime.now(et)
        if now_et.weekday() == 0:
            fallback_day = now_et - timedelta(days=3)
        else:
            fallback_day = now_et - timedelta(days=1)
        fallback_midnight = fallback_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return fallback_midnight.timestamp()

    def _estimate_carryover_entry_time(
        self,
        symbol: str,
        side: str,
        closed_orders: Optional[List[Dict]],
    ) -> Tuple[float, str]:
        fallback = self._carryover_fallback_entry_time()
        if not closed_orders:
            return fallback, "broker_fallback"

        deltas = []
        for order in closed_orders:
            if str(order.get("symbol", "")).upper() != str(symbol).upper():
                continue
            order_side = str(order.get("side", "") or "").lower()
            if order_side not in ("buy", "sell"):
                continue
            try:
                filled_qty = float(order.get("filled_qty", order.get("qty", 0)) or 0)
            except Exception:
                filled_qty = 0.0
            if filled_qty <= 0:
                continue
            ts = self._parse_iso_ts(
                order.get("filled_at")
                or order.get("submitted_at")
                or order.get("created_at")
            )
            if ts is None:
                continue
            signed_delta = filled_qty if order_side == "buy" else -filled_qty
            deltas.append((ts, signed_delta))

        if not deltas:
            return fallback, "broker_fallback"

        deltas.sort(key=lambda item: item[0])
        net_qty = 0.0
        entry_time = None
        for ts, delta in deltas:
            prev_qty = net_qty
            net_qty += delta
            if abs(net_qty) < 1e-6:
                net_qty = 0.0

            prev_sign = 1 if prev_qty > 0 else (-1 if prev_qty < 0 else 0)
            new_sign = 1 if net_qty > 0 else (-1 if net_qty < 0 else 0)
            if new_sign == 0:
                entry_time = None
                continue
            if prev_sign == 0 or prev_sign != new_sign:
                entry_time = ts

        target_sign = -1 if side == "short" else 1
        final_sign = 1 if net_qty > 0 else (-1 if net_qty < 0 else 0)
        if final_sign == target_sign and entry_time is not None:
            return entry_time, "broker_orders"
        return fallback, "broker_fallback"

    async def _cancel_conflicting_protection_orders(self, symbol: str, side: str) -> int:
        if not self.broker:
            return 0
        cancel_fn = None
        if side == "short" and hasattr(self.broker, "cancel_open_buys_for_symbol"):
            cancel_fn = self.broker.cancel_open_buys_for_symbol
        elif side != "short" and hasattr(self.broker, "cancel_open_sells_for_symbol"):
            cancel_fn = self.broker.cancel_open_sells_for_symbol
        if not cancel_fn:
            return 0
        try:
            cancelled = await asyncio.get_event_loop().run_in_executor(None, cancel_fn, symbol)
            return int(cancelled or 0)
        except Exception as e:
            logger.warning(f"Could not cancel conflicting protection orders for {symbol}: {e}")
            return 0

    async def _place_entry_protection_order(self, symbol: str, qty: int, trail_pct: float, side: str):
        if qty < 1:
            return None, False
        if side == "short" and hasattr(self.broker, "place_trailing_stop_short"):
            trail_fn = self.broker.place_trailing_stop_short
        elif hasattr(self.broker, "place_trailing_stop"):
            trail_fn = self.broker.place_trailing_stop
        else:
            return None, False

        order = await asyncio.get_event_loop().run_in_executor(None, trail_fn, symbol, qty, trail_pct)
        if order:
            return order, False

        for attempt in range(1, 4):
            cancelled = await self._cancel_conflicting_protection_orders(symbol, side)
            if cancelled:
                logger.info(
                    f"Cancelled {cancelled} conflicting protection orders for {symbol} before retry {attempt}/3"
                )
            await asyncio.sleep(1)
            order = await asyncio.get_event_loop().run_in_executor(None, trail_fn, symbol, qty, trail_pct)
            if order:
                return order, False

        logger.critical(f"Protection placement failed for {symbol} after 3 retries")
        return None, True

    def _set_gate(self, symbol: str, allowed: bool, reason: str):
        self.last_gate = {"symbol": symbol, "allowed": allowed, "reason": reason}
        return allowed

    @staticmethod
    def _copy_trader_size_multiplier(sentiment_data: Dict, swing_only: bool) -> float:
        if swing_only:
            return 1.0
        try:
            multiplier = float(sentiment_data.get("copy_trader_size_multiplier", 1.0) or 1.0)
        except Exception:
            multiplier = 1.0
        return max(0.75, min(1.25, multiplier))

    def _apply_strategy_controls(self, symbol: str, sentiment_data: Dict, notional: float) -> Optional[float]:
        controls = strategy_controls.load_controls()
        strategy_tag = str(sentiment_data.get("strategy_tag", "unknown") or "unknown")
        disabled = strategy_controls.get_effective_disabled(controls)
        if strategy_tag in disabled:
            logger.warning(f"⛔ Strategy '{strategy_tag}' is disabled — blocking entry for {symbol}")
            self.last_order_error = "strategy_disabled"
            return None

        size_mult = strategy_controls.get_size_multiplier(strategy_tag, controls)
        if size_mult < 1.0:
            logger.info(f"📉 Strategy '{strategy_tag}' size reduced to {size_mult:.0%} by control plane")
            notional *= size_mult
        return notional

    async def can_enter(self, symbol: str, sentiment_score: float, current_positions: List[Dict]) -> bool:
        """Check all entry conditions."""
        self.last_order_error = ""
        if not self.is_market_open():
            logger.debug("Market closed, cannot enter")
            return self._set_gate(symbol, False, "market_closed")

        if symbol in getattr(self, "_halted_symbols", set()):
            logger.info(f"⛔ {symbol} is halted — blocking entry")
            return self._set_gate(symbol, False, "halted")

        if symbol in self.positions:
            logger.info(f"⛔ Already in position: {symbol} — duplicate entry blocked")
            return self._set_gate(symbol, False, "already_held")

        if sentiment_score < self.min_sentiment:
            logger.info(f"⛔ {symbol} sentiment {sentiment_score:.2f} < threshold {self.min_sentiment}")
            return self._set_gate(symbol, False, "sentiment_below_threshold")

        if self.risk and not self.risk.can_open_position(current_positions, symbol=symbol):
            return self._set_gate(symbol, False, "risk_open_position_block")

        if self.risk and not self.risk.can_enter_sector(symbol, current_positions):
            return self._set_gate(symbol, False, "sector_block")

        return self._set_gate(symbol, True, "ok")

    async def enter_position(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Execute entry: get price → size → limit order → wait for fill.
        Returns position dict on success, None on failure.
        """
        if not self.broker or not self.polygon:
            logger.error("Broker or Polygon client not available")
            self.last_order_error = "broker_or_polygon_unavailable"
            return None
        self.last_order_error = ""
        if symbol in getattr(self, "_halted_symbols", set()):
            logger.warning(f"Entry blocked for halted symbol {symbol}")
            self.last_order_error = "halted"
            return None
        if symbol in self.positions:
            logger.warning(f"Duplicate long entry blocked for {symbol}")
            self.last_order_error = "duplicate_position"
            return None

        # Get current price
        price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if price <= 0:
            logger.warning(f"Could not get price for {symbol}")
            self.last_order_error = "price_unavailable"
            return None
        signal_timestamp = float(sentiment_data.get("signal_timestamp", time.time()) or time.time())

        # Consensus already ran in main loop — use the modifier passed in sentiment_data
        consensus_size_modifier = sentiment_data.get("consensus_size_modifier", 1.0)

        # Get buying power (cash account aware)
        balances = await asyncio.get_event_loop().run_in_executor(
            None, self.broker.get_balances
        )
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)
        swing_only = bool(self.risk and getattr(self.risk, "is_swing_mode", None) and self.risk.is_swing_mode())

        # Extended hours adjustments
        extended = self.is_extended_hours()
        if extended:
            logger.info(f"Extended hours detected — forcing limit orders, reducing size")

        # Signal price for chase detection
        signal_price = price

        # Dynamic position sizing from risk tier
        # Determine conviction from sentiment strength
        sent_score = sentiment_data.get("score", 0)
        if sent_score > 0.6:
            conviction = "high"
        elif sent_score < 0.1:
            conviction = "speculative"
        else:
            conviction = "normal"

        notional = self.risk.get_position_size(price, buying_power, conviction) if self.risk else 0
        # Apply consensus size modifier
        notional *= consensus_size_modifier
        notional *= self._copy_trader_size_multiplier(sentiment_data, swing_only)
        # If options were placed, reduce share notional to keep total risk inside tier budget.
        share_mult = float(sentiment_data.get("share_notional_multiplier", 1.0) or 1.0)
        notional *= max(0.0, min(1.0, share_mult))
        adjusted_notional = self._apply_strategy_controls(symbol, sentiment_data, notional)
        if adjusted_notional is None:
            return None
        notional = adjusted_notional
        # Reduce size during extended hours
        if extended:
            notional *= settings.EXTENDED_HOURS_SIZE_MULT
        shares = self.risk.get_shares(price, notional) if self.risk else 0
        if shares <= 0 or notional <= 0:
            tier = self.risk.get_risk_tier() if self.risk else {}
            logger.warning(
                f"Position size is 0 for {symbol} @ ${price:.2f} "
                f"(buying_power=${buying_power:.2f}, tier={tier.get('name', '?')}, conviction={conviction})"
            )
            self.last_order_error = "position_size_zero"
            return None

        # Use smart order execution for larger positions
        shares_int = int(shares) if shares >= 1 else shares
        logger.info(
            f"Entering {symbol}: ${notional:.2f} notional, {shares:.4f} shares @ ${price:.2f} "
            f"(conviction={conviction}, tier={self.risk.get_risk_tier().get('name', '?') if self.risk else '?'}"
            f"{', EXTENDED' if extended else ''})"
        )

        # ── Chase Prevention: re-check price before executing ──────
        recheck_price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if recheck_price > 0:
            chase_pct = abs((recheck_price - signal_price) / signal_price) * 100
            if chase_pct > self.max_chase_pct:
                logger.warning(f"CHASE PREVENTION: {symbol} moved {chase_pct:.2f}% since signal → SKIPPING")
                self.last_order_error = "chase_prevention"
                return None
            price = recheck_price  # use freshest price

        # Calculate ATR for dynamic stops (store in position)
        atr_value = None
        if hasattr(self, '_exit_manager') and self._exit_manager:
            atr_value = self._exit_manager.calculate_atr(symbol)
        else:
            # Try importing exit manager's ATR calc via polygon directly
            try:
                from src.exit.exit_manager import ExitManager
                _tmp = ExitManager.__new__(ExitManager)
                _tmp.polygon = self.polygon
                atr_value = _tmp.calculate_atr(symbol)
            except Exception:
                pass

        # Calculate trail percent: prefer jury recommendation, then ATR-based, then default 3%
        trail_pct = sentiment_data.get("jury_trail_pct", None)
        if trail_pct is None:
            trail_pct = 3.0  # default
            if atr_value and atr_value > 0:
                # ATR-based trail: 1.5x ATR as percentage of price
                trail_pct = round((atr_value * 1.5 / price) * 100, 2)
                trail_pct = max(1.0, min(trail_pct, 4.0))  # clamp 1-4%
        else:
            trail_pct = max(1.0, min(5.0, float(trail_pct)))  # clamp jury recommendation
        if swing_only:
            swing_trail = max(1.0, float(getattr(settings, "SWING_MODE_TRAIL_PCT", 4.5) or 4.5))
            trail_pct = max(trail_pct, swing_trail)

        # ── STEP 1: BUY the stock ─────────────────────────────────
        order = None
        entry_order_timestamp = None
        for attempt in range(1, self.max_retries + 1):
            limit_price = round(price * 1.002, 2)  # 0.2% slippage buffer
            attempt_order_ts = time.time()

            if extended:
                if hasattr(self.broker, 'place_limit_buy_extended'):
                    order = await asyncio.get_event_loop().run_in_executor(
                        None, self.broker.place_limit_buy_extended, symbol, int(shares) if shares >= 1 else shares, limit_price
                    )
                else:
                    order = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self.broker.place_limit_buy,
                        symbol,
                        int(shares) if shares >= 1 else shares,
                        limit_price,
                        True,
                    )
            elif hasattr(self.broker, 'smart_buy'):
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.smart_buy, symbol, notional
                )
            else:
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_limit_buy, symbol, int(shares), limit_price
                )
            if order:
                entry_order_timestamp = attempt_order_ts
                break

            price = await asyncio.get_event_loop().run_in_executor(
                None, self.polygon.get_price, symbol
            )
            if price <= 0:
                break
            await asyncio.sleep(2)

        # ── STEP 2: Immediately place trailing stop % ─────────────
        # This is the ONLY exit strategy. Up or down, if it gives back trail_pct%, we're out.
        trailing_stop_order = None
        protection_failed = False
        if swing_only:
            logger.info(f"🌙 Swing-only entry for {symbol}: deferring trailing stop placement until next trading day")
        elif order and hasattr(self.broker, 'place_trailing_stop'):
            filled_qty = int(float(order.get("filled_qty", order.get("qty", shares))))
            if filled_qty >= 1:
                # Small delay to let the fill register
                await asyncio.sleep(1)
                trailing_stop_order, protection_failed = await self._place_entry_protection_order(
                    symbol, filled_qty, trail_pct, "long"
                )
                if trailing_stop_order:
                    logger.success(f"📈 Trailing stop set: {symbol} {filled_qty}sh trail={trail_pct}%")
                else:
                    logger.warning(f"⚠️ Trailing stop FAILED for {symbol}")

        if not order:
            # Check if we accidentally got filled on a limit before smart_buy cancelled it
            try:
                alpaca_positions = self.broker.get_positions()
                for p in alpaca_positions:
                    if p.get("symbol") == symbol:
                        actual_qty = float(p.get("qty", p.get("quantity", 0)) or 0)
                        actual_price = float(p.get("avg_entry_price", p.get("average_price", price)) or price)
                        if actual_qty > 0:
                            logger.warning(f"⚠️ {symbol}: order failed but found {actual_qty} shares on Alpaca — recording position")
                            shares = actual_qty
                            price = actual_price
                            order = {"id": "recovered", "filled_qty": str(actual_qty)}
                            entry_order_timestamp = time.time()
                            break
            except Exception:
                pass

        if not order:
            logger.error(f"Failed to enter {symbol} after {self.max_retries} attempts")
            self.last_order_error = "entry_order_failed"
            return None

        fill_timestamp = self._parse_iso_ts(order.get("filled_at")) if isinstance(order, dict) else None
        fill_timestamp_source = "order_response" if fill_timestamp is not None else "unknown"
        try:
            fill_price = float(order.get("filled_avg_price", price) or price)
        except Exception:
            fill_price = price
        entry_price = fill_price if fill_price > 0 else price
        try:
            requested_qty = float(shares)
        except Exception:
            requested_qty = 0.0
        try:
            order_qty = float(order.get("qty", requested_qty) or requested_qty)
        except Exception:
            order_qty = requested_qty
        try:
            filled_qty = float(order.get("filled_qty", order_qty) or order_qty)
        except Exception:
            filled_qty = 0.0
        actual_qty = filled_qty if filled_qty > 0 else order_qty
        if actual_qty <= 0:
            actual_qty = requested_qty
        if actual_qty <= 0:
            logger.error(f"Failed to determine filled quantity for {symbol}")
            return None
        order_status = str(order.get("status", "") or "").lower()
        if not order_status:
            order_status = "pending" if extended else "filled"
        actual_notional = entry_price * actual_qty

        # Record position
        signal_sources = sentiment_data.get("signal_sources", ["unknown"])
        if isinstance(signal_sources, str):
            signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
        if not isinstance(signal_sources, list):
            signal_sources = ["unknown"]
        if not signal_sources:
            signal_sources = ["unknown"]
        position = {
            "symbol": symbol,
            "entry_price": entry_price,
            "fill_price": fill_price,
            "quantity": actual_qty,
            "entry_time": time.time(),
            "signal_timestamp": signal_timestamp,
            "entry_order_timestamp": entry_order_timestamp,
            "fill_timestamp": fill_timestamp,
            "fill_timestamp_source": fill_timestamp_source,
            "sentiment_at_entry": sentiment_data.get("score", 0),
            "peak_price": entry_price,
            "side": "long",
            "order_id": order.get("id", order.get("brokerage_order_id", "")),
            "partial_exit": False,
            "atr_at_entry": atr_value,
            "extended_hours_entry": extended,
            "conviction_level": conviction,
            "risk_tier": self.risk.get_risk_tier().get("name", "?") if self.risk else "?",
            "notional": actual_notional,
            "trail_pct": trail_pct,
            "trailing_stop_order_id": trailing_stop_order.get("id") if trailing_stop_order else None,
            "has_trailing_stop": trailing_stop_order is not None,
            "protection_failed": protection_failed,
            "order_status": order_status,
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "entry_path": sentiment_data.get("entry_path", "jury"),
            "signal_sources": signal_sources,
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "signal_price": sentiment_data.get("signal_price", price),
            "decision_price": sentiment_data.get("decision_price", price),
            "intended_notional": float(notional or 0),
            "actual_notional": actual_notional,
            "intended_qty": float(requested_qty or 0),
            "actual_qty": actual_qty,
            "anomaly_flags": list(sentiment_data.get("anomaly_flags", []) or []),
            "scout_escalated": bool(sentiment_data.get("scout_escalated", False)),
            "copy_trader_context": sentiment_data.get("copy_trader_context", ""),
            "copy_trader_handles": list(sentiment_data.get("copy_trader_handles", []) or []),
            "copy_trader_signal_count": int(sentiment_data.get("copy_trader_signal_count", 0) or 0),
            "copy_trader_convergence": int(sentiment_data.get("copy_trader_convergence", 0) or 0),
            "copy_trader_weight": float(sentiment_data.get("copy_trader_weight", 1.0) or 1.0),
            "swing_only": swing_only,
            "_exit_recorded": False,
        }
        self.positions[symbol] = position
        if extended:
            logger.success(
                f"📋 LIMIT ORDER PLACED: {actual_qty:.4f} {symbol} @ ${price:.2f} "
                f"(${actual_notional:.2f} est) — awaiting fill"
            )
        else:
            trail_info = f" 📈 trail={trail_pct}%" if position["has_trailing_stop"] else " ⚠️ NO TRAILING STOP"
            logger.success(
                f"✅ ENTERED: {actual_qty:.4f} {symbol} @ ${entry_price:.2f} "
                f"(${actual_notional:.2f} total){trail_info}"
            )
        return position

    async def enter_short(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Execute SHORT entry: get price → size → sell short → trailing stop (buy to cover).
        Returns position dict on success, None on failure.
        """
        if not self.broker or not self.polygon:
            logger.error("Broker or Polygon client not available")
            self.last_order_error = "broker_or_polygon_unavailable"
            return None
        self.last_order_error = ""
        if symbol in getattr(self, "_halted_symbols", set()):
            logger.warning(f"Short entry blocked for halted symbol {symbol}")
            self.last_order_error = "halted"
            return None
        if symbol in self.positions:
            logger.warning(f"Duplicate short entry blocked for {symbol}")
            self.last_order_error = "duplicate_position"
            return None

        price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if price <= 0:
            logger.warning(f"Could not get price for {symbol}")
            self.last_order_error = "price_unavailable"
            return None
        signal_timestamp = float(sentiment_data.get("signal_timestamp", time.time()) or time.time())

        # Get buying power
        balances = await asyncio.get_event_loop().run_in_executor(
            None, self.broker.get_balances
        )
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)
        swing_only = bool(self.risk and getattr(self.risk, "is_swing_mode", None) and self.risk.is_swing_mode())

        extended = self.is_extended_hours()

        # Conviction from sentiment (inverted for shorts — bearish = high conviction)
        sent_score = sentiment_data.get("score", 0)
        if sent_score < -0.3:
            conviction = "high"
        elif sent_score < 0:
            conviction = "normal"
        else:
            conviction = "speculative"

        consensus_size_modifier = sentiment_data.get("consensus_size_modifier", 1.0)

        notional = self.risk.get_position_size(price, buying_power, conviction) if self.risk else 0
        notional *= consensus_size_modifier
        notional *= self._copy_trader_size_multiplier(sentiment_data, swing_only)
        share_mult = float(sentiment_data.get("share_notional_multiplier", 1.0) or 1.0)
        notional *= max(0.0, min(1.0, share_mult))
        adjusted_notional = self._apply_strategy_controls(symbol, sentiment_data, notional)
        if adjusted_notional is None:
            return None
        notional = adjusted_notional
        if extended:
            notional *= settings.EXTENDED_HOURS_SIZE_MULT
        shares = int(notional / price) if price > 0 else 0
        if shares < 1:
            logger.warning(f"SHORT position size too small for {symbol} @ ${price:.2f}")
            self.last_order_error = "position_size_zero"
            return None

        # Calculate trail percent
        trail_pct = 3.0
        try:
            from src.exit.exit_manager import ExitManager
            _tmp = ExitManager.__new__(ExitManager)
            _tmp.polygon = self.polygon
            atr_value = _tmp.calculate_atr(symbol)
            if atr_value and atr_value > 0:
                trail_pct = round((atr_value * 1.5 / price) * 100, 2)
                trail_pct = max(1.0, min(trail_pct, 4.0))
        except Exception:
            pass

        if swing_only:
            swing_trail = max(1.0, float(getattr(settings, "SWING_MODE_TRAIL_PCT", 4.5) or 4.5))
            trail_pct = max(trail_pct, swing_trail)

        logger.info(f"🩳 Shorting {symbol}: {shares}sh @ ${price:.2f} (${shares * price:.2f} total, conviction={conviction})")

        # Place short sell order via REST API
        order = None
        entry_order_timestamp = time.time()
        try:
            import requests as req_lib
            order_data = {
                'symbol': symbol,
                'qty': str(shares),
                'side': 'sell',
                'type': 'market',
                'time_in_force': 'day',
            }
            resp = req_lib.post(
                f'{self.broker._base_url}/v2/orders',
                headers=self.broker._rest_headers(),
                json=order_data,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                order = resp.json()
            else:
                logger.error(f"Short sell failed: {resp.status_code} {resp.text[:200]}")
                self.last_order_error = f"alpaca_short_rejected_{resp.status_code}"
                return None
        except Exception as e:
            logger.error(f"Short sell error for {symbol}: {e}")
            self.last_order_error = "alpaca_short_exception"
            return None

        # Place trailing stop (buy to cover) for the short
        trailing_stop_order = None
        protection_failed = False
        if swing_only:
            logger.info(f"🌙 Swing-only short entry for {symbol}: deferring buy-to-cover trailing stop until next trading day")
        elif order:
            await asyncio.sleep(1)
            try:
                try:
                    stop_qty = int(float(order.get("filled_qty", order.get("qty", shares)) or shares))
                except Exception:
                    stop_qty = int(shares)
                trailing_stop_order, protection_failed = await self._place_entry_protection_order(
                    symbol, stop_qty, trail_pct, "short"
                )

                if trailing_stop_order:
                    logger.success(f"📉 SHORT trailing stop set: {symbol} {stop_qty}sh trail={trail_pct}%")
                else:
                    logger.warning(f"⚠️ SHORT trailing stop FAILED for {symbol}")
            except Exception as e:
                logger.warning(f"⚠️ SHORT trailing stop error for {symbol}: {e}")

        signal_sources = sentiment_data.get("signal_sources", ["unknown"])
        if isinstance(signal_sources, str):
            signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
        if not isinstance(signal_sources, list):
            signal_sources = ["unknown"]
        if not signal_sources:
            signal_sources = ["unknown"]

        fill_timestamp = self._parse_iso_ts(order.get("filled_at")) if isinstance(order, dict) else None
        fill_timestamp_source = "order_response" if fill_timestamp is not None else "unknown"
        try:
            fill_price = float(order.get("filled_avg_price", price) or price)
        except Exception:
            fill_price = price
        entry_price = fill_price if fill_price > 0 else price
        try:
            requested_qty = float(shares)
        except Exception:
            requested_qty = 0.0
        try:
            order_qty = float(order.get("qty", requested_qty) or requested_qty)
        except Exception:
            order_qty = requested_qty
        try:
            filled_qty = float(order.get("filled_qty", order_qty) or order_qty)
        except Exception:
            filled_qty = 0.0
        actual_qty = filled_qty if filled_qty > 0 else order_qty
        if actual_qty <= 0:
            actual_qty = requested_qty
        if actual_qty <= 0:
            logger.error(f"Failed to determine short filled quantity for {symbol}")
            return None
        order_status = str(order.get("status", "") or "").lower()
        if not order_status:
            order_status = "filled"
        actual_notional = entry_price * actual_qty

        position = {
            "symbol": symbol,
            "side": "short",
            "entry_price": entry_price,
            "fill_price": fill_price,
            "quantity": actual_qty,
            "entry_time": time.time(),
            "signal_timestamp": signal_timestamp,
            "entry_order_timestamp": entry_order_timestamp,
            "fill_timestamp": fill_timestamp,
            "fill_timestamp_source": fill_timestamp_source,
            "sentiment_at_entry": sentiment_data.get("score", 0),
            "peak_price": entry_price,
            "order_id": order.get("id", ""),
            "partial_exit": False,
            "extended_hours_entry": extended,
            "conviction_level": conviction,
            "risk_tier": self.risk.get_risk_tier().get("name", "?") if self.risk else "?",
            "notional": actual_notional,
            "trail_pct": trail_pct,
            "trailing_stop_order_id": trailing_stop_order.get("id") if trailing_stop_order else None,
            "has_trailing_stop": trailing_stop_order is not None,
            "protection_failed": protection_failed,
            "order_status": order_status,
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "entry_path": sentiment_data.get("entry_path", "jury"),
            "signal_sources": signal_sources,
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "signal_price": sentiment_data.get("signal_price", price),
            "decision_price": sentiment_data.get("decision_price", price),
            "intended_notional": float(notional or 0),
            "actual_notional": actual_notional,
            "intended_qty": float(requested_qty or 0),
            "actual_qty": actual_qty,
            "anomaly_flags": list(sentiment_data.get("anomaly_flags", []) or []),
            "scout_escalated": bool(sentiment_data.get("scout_escalated", False)),
            "copy_trader_context": sentiment_data.get("copy_trader_context", ""),
            "copy_trader_handles": list(sentiment_data.get("copy_trader_handles", []) or []),
            "copy_trader_signal_count": int(sentiment_data.get("copy_trader_signal_count", 0) or 0),
            "copy_trader_convergence": int(sentiment_data.get("copy_trader_convergence", 0) or 0),
            "copy_trader_weight": float(sentiment_data.get("copy_trader_weight", 1.0) or 1.0),
            "swing_only": swing_only,
            "_exit_recorded": False,
        }
        self.positions[symbol] = position
        trail_info = f" 📉 trail={trail_pct}%" if position["has_trailing_stop"] else " ⚠️ NO TRAILING STOP"
        logger.success(f"🩳 SHORTED: {actual_qty:.4f} {symbol} @ ${entry_price:.2f} (${actual_notional:.2f}){trail_info}")
        return position

    async def add_to_scout(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Escalate a fast-path scout position to full size.
        Only valid for breakout_fast_path positions and only once.
        """
        pos = self.positions.get(symbol)
        if not pos:
            return None
        if pos.get("strategy_tag") != "breakout_fast_path":
            return None
        if pos.get("scout_escalated"):
            return None
        if pos.get("side", "long") != "long":
            return None
        if not self.broker or not self.polygon:
            return None

        current_positions = self.get_positions()
        if self.risk and hasattr(self.risk, "can_trade") and not self.risk.can_trade():
            return None
        if self.risk and not self.risk.can_enter_sector(symbol, current_positions):
            return None

        price = await asyncio.get_event_loop().run_in_executor(None, self.polygon.get_price, symbol)
        if price <= 0:
            return None

        balances = await asyncio.get_event_loop().run_in_executor(None, self.broker.get_balances)
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)

        sent_score = float(sentiment_data.get("score", pos.get("sentiment_at_entry", 0)) or 0)
        if sent_score > 0.6:
            conviction = "high"
        elif sent_score < 0.1:
            conviction = "speculative"
        else:
            conviction = "normal"

        consensus_size_modifier = float(sentiment_data.get("consensus_size_modifier", 1.0) or 1.0)
        if self.risk:
            target_notional = self.risk.get_position_size(price, buying_power, conviction) * consensus_size_modifier
        else:
            target_notional = float(pos.get("notional", 0) or 0)

        current_qty = float(pos.get("quantity", 0) or 0)
        current_notional = float(pos.get("entry_price", price) or price) * current_qty
        add_notional = max(0.0, target_notional - current_notional)
        if add_notional <= 0:
            return None

        if self.risk:
            add_shares = self.risk.get_shares(price, add_notional)
        else:
            add_shares = add_notional / price
        add_qty = int(add_shares)
        if add_qty < 1:
            return None

        extended = self.is_extended_hours()
        entry_order_timestamp = time.time()
        order = None
        if extended:
            limit_price = round(price * 1.002, 2)
            if hasattr(self.broker, "place_limit_buy_extended"):
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_limit_buy_extended, symbol, add_qty, limit_price
                )
            else:
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_limit_buy, symbol, add_qty, limit_price, True
                )
        elif hasattr(self.broker, "smart_buy"):
            order = await asyncio.get_event_loop().run_in_executor(None, self.broker.smart_buy, symbol, add_notional)
        else:
            limit_price = round(price * 1.002, 2)
            order = await asyncio.get_event_loop().run_in_executor(
                None, self.broker.place_limit_buy, symbol, add_qty, limit_price
            )
        if not order:
            return None

        try:
            filled_qty = float(order.get("filled_qty", order.get("qty", add_qty)) or add_qty)
        except Exception:
            filled_qty = float(add_qty)
        if filled_qty <= 0:
            filled_qty = float(add_qty)
        try:
            add_fill_price = float(order.get("filled_avg_price", price) or price)
        except Exception:
            add_fill_price = price

        new_qty = current_qty + filled_qty
        if new_qty <= 0:
            return None
        old_cost = float(pos.get("entry_price", price) or price) * current_qty
        add_cost = add_fill_price * filled_qty
        new_entry_price = (old_cost + add_cost) / new_qty

        pos["quantity"] = new_qty
        pos["entry_price"] = new_entry_price
        pos["fill_price"] = add_fill_price
        pos["notional"] = new_entry_price * new_qty
        pos["actual_notional"] = pos["notional"]
        pos["actual_qty"] = new_qty
        pos["intended_notional"] = max(float(pos.get("intended_notional", 0) or 0), float(target_notional or 0))
        pos["intended_qty"] = max(float(pos.get("intended_qty", 0) or 0), float(new_qty))
        pos["order_id"] = order.get("id", pos.get("order_id", ""))
        pos["entry_order_timestamp"] = entry_order_timestamp
        pos["signal_timestamp"] = pos.get("signal_timestamp", sentiment_data.get("signal_timestamp"))

        fill_timestamp = self._parse_iso_ts(order.get("filled_at"))
        if fill_timestamp is not None:
            pos["fill_timestamp"] = fill_timestamp
            pos["fill_timestamp_source"] = "order_response"
        else:
            pos.setdefault("fill_timestamp", None)
            pos.setdefault("fill_timestamp_source", "unknown")

        pos["decision_confidence"] = sentiment_data.get("consensus_confidence", pos.get("decision_confidence", 0))
        pos["provider_used"] = sentiment_data.get("provider_used", pos.get("provider_used", ""))
        pos["scout_escalated"] = True

        trail_pct = sentiment_data.get("jury_trail_pct", pos.get("trail_pct", 3.0))
        trail_pct = max(1.0, min(5.0, float(trail_pct)))
        pos["trail_pct"] = trail_pct
        if hasattr(self.broker, "place_trailing_stop"):
            try:
                trail_order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_trailing_stop, symbol, int(new_qty), trail_pct
                )
                if trail_order:
                    pos["has_trailing_stop"] = True
                    pos["trailing_stop_order_id"] = trail_order.get(
                        "id", pos.get("trailing_stop_order_id")
                    )
            except Exception as e:
                logger.warning(f"Could not refresh trailing stop after scout add for {symbol}: {e}")

        logger.success(
            f"⚡ Scout escalated: {symbol} +{filled_qty:.2f} -> {new_qty:.2f} shares @ avg ${new_entry_price:.2f}"
        )
        return pos

    def _load_brokerage_positions(self):
        """Load existing positions from brokerage into tracking."""
        if not self.broker:
            return
        try:
            brokerage_positions = self.broker.get_positions()
            self.sync_positions_from_brokerage(brokerage_positions)
            # Check for existing trailing stop orders and mark positions accordingly
            try:
                open_orders = self.broker.get_orders(status="open")
                for order in open_orders:
                    sym = order.get("symbol", "")
                    otype = order.get("type", "")
                    if sym in self.positions and otype == "trailing_stop":
                        self.positions[sym]["has_trailing_stop"] = True
                        self.positions[sym]["trailing_stop_order_id"] = order.get("id", "")
                        logger.info(f"🔗 {sym} has existing trailing stop order {order.get('id', '')[:8]}...")
            except Exception as e:
                logger.warning(f"Could not check existing trailing stop orders: {e}")

            logger.success(f"Loaded {len(self.positions)} existing positions from Alpaca")
        except Exception as e:
            logger.error(f"Failed to load brokerage positions: {e}")

    def sync_positions_from_brokerage(self, brokerage_positions: Optional[List[Dict]] = None) -> int:
        """Upsert Alpaca positions into local tracking and keep quantities in sync."""
        if brokerage_positions is None:
            if not self.broker:
                return 0
            brokerage_positions = self.broker.get_positions()

        missing_symbols = [
            str(p.get("symbol", "")).upper()
            for p in (brokerage_positions or [])
            if p.get("symbol") and p.get("symbol") not in self.positions
        ]
        closed_orders = None
        if missing_symbols and self.broker and hasattr(self.broker, "get_orders"):
            try:
                closed_orders = self.broker.get_orders(status="closed")
            except Exception as e:
                logger.warning(f"Could not fetch closed orders for carryover entry times: {e}")

        updates = 0
        for p in brokerage_positions or []:
            sym = p.get("symbol", "")
            if not sym:
                continue
            raw_qty = float(p.get("quantity", 0) or 0)
            side = p.get("side")
            if side not in ("long", "short"):
                side = "short" if raw_qty < 0 else "long"
            qty = abs(raw_qty)
            if qty <= 0:
                continue
            avg_price = float(p.get("average_price", 0) or 0)
            cur_price = float(p.get("current_price", avg_price) or avg_price)

            existing = self.positions.get(sym)
            if existing:
                old_qty = float(existing.get("quantity", 0) or 0)
                if abs(old_qty - qty) > 1e-6:
                    existing["quantity"] = qty
                    existing["side"] = side
                    existing["current_price"] = cur_price
                    existing["broker_synced_at"] = time.time()
                    existing["actual_qty"] = qty
                    entry_price = float(existing.get("entry_price", avg_price) or avg_price)
                    existing["actual_notional"] = entry_price * qty
                    if qty < old_qty and qty < 1.0:
                        existing["_dust_remainder"] = True
                    elif qty >= 1.0:
                        existing.pop("_dust_remainder", None)
                    logger.warning(f"🔄 Synced {sym} quantity {old_qty:.4f} → {qty:.4f} from Alpaca")
                    updates += 1
                continue

            entry_time, entry_time_source = self._estimate_carryover_entry_time(sym, side, closed_orders)
            self.positions[sym] = {
                "symbol": sym,
                "side": side,
                "entry_price": avg_price,
                "quantity": qty,
                "entry_time": entry_time,
                "entry_time_source": entry_time_source,
                "signal_timestamp": None,
                "entry_order_timestamp": None,
                "fill_timestamp": None,
                "fill_timestamp_source": "unknown",
                "sentiment_at_entry": 0,
                "peak_price": max(avg_price, cur_price) if side == "long" else min(avg_price, cur_price),
                "order_id": "",
                "partial_exit": False,
                "from_brokerage": True,
                "strategy_tag": "carryover",
                "entry_path": "broker_sync",
                "signal_sources": ["broker_sync"],
                "decision_confidence": 0,
                "provider_used": "",
                "signal_price": avg_price,
                "decision_price": avg_price,
                "intended_notional": avg_price * qty,
                "actual_notional": avg_price * qty,
                "intended_qty": qty,
                "actual_qty": qty,
                "anomaly_flags": ["carryover_sync"],
                "scout_escalated": False,
                "swing_only": False,
                "_exit_recorded": False,
                "current_price": cur_price,
                "broker_synced_at": time.time(),
            }
            side_tag = "SHORT" if side == "short" else "LONG"
            logger.info(
                f"📦 Loaded {side_tag} position: {qty:.4f} {sym} @ ${avg_price:.2f} "
                f"(current ${cur_price:.2f}, P&L ${p.get('open_pnl', 0):.2f}, entry={entry_time_source})"
            )
            updates += 1
        return updates

    def get_positions(self) -> List[Dict]:
        """Return list of tracked positions."""
        return list(self.positions.values())

    def remove_position(self, symbol: str):
        """Remove a position after full exit."""
        self.positions.pop(symbol, None)

    def update_peak_price(self, symbol: str, current_price: float):
        """Update peak price for trailing stop tracking."""
        if symbol in self.positions:
            pos = self.positions[symbol]
            if current_price > pos.get("peak_price", 0):
                pos["peak_price"] = current_price
