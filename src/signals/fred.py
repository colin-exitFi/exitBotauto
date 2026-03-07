"""
FRED macro client.

Provides a compact macro snapshot for the macro agent using a few stable,
high-signal economic series.
"""

import time
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from config import settings


class FredClient:
    BASE_URL = "https://api.stlouisfed.org/fred"

    def __init__(self, api_key: Optional[str] = None, timeout: float = 10.0):
        self.api_key = api_key or getattr(settings, "FRED_API_KEY", "")
        self.timeout = timeout
        self._cache: Dict[Tuple, Tuple[float, object]] = {}
        if self.api_key:
            logger.info("FRED macro client initialized")
        else:
            logger.info("FRED macro client disabled (FRED_API_KEY missing)")

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
                params={**params, "api_key": self.api_key, "file_type": "json"},
                timeout=self.timeout,
            )
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict):
                return payload
        except Exception as e:
            logger.debug(f"FRED request failed for {path}: {e}")
        return {}

    @staticmethod
    def _to_float(value) -> Optional[float]:
        try:
            if value in (None, "", "."):
                return None
            return float(value)
        except Exception:
            return None

    def get_series_observations(self, series_id: str, limit: int = 24) -> List[Dict]:
        cache_key = ("series", series_id, int(limit))
        cached = self._get_cached(cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached
        payload = self._request(
            "/series/observations",
            {
                "series_id": series_id,
                "sort_order": "desc",
                "limit": int(limit),
            },
        )
        observations = payload.get("observations", []) if isinstance(payload, dict) else []
        return self._set_cached(cache_key, observations if isinstance(observations, list) else [])

    def get_macro_snapshot(self) -> Dict:
        cache_key = ("macro_snapshot",)
        cached = self._get_cached(cache_key, ttl_seconds=3600)
        if cached is not None:
            return cached
        if not self.is_configured():
            return {
                "macro_bias": "neutral",
                "headwinds": [],
                "summary": "FRED unavailable",
            }

        cpi_obs = self.get_series_observations("CPIAUCSL", limit=14)
        fed_obs = self.get_series_observations("FEDFUNDS", limit=3)
        unrate_obs = self.get_series_observations("UNRATE", limit=3)
        curve_obs = self.get_series_observations("T10Y2Y", limit=3)

        latest_cpi = self._to_float((cpi_obs[0] if cpi_obs else {}).get("value"))
        prior_cpi = self._to_float((cpi_obs[12] if len(cpi_obs) > 12 else {}).get("value"))
        cpi_yoy = 0.0
        if latest_cpi and prior_cpi and prior_cpi > 0:
            cpi_yoy = ((latest_cpi - prior_cpi) / prior_cpi) * 100.0

        fed_funds = self._to_float((fed_obs[0] if fed_obs else {}).get("value")) or 0.0
        unemployment = self._to_float((unrate_obs[0] if unrate_obs else {}).get("value")) or 0.0
        curve_10y2y = self._to_float((curve_obs[0] if curve_obs else {}).get("value")) or 0.0

        headwinds = []
        if cpi_yoy >= 3.2:
            headwinds.append("inflation_hot")
        if fed_funds >= 4.5:
            headwinds.append("rates_restrictive")
        if curve_10y2y < 0:
            headwinds.append("yield_curve_inverted")
        if unemployment >= 4.5:
            headwinds.append("labor_softening")

        macro_bias = "neutral"
        if len(headwinds) >= 2:
            macro_bias = "risk_off"
        elif cpi_yoy <= 2.5 and fed_funds <= 3.0 and unemployment <= 4.2 and curve_10y2y >= 0:
            macro_bias = "risk_on"

        result = {
            "cpi_yoy": round(cpi_yoy, 2),
            "fed_funds": round(fed_funds, 2),
            "unemployment_rate": round(unemployment, 2),
            "yield_curve_10y2y": round(curve_10y2y, 2),
            "macro_bias": macro_bias,
            "headwinds": headwinds,
            "summary": (
                f"CPI {cpi_yoy:.1f}% YoY; Fed funds {fed_funds:.2f}%; "
                f"unemployment {unemployment:.1f}%; 10Y-2Y {curve_10y2y:+.2f}; bias {macro_bias}"
            ),
        }
        return self._set_cached(cache_key, result)
