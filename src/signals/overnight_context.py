"""
Overnight index ETF context for non-regular-session bias.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, Optional

import requests
from loguru import logger

from config import settings


class OvernightContext:
    """
    Pulls overnight/pre-market direction from index ETFs.
    Provides directional bias to Observer, jury, and overnight thesis.
    """

    TICKERS = ("SPY", "QQQ", "DIA", "IWM")
    CACHE_TTL = 60

    def __init__(self, alpaca_client=None, polygon_client=None):
        self.alpaca = alpaca_client
        self.polygon = polygon_client
        self._cache: Dict = {}
        self._cache_at = 0.0

    def get_bias(self, refresh: bool = False) -> Dict:
        now_ts = time.time()
        if not refresh and self._cache and (now_ts - self._cache_at) < self.CACHE_TTL:
            return dict(self._cache)

        values: Dict[str, float] = {}
        populated = []
        source = "none"
        for ticker in self.TICKERS:
            snap = self._get_alpaca_snapshot(ticker)
            if snap:
                source = "alpaca"
            else:
                snap = self._get_polygon_snapshot(ticker)
                if snap and source == "none":
                    source = "polygon"
            change_pct = self._compute_change_pct(snap)
            if change_pct is not None:
                values[ticker] = round(change_pct, 2)
                populated.append(change_pct)
            else:
                values[ticker] = 0.0

        avg_change_pct = round(sum(populated) / max(1, len(populated)), 2) if populated else 0.0
        payload = {
            "direction": self._direction_from_avg(avg_change_pct),
            "spy_change_pct": values.get("SPY", 0.0),
            "qqq_change_pct": values.get("QQQ", 0.0),
            "dia_change_pct": values.get("DIA", 0.0),
            "iwm_change_pct": values.get("IWM", 0.0),
            "avg_change_pct": avg_change_pct,
            "confidence": self._confidence(avg_change_pct, populated),
            "timestamp": now_ts,
            "session": self._session_name(),
            "count": len(populated),
            "source": source,
        }
        self._cache = dict(payload)
        self._cache_at = now_ts
        return payload

    @staticmethod
    def format_summary(bias: Optional[Dict]) -> str:
        if not isinstance(bias, dict) or not bias:
            return "Unavailable"
        return (
            f"{bias.get('direction', 'flat')} avg={float(bias.get('avg_change_pct', 0.0) or 0.0):+.2f}% "
            f"conf={float(bias.get('confidence', 0.0) or 0.0):.2f} "
            f"session={bias.get('session', 'unknown')} "
            f"| SPY {float(bias.get('spy_change_pct', 0.0) or 0.0):+.2f}% "
            f"QQQ {float(bias.get('qqq_change_pct', 0.0) or 0.0):+.2f}% "
            f"DIA {float(bias.get('dia_change_pct', 0.0) or 0.0):+.2f}% "
            f"IWM {float(bias.get('iwm_change_pct', 0.0) or 0.0):+.2f}%"
        )

    def _get_alpaca_snapshot(self, ticker: str) -> Optional[Dict]:
        if not self.alpaca:
            return None
        try:
            headers = (
                self.alpaca._rest_headers()
                if hasattr(self.alpaca, "_rest_headers")
                else {
                    "APCA-API-KEY-ID": getattr(self.alpaca, "api_key", settings.ALPACA_API_KEY),
                    "APCA-API-SECRET-KEY": getattr(self.alpaca, "secret_key", settings.ALPACA_SECRET_KEY),
                }
            )
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={ticker}&feed=iex",
                headers=headers,
                timeout=5,
            )
            resp.raise_for_status()
            data = resp.json().get(ticker, {})
            latest = data.get("latestTrade", {})
            daily = data.get("dailyBar", {})
            prev = data.get("prevDailyBar", {})
            price = float(latest.get("p", daily.get("c", 0)) or 0)
            prev_close = float(prev.get("c", 0) or 0)
            if price <= 0 or prev_close <= 0:
                return None
            return {"price": price, "prev_close": prev_close}
        except Exception as e:
            logger.debug(f"OvernightContext Alpaca snapshot failed for {ticker}: {e}")
            return None

    def _get_polygon_snapshot(self, ticker: str) -> Optional[Dict]:
        if not self.polygon:
            return None
        try:
            snapshot = self.polygon.get_snapshot(ticker) or {}
            price = float(snapshot.get("price", 0) or 0)
            prev_close = float(snapshot.get("prev_close", 0) or 0)
            if price <= 0 or prev_close <= 0:
                return None
            return {"price": price, "prev_close": prev_close}
        except Exception as e:
            logger.debug(f"OvernightContext Polygon snapshot failed for {ticker}: {e}")
            return None

    @staticmethod
    def _compute_change_pct(snapshot: Optional[Dict]) -> Optional[float]:
        if not snapshot:
            return None
        try:
            price = float(snapshot.get("price", 0) or 0)
            prev_close = float(snapshot.get("prev_close", 0) or 0)
        except Exception:
            return None
        if price <= 0 or prev_close <= 0:
            return None
        return ((price - prev_close) / prev_close) * 100.0

    @staticmethod
    def _direction_from_avg(avg_change_pct: float) -> str:
        if avg_change_pct >= 0.15:
            return "bullish"
        if avg_change_pct <= -0.15:
            return "bearish"
        return "flat"

    @staticmethod
    def _confidence(avg_change_pct: float, populated: list) -> float:
        if not populated:
            return 0.0
        magnitude = min(1.0, abs(float(avg_change_pct or 0.0)) / 0.75)
        sign_score = sum(1 if value > 0 else (-1 if value < 0 else 0) for value in populated)
        agreement = abs(sign_score) / max(1, len(populated))
        return round(min(1.0, (0.65 * magnitude) + (0.35 * agreement)), 3)

    @staticmethod
    def _session_name() -> str:
        try:
            import zoneinfo

            current = datetime.now(zoneinfo.ZoneInfo("US/Eastern"))
        except Exception:
            current = datetime.now()
        if current.weekday() >= 5:
            return "overnight"
        hour = current.hour
        minute = current.minute
        if (hour == 9 and minute >= 30) or (10 <= hour < 16):
            return "regular"
        if (4 <= hour < 9) or (hour == 9 and minute < 30):
            return "pre_market"
        if 16 <= hour < 20:
            return "after_hours"
        return "overnight"
