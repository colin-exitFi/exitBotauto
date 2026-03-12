"""
Alpaca Market Data WebSocket — Real-time trades, quotes, and bars.

Replaces polling with sub-second market data for:
  1. Real-time price updates for positions (instant P&L)
  2. Breakout detection (volume spikes, price surges)
  3. Extended hours price monitoring
  4. Feed data into the scanner for instant signal detection

Endpoints:
  - Stock: wss://stream.data.alpaca.markets/v2/iex (free) or /v2/sip (paid)
  - Paper: wss://stream.data.sandbox.alpaca.markets/v2/iex
  
Channels: trades, quotes, bars, statuses, lulds
"""

import asyncio
import json
import time
from typing import Callable, Dict, List, Optional, Set
from loguru import logger

import websockets

from config import settings


class MarketStream:
    """
    Real-time market data via Alpaca WebSocket.
    Maintains live prices, detects breakouts, feeds scanner.
    """

    # Free tier uses IEX feed — works with both paper and live keys
    # The sandbox URL is only for the "FAKEPACA" test symbol
    # Paper/live accounts both use the main URL with IEX feed
    WS_URL = "wss://stream.data.alpaca.markets/v2/iex"
    SANDBOX_URL = "wss://stream.data.alpaca.markets/v2/iex"  # Same URL — paper keys work here

    def __init__(self):
        self._ws = None
        self._running = False
        self._subscribed_symbols: Set[str] = set()
        self._task = None

        # Live price cache: symbol -> {price, timestamp, volume, bid, ask}
        self.live_prices: Dict[str, Dict] = {}

        # Breakout detection: symbol -> {volume_1min, price_1min_ago, alert_sent}
        self._volume_window: Dict[str, List[float]] = {}
        self._price_window: Dict[str, List[float]] = {}
        self._prev_closes: Dict[str, float] = {}  # symbol -> previous day close for daily % calc

        # Callbacks
        self._on_breakout: Optional[Callable] = None
        self._on_trade: Optional[Callable] = None
        self._on_halt: Optional[Callable] = None
        self._on_luld: Optional[Callable] = None

        self.market_status: Dict[str, Dict] = {}
        self.luld_bands: Dict[str, Dict] = {}

        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._last_message_time = 0

    def set_breakout_callback(self, callback: Callable):
        """Set callback for breakout detection: callback(symbol, price, volume_spike, pct_change)"""
        self._on_breakout = callback

    def set_trade_callback(self, callback: Callable):
        """Set callback for every trade: callback(symbol, price, size, timestamp)"""
        self._on_trade = callback

    def set_halt_callback(self, callback: Callable):
        """Set callback for market status changes: callback(symbol, status_code, reason, halted)"""
        self._on_halt = callback

    def set_luld_callback(self, callback: Callable):
        """Set callback for LULD bands: callback(symbol, band_data)"""
        self._on_luld = callback

    async def start(self):
        """Start the WebSocket connection in background."""
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        logger.info("📡 Market stream starting...")

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("📡 Market stream stopped")

    async def subscribe(self, symbols: List[str]):
        """Subscribe to real-time trades + quotes for symbols."""
        new_symbols = set(s.upper() for s in symbols) - self._subscribed_symbols
        if not new_symbols:
            return

        self._subscribed_symbols.update(new_symbols)
        if self._ws:
            try:
                msg = self._build_subscription_message(new_symbols, action="subscribe")
                await self._ws.send(json.dumps(msg))
                logger.info(f"📡 Subscribed to {len(new_symbols)} symbols: {', '.join(list(new_symbols)[:5])}...")
            except Exception as e:
                logger.error(f"Subscribe error: {e}")

    def set_prev_closes(self, prev_closes: Dict[str, float]):
        """Set previous day close prices for accurate daily % change in breakout alerts."""
        self._prev_closes.update(prev_closes)

    async def unsubscribe(self, symbols: List[str]):
        """Unsubscribe from symbols."""
        remove = set(s.upper() for s in symbols) & self._subscribed_symbols
        if not remove:
            return
        self._subscribed_symbols -= remove
        if self._ws:
            try:
                await self._ws.send(json.dumps(self._build_subscription_message(remove, action="unsubscribe")))
            except Exception:
                pass

    def get_price(self, symbol: str) -> float:
        """Get the latest live price for a symbol (0 if not streaming)."""
        data = self.live_prices.get(symbol.upper(), {})
        return data.get("price", 0)

    def get_spread(self, symbol: str) -> Dict:
        """Get bid/ask spread for a symbol."""
        data = self.live_prices.get(symbol.upper(), {})
        return {
            "bid": data.get("bid", 0),
            "ask": data.get("ask", 0),
            "spread": data.get("ask", 0) - data.get("bid", 0),
        }

    def get_stats(self) -> Dict:
        """Dashboard-friendly stats."""
        return {
            "connected": self._ws is not None and self._running,
            "subscribed_count": len(self._subscribed_symbols),
            "live_prices_count": len(self.live_prices),
            "last_message_age": round(time.time() - self._last_message_time, 1) if self._last_message_time else None,
        }

    @staticmethod
    def _build_auth_message() -> Dict:
        return {
            "action": "auth",
            "key": settings.ALPACA_API_KEY,
            "secret": settings.ALPACA_SECRET_KEY,
        }

    @staticmethod
    def _build_subscription_message(symbols, action: str = "subscribe") -> Dict:
        rows = sorted({str(symbol).upper() for symbol in symbols if str(symbol).strip()})
        return {
            "action": action,
            "trades": rows,
            "quotes": rows,
            "bars": rows,
            "statuses": rows,
            "lulds": rows,
        }

    @staticmethod
    def _auth_succeeded(payload) -> bool:
        messages = payload if isinstance(payload, list) else [payload]
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            if msg.get("T") == "success" and msg.get("msg") == "authenticated":
                return True
            if msg.get("stream") == "authorization" and msg.get("data", {}).get("status") == "authorized":
                return True
        return False

    @staticmethod
    def _extract_auth_error(payload) -> Optional[Dict]:
        messages = payload if isinstance(payload, list) else [payload]
        for msg in messages:
            if isinstance(msg, dict) and msg.get("T") == "error":
                return msg
        return None

    @staticmethod
    def _is_retryable_auth_error(error: Optional[Dict]) -> bool:
        if not isinstance(error, dict):
            return False
        code = str(error.get("code", "") or "").strip()
        message = str(error.get("msg", "") or "").lower()
        return code == "406" or "connection limit exceeded" in message or "too many requests" in message

    # ── Internal ──────────────────────────────────────────────────

    async def _run_forever(self):
        """Reconnecting WebSocket loop."""
        url = self.SANDBOX_URL if settings.ALPACA_PAPER else self.WS_URL

        while self._running:
            try:
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    # NOTE: do NOT reset _reconnect_delay here — only after successful auth

                    # Wait for welcome
                    welcome = await ws.recv()
                    logger.debug(f"WS welcome: {welcome}")

                    # Authenticate
                    auth_msg = json.dumps(self._build_auth_message())
                    await ws.send(auth_msg)
                    auth_resp = await ws.recv()
                    resp_data = json.loads(auth_resp)
                    auth_error = self._extract_auth_error(resp_data)
                    if auth_error:
                        delay = max(
                            float(getattr(settings, "MARKET_STREAM_AUTH_LIMIT_RETRY_SECONDS", 5.0) or 5.0),
                            float(self._reconnect_delay or 1),
                        )
                        if self._is_retryable_auth_error(auth_error):
                            logger.warning(f"WS auth limited: {auth_error}. Retrying in {delay:.1f}s...")
                        else:
                            logger.error(f"WS auth error: {auth_error}. Retrying in {delay:.1f}s (not giving up)...")
                        self._reconnect_delay = min(delay * 2.0, self._max_reconnect_delay)
                        await asyncio.sleep(delay)
                        continue
                    if self._auth_succeeded(resp_data):
                        self._reconnect_delay = 1
                        logger.success("Market stream connected + authenticated")
                    else:
                        logger.error(f"WS auth failed: {resp_data}. Retrying with backoff...")
                        await asyncio.sleep(self._reconnect_delay)
                        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
                        continue

                    # Re-subscribe to all symbols
                    if self._subscribed_symbols:
                        sub_msg = self._build_subscription_message(self._subscribed_symbols, action="subscribe")
                        await ws.send(json.dumps(sub_msg))

                    # Process messages
                    async for raw in ws:
                        self._last_message_time = time.time()
                        try:
                            messages = json.loads(raw)
                            if not isinstance(messages, list):
                                messages = [messages]
                            for msg in messages:
                                await self._handle_message(msg)
                        except json.JSONDecodeError:
                            pass

            except websockets.exceptions.ConnectionClosed as e:
                logger.warning(f"📡 WS disconnected: {e}. Reconnecting in {self._reconnect_delay}s...")
            except Exception as e:
                logger.error(f"📡 WS error: {e}. Reconnecting in {self._reconnect_delay}s...")

            self._ws = None
            if self._running:
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    async def _handle_message(self, msg: Dict):
        """Process a single WebSocket message."""
        msg_type = msg.get("T")

        if msg_type == "t":  # Trade
            symbol = msg.get("S", "")
            price = msg.get("p", 0)
            size = msg.get("s", 0)
            timestamp = msg.get("t", "")

            self.live_prices[symbol] = {
                **self.live_prices.get(symbol, {}),
                "price": price,
                "last_trade_size": size,
                "last_trade_time": timestamp,
                "updated": time.time(),
            }

            # Feed breakout detector
            self._track_volume(symbol, size)
            self._track_price(symbol, price)

            if self._on_trade:
                try:
                    self._on_trade(symbol, price, size, timestamp)
                except Exception:
                    pass

        elif msg_type == "q":  # Quote
            symbol = msg.get("S", "")
            bid = msg.get("bp", 0)
            ask = msg.get("ap", 0)
            self.live_prices[symbol] = {
                **self.live_prices.get(symbol, {}),
                "bid": bid,
                "ask": ask,
                "mid": (bid + ask) / 2 if bid and ask else 0,
                "updated": time.time(),
            }

        elif msg_type == "b":  # Bar (1-min)
            symbol = msg.get("S", "")
            close = msg.get("c", 0)
            volume = msg.get("v", 0)
            high = msg.get("h", 0)
            low = msg.get("l", 0)
            self.live_prices[symbol] = {
                **self.live_prices.get(symbol, {}),
                "bar_close": close,
                "bar_volume": volume,
                "bar_high": high,
                "bar_low": low,
                "bar_time": msg.get("t", ""),
                "updated": time.time(),
            }

            # Check for volume breakout on bar close
            self._check_breakout(symbol, close, volume)

        elif msg_type == "s":  # Trading status / halt / resume
            symbol = msg.get("S", "")
            status_code = str(msg.get("sc") or msg.get("status_code") or msg.get("status") or "").upper()
            reason = str(msg.get("rc") or msg.get("reason_code") or msg.get("reason") or "").strip()
            status_message = str(msg.get("sm") or msg.get("status_message") or "").strip()
            halted = status_code in {"H", "HALTED", "2"}

            if symbol:
                self.market_status[symbol] = {
                    "status_code": status_code,
                    "reason": reason,
                    "status_message": status_message,
                    "halted": halted,
                    "updated": time.time(),
                    "raw": msg,
                }

            if halted:
                logger.warning(f"🚨 HALT: {symbol} status={status_code} reason={reason or status_message or 'unknown'}")
            else:
                logger.info(f"✅ STATUS: {symbol} status={status_code} reason={reason or status_message or 'normal'}")

            if self._on_halt:
                try:
                    self._on_halt(symbol, status_code, reason or status_message, halted)
                except Exception:
                    pass

        elif msg_type == "l":  # LULD bands
            symbol = msg.get("S", "")
            band_data = {
                "symbol": symbol,
                "upper_band": msg.get("u") or msg.get("upper_band"),
                "lower_band": msg.get("d") or msg.get("lower_band"),
                "indicator": msg.get("i") or msg.get("indicator"),
                "band_state": msg.get("bs") or msg.get("state") or "",
                "timestamp": msg.get("t", ""),
                "raw": msg,
            }
            if symbol:
                self.luld_bands[symbol] = {**band_data, "updated": time.time()}
                logger.warning(
                    f"⚠️ LULD: {symbol} lower={band_data['lower_band']} upper={band_data['upper_band']} "
                    f"state={band_data['band_state'] or band_data['indicator'] or 'active'}"
                )
            if self._on_luld:
                try:
                    self._on_luld(symbol, band_data)
                except Exception:
                    pass

    def _track_volume(self, symbol: str, size: float):
        """Track rolling 1-min volume for breakout detection."""
        now = time.time()
        if symbol not in self._volume_window:
            self._volume_window[symbol] = []
        self._volume_window[symbol].append((now, size))
        # Keep last 5 minutes
        cutoff = now - 300
        self._volume_window[symbol] = [(t, s) for t, s in self._volume_window[symbol] if t > cutoff]

    def _track_price(self, symbol: str, price: float):
        """Track rolling prices for momentum detection."""
        now = time.time()
        if symbol not in self._price_window:
            self._price_window[symbol] = []
        self._price_window[symbol].append((now, price))
        # Keep last 5 minutes
        cutoff = now - 300
        self._price_window[symbol] = [(t, p) for t, p in self._price_window[symbol] if t > cutoff]

    def _check_breakout(self, symbol: str, price: float, bar_volume: float):
        """Detect breakouts: volume spike + price surge in 1-min bar."""
        if not self._on_breakout:
            return

        # Need price history to calculate change
        prices = self._price_window.get(symbol, [])
        if len(prices) < 2:
            return

        # Price change: use prev_close for daily change if available, else 5-min window
        prev_close = self._prev_closes.get(symbol, 0)
        if prev_close > 0:
            pct_change = ((price - prev_close) / prev_close) * 100
        else:
            oldest_price = prices[0][1]
            if oldest_price <= 0:
                return
            pct_change = ((price - oldest_price) / oldest_price) * 100

        # Volume spike: compare bar volume to rolling average
        volumes = self._volume_window.get(symbol, [])
        recent_volume = sum(s for _, s in volumes)
        avg_volume = recent_volume / max(len(volumes), 1)

        # Breakout: >2% move + above-average volume
        if abs(pct_change) >= 2.0 and bar_volume > avg_volume * 1.5:
            try:
                self._on_breakout(symbol, price, bar_volume / max(avg_volume, 1), pct_change)
            except Exception:
                pass
