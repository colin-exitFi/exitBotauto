"""
Unusual Whales REST client.

Primary source for whale flow, dark pool, market tide, and congress trades.
Uses a small in-memory TTL cache to stay under rate limits.
"""

import time
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from config import settings


class UnusualWhalesClient:
    BASE_URL = "https://api.unusualwhales.com"
    CLIENT_API_ID = "100001"

    def __init__(self, api_token: Optional[str] = None, timeout: float = 10.0):
        self.api_token = api_token or getattr(settings, "UW_API_TOKEN", "")
        self.timeout = timeout
        self._cache: Dict[Tuple, Tuple[float, object]] = {}
        self._default_flow_premium = 100_000
        if self.api_token:
            logger.info("Unusual Whales client initialized")
        else:
            logger.info("Unusual Whales client disabled (UW_API_TOKEN missing)")

    def is_configured(self) -> bool:
        return bool(self.api_token)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_token}",
            "UW-CLIENT-API-ID": self.CLIENT_API_ID,
        }

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

    @staticmethod
    def _to_float(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    @staticmethod
    def _to_int(value, default: int = 0) -> int:
        try:
            return int(float(value))
        except Exception:
            return default

    def _request(self, path: str, params: Optional[Dict] = None):
        if not self.is_configured():
            return []
        url = f"{self.BASE_URL}{path}"
        try:
            response = httpx.get(url, headers=self._headers(), params=params or {}, timeout=self.timeout)
            response.raise_for_status()
            payload = response.json()
            if isinstance(payload, dict) and "data" in payload:
                return payload.get("data") or []
            return payload
        except Exception as e:
            logger.warning(f"Unusual Whales request failed for {path}: {e}")
            return []

    @staticmethod
    def _extract_ticker(item: Dict) -> str:
        for key in ("ticker", "ticker_symbol", "symbol", "issuer_symbol", "underlying_symbol"):
            value = str(item.get(key, "") or "").strip().upper()
            if value:
                return value
        return ""

    def _normalize_option_type(self, item: Dict) -> str:
        option_type = str(item.get("type") or item.get("option_type") or item.get("contract_type") or "").strip().lower()
        if option_type in ("call", "calls"):
            return "call"
        if option_type in ("put", "puts"):
            return "put"
        if item.get("is_call") is True:
            return "call"
        if item.get("is_put") is True:
            return "put"
        return option_type or "unknown"

    def _normalize_sentiment(self, item: Dict, option_type: str = "") -> str:
        raw = str(item.get("sentiment") or item.get("side") or item.get("direction") or "").strip().lower()
        if raw in ("bull", "bullish", "buy", "bought"):
            return "bullish"
        if raw in ("bear", "bearish", "sell", "sold"):
            return "bearish"
        if option_type == "call":
            return "bullish"
        if option_type == "put":
            return "bearish"
        return "neutral"

    def _derive_dark_pool_sentiment(self, record: Dict) -> str:
        raw_side = str(record.get("sentiment") or record.get("side") or record.get("execution_estimate") or "").strip().lower()
        if raw_side in ("buy", "bullish", "accumulation"):
            return "bullish"
        if raw_side in ("sell", "bearish", "distribution"):
            return "bearish"

        price = self._to_float(record.get("price") or record.get("avg_price"))
        bid = self._to_float(record.get("nbbo_bid") or record.get("bid"))
        ask = self._to_float(record.get("nbbo_ask") or record.get("ask"))
        midpoint = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        if price > 0 and ask > 0 and price >= ask:
            return "bullish"
        if price > 0 and bid > 0 and price <= bid:
            return "bearish"
        if price > 0 and midpoint > 0:
            if price > midpoint:
                return "bullish"
            if price < midpoint:
                return "bearish"
        return "neutral"

    def _normalize_flow_alerts(self, records: List[Dict]) -> List[Dict]:
        normalized = []
        for record in records or []:
            ticker = self._extract_ticker(record)
            if not ticker:
                continue
            option_type = self._normalize_option_type(record)
            premium = self._to_float(
                record.get("total_premium")
                or record.get("premium")
                or record.get("premium_price")
                or record.get("size_total")
                or record.get("notional")
            )
            volume = self._to_int(
                record.get("volume")
                or record.get("total_size")
                or record.get("size")
            )
            normalized.append(
                {
                    "ticker": ticker,
                    "strike": self._to_float(record.get("strike", record.get("strike_price"))),
                    "expiry": str(record.get("expiry") or record.get("expiration") or record.get("expiration_date") or ""),
                    "type": option_type,
                    "sentiment": self._normalize_sentiment(record, option_type=option_type),
                    "premium": premium,
                    "volume": volume,
                    "open_interest": self._to_int(record.get("open_interest") or record.get("oi")),
                    "contract_symbol": str(
                        record.get("contract_symbol")
                        or record.get("option_chain")
                        or record.get("option_symbol")
                        or record.get("symbol")
                        or ""
                    ),
                    "ask_side_premium": self._to_float(
                        record.get("total_ask_side_prem")
                        or record.get("ask_side_premium")
                    ),
                    "bid_side_premium": self._to_float(
                        record.get("total_bid_side_prem")
                        or record.get("bid_side_premium")
                    ),
                    "price": self._to_float(record.get("price")),
                    "underlying_price": self._to_float(record.get("underlying_price")),
                    "timestamp": str(
                        record.get("timestamp")
                        or record.get("executed_at")
                        or record.get("start_time")
                        or record.get("created_at")
                        or ""
                    ),
                    "raw": record,
                }
            )
        return normalized

    def _normalize_dark_pool(self, records: List[Dict]) -> List[Dict]:
        normalized = []
        for record in records or []:
            ticker = self._extract_ticker(record)
            if not ticker:
                continue
            price = self._to_float(record.get("price") or record.get("avg_price"))
            size = self._to_int(record.get("size") or record.get("volume") or record.get("shares"))
            premium = self._to_float(record.get("premium"))
            if premium <= 0 and price > 0 and size > 0:
                premium = round(price * size, 2)
            sentiment = self._derive_dark_pool_sentiment(record)
            normalized.append(
                {
                    "ticker": ticker,
                    "price": price,
                    "size": size,
                    "premium": premium,
                    "sentiment": sentiment,
                    "timestamp": str(record.get("executed_at") or record.get("timestamp") or ""),
                    "raw": record,
                }
            )
        return normalized

    def _normalize_congress_trades(self, records: List[Dict]) -> List[Dict]:
        normalized = []
        for record in records or []:
            ticker = self._extract_ticker(record)
            if not ticker:
                continue
            transaction = str(
                record.get("transaction")
                or record.get("transaction_type")
                or record.get("action")
                or ""
            ).strip().lower()
            normalized.append(
                {
                    "ticker": ticker,
                    "member": str(record.get("member") or record.get("politician") or record.get("name") or "Unknown"),
                    "transaction": transaction,
                    "amount": str(record.get("amount") or record.get("range") or ""),
                    "date": str(record.get("transaction_date") or record.get("date") or ""),
                    "source": "unusual_whales",
                    "raw": record,
                }
            )
        return normalized

    def _normalize_gamma_exposure(self, symbol: str, records) -> Dict:
        rows = records if isinstance(records, list) else [records]
        levels = []
        for record in rows or []:
            if not isinstance(record, dict):
                continue
            strike = self._to_float(record.get("strike") or record.get("strike_price"))
            gamma = self._to_float(
                record.get("gex")
                or record.get("gamma_exposure")
                or record.get("net_gamma")
                or record.get("value")
            )
            if strike > 0:
                levels.append({"strike": strike, "gamma": gamma})

        levels.sort(key=lambda x: abs(float(x.get("gamma", 0.0) or 0.0)), reverse=True)
        top_levels = levels[:5]
        return {
            "ticker": str(symbol or "").upper(),
            "levels": top_levels,
            "max_gamma_strike": top_levels[0]["strike"] if top_levels else 0,
            "support_strikes": [row["strike"] for row in top_levels if float(row.get("gamma", 0) or 0) > 0][:3],
            "resistance_strikes": [row["strike"] for row in top_levels if float(row.get("gamma", 0) or 0) < 0][:3],
        }

    def _normalize_insider_trades(self, records: List[Dict]) -> List[Dict]:
        normalized = []
        for record in records or []:
            ticker = self._extract_ticker(record)
            if not ticker:
                continue
            transaction = str(
                record.get("transaction_type")
                or record.get("acquisition_or_disposal")
                or record.get("transaction")
                or record.get("action")
                or ""
            ).strip().upper()
            is_buy = transaction in ("P", "A", "BUY", "PURCHASE")
            is_sell = transaction in ("S", "D", "SELL", "SALE")
            normalized.append(
                {
                    "ticker": ticker,
                    "insider_name": str(record.get("insider_name") or record.get("name") or "Unknown"),
                    "title": str(record.get("insider_title") or record.get("title") or ""),
                    "transaction": "buy" if is_buy else ("sell" if is_sell else transaction.lower()),
                    "shares": self._to_int(record.get("shares") or record.get("quantity")),
                    "price": self._to_float(record.get("price") or record.get("price_per_share")),
                    "value": self._to_float(record.get("value") or record.get("total_value")),
                    "date": str(record.get("filing_date") or record.get("date") or ""),
                    "source": "unusual_whales",
                    "raw": record,
                }
            )
        return normalized

    def _normalize_market_tide(self, payload) -> Dict:
        record = payload
        if isinstance(payload, list):
            record = payload[0] if payload else {}
        if not isinstance(record, dict):
            record = {}

        put_premium = self._to_float(
            record.get("net_put_premium")
            or record.get("put_premium")
            or record.get("net_puts")
            or record.get("total_put_premium")
        )
        call_premium = self._to_float(
            record.get("net_call_premium")
            or record.get("call_premium")
            or record.get("net_calls")
            or record.get("total_call_premium")
        )
        put_call_ratio = self._to_float(record.get("put_call_ratio") or record.get("pc_ratio"))
        if put_call_ratio <= 0 and call_premium > 0:
            put_call_ratio = put_premium / call_premium

        bias = "mixed"
        if put_call_ratio >= 1.3 or put_premium > (call_premium * 1.25):
            bias = "risk_off"
        elif 0 < put_call_ratio <= 0.8 or call_premium > (put_premium * 1.25):
            bias = "risk_on"

        return {
            "net_put_premium": put_premium,
            "net_call_premium": call_premium,
            "put_call_ratio": round(put_call_ratio, 3) if put_call_ratio else 0.0,
            "bias": bias,
            "raw": record,
        }

    def get_flow_alerts(self, min_premium: int = 100_000, symbol: Optional[str] = None, limit: int = 50) -> List[Dict]:
        cache_key = ("flow_alerts", min_premium, (symbol or "").upper(), limit)
        cached = self._get_cached(cache_key, ttl_seconds=60)
        if cached is not None:
            return cached

        params = {
            "min_premium": int(min_premium),
            "limit": int(limit),
        }
        if symbol:
            params["ticker_symbol"] = str(symbol).upper()
        records = self._request("/api/option-trades/flow-alerts", params=params)
        return self._set_cached(cache_key, self._normalize_flow_alerts(records))

    def summarize_flow_for_symbol(self, symbol: str, min_premium: Optional[int] = None) -> Dict:
        alerts = self.get_flow_alerts(
            min_premium=min_premium or self._default_flow_premium,
            symbol=symbol,
            limit=50,
        )
        bullish_premium = sum(a.get("premium", 0.0) for a in alerts if a.get("sentiment") == "bullish")
        bearish_premium = sum(a.get("premium", 0.0) for a in alerts if a.get("sentiment") == "bearish")
        if bullish_premium > bearish_premium:
            bias = "bullish"
        elif bearish_premium > bullish_premium:
            bias = "bearish"
        else:
            bias = "neutral"
        return {
            "ticker": str(symbol).upper(),
            "has_unusual": bool(alerts),
            "signals": alerts,
            "bullish_premium": round(bullish_premium, 2),
            "bearish_premium": round(bearish_premium, 2),
            "bias": bias,
            "summary": (
                f"{len(alerts)} flow alerts; bullish ${bullish_premium:,.0f}; bearish ${bearish_premium:,.0f}; bias {bias}"
                if alerts
                else "No unusual flow"
            ),
        }

    def get_dark_pool(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict]:
        cache_key = ("dark_pool", (symbol or "").upper(), limit)
        cached = self._get_cached(cache_key, ttl_seconds=60)
        if cached is not None:
            return cached

        path = f"/api/darkpool/{str(symbol).upper()}" if symbol else "/api/darkpool/recent"
        params = {"limit": int(limit)} if not symbol else None
        records = self._request(path, params=params)
        return self._set_cached(cache_key, self._normalize_dark_pool(records))

    def get_market_tide(self) -> Dict:
        cache_key = ("market_tide",)
        cached = self._get_cached(cache_key, ttl_seconds=60)
        if cached is not None:
            return cached

        payload = self._request("/api/market/market-tide", params={"interval_5m": False})
        return self._set_cached(cache_key, self._normalize_market_tide(payload))

    def get_congress_trades(self, limit: int = 50) -> List[Dict]:
        cache_key = ("congress_trades", limit)
        cached = self._get_cached(cache_key, ttl_seconds=900)
        if cached is not None:
            return cached

        records = self._request("/api/congress/recent-trades", params={"limit": int(limit)})
        return self._set_cached(cache_key, self._normalize_congress_trades(records))

    def get_gamma_exposure(self, symbol: str) -> Dict:
        cache_key = ("gamma_exposure", str(symbol or "").upper())
        cached = self._get_cached(cache_key, ttl_seconds=300)
        if cached is not None:
            return cached

        ticker = str(symbol or "").upper().strip()
        if not ticker:
            return {"ticker": "", "levels": [], "max_gamma_strike": 0, "support_strikes": [], "resistance_strikes": []}

        records = self._request(f"/api/stock/{ticker}/spot-exposures/strike")
        return self._set_cached(cache_key, self._normalize_gamma_exposure(ticker, records))

    def get_insider_trades(self, symbol: Optional[str] = None, limit: int = 50) -> List[Dict]:
        ticker = str(symbol or "").upper().strip()
        cache_key = ("insider_trades", ticker, int(limit or 50))
        cached = self._get_cached(cache_key, ttl_seconds=300)
        if cached is not None:
            return cached

        path = f"/api/insider/{ticker}/trades" if ticker else "/api/insider/recent-trades"
        records = self._request(path, params={"limit": int(limit)})
        return self._set_cached(cache_key, self._normalize_insider_trades(records))
