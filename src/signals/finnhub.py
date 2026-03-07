"""
Finnhub calendar client.

Uses Finnhub's economic and IPO calendars to enrich macro context and the
overnight watchlist.
"""

import time
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from config import settings


class FinnhubClient:
    BASE_URL = "https://finnhub.io/api/v1"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 10.0):
        self.api_key = api_key or getattr(settings, "FINNHUB_API_KEY", "")
        self.timeout = timeout
        self._cache: Dict[Tuple, Tuple[float, object]] = {}
        if self.api_key:
            logger.info("Finnhub calendar client initialized")
        else:
            logger.info("Finnhub calendar client disabled (FINNHUB_API_KEY missing)")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    def _get_cached(self, cache_key: Tuple, ttl_seconds: int):
        cached = self._cache.get(cache_key)
        if not cached:
            return None
        ts, value = cached
        if (time.time() - ts) < ttl_seconds:
            return value
        self._cache.pop(cache_key, None)
        return None

    def _set_cached(self, cache_key: Tuple, value):
        self._cache[cache_key] = (time.time(), value)
        return value

    def _request(self, path: str, params: Dict) -> Dict:
        if not self.is_configured():
            return {}
        try:
            response = httpx.get(
                f"{self.BASE_URL}{path}",
                params={**params, "token": self.api_key},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        except Exception as e:
            logger.debug(f"Finnhub request failed for {path}: {e}")
        return {}

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    @staticmethod
    def _future(days: int) -> str:
        return (date.today() + timedelta(days=days)).isoformat()

    @staticmethod
    def _normalize_impact(value) -> str:
        raw = str(value or "").strip().lower()
        if raw in ("high", "3", "h"):
            return "high"
        if raw in ("medium", "2", "m"):
            return "medium"
        if raw in ("low", "1", "l"):
            return "low"
        return raw or "unknown"

    @staticmethod
    def _impact_rank(value: str) -> int:
        return {"high": 3, "medium": 2, "low": 1}.get(str(value or "").lower(), 0)

    def _normalize_economic_calendar(self, payload) -> List[Dict]:
        rows = []
        if isinstance(payload, dict):
            rows = (
                payload.get("economicCalendar")
                or payload.get("economic_calendar")
                or payload.get("data")
                or []
            )
        elif isinstance(payload, list):
            rows = payload
        normalized = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            normalized.append(
                {
                    "date": str(row.get("date") or row.get("eventDate") or ""),
                    "time": str(row.get("time") or ""),
                    "country": str(row.get("country") or "").upper(),
                    "event": str(row.get("event") or row.get("name") or row.get("indicator") or ""),
                    "impact": self._normalize_impact(row.get("impact") or row.get("importance") or row.get("priority")),
                    "actual": str(row.get("actual") or ""),
                    "estimate": str(row.get("estimate") or row.get("consensus") or ""),
                    "previous": str(row.get("prev") or row.get("previous") or ""),
                }
            )
        normalized.sort(
            key=lambda row: (row.get("date", ""), -self._impact_rank(row.get("impact", "unknown")), row.get("event", "")),
        )
        return normalized

    def _normalize_ipo_calendar(self, payload) -> List[Dict]:
        rows = []
        if isinstance(payload, dict):
            rows = payload.get("ipoCalendar") or payload.get("ipo_calendar") or payload.get("data") or []
        elif isinstance(payload, list):
            rows = payload
        normalized = []
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or row.get("ticker") or "").upper().strip()
            if not symbol:
                continue
            normalized.append(
                {
                    "symbol": symbol,
                    "date": str(row.get("date") or row.get("listingDate") or ""),
                    "name": str(row.get("name") or row.get("companyName") or ""),
                    "exchange": str(row.get("exchange") or ""),
                    "shares": str(row.get("numberOfShares") or row.get("shares") or ""),
                    "price": str(row.get("price") or row.get("priceRange") or ""),
                    "status": str(row.get("status") or "upcoming"),
                }
            )
        normalized.sort(key=lambda row: (row.get("date", ""), row.get("symbol", "")))
        return normalized

    def get_economic_calendar(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> List[Dict]:
        from_date = from_date or self._today()
        to_date = to_date or self._future(7)
        cache_key = ("economic_calendar", from_date, to_date)
        cached = self._get_cached(cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached
        payload = self._request("/calendar/economic", {"from": from_date, "to": to_date})
        return self._set_cached(cache_key, self._normalize_economic_calendar(payload))

    def summarize_economic_calendar(self, days: int = 7) -> Dict:
        events = self.get_economic_calendar(self._today(), self._future(days))
        us_events = [event for event in events if event.get("country") in ("US", "USA", "")]
        ranked = sorted(
            us_events,
            key=lambda row: (-self._impact_rank(row.get("impact", "unknown")), row.get("date", ""), row.get("event", "")),
        )
        top = ranked[:5]
        summary = "No notable US macro events"
        if top:
            summary = "; ".join(
                f"{row['date']} {row['event']} ({row['impact']})"
                for row in top
            )
        return {
            "events": top,
            "high_impact_count": sum(1 for row in us_events if row.get("impact") == "high"),
            "summary": summary,
        }

    def get_ipo_calendar(self, from_date: Optional[str] = None, to_date: Optional[str] = None) -> List[Dict]:
        from_date = from_date or self._today()
        to_date = to_date or self._future(14)
        cache_key = ("ipo_calendar", from_date, to_date)
        cached = self._get_cached(cache_key, ttl_seconds=21600)
        if cached is not None:
            return cached
        payload = self._request("/calendar/ipo", {"from": from_date, "to": to_date})
        return self._set_cached(cache_key, self._normalize_ipo_calendar(payload))
