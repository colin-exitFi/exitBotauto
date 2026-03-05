"""
Alpaca Trade Updates WebSocket — Real-time order fill notifications.

Streams: new, fill, partial_fill, canceled, expired, replaced, etc.
Replaces polling for trailing stop detection.

Endpoint: wss://paper-api.alpaca.markets/stream (paper)
          wss://api.alpaca.markets/stream (live)

Message format:
  {"stream": "trade_updates", "data": {"event": "fill", "order": {...}, ...}}
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional
from loguru import logger

import websockets

from config import settings


class TradeStream:
    """
    Real-time trade/order updates from Alpaca.
    Instantly detects trailing stop fills, order cancellations, etc.
    """

    PAPER_URL = "wss://paper-api.alpaca.markets/stream"
    LIVE_URL = "wss://api.alpaca.markets/stream"

    def __init__(self):
        self._ws = None
        self._running = False
        self._task = None
        self._reconnect_delay = 1

        # Callbacks
        self._on_fill: Optional[Callable] = None  # (order_data, event_type)
        self._on_stop_triggered: Optional[Callable] = None  # (symbol, fill_price, qty)

        # Recent events for dashboard
        self.recent_events: List[Dict] = []
        self._max_events = 50
        self._last_message_time = 0

    def set_fill_callback(self, callback: Callable):
        """Called on any fill: callback(order_data, event_type)"""
        self._on_fill = callback

    def set_stop_callback(self, callback: Callable):
        """Called specifically when trailing stop fills: callback(symbol, fill_price, qty)"""
        self._on_stop_triggered = callback

    async def start(self):
        """Start the trade updates stream."""
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info("📡 Trade stream starting...")

    async def stop(self):
        """Stop the stream."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()

    def get_stats(self) -> Dict:
        return {
            "connected": self._ws is not None and self._running,
            "events_received": len(self.recent_events),
            "last_message_age": round(time.time() - self._last_message_time, 1) if self._last_message_time else None,
        }

    def _capture_ws_payload(self, msg: Dict):
        """Write a raw WS payload to data/alpaca_capture/ when capture mode is on."""
        if not getattr(settings, "CAPTURE_ALPACA_PAYLOADS", False):
            return
        try:
            capture_dir = Path(__file__).resolve().parent.parent.parent / "data" / "alpaca_capture"
            capture_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time() * 1000)
            symbol = msg.get("data", {}).get("order", {}).get("symbol", "unknown")
            event = msg.get("data", {}).get("event", "unknown")
            filepath = capture_dir / f"ws_{event}_{symbol}_{ts}.json"
            filepath.write_text(json.dumps(msg, indent=2, default=str))
        except Exception as e:
            logger.debug(f"WS payload capture failed: {e}")

    async def _run_forever(self):
        url = self.PAPER_URL if settings.ALPACA_PAPER else self.LIVE_URL

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    self._reconnect_delay = 1

                    # Authenticate
                    auth = {
                        "action": "authenticate",
                        "data": {
                            "key_id": settings.ALPACA_API_KEY,
                            "secret_key": settings.ALPACA_SECRET_KEY,
                        },
                    }
                    await ws.send(json.dumps(auth))
                    auth_resp = await ws.recv()
                    logger.debug(f"Trade WS auth: {auth_resp}")

                    resp = json.loads(auth_resp)
                    if resp.get("data", {}).get("status") == "authorized":
                        logger.success("📡 Trade stream connected + authenticated")
                    elif resp.get("stream") == "authorization" and resp.get("data", {}).get("status") == "authorized":
                        logger.success("📡 Trade stream connected + authenticated")

                    # Subscribe to trade updates
                    sub = {"action": "listen", "data": {"streams": ["trade_updates"]}}
                    await ws.send(json.dumps(sub))

                    # Process messages
                    async for raw in ws:
                        self._last_message_time = time.time()
                        try:
                            msg = json.loads(raw)
                            await self._handle_message(msg)
                        except json.JSONDecodeError:
                            pass

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"📡 Trade WS disconnected: {e}")
            except Exception as e:
                logger.error(f"📡 Trade WS error: {e}")

            self._ws = None
            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, 60)

    async def _handle_message(self, msg: Dict):
        """Handle trade update message."""
        stream = msg.get("stream")
        if stream != "trade_updates":
            return

        self._capture_ws_payload(msg)

        data = msg.get("data", {})
        event = data.get("event", "")
        order = data.get("order", {})
        symbol = order.get("symbol", "")
        order_type = order.get("type", "")

        # Log event
        event_record = {
            "time": time.time(),
            "event": event,
            "symbol": symbol,
            "type": order_type,
            "side": order.get("side", ""),
            "qty": order.get("qty"),
            "filled_qty": order.get("filled_qty"),
            "filled_avg_price": order.get("filled_avg_price"),
        }
        self.recent_events.append(event_record)
        if len(self.recent_events) > self._max_events:
            self.recent_events = self.recent_events[-self._max_events:]

        if event == "fill":
            fill_price = float(order.get("filled_avg_price", 0))
            filled_qty = float(order.get("filled_qty", 0))

            logger.info(
                f"📡 ORDER FILLED: {event} {order.get('side')} {filled_qty} {symbol} "
                f"@ ${fill_price:.2f} (type={order_type})"
            )

            # Detect trailing stop fill (longs: sell, shorts: buy-to-cover)
            if order_type == "trailing_stop":
                logger.info(
                    f"🛑 TRAILING STOP FILLED: {symbol} side={order.get('side')} @ ${fill_price:.2f}"
                )
                if self._on_stop_triggered:
                    try:
                        if asyncio.iscoroutinefunction(self._on_stop_triggered):
                            await self._on_stop_triggered(symbol, fill_price, filled_qty)
                        else:
                            self._on_stop_triggered(symbol, fill_price, filled_qty)
                    except Exception as e:
                        logger.error(f"Stop callback error: {e}")

            # General fill callback
            if self._on_fill:
                try:
                    if asyncio.iscoroutinefunction(self._on_fill):
                        await self._on_fill(data, event)
                    else:
                        self._on_fill(data, event)
                except Exception as e:
                    logger.error(f"Fill callback error: {e}")

        elif event in ("partial_fill",):
            logger.info(f"📡 PARTIAL FILL: {symbol} {order.get('filled_qty')}/{order.get('qty')}")

        elif event in ("canceled", "expired"):
            logger.info(f"📡 ORDER {event.upper()}: {symbol} (type={order_type})")

        elif event == "rejected":
            logger.warning(f"⚠️ ORDER REJECTED: {symbol} — {order.get('reject_reason', 'unknown')}")
