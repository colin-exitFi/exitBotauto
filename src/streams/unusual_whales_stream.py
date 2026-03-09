"""
Unusual Whales realtime stream.

Streams live flow alerts and off-lit (dark pool) trades from the published
Unusual Whales websocket API. Market tide remains cache-backed via the REST
client inside the same subsystem because no dedicated market-tide channel is
documented in the public socket spec.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import websockets
from loguru import logger

from config import settings
from src.signals.unusual_whales import UnusualWhalesClient


class UnusualWhalesStream:
    CHANNEL_FLOW_ALERTS = "flow-alerts"
    CHANNEL_OFF_LIT_TRADES = "off_lit_trades"
    DEFAULT_URL = "wss://api.unusualwhales.com/socket"

    def __init__(self, rest_client: Optional[UnusualWhalesClient] = None):
        self.rest_client = rest_client or UnusualWhalesClient()
        self.api_token = getattr(self.rest_client, "api_token", "") or getattr(settings, "UW_API_TOKEN", "")
        self.mode = str(getattr(settings, "UW_STREAM_MODE", "auto") or "auto").lower()
        if self.mode not in {"auto", "stream", "rest"}:
            self.mode = "auto"

        self.ws_url = str(getattr(settings, "UW_STREAM_URL", self.DEFAULT_URL) or self.DEFAULT_URL).strip()
        self.stale_seconds = max(15, int(getattr(settings, "UW_STREAM_STALE_SECONDS", 90) or 90))
        self.signal_window_seconds = max(
            self.stale_seconds,
            int(getattr(settings, "UW_STREAM_SIGNAL_WINDOW_SECONDS", 300) or 300),
        )
        self.base_backoff_seconds = max(
            1.0,
            float(getattr(settings, "UW_STREAM_BASE_BACKOFF_SECONDS", 5.0) or 5.0),
        )
        self.max_backoff_seconds = max(
            self.base_backoff_seconds,
            float(getattr(settings, "UW_STREAM_MAX_BACKOFF_SECONDS", 120.0) or 120.0),
        )
        self.log_raw = bool(getattr(settings, "UW_STREAM_LOG_RAW", False))
        self._capture_path = Path("data/uw_stream_capture.jsonl")

        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._market_tide_task: Optional[asyncio.Task] = None
        self._ws = None
        self._connected = False
        self._reconnect_delay = self.base_backoff_seconds
        self._last_event_ts = 0.0
        self._last_connect_ts = 0.0
        self._last_error = ""
        self._joined_channels: Dict[str, bool] = {}

        self._recent_flow: List[Dict] = []
        self._recent_dark_pool: List[Dict] = []
        self._market_tide: Dict = {}
        self._market_tide_refreshed_at = 0.0
        self._recent_emitted: List[Dict] = []

        self._signal_callback: Optional[Callable[[Dict], Optional[Awaitable[None]]]] = None

        if self.api_token:
            logger.info(f"🐋 Unusual Whales stream initialized ({self.mode} mode)")
        else:
            logger.info("🐋 Unusual Whales stream disabled (UW_API_TOKEN missing)")

    def is_configured(self) -> bool:
        return bool(self.api_token)

    def stream_enabled(self) -> bool:
        return self.is_configured() and self.mode in {"auto", "stream"}

    def is_fresh(self) -> bool:
        if not self.stream_enabled():
            return False
        if not self._connected:
            return False
        if not self._last_event_ts:
            return False
        return (time.time() - self._last_event_ts) <= self.stale_seconds

    def set_signal_callback(self, callback: Callable[[Dict], Optional[Awaitable[None]]]):
        self._signal_callback = callback

    async def start(self):
        if not self.stream_enabled():
            if self.mode == "rest":
                logger.info("🐋 Unusual Whales stream disabled by UW_STREAM_MODE=rest")
            return
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run_forever())
        if self.rest_client and self.rest_client.is_configured():
            self._market_tide_task = asyncio.create_task(self._refresh_market_tide_loop())
        logger.info("🐋 Unusual Whales stream starting...")

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        if self._market_tide_task:
            self._market_tide_task.cancel()
            try:
                await self._market_tide_task
            except asyncio.CancelledError:
                pass
        self._connected = False
        logger.info("🐋 Unusual Whales stream stopped")

    def get_recent_flow(self, symbol: Optional[str] = None, since_seconds: Optional[int] = None) -> List[Dict]:
        return self._filter_recent(self._recent_flow, symbol=symbol, since_seconds=since_seconds)

    def get_recent_dark_pool(self, symbol: Optional[str] = None, since_seconds: Optional[int] = None) -> List[Dict]:
        return self._filter_recent(self._recent_dark_pool, symbol=symbol, since_seconds=since_seconds)

    def get_market_tide(self) -> Dict:
        if self._market_tide:
            return dict(self._market_tide)
        if self.rest_client and self.rest_client.is_configured():
            try:
                tide = self.rest_client.get_market_tide()
                if tide:
                    self._market_tide = dict(tide)
                    self._market_tide_refreshed_at = time.time()
            except Exception as e:
                self._last_error = str(e)
        return dict(self._market_tide)

    def get_snapshot(self) -> Dict:
        return {
            "flow_alerts": self.get_recent_flow(since_seconds=self.signal_window_seconds),
            "dark_pool": self.get_recent_dark_pool(since_seconds=self.signal_window_seconds),
            "market_tide": self.get_market_tide(),
            "fresh": self.is_fresh(),
            "connected": self._connected,
            "fallback_active": self.using_rest_fallback(),
        }

    def using_rest_fallback(self) -> bool:
        if self.mode == "rest":
            return True
        return not self.is_fresh()

    def get_stats(self) -> Dict:
        return {
            "configured": self.is_configured(),
            "mode": self.mode,
            "connected": self._connected,
            "fresh": self.is_fresh(),
            "fallback_active": self.using_rest_fallback(),
            "joined_channels": sorted([ch for ch, ok in self._joined_channels.items() if ok]),
            "recent_flow_count": len(self.get_recent_flow(since_seconds=self.signal_window_seconds)),
            "recent_dark_pool_count": len(self.get_recent_dark_pool(since_seconds=self.signal_window_seconds)),
            "market_tide_bias": str(self._market_tide.get("bias", "") or ""),
            "market_tide_refreshed_at": self._market_tide_refreshed_at or None,
            "last_event_age": round(time.time() - self._last_event_ts, 1) if self._last_event_ts else None,
            "last_connect_age": round(time.time() - self._last_connect_ts, 1) if self._last_connect_ts else None,
            "last_error": self._last_error,
            "recent_emitted": list(self._recent_emitted[-8:]),
        }

    def _build_url(self) -> str:
        parsed = urlparse(self.ws_url or self.DEFAULT_URL)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query["token"] = self.api_token
        return urlunparse(parsed._replace(query=urlencode(query)))

    @staticmethod
    def _join_message(channel: str) -> Dict:
        return {"channel": channel, "msg_type": "join"}

    @staticmethod
    def _is_join_ack(channel: str, payload) -> bool:
        return isinstance(channel, str) and isinstance(payload, dict) and payload.get("status") == "ok"

    @staticmethod
    def _filter_recent(rows: List[Dict], symbol: Optional[str] = None, since_seconds: Optional[int] = None) -> List[Dict]:
        now = time.time()
        cutoff = now - float(since_seconds or 0)
        ticker = str(symbol or "").upper().strip()
        result = []
        for row in rows:
            if since_seconds and float(row.get("ingested_at", 0) or 0) < cutoff:
                continue
            if ticker and str(row.get("ticker", "")).upper() != ticker:
                continue
            result.append(dict(row))
        return result

    def _prune_recent(self):
        cutoff = time.time() - self.signal_window_seconds
        self._recent_flow = [row for row in self._recent_flow if float(row.get("ingested_at", 0) or 0) >= cutoff]
        self._recent_dark_pool = [
            row for row in self._recent_dark_pool if float(row.get("ingested_at", 0) or 0) >= cutoff
        ]

    def _append_recent(self, bucket: List[Dict], row: Dict):
        payload = dict(row)
        payload["ingested_at"] = time.time()
        bucket.append(payload)
        self._prune_recent()

    def _record_emitted(self, event: Dict):
        summary = {
            "symbol": str(event.get("symbol", "") or event.get("ticker", "") or "").upper(),
            "event_type": str(event.get("event_type", "") or ""),
            "side": str(event.get("side", "") or ""),
            "premium": float(event.get("premium", 0.0) or 0.0),
            "timestamp": time.time(),
        }
        self._recent_emitted.append(summary)
        self._recent_emitted = self._recent_emitted[-20:]

    def _capture_raw_message(self, raw: str):
        if not self.log_raw:
            return
        try:
            self._capture_path.parent.mkdir(parents=True, exist_ok=True)
            with self._capture_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"ts": time.time(), "raw": raw}) + "\n")
        except Exception as e:
            self._last_error = str(e)

    async def _emit_signal(self, event_type: str, payload: Dict):
        event = dict(payload)
        event["event_type"] = event_type
        self._record_emitted(event)
        callback = self._signal_callback
        if not callback:
            return
        try:
            result = callback(event)
            if asyncio.iscoroutine(result):
                await result
        except Exception as e:
            logger.debug(f"UW stream callback failed: {e}")

    async def _refresh_market_tide_loop(self):
        while self._running and self.rest_client and self.rest_client.is_configured():
            try:
                tide = self.rest_client.get_market_tide()
                if tide:
                    self._market_tide = dict(tide)
                    self._market_tide["source"] = "unusual_whales_rest"
                    self._market_tide_refreshed_at = time.time()
            except Exception as e:
                self._last_error = str(e)
                logger.debug(f"UW market tide refresh failed: {e}")
            await asyncio.sleep(30)

    async def _run_forever(self):
        while self._running:
            try:
                url = self._build_url()
                async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                    self._ws = ws
                    self._connected = True
                    self._last_connect_ts = time.time()
                    self._last_error = ""
                    self._reconnect_delay = self.base_backoff_seconds
                    self._joined_channels = {}

                    for channel in (self.CHANNEL_FLOW_ALERTS, self.CHANNEL_OFF_LIT_TRADES):
                        await ws.send(json.dumps(self._join_message(channel)))
                        logger.info(f"🐋 UW stream joining channel: {channel}")

                    async for raw in ws:
                        self._capture_raw_message(raw)
                        await self._handle_raw_message(raw)
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed as e:
                self._last_error = f"closed:{e.code}"
                logger.warning(f"🐋 UW stream disconnected ({e.code})")
            except Exception as e:
                self._last_error = str(e)
                logger.warning(f"🐋 UW stream error: {e}")
            finally:
                self._ws = None
                self._connected = False

            if not self._running:
                break
            delay = min(self._reconnect_delay, self.max_backoff_seconds)
            logger.info(f"🐋 UW stream reconnecting in {delay:.1f}s...")
            await asyncio.sleep(delay)
            self._reconnect_delay = min(max(delay * 2.0, self.base_backoff_seconds), self.max_backoff_seconds)

    async def _handle_raw_message(self, raw: str):
        try:
            message = json.loads(raw)
        except Exception:
            return

        if not isinstance(message, list) or len(message) < 2:
            return
        channel, payload = message[0], message[1]
        if not isinstance(channel, str):
            return

        if self._is_join_ack(channel, payload):
            self._joined_channels[channel] = True
            logger.success(f"🐋 UW stream joined {channel}")
            return

        if channel == self.CHANNEL_FLOW_ALERTS:
            normalized = self.rest_client._normalize_flow_alerts([payload])
            if not normalized:
                return
            self._last_event_ts = time.time()
            row = dict(normalized[0])
            row["source"] = "unusual_whales_stream"
            row["stream_channel"] = channel
            self._append_recent(self._recent_flow, row)
            await self._emit_signal("flow_alert", row)
            return

        if channel == self.CHANNEL_OFF_LIT_TRADES:
            normalized = self.rest_client._normalize_dark_pool([payload])
            if not normalized:
                return
            self._last_event_ts = time.time()
            row = dict(normalized[0])
            row["source"] = "unusual_whales_stream"
            row["stream_channel"] = channel
            self._append_recent(self._recent_dark_pool, row)
            await self._emit_signal("dark_pool", row)
