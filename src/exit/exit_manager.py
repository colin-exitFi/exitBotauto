"""
Exit Manager - Take profit, stop loss, trailing stop, sentiment exit, time exit, EOD exit.
Executes via Alpaca market orders for instant fills.
"""

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from loguru import logger

from config import settings
from src.exit.order_conflicts import cancel_conflicting_exit_orders


class ExitManager:
    # ── ATR Calculation ────────────────────────────────────────────

    def calculate_atr(self, symbol: str) -> Optional[float]:
        """
        Calculate ATR from 14-period 5-min bars via Polygon.
        Returns ATR in dollars, or None if calculation fails.
        """
        if not self.polygon:
            return None
        try:
            bars = self.polygon.get_bars(
                symbol,
                timespan="minute",
                multiplier=5,
                limit=settings.ATR_PERIOD + 1,
            )
            if not bars or len(bars) < settings.ATR_PERIOD + 1:
                logger.warning(f"Not enough bars for ATR on {symbol}, got {len(bars) if bars else 0}")
                return None
            true_ranges = []
            for i in range(1, len(bars)):
                high = bars[i].get("high", bars[i].get("h", 0))
                low = bars[i].get("low", bars[i].get("l", 0))
                prev_close = bars[i - 1].get("close", bars[i - 1].get("c", 0))
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                true_ranges.append(tr)
            atr = sum(true_ranges[-settings.ATR_PERIOD:]) / settings.ATR_PERIOD
            logger.debug(f"ATR for {symbol}: ${atr:.4f}")
            return atr if atr > 0 else None
        except Exception as e:
            logger.warning(f"ATR calculation failed for {symbol}: {e}")
            return None

    """
    Exit triggers (checked every tick):
      A. Take profit: +1.5% sell half, +2.5% sell rest OR trailing -0.5% from peak
      B. Stop loss: -1% hard
      C. Sentiment exit: score < 0
      D. Time exit: 4 hours max hold
      E. EOD exit: close all at 3:30 PM ET
    """

    def __init__(self, alpaca_client=None, polygon_client=None, risk_manager=None, entry_manager=None):
        self.broker = alpaca_client
        self.polygon = polygon_client
        self.risk = risk_manager  # RiskManager for dynamic stop loss
        self.entry = entry_manager

        self.tp1_pct = settings.TAKE_PROFIT_1_PCT
        self.tp2_pct = settings.TAKE_PROFIT_2_PCT
        self.stop_loss_pct = settings.STOP_LOSS_PCT
        self.trailing_pct = settings.TRAILING_STOP_PCT
        self.max_hold_seconds = settings.MAX_HOLD_HOURS * 3600
        self.eod_exit_time = settings.EOD_EXIT_TIME  # "15:30" ET

        self.exit_history: List[Dict] = []
        logger.info("Exit manager initialized")

    async def check_and_exit(self, position: Dict, current_price: float, sentiment_score: float) -> Optional[Dict]:
        """
        Check all exit conditions for a position.
        Returns exit trade dict if exited, None if still holding.
        """
        symbol = position["symbol"]
        entry_price = position["entry_price"]
        quantity = position["quantity"]
        side = position.get("side", "long")
        peak_price = position.get("peak_price", entry_price)
        entry_time = position.get("entry_time", time.time())
        partial = position.get("partial_exit", False)

        if side == "short":
            pnl_pct = ((entry_price - current_price) / entry_price) * 100
        else:
            pnl_pct = ((current_price - entry_price) / entry_price) * 100
        hold_seconds = time.time() - entry_time

        # Update favorable extreme for trailing stop:
        # - longs track highest price since entry
        # - shorts track lowest price since entry (stored in peak_price key for compatibility)
        if self.entry:
            if side == "short":
                if current_price < peak_price:
                    if symbol in self.entry.positions:
                        self.entry.positions[symbol]["peak_price"] = current_price
                    peak_price = current_price
            elif current_price > peak_price:
                self.entry.update_peak_price(symbol, current_price)
                peak_price = current_price

        # ── A. STOP LOSS: ATR-based with fixed backup ──────────────
        atr_at_entry = position.get("atr_at_entry")
        if atr_at_entry and atr_at_entry > 0:
            if side == "short":
                # Short ATR stop: cover if price rises 1.5x ATR above entry
                atr_stop_price = entry_price + (settings.ATR_STOP_MULTIPLIER * atr_at_entry)
                if current_price >= atr_stop_price:
                    return await self._execute_exit(position, quantity, current_price, "atr_stop_loss", pnl_pct)
            else:
                # Long ATR stop: sell if price falls 1.5x ATR below entry
                atr_stop_price = entry_price - (settings.ATR_STOP_MULTIPLIER * atr_at_entry)
                if current_price <= atr_stop_price:
                    return await self._execute_exit(position, quantity, current_price, "atr_stop_loss", pnl_pct)
        else:
            # Fallback: fixed percentage stop from risk tier
            dynamic_stop = self.stop_loss_pct
            if self.risk:
                tier = self.risk.get_risk_tier()
                dynamic_stop = tier.get("stop_pct", self.stop_loss_pct)
            if pnl_pct <= -dynamic_stop:
                return await self._execute_exit(position, quantity, current_price, "stop_loss", pnl_pct)

        # ── B. TAKE PROFIT 1: +1.5% sell half ─────────────────────
        if not partial and pnl_pct >= self.tp1_pct:
            half = round(quantity / 2, 4) if quantity < 2 else max(1, int(quantity // 2))
            result = await self._execute_exit(position, half, current_price, "take_profit_1", pnl_pct)
            if result:
                position["partial_exit"] = True
                position["quantity"] = quantity - half
                if position["quantity"] <= 0:
                    return result
                # Don't return — still holding remainder
                logger.info(f"Partial exit {symbol}: sold {half}, holding {position['quantity']}")
                return None  # still in position
            return None

        # ── C. TAKE PROFIT 2: +2.5% sell rest ─────────────────────
        if partial and pnl_pct >= self.tp2_pct:
            return await self._execute_exit(position, quantity, current_price, "take_profit_2", pnl_pct)

        # ── D. TRAILING STOP: ATR-based or fixed from peak ──────────
        if partial:
            atr_at_entry = position.get("atr_at_entry")
            if side == "short":
                # For shorts, peak_price stores lowest favorable price reached.
                if peak_price < entry_price:
                    if atr_at_entry and atr_at_entry > 0:
                        # Short ATR trailing: cover if price bounces 2x ATR above low-water mark.
                        atr_trail_price = peak_price + (settings.ATR_TRAIL_MULTIPLIER * atr_at_entry)
                        if current_price >= atr_trail_price:
                            return await self._execute_exit(position, quantity, current_price, "atr_trailing_stop", pnl_pct)
                    else:
                        trailing_retrace = ((current_price - peak_price) / peak_price) * 100 if peak_price else 0
                        if trailing_retrace >= self.trailing_pct:
                            return await self._execute_exit(position, quantity, current_price, "trailing_stop", pnl_pct)
            else:
                if peak_price > entry_price:
                    if atr_at_entry and atr_at_entry > 0:
                        # Long ATR trailing: sell if price drops 2x ATR below high-water mark.
                        atr_trail_price = peak_price - (settings.ATR_TRAIL_MULTIPLIER * atr_at_entry)
                        if current_price <= atr_trail_price:
                            return await self._execute_exit(position, quantity, current_price, "atr_trailing_stop", pnl_pct)
                    else:
                        trailing_pnl = ((current_price - peak_price) / peak_price) * 100 if peak_price else 0
                        if trailing_pnl <= -self.trailing_pct:
                            return await self._execute_exit(position, quantity, current_price, "trailing_stop", pnl_pct)

        # ── E. SENTIMENT EXIT: score < 0 ──────────────────────────
        if sentiment_score < 0:
            return await self._execute_exit(position, quantity, current_price, "sentiment_exit", pnl_pct)

        # ── F. TIME EXIT: 4 hours max hold ────────────────────────
        if hold_seconds >= self.max_hold_seconds:
            return await self._execute_exit(position, quantity, current_price, "time_exit", pnl_pct)

        # EOD exit DISABLED — we hold winners overnight
        # The mission is $1M, not day trading

        return None

    async def close_all(self, positions: List[Dict], reason: str = "eod"):
        """Close all positions (EOD or emergency)."""
        logger.warning(f"🚨 Closing ALL {len(positions)} positions: {reason}")
        for pos in positions:
            symbol = pos["symbol"]
            price = await asyncio.get_event_loop().run_in_executor(
                None, self.polygon.get_price, symbol
            ) if self.polygon else pos.get("entry_price", 0)
            side = pos.get("side", "long")
            if pos["entry_price"]:
                if side == "short":
                    pnl_pct = ((pos["entry_price"] - price) / pos["entry_price"]) * 100
                else:
                    pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"]) * 100
            else:
                pnl_pct = 0
            await self._execute_exit(pos, pos["quantity"], price, reason, pnl_pct)

    # ── Execution ──────────────────────────────────────────────────

    async def _cancel_conflicting_exit_orders(self, symbol: str, exit_side: str) -> int:
        return await cancel_conflicting_exit_orders(self.broker, symbol, exit_side)

    async def _execute_exit(self, position: Dict, quantity: int, price: float, reason: str, pnl_pct: float) -> Optional[Dict]:
        """Execute market sell order."""
        symbol = position["symbol"]
        if quantity <= 0:
            return None
        if position.get("exit_pending") and not position.get("exit_recorded"):
            logger.debug(f"{symbol}: exit already pending with broker — skipping duplicate request")
            return None
        if self.risk and hasattr(self.risk, "can_exit_position"):
            if not self.risk.can_exit_position(position, reason=reason):
                logger.info(f"⛔ Exit deferred for {symbol}: {reason}")
                return None

        logger.warning(f"🔴 EXIT {symbol}: {reason} | {quantity} shares @ ${price:.2f} | P&L: {pnl_pct:+.2f}%")

        order = None
        side = position.get("side", "long")
        if self.broker:
            exit_side = "buy" if side == "short" else "sell"
            canceled = await self._cancel_conflicting_exit_orders(symbol, exit_side=exit_side)
            if canceled:
                logger.info(f"Cancelled {canceled} conflicting {exit_side} orders for {symbol} before market exit")
            broker_call = self.broker.place_market_buy if side == "short" else self.broker.place_market_sell
            order = await asyncio.get_event_loop().run_in_executor(None, broker_call, symbol, quantity)
            if not order:
                logger.error(f"Exit order FAILED for {symbol}! Will retry next tick.")
                return None
            position["exit_pending"] = True
            position["exit_order_id"] = order.get("id")
            position["exit_submitted_at"] = time.time()
            position["exit_fill_qty"] = 0.0
            position["exit_finalized_at"] = None
            position["exit_recorded"] = False
            position["last_exit_reason"] = reason
            position["last_exit_attempt_at"] = time.time()
            return {
                "symbol": symbol,
                "side": "buy_to_cover" if side == "short" else "sell",
                "reason": reason,
                "quantity": quantity,
                "exit_price": price,
                "pnl_pct": round(pnl_pct, 2),
                "order": order,
                "status": "exit_pending",
            }

        if side == "short":
            pnl_dollars = (position["entry_price"] - price) * quantity
        else:
            pnl_dollars = (price - position["entry_price"]) * quantity
        trade = {
            "symbol": symbol,
            "side": "buy_to_cover" if side == "short" else "sell",
            "entry_price": position["entry_price"],
            "exit_price": price,
            "quantity": quantity,
            "pnl": round(pnl_dollars, 2),
            "pnl_pct": round(pnl_pct, 2),
            "reason": reason,
            "hold_seconds": time.time() - position.get("entry_time", time.time()),
            "exit_time": time.time(),
            "order": order,
        }
        self.exit_history.append(trade)

        # Update risk manager
        if self.risk:
            self.risk.record_trade(trade)

        # Remove from entry manager positions
        if self.entry and reason != "take_profit_1":
            self.entry.remove_position(symbol)

        return trade

    def _is_eod(self) -> bool:
        """Check if we're past EOD exit time (default 3:30 PM ET)."""
        try:
            from pytz import timezone as tz
            et = tz("US/Eastern")
        except Exception:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")

        now = datetime.now(et)
        h, m = map(int, self.eod_exit_time.split(":"))
        cutoff = now.replace(hour=h, minute=m, second=0, microsecond=0)
        return now >= cutoff

    def get_history(self, limit: int = 50) -> List[Dict]:
        return self.exit_history[-limit:]
