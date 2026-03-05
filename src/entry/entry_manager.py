"""
Entry Manager - Validate conditions, size positions, execute via Alpaca.
"""

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional
from loguru import logger

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings


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

    async def can_enter(self, symbol: str, sentiment_score: float, current_positions: List[Dict]) -> bool:
        """Check all entry conditions."""
        if not self.is_market_open():
            logger.debug("Market closed, cannot enter")
            return False

        if symbol in self.positions:
            logger.debug(f"Already in position: {symbol}")
            return False

        if sentiment_score < self.min_sentiment:
            logger.debug(f"{symbol} sentiment {sentiment_score:.2f} < threshold {self.min_sentiment}")
            return False

        if self.risk and not self.risk.can_open_position(current_positions, symbol=symbol):
            return False

        if self.risk and not self.risk.can_enter_sector(symbol, current_positions):
            return False

        return True

    async def enter_position(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Execute entry: get price → size → limit order → wait for fill.
        Returns position dict on success, None on failure.
        """
        if not self.broker or not self.polygon:
            logger.error("Broker or Polygon client not available")
            return None

        # Get current price
        price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if price <= 0:
            logger.warning(f"Could not get price for {symbol}")
            return None

        # Consensus already ran in main loop — use the modifier passed in sentiment_data
        consensus_size_modifier = sentiment_data.get("consensus_size_modifier", 1.0)

        # Get buying power (cash account aware)
        balances = await asyncio.get_event_loop().run_in_executor(
            None, self.broker.get_balances
        )
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)

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

        # Calculate trail percent (ATR-based or default 3%)
        trail_pct = 3.0  # default
        if atr_value and atr_value > 0:
            # ATR-based trail: 1.5x ATR as percentage of price
            trail_pct = round((atr_value * 1.5 / price) * 100, 2)
            trail_pct = max(1.5, min(trail_pct, 5.0))  # clamp 1.5-5%

        # ── STEP 1: BUY the stock ─────────────────────────────────
        order = None
        for attempt in range(1, self.max_retries + 1):
            limit_price = round(price * 1.002, 2)  # 0.2% slippage buffer

            if extended:
                if hasattr(self.broker, 'place_limit_buy_extended'):
                    order = await asyncio.get_event_loop().run_in_executor(
                        None, self.broker.place_limit_buy_extended, symbol, int(shares) if shares >= 1 else shares, limit_price
                    )
                else:
                    order = await asyncio.get_event_loop().run_in_executor(
                        None, self.broker.place_limit_buy, symbol, int(shares) if shares >= 1 else shares, limit_price
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
        if order and hasattr(self.broker, 'place_trailing_stop'):
            filled_qty = int(float(order.get("filled_qty", order.get("qty", shares))))
            if filled_qty >= 1:
                # Small delay to let the fill register
                await asyncio.sleep(1)
                trailing_stop_order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_trailing_stop, symbol, filled_qty, trail_pct
                )
                if trailing_stop_order:
                    logger.success(f"📈 Trailing stop set: {symbol} {filled_qty}sh trail={trail_pct}%")
                else:
                    logger.warning(f"⚠️ Trailing stop FAILED for {symbol} — will retry next monitor cycle")

        if not order:
            logger.error(f"Failed to enter {symbol} after {self.max_retries} attempts")
            return None

        # Record position
        position = {
            "symbol": symbol,
            "entry_price": price,
            "quantity": shares,
            "entry_time": time.time(),
            "sentiment_at_entry": sentiment_data.get("score", 0),
            "peak_price": price,
            "side": "long",
            "order_id": order.get("id", order.get("brokerage_order_id", "")),
            "partial_exit": False,
            "atr_at_entry": atr_value,
            "extended_hours_entry": extended,
            "conviction_level": conviction,
            "risk_tier": self.risk.get_risk_tier().get("name", "?") if self.risk else "?",
            "notional": notional,
            "trail_pct": trail_pct,
            "trailing_stop_order_id": trailing_stop_order.get("id") if trailing_stop_order else None,
            "has_trailing_stop": trailing_stop_order is not None,
        }
        self.positions[symbol] = position
        trail_info = f" 📈 trail={trail_pct}%" if position["has_trailing_stop"] else " ⚠️ NO TRAILING STOP"
        logger.success(f"✅ ENTERED: {shares} {symbol} @ ${price:.2f} (${shares * price:.2f} total){trail_info}")
        return position

    async def enter_short(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Execute SHORT entry: get price → size → sell short → trailing stop (buy to cover).
        Returns position dict on success, None on failure.
        """
        if not self.broker or not self.polygon:
            logger.error("Broker or Polygon client not available")
            return None

        price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if price <= 0:
            logger.warning(f"Could not get price for {symbol}")
            return None

        # Get buying power
        balances = await asyncio.get_event_loop().run_in_executor(
            None, self.broker.get_balances
        )
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)

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
        if extended:
            notional *= settings.EXTENDED_HOURS_SIZE_MULT
        shares = int(notional / price) if price > 0 else 0
        if shares < 1:
            logger.warning(f"SHORT position size too small for {symbol} @ ${price:.2f}")
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
                trail_pct = max(1.5, min(trail_pct, 5.0))
        except Exception:
            pass

        logger.info(f"🩳 Shorting {symbol}: {shares}sh @ ${price:.2f} (${shares * price:.2f} total, conviction={conviction})")

        # Place short sell order via REST API
        order = None
        try:
            import requests as req_lib
            order_data = {
                'symbol': symbol,
                'qty': str(shares),
                'side': 'sell',
                'type': 'market',
                'time_in_force': 'gtc' if not extended else 'day',
            }
            resp = req_lib.post(
                f'{self.broker._base_url}/v2/orders',
                headers=self.broker._rest_headers(),
                json=order_data,
            )
            if resp.status_code in (200, 201):
                order = resp.json()
            else:
                logger.error(f"Short sell failed: {resp.status_code} {resp.text[:200]}")
                return None
        except Exception as e:
            logger.error(f"Short sell error for {symbol}: {e}")
            return None

        # Place trailing stop (buy to cover) for the short
        trailing_stop_order = None
        if order:
            await asyncio.sleep(1)
            try:
                import requests as req_lib
                stop_data = {
                    'symbol': symbol,
                    'qty': str(shares),
                    'side': 'buy',  # buy to cover
                    'type': 'trailing_stop',
                    'trail_percent': str(trail_pct),
                    'time_in_force': 'gtc',
                }
                resp = req_lib.post(
                    f'{self.broker._base_url}/v2/orders',
                    headers=self.broker._rest_headers(),
                    json=stop_data,
                )
                if resp.status_code in (200, 201):
                    trailing_stop_order = resp.json()
                    logger.success(f"📉 SHORT trailing stop set: {symbol} {shares}sh trail={trail_pct}%")
                else:
                    logger.warning(f"⚠️ SHORT trailing stop FAILED for {symbol}: {resp.text[:200]}")
            except Exception as e:
                logger.warning(f"⚠️ SHORT trailing stop error for {symbol}: {e}")

        position = {
            "symbol": symbol,
            "side": "short",
            "entry_price": price,
            "quantity": shares,
            "entry_time": time.time(),
            "sentiment_at_entry": sentiment_data.get("score", 0),
            "peak_price": price,
            "order_id": order.get("id", ""),
            "partial_exit": False,
            "extended_hours_entry": extended,
            "conviction_level": conviction,
            "risk_tier": self.risk.get_risk_tier().get("name", "?") if self.risk else "?",
            "notional": shares * price,
            "trail_pct": trail_pct,
            "trailing_stop_order_id": trailing_stop_order.get("id") if trailing_stop_order else None,
            "has_trailing_stop": trailing_stop_order is not None,
        }
        self.positions[symbol] = position
        trail_info = f" 📉 trail={trail_pct}%" if position["has_trailing_stop"] else " ⚠️ NO TRAILING STOP"
        logger.success(f"🩳 SHORTED: {shares} {symbol} @ ${price:.2f} (${shares * price:.2f}){trail_info}")
        return position

    def _load_brokerage_positions(self):
        """Load existing positions from brokerage into tracking."""
        if not self.broker:
            return
        try:
            brokerage_positions = self.broker.get_positions()
            for p in brokerage_positions:
                sym = p.get("symbol", "")
                if not sym or sym in self.positions:
                    continue
                # Note: crypto positions use Coinbase API for pricing
                qty = p.get("quantity", 0)
                if qty <= 0:
                    continue
                avg_price = p.get("average_price", 0)
                cur_price = p.get("current_price", avg_price)
                self.positions[sym] = {
                    "symbol": sym,
                    "entry_price": avg_price,
                    "quantity": qty,
                    "entry_time": time.time(),  # approximate — we don't know real entry time
                    "sentiment_at_entry": 0,
                    "peak_price": max(avg_price, cur_price),
                    "order_id": "",
                    "partial_exit": False,
                    "from_brokerage": True,  # flag so we know this was pre-existing
                }
                logger.info(f"📦 Loaded position: {qty:.4f} {sym} @ ${avg_price:.2f} (current ${cur_price:.2f}, P&L ${p.get('open_pnl', 0):.2f})")
            logger.success(f"Loaded {len(self.positions)} existing positions from Alpaca")
        except Exception as e:
            logger.error(f"Failed to load brokerage positions: {e}")

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
