"""
Extended Hours Guard — Dynamic stop protection when Alpaca trailing stops don't work.

During regular market hours (9:30 AM - 4:00 PM ET): Alpaca trailing stop % handles exits server-side.
During extended hours (4:00 AM - 9:30 AM, 4:00 PM - 8:00 PM ET): Trailing stops DON'T execute.

This module manages dynamic limit sell orders during extended hours that mimic trailing stop behavior:
  1. Track high-water mark (HWM) per position
  2. Set limit sell at HWM - trail%
  3. Every 30s: check current price, update HWM if higher, adjust limit sell upward
  4. Limit sells ONLY go UP (ratchet) — never lowered
  5. At market open (9:30 AM): cancel limit sells, re-place trailing stop %

Every position ALWAYS has a sell order. No exceptions. No gaps.
"""

import asyncio
import time
from typing import Dict, List, Optional
from loguru import logger
from datetime import datetime

import pytz

from config import settings


ET = pytz.timezone("US/Eastern")


class ExtendedHoursGuard:
    """
    Manages dynamic limit sell orders during extended hours.
    Replaces trailing stops that don't work outside regular session.
    """

    def __init__(self, alpaca_client, polygon_client):
        self.broker = alpaca_client
        self.polygon = polygon_client

        # Track per-position: {symbol: {hwm, limit_order_id, trail_pct, last_limit_price}}
        self._guards: Dict[str, Dict] = {}
        self._last_check = 0
        self._check_interval = 30  # seconds

    def is_extended_hours(self) -> bool:
        """Check if we're in extended hours (trailing stops don't work)."""
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False  # Weekend
        hour, minute = now.hour, now.minute
        # Regular session: 9:30 AM - 4:00 PM ET
        if (hour == 9 and minute >= 30) or (10 <= hour < 16):
            return False  # Regular hours — trailing stops work
        # Extended hours: 4:00 AM - 9:30 AM, 4:00 PM - 8:00 PM
        if (4 <= hour < 9) or (hour == 9 and minute < 30) or (16 <= hour < 20):
            return True
        return False  # Dead hours (8 PM - 4 AM) — market closed

    def is_regular_hours(self) -> bool:
        """Check if regular market session (trailing stops work)."""
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return False
        hour, minute = now.hour, now.minute
        return (hour == 9 and minute >= 30) or (10 <= hour < 16)

    @staticmethod
    def _same_eastern_trading_day(entry_time: Optional[float]) -> bool:
        if not entry_time:
            return False
        try:
            entry_dt = datetime.fromtimestamp(float(entry_time), ET)
        except Exception:
            return False
        return entry_dt.strftime("%Y-%m-%d") == datetime.now(ET).strftime("%Y-%m-%d")

    def _should_defer_swing_protection(self, pos: Dict) -> bool:
        return bool(pos.get("swing_only")) and self._same_eastern_trading_day(pos.get("entry_time"))

    async def protect_positions(self, positions: List[Dict]) -> Dict[str, str]:
        """
        Main loop call — ensure every position has protection.
        Returns dict of {symbol: action_taken}.
        """
        now = time.time()
        if now - self._last_check < self._check_interval:
            return {}
        self._last_check = now

        actions = {}
        extended = self.is_extended_hours()
        regular = self.is_regular_hours()

        for pos in positions:
            symbol = pos["symbol"]
            trail_pct = pos.get("trail_pct", 3.0)
            side = pos.get("side", "long")
            qty = int(float(pos.get("quantity", 0)))

            if qty < 1:
                continue
            if self._should_defer_swing_protection(pos):
                if symbol in self._guards:
                    await self._cancel_guard(symbol)
                    self._guards.pop(symbol, None)
                continue

            if extended:
                # ── EXTENDED HOURS: Bot-managed dynamic limit sells ──
                action = await self._manage_extended_protection(symbol, qty, trail_pct, side, pos)
                if action:
                    actions[symbol] = action

            elif regular:
                # ── REGULAR HOURS: Ensure trailing stop exists, cancel any limit sells ──
                action = await self._transition_to_trailing_stop(symbol, qty, trail_pct, side, pos)
                if action:
                    actions[symbol] = action

        # Clean up guards for positions we no longer hold
        held_symbols = {p["symbol"] for p in positions}
        for sym in list(self._guards.keys()):
            if sym not in held_symbols:
                await self._cancel_guard(sym)
                del self._guards[sym]

        return actions

    async def _manage_extended_protection(self, symbol: str, qty: int, trail_pct: float,
                                           side: str, pos: Dict) -> Optional[str]:
        """
        During extended hours: manage a dynamic limit sell that ratchets up.
        Acts as a software trailing stop since Alpaca's doesn't work.
        """
        if self._should_defer_swing_protection(pos):
            return None

        # Get current price
        try:
            price = self.broker.get_price(symbol)
            if price <= 0:
                price = self.polygon.get_price(symbol)
            if price <= 0:
                return None
        except Exception:
            return None

        guard = self._guards.get(symbol, {})
        entry_price = pos.get("entry_price", price)

        # Initialize guard if new
        if not guard:
            hwm = max(price, pos.get("peak_price", price), entry_price)
            guard = {
                "hwm": hwm,
                "lwm": min(price, entry_price),
                "limit_order_id": None,
                "trail_pct": trail_pct,
                "last_limit_price": 0,
                "entry_price": entry_price,
                "side": side,
            }
            self._guards[symbol] = guard

        # Update high-water mark (only goes up for longs, only goes down for shorts)
        if side == "long":
            if price > guard["hwm"]:
                guard["hwm"] = price
                logger.info(f"📈 {symbol} new HWM: ${price:.2f} (extended hours)")
        else:  # short
            if price < guard.get("lwm", price):
                guard["lwm"] = price

        # Calculate stop price
        if side == "long":
            hwm = guard["hwm"]
            stop_price = round(hwm * (1 - trail_pct / 100), 2)
            # Never set stop below entry (goal: never take a loss)
            stop_price = max(stop_price, round(entry_price * 0.995, 2))  # at worst, -0.5% from entry
        else:
            lwm = guard.get("lwm", price)
            stop_price = round(lwm * (1 + trail_pct / 100), 2)

        # Check if current price has hit our stop level
        if side == "long" and price <= stop_price:
            logger.warning(f"🚨 {symbol} hit extended hours stop @ ${price:.2f} (stop=${stop_price:.2f})")
            # Cancel existing limit order and market sell immediately
            await self._cancel_guard(symbol)
            try:
                # Use limit sell at current price for extended hours (market orders may not work)
                self._place_extended_limit_sell(symbol, qty, round(price * 0.998, 2))
                return f"STOP_HIT_SELL @ ${price:.2f}"
            except Exception as e:
                logger.error(f"Emergency extended sell failed for {symbol}: {e}")
                return None
        elif side == "short" and price >= stop_price:
            logger.warning(f"🚨 {symbol} short extended stop hit @ ${price:.2f} (stop=${stop_price:.2f})")
            await self._cancel_guard(symbol)
            try:
                self._place_extended_limit_buy(symbol, qty, round(price * 1.002, 2))
                return f"STOP_HIT_BUY_TO_COVER @ ${price:.2f}"
            except Exception as e:
                logger.error(f"Emergency short extended cover failed for {symbol}: {e}")
                return None

        # Only update limit order if stop price moved up (ratchet — never lowers)
        should_update = False
        if side == "long":
            should_update = stop_price > guard["last_limit_price"]
        else:
            should_update = guard["last_limit_price"] == 0 or stop_price < guard["last_limit_price"]

        if should_update:
            # Cancel old limit order
            if guard.get("limit_order_id"):
                try:
                    self.broker.cancel_order(guard["limit_order_id"])
                except Exception:
                    pass

            # Place new limit order at stop price (sell for longs, buy-to-cover for shorts)
            if side == "long":
                order = self._place_extended_limit_sell(symbol, qty, stop_price)
            else:
                order = self._place_extended_limit_buy(symbol, qty, stop_price)
            if order:
                guard["limit_order_id"] = order.get("id", "")
                guard["last_limit_price"] = stop_price
                if side == "long":
                    logger.info(
                        f"🛡️ {symbol} extended guard updated: limit sell @ ${stop_price:.2f} "
                        f"(HWM=${guard['hwm']:.2f}, trail={trail_pct}%, price=${price:.2f})"
                    )
                    return f"LIMIT_UPDATED @ ${stop_price:.2f}"
                logger.info(
                    f"🛡️ {symbol} short extended guard updated: limit buy @ ${stop_price:.2f} "
                    f"(LWM=${guard.get('lwm', price):.2f}, trail={trail_pct}%, price=${price:.2f})"
                )
                return f"LIMIT_BUY_UPDATED @ ${stop_price:.2f}"
            else:
                logger.warning(f"⚠️ Failed to place extended guard for {symbol}")
                return None

        return None  # No action needed — stop price hasn't moved up

    async def _transition_to_trailing_stop(self, symbol: str, qty: int, trail_pct: float,
                                            side: str, pos: Dict) -> Optional[str]:
        """
        At market open: cancel extended hours limit sells, ensure trailing stop is active.
        Smooth transition — no gap in protection.
        """
        if self._should_defer_swing_protection(pos):
            return None
        guard = self._guards.get(symbol)

        # If we have an extended hours guard, transition it
        if guard and guard.get("limit_order_id"):
            # Cancel the limit sell
            try:
                self.broker.cancel_order(guard["limit_order_id"])
                logger.info(f"🔄 {symbol}: cancelled extended hours limit sell")
            except Exception:
                pass

            await self._cancel_conflicting_orders(symbol, side)

            # Place trailing stop % (regular hours)
            if not pos.get("has_trailing_stop"):
                try:
                    side = pos.get("side", "long")
                    if side == "short" and hasattr(self.broker, "place_trailing_stop_short"):
                        stop_order = self.broker.place_trailing_stop_short(symbol, qty, trail_pct)
                    else:
                        stop_order = self.broker.place_trailing_stop(symbol, qty, trail_pct)
                    if stop_order:
                        pos["has_trailing_stop"] = True
                        pos["trailing_stop_order_id"] = stop_order.get("id")
                        logger.success(f"📈 {symbol}: trailing stop restored @ {trail_pct}% for market hours")
                        # Clean up guard
                        del self._guards[symbol]
                        return f"TRAILING_STOP_RESTORED @ {trail_pct}%"
                except Exception as e:
                    logger.error(f"Failed to restore trailing stop for {symbol}: {e}")
            else:
                # Already has trailing stop — just clean up guard
                del self._guards[symbol]
                return "GUARD_CLEARED"

        # No guard but also no trailing stop — fix it
        if not pos.get("has_trailing_stop"):
            try:
                await self._cancel_conflicting_orders(symbol, side)
                side = pos.get("side", "long")
                if side == "short" and hasattr(self.broker, "place_trailing_stop_short"):
                    stop_order = self.broker.place_trailing_stop_short(symbol, qty, trail_pct)
                else:
                    stop_order = self.broker.place_trailing_stop(symbol, qty, trail_pct)
                if stop_order:
                    pos["has_trailing_stop"] = True
                    pos["trailing_stop_order_id"] = stop_order.get("id")
                    return f"TRAILING_STOP_PLACED @ {trail_pct}%"
            except Exception as e:
                logger.error(f"Failed to place trailing stop for {symbol}: {e}")

        return None

    async def _cancel_conflicting_orders(self, symbol: str, side: str):
        if not self.broker:
            return
        try:
            cancel_fn = None
            if side == "short" and hasattr(self.broker, "cancel_open_buys_for_symbol"):
                cancel_fn = self.broker.cancel_open_buys_for_symbol
            elif side != "short" and hasattr(self.broker, "cancel_open_sells_for_symbol"):
                cancel_fn = self.broker.cancel_open_sells_for_symbol
            if not cancel_fn:
                return
            cancelled = await asyncio.get_event_loop().run_in_executor(None, cancel_fn, symbol)
            if cancelled:
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.warning(f"Could not cancel conflicting guard orders for {symbol}: {e}")

    def _place_extended_limit_sell(self, symbol: str, qty: int, price: float) -> Optional[Dict]:
        """Place a limit sell order valid for extended hours."""
        try:
            import requests
            actual_qty = float(qty or 0)
            if hasattr(self.broker, "get_position"):
                broker_pos = self.broker.get_position(symbol)
                if broker_pos:
                    actual_qty = min(actual_qty, float(broker_pos.get("quantity", actual_qty) or actual_qty))
            if actual_qty <= 0:
                return None
            ts = int(time.time() * 1000)
            order_data = {
                'symbol': symbol,
                'qty': str(int(actual_qty) if actual_qty == int(actual_qty) else actual_qty),
                'side': 'sell',
                'type': 'limit',
                'limit_price': str(round(price, 2)),
                'time_in_force': 'day',
                'extended_hours': True,
                'client_order_id': f'ehg-{symbol}-{ts}',
            }
            resp = requests.post(
                f'{self.broker._base_url}/v2/orders',
                headers=self.broker._rest_headers(),
                json=order_data,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            if hasattr(self.broker, "cancel_related_orders_from_error"):
                cancelled = self.broker.cancel_related_orders_from_error(symbol, resp.text, preferred_side="sell")
                if cancelled:
                    time.sleep(0.5)
                    return self._place_extended_limit_sell(symbol, actual_qty, price)
            logger.error(f"Extended limit sell failed: {resp.status_code} {resp.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"Extended limit sell error for {symbol}: {e}")
            return None

    def _place_extended_limit_buy(self, symbol: str, qty: int, price: float) -> Optional[Dict]:
        """Place a limit buy order valid for extended hours (short cover protection)."""
        try:
            import requests
            actual_qty = float(qty or 0)
            if hasattr(self.broker, "get_position"):
                broker_pos = self.broker.get_position(symbol)
                if broker_pos:
                    actual_qty = min(actual_qty, float(broker_pos.get("quantity", actual_qty) or actual_qty))
            if actual_qty <= 0:
                return None
            ts = int(time.time() * 1000)
            order_data = {
                'symbol': symbol,
                'qty': str(int(actual_qty) if actual_qty == int(actual_qty) else actual_qty),
                'side': 'buy',
                'type': 'limit',
                'limit_price': str(round(price, 2)),
                'time_in_force': 'day',
                'extended_hours': True,
                'client_order_id': f'ehg-{symbol}-{ts}',
            }
            resp = requests.post(
                f'{self.broker._base_url}/v2/orders',
                headers=self.broker._rest_headers(),
                json=order_data,
                timeout=10,
            )
            if resp.status_code in (200, 201):
                return resp.json()
            if hasattr(self.broker, "cancel_related_orders_from_error"):
                cancelled = self.broker.cancel_related_orders_from_error(symbol, resp.text, preferred_side="buy")
                if cancelled:
                    time.sleep(0.5)
                    return self._place_extended_limit_buy(symbol, actual_qty, price)
            logger.error(f"Extended limit buy failed: {resp.status_code} {resp.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"Extended limit buy error for {symbol}: {e}")
            return None

    async def _cancel_guard(self, symbol: str):
        """Cancel any active guard limit order for a symbol."""
        guard = self._guards.get(symbol, {})
        if guard.get("limit_order_id"):
            try:
                self.broker.cancel_order(guard["limit_order_id"])
            except Exception:
                pass

    def get_guard_status(self) -> Dict:
        """Dashboard-friendly status of all guards."""
        return {
            sym: {
                "hwm": g.get("hwm", 0),
                "limit_price": g.get("last_limit_price", 0),
                "trail_pct": g.get("trail_pct", 0),
                "has_limit_order": bool(g.get("limit_order_id")),
            }
            for sym, g in self._guards.items()
        }
