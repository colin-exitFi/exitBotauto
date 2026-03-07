"""Historical OHLCV loader with Polygon fetch + local cache."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import pandas as pd
import requests


BASE_URL = "https://api.polygon.io"
TIMEFRAME_MAP = {
    "1min": (1, "minute"),
    "5min": (5, "minute"),
    "15min": (15, "minute"),
    "1hour": (1, "hour"),
    "1day": (1, "day"),
}


class DataLoader:
    """Fetch and cache OHLCV bars from Polygon."""

    def __init__(self, polygon_api_key: str, cache_dir: str = "data/backtest_cache", min_request_interval: float = 12.5):
        self.polygon_api_key = polygon_api_key or ""
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_request_interval = max(0.0, float(min_request_interval or 0.0))
        self._last_request_ts = 0.0
        self._session = requests.Session()

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: str,
        end: str,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        symbol = str(symbol or "").upper().strip()
        if not symbol:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])
        if timeframe not in TIMEFRAME_MAP:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        parquet_path, pickle_path = self._cache_paths(symbol, timeframe, start, end)
        if not force_refresh:
            cached = self._read_cache(parquet_path, pickle_path)
            if cached is not None:
                return cached

        start_dt = self._parse_date(start)
        end_dt = self._parse_date(end)
        frames: List[pd.DataFrame] = []
        for window_start, window_end in self._iter_windows(timeframe, start_dt, end_dt):
            frames.append(self._fetch_window(symbol, timeframe, window_start, window_end))

        if frames:
            df = pd.concat(frames).sort_index()
            df = df[~df.index.duplicated(keep="last")]
        else:
            df = pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])
        self._write_cache(df, parquet_path, pickle_path)
        return df

    def get_universe_bars(
        self,
        symbols: List[str],
        timeframe: str,
        start: str,
        end: str,
    ) -> Dict[str, pd.DataFrame]:
        universe = {}
        for symbol in symbols or []:
            universe[str(symbol).upper()] = self.get_bars(symbol, timeframe, start, end)
        return universe

    def _cache_paths(self, symbol: str, timeframe: str, start: str, end: str) -> Tuple[Path, Path]:
        stem = f"{symbol}_{timeframe}_{start}_{end}"
        return self.cache_dir / f"{stem}.parquet", self.cache_dir / f"{stem}.pkl"

    @staticmethod
    def _parse_date(value: str) -> datetime:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)

    def _iter_windows(self, timeframe: str, start_dt: datetime, end_dt: datetime) -> Iterable[Tuple[datetime, datetime]]:
        _, timespan = TIMEFRAME_MAP[timeframe]
        chunk_days = 365
        if timespan == "minute":
            chunk_days = 30
        elif timespan == "hour":
            chunk_days = 90

        current = start_dt
        while current <= end_dt:
            window_end = min(end_dt, current + timedelta(days=chunk_days - 1))
            yield current, window_end
            current = window_end + timedelta(days=1)

    def _fetch_window(self, symbol: str, timeframe: str, start_dt: datetime, end_dt: datetime) -> pd.DataFrame:
        multiplier, timespan = TIMEFRAME_MAP[timeframe]
        url = (
            f"{BASE_URL}/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/"
            f"{start_dt.strftime('%Y-%m-%d')}/{end_dt.strftime('%Y-%m-%d')}"
        )
        params = {
            "adjusted": "true",
            "sort": "asc",
            "limit": 50000,
            "apiKey": self.polygon_api_key,
        }

        rows = []
        next_url = url
        next_params = dict(params)
        while next_url:
            payload = self._request_json(next_url, next_params)
            rows.extend(payload.get("results", []) or [])
            next_url = payload.get("next_url")
            next_params = None
            if next_url and "apiKey=" not in next_url:
                sep = "&" if "?" in next_url else "?"
                next_url = f"{next_url}{sep}apiKey={self.polygon_api_key}"

        if not rows:
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "vwap"])

        df = pd.DataFrame(
            [
                {
                    "timestamp": pd.to_datetime(int(row.get("t", 0)), unit="ms", utc=True),
                    "open": float(row.get("o", 0) or 0),
                    "high": float(row.get("h", 0) or 0),
                    "low": float(row.get("l", 0) or 0),
                    "close": float(row.get("c", 0) or 0),
                    "volume": float(row.get("v", 0) or 0),
                    "vwap": float(row.get("vw", row.get("c", 0)) or 0),
                }
                for row in rows
            ]
        )
        df = df.set_index("timestamp").sort_index()
        df.index.name = "timestamp"
        return df

    def _request_json(self, url: str, params: Dict | None) -> Dict:
        self._rate_limit()
        response = self._session.get(url, params=params, timeout=30)
        response.raise_for_status()
        return response.json()

    def _rate_limit(self):
        elapsed = time.time() - self._last_request_ts
        if elapsed < self.min_request_interval:
            self._sleep(self.min_request_interval - elapsed)
        self._last_request_ts = time.time()

    @staticmethod
    def _sleep(seconds: float):
        time.sleep(max(0.0, seconds))

    @staticmethod
    def _read_cache(parquet_path: Path, pickle_path: Path) -> pd.DataFrame | None:
        try:
            if parquet_path.exists():
                return pd.read_parquet(parquet_path).sort_index()
            if pickle_path.exists():
                return pd.read_pickle(pickle_path).sort_index()
        except Exception:
            return None
        return None

    @staticmethod
    def _write_cache(df: pd.DataFrame, parquet_path: Path, pickle_path: Path):
        try:
            df.to_parquet(parquet_path)
            if pickle_path.exists():
                pickle_path.unlink()
            return
        except Exception:
            df.to_pickle(pickle_path)

