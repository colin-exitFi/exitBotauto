"""
Polygon Client - Real-time quotes, historical bars, gainers/losers scanning.
Uses polygon-api-client and REST fallback.
"""

import time
import re
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from loguru import logger

try:
    from polygon import RESTClient as PolygonRESTClient
    HAS_POLYGON_SDK = True
except ImportError:
    HAS_POLYGON_SDK = False

from config import settings
from src.data.symbols import is_supported_trade_symbol, normalize_trade_symbol


class PolygonClient:
    """Polygon.io market data client."""

    BASE_URL = "https://api.polygon.io"

    def __init__(self):
        self.api_key = settings.POLYGON_API_KEY
        self._client = None
        self._session = requests.Session()
        self._session.params = {"apiKey": self.api_key}  # type: ignore
        self._rate_limit_until = 0.0
        self._unsupported_symbols = set()

    def initialize(self) -> bool:
        if not self.api_key or not getattr(settings, "POLYGON_API_ENABLED", True):
            # Lite / Alpaca-only mode: Polygon REST disabled. get_price() will
            # route through Alpaca (set via set_alpaca_client). Methods that
            # Alpaca doesn't cover (gainers/losers/bars) return empty lists.
            self.api_key = ""
            logger.warning(
                "Polygon disabled (no key or POLYGON_API_ENABLED=false) — "
                "using Alpaca-only market data. Gainers/losers/historical bars unavailable."
            )
            return True
        if HAS_POLYGON_SDK:
            try:
                self._client = PolygonRESTClient(api_key=self.api_key)
                logger.success("Polygon SDK client initialized")
            except Exception as e:
                logger.warning(f"Polygon SDK init failed, using REST fallback: {e}")
        else:
            logger.info("Polygon SDK not installed, using REST API directly")
        return True

    # ── Rate limiting ──────────────────────────────────────────────

    def _wait_rate_limit(self):
        now = time.time()
        if now < self._rate_limit_until:
            wait = self._rate_limit_until - now
            logger.debug(f"Rate limit: waiting {wait:.1f}s")
            time.sleep(wait)

    def _rest_get(self, path: str, params: Dict = None) -> Optional[Dict]:
        """GET request with rate-limit handling. No-op when Polygon disabled."""
        if not self.api_key:
            return None  # Lite mode: caller falls through to Alpaca / empty
        self._wait_rate_limit()
        url = f"{self.BASE_URL}{path}"
        try:
            resp = self._session.get(url, params=params or {}, timeout=10)
            if resp.status_code == 429:
                self._rate_limit_until = time.time() + 12
                logger.warning("Polygon rate limited, backing off 12s")
                return None
            if resp.status_code == 404:
                symbol = self._extract_symbol_from_path(path)
                if symbol:
                    self._unsupported_symbols.add(symbol)
                    return None
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.error(f"Polygon REST error ({path}): {e}")
            return None

    @staticmethod
    def _extract_symbol_from_path(path: str) -> str:
        text = str(path or "")
        for pattern in (
            r"/v2/last/trade/([^/?]+)",
            r"/v2/snapshot/locale/us/markets/stocks/tickers/([^/?]+)",
        ):
            match = re.search(pattern, text)
            if match:
                return normalize_trade_symbol(match.group(1))
        return ""

    # ── Quotes ─────────────────────────────────────────────────────

    def get_quote(self, symbol: str) -> Optional[Dict]:
        """Get latest quote for a symbol."""
        symbol = normalize_trade_symbol(symbol)
        if not is_supported_trade_symbol(symbol) or symbol in self._unsupported_symbols:
            return None
        data = self._rest_get(f"/v2/last/trade/{symbol}")
        if data and "results" in data:
            r = data["results"]
            return {"symbol": symbol, "price": r.get("p", 0), "size": r.get("s", 0), "timestamp": r.get("t", 0)}
        # Fallback: snapshot
        return self.get_snapshot(symbol)

    CRYPTO_SYMBOLS = {"BTC", "ETH", "SOL", "DOGE", "SHIB", "AVAX", "ADA", "DOT", "MATIC", "LINK", "XRP", "LTC"}

    def set_alpaca_client(self, alpaca_client):
        """Set Alpaca client as primary data fallback."""
        self._alpaca = alpaca_client

    def get_price(self, symbol: str) -> float:
        """
        Get current price with failover chain:
          1. Alpaca market data (free with account)
          2. Polygon REST API
          3. Coinbase (crypto only)
        Returns 0 on total failure.
        """
        symbol = normalize_trade_symbol(symbol)
        if symbol.upper() in self.CRYPTO_SYMBOLS:
            return self._get_crypto_price(symbol)
        if not is_supported_trade_symbol(symbol) or symbol in self._unsupported_symbols:
            return 0.0

        # Primary: Alpaca data API
        if hasattr(self, '_alpaca') and self._alpaca:
            try:
                price = self._alpaca.get_price(symbol)
                if price > 0:
                    return price
            except Exception:
                pass

        # Fallback 1: Polygon
        q = self.get_quote(symbol)
        if q and q.get("price", 0) > 0:
            return q["price"]

        # Fallback 2: Polygon snapshot
        snap = self.get_snapshot(symbol)
        if snap and snap.get("price", 0) > 0:
            return snap["price"]

        return 0.0

    def _get_crypto_price(self, symbol: str) -> float:
        """Get crypto price from Coinbase API (free, no key needed)."""
        try:
            resp = requests.get(f"https://api.coinbase.com/v2/prices/{symbol}-USD/spot", timeout=5)
            if resp.status_code == 200:
                return float(resp.json()["data"]["amount"])
        except Exception as e:
            logger.warning(f"Coinbase price fetch failed for {symbol}: {e}")
        return 0.0

    def get_snapshot(self, symbol: str) -> Optional[Dict]:
        """Get full snapshot for a single ticker."""
        symbol = normalize_trade_symbol(symbol)
        if not is_supported_trade_symbol(symbol) or symbol in self._unsupported_symbols:
            return None
        data = self._rest_get(f"/v2/snapshot/locale/us/markets/stocks/tickers/{symbol}")
        if data and "ticker" in data:
            t = data["ticker"]
            day = t.get("day", {})
            prev = t.get("prevDay", {})
            return {
                "symbol": symbol,
                "price": t.get("lastTrade", {}).get("p", day.get("c", 0)),
                "open": day.get("o", 0),
                "high": day.get("h", 0),
                "low": day.get("l", 0),
                "close": day.get("c", 0),
                "volume": day.get("v", 0),
                "prev_close": prev.get("c", 0),
                "prev_volume": prev.get("v", 0),
                "change_pct": t.get("todaysChangePerc", 0),
                "vwap": day.get("vw", 0),
            }
        return None

    # ── Gainers / Losers ───────────────────────────────────────────

    def get_gainers(self, include_otc: bool = False) -> List[Dict]:
        """Get today's top gainers from Polygon snapshot."""
        data = self._rest_get(
            "/v2/snapshot/locale/us/markets/stocks/gainers",
            {"include_otc": str(include_otc).lower()},
        )
        return self._parse_snapshot_list(data)

    def get_losers(self, include_otc: bool = False) -> List[Dict]:
        """Get today's top losers."""
        data = self._rest_get(
            "/v2/snapshot/locale/us/markets/stocks/losers",
            {"include_otc": str(include_otc).lower()},
        )
        return self._parse_snapshot_list(data)

    def get_all_snapshots(self, tickers: Optional[List[str]] = None) -> List[Dict]:
        """Get snapshots for all or specific tickers."""
        params = {}
        if tickers:
            params["tickers"] = ",".join(tickers)
        data = self._rest_get("/v2/snapshot/locale/us/markets/stocks/tickers", params)
        return self._parse_snapshot_list(data)

    def _parse_snapshot_list(self, data: Optional[Dict]) -> List[Dict]:
        if not data or "tickers" not in data:
            return []
        results = []
        for t in data["tickers"]:
            day = t.get("day", {})
            prev = t.get("prevDay", {})
            ticker = t.get("ticker", "")
            results.append({
                "symbol": ticker,
                "price": day.get("c", t.get("lastTrade", {}).get("p", 0)),
                "open": day.get("o", 0),
                "high": day.get("h", 0),
                "low": day.get("l", 0),
                "volume": day.get("v", 0),
                "prev_close": prev.get("c", 0),
                "prev_volume": prev.get("v", 0),
                "change_pct": t.get("todaysChangePerc", 0),
                "vwap": day.get("vw", 0),
            })
        return results

    # ── Historical Bars ────────────────────────────────────────────

    def get_bars(
        self,
        symbol: str,
        timespan: str = "day",
        multiplier: int = 1,
        from_date: str = "",
        to_date: str = "",
        limit: int = 50,
    ) -> List[Dict]:
        """
        Get historical aggregate bars.
        timespan: minute, hour, day, week, month
        """
        symbol = normalize_trade_symbol(symbol)
        if symbol in self._unsupported_symbols:
            return []
        if symbol.upper() not in self.CRYPTO_SYMBOLS and not is_supported_trade_symbol(symbol):
            return []
        if not from_date:
            from_date = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d")
        if not to_date:
            to_date = datetime.utcnow().strftime("%Y-%m-%d")

        data = self._rest_get(
            f"/v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from_date}/{to_date}",
            # Ask Polygon for the newest bars first, then reverse them locally so
            # callers still receive chronological order. Using ascending sort with
            # a broad date range returns the oldest candles inside the window.
            {"adjusted": "true", "sort": "desc", "limit": str(limit)},
        )
        if not data or "results" not in data:
            return []
        bars = []
        for r in reversed(data["results"]):
            bars.append({
                "timestamp": r.get("t", 0),
                "open": r.get("o", 0),
                "high": r.get("h", 0),
                "low": r.get("l", 0),
                "close": r.get("c", 0),
                "volume": r.get("v", 0),
                "vwap": r.get("vw", 0),
            })
        return bars

    def get_avg_volume(self, symbol: str, days: int = 20) -> float:
        """Calculate average daily volume over N days."""
        bars = self.get_bars(symbol, "day", 1, limit=days)
        if not bars:
            return 0
        total = sum(b["volume"] for b in bars)
        return total / len(bars)
