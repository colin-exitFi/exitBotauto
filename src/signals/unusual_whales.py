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
        self._usage_stats: Dict[str, object] = {
            "configured": bool(self.api_token),
            "last_request_path": "",
            "last_request_at": 0.0,
            "daily_request_count": 0,
            "minute_request_count": 0,
            "minute_remaining": 0,
            "minute_reset": 0,
            "daily_limit": 0,
        }
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

    def _update_usage_stats(self, headers: Dict[str, str], path: str):
        self._usage_stats.update(
            {
                "configured": bool(self.api_token),
                "last_request_path": path,
                "last_request_at": time.time(),
                "daily_request_count": self._to_int(headers.get("x-uw-daily-req-count")),
                "minute_request_count": self._to_int(headers.get("x-uw-minute-req-counter")),
                "minute_remaining": self._to_int(headers.get("x-uw-req-per-minute-remaining")),
                "minute_reset": self._to_int(headers.get("x-uw-req-per-minute-reset")),
                "daily_limit": self._to_int(headers.get("x-uw-token-req-limit")),
                "request_id": str(headers.get("x-request-id") or ""),
            }
        )

    def get_usage_stats(self) -> Dict[str, object]:
        return dict(self._usage_stats)

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
            self._update_usage_stats(response.headers, path)
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
        tags = item.get("tags") or []
        if isinstance(tags, list):
            lowered_tags = {str(tag).strip().lower() for tag in tags}
            if "bullish" in lowered_tags:
                return "bullish"
            if "bearish" in lowered_tags:
                return "bearish"
            if "ask_side" in lowered_tags:
                if option_type == "call":
                    return "bullish"
                if option_type == "put":
                    return "bearish"
            if "bid_side" in lowered_tags:
                if option_type == "call":
                    return "bearish"
                if option_type == "put":
                    return "bullish"
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

    def _normalize_option_screener(self, records: List[Dict]) -> List[Dict]:
        normalized = []
        for record in records or []:
            ticker = self._extract_ticker(record)
            if not ticker:
                continue
            option_type = self._normalize_option_type(record)
            volume = self._to_int(record.get("volume"))
            open_interest = self._to_int(record.get("open_interest") or record.get("oi"))
            ask_side_volume = self._to_int(record.get("ask_side_volume") or record.get("ask_vol"))
            bid_side_volume = self._to_int(record.get("bid_side_volume") or record.get("bid_vol"))
            ask_side_pct = self._to_float(record.get("ask_side_perc") or record.get("ask_side_perc_7_day"))
            if ask_side_pct <= 0 and volume > 0 and ask_side_volume > 0:
                ask_side_pct = ask_side_volume / max(volume, 1)
            bid_side_pct = self._to_float(record.get("bid_side_perc") or record.get("bid_side_perc_7_day"))
            if bid_side_pct <= 0 and volume > 0 and bid_side_volume > 0:
                bid_side_pct = bid_side_volume / max(volume, 1)
            premium = self._to_float(record.get("premium") or record.get("notional"))
            normalized.append(
                {
                    "ticker": ticker,
                    "contract_symbol": str(record.get("option_symbol") or record.get("contract_symbol") or ""),
                    "type": option_type,
                    "sentiment": self._normalize_sentiment(record, option_type=option_type),
                    "premium": premium,
                    "volume": volume,
                    "open_interest": open_interest,
                    "vol_to_oi": round(volume / max(open_interest, 1), 3) if volume > 0 and open_interest > 0 else 0.0,
                    "is_new_position": bool(volume > 0 and open_interest > 0 and volume > open_interest),
                    "ask_side_volume": ask_side_volume,
                    "bid_side_volume": bid_side_volume,
                    "ask_side_pct": round(ask_side_pct, 4) if ask_side_pct else 0.0,
                    "bid_side_pct": round(bid_side_pct, 4) if bid_side_pct else 0.0,
                    "strike": self._to_float(record.get("strike") or record.get("strike_price")),
                    "expiry": str(record.get("expiry") or record.get("expiration") or ""),
                    "stock_price": self._to_float(record.get("stock_price") or record.get("underlying_price")),
                    "avg_price": self._to_float(record.get("avg_price") or record.get("price")),
                    "delta": self._to_float(record.get("delta")),
                    "implied_volatility": self._to_float(record.get("implied_volatility") or record.get("iv")),
                    "sector": str(record.get("sector") or ""),
                    "issue_type": str(record.get("issue_type") or ""),
                    "next_earnings_date": str(record.get("next_earnings_date") or ""),
                    "timestamp": str(record.get("last_fill") or record.get("executed_at") or ""),
                    "raw": record,
                }
            )
        return normalized

    def _normalize_net_premium_ticks(self, records) -> List[Dict]:
        rows = records if isinstance(records, list) else ([records] if records else [])
        normalized = []
        for record in rows:
            if not isinstance(record, dict):
                continue
            normalized.append(
                {
                    "date": str(record.get("date") or ""),
                    "tape_time": str(record.get("tape_time") or record.get("time") or ""),
                    "call_volume": self._to_int(record.get("call_volume")),
                    "put_volume": self._to_int(record.get("put_volume")),
                    "call_volume_ask_side": self._to_int(record.get("call_volume_ask_side")),
                    "call_volume_bid_side": self._to_int(record.get("call_volume_bid_side")),
                    "put_volume_ask_side": self._to_int(record.get("put_volume_ask_side")),
                    "put_volume_bid_side": self._to_int(record.get("put_volume_bid_side")),
                    "net_call_volume": self._to_float(record.get("net_call_volume")),
                    "net_put_volume": self._to_float(record.get("net_put_volume")),
                    "net_call_premium": self._to_float(record.get("net_call_premium")),
                    "net_put_premium": self._to_float(record.get("net_put_premium")),
                    "net_delta": self._to_float(record.get("net_delta")),
                    "raw": record,
                }
            )
        return normalized

    def _normalize_interpolated_iv(self, records) -> List[Dict]:
        rows = records if isinstance(records, list) else ([records] if records else [])
        normalized = []
        for record in rows:
            if not isinstance(record, dict):
                continue
            normalized.append(
                {
                    "date": str(record.get("date") or ""),
                    "days": self._to_int(record.get("days")),
                    "percentile": self._to_float(record.get("percentile")),
                    "volatility": self._to_float(record.get("volatility")),
                    "implied_move_pct": self._to_float(record.get("implied_move_perc")),
                    "raw": record,
                }
            )
        normalized.sort(key=lambda row: (row.get("days", 0), row.get("date", "")))
        return normalized

    def _normalize_options_volume(self, records) -> List[Dict]:
        rows = records if isinstance(records, list) else ([records] if records else [])
        normalized = []
        for record in rows:
            if not isinstance(record, dict):
                continue
            call_premium = self._to_float(record.get("call_premium"))
            put_premium = self._to_float(record.get("put_premium"))
            bullish_premium = self._to_float(record.get("bullish_premium"))
            bearish_premium = self._to_float(record.get("bearish_premium"))
            call_volume = self._to_int(record.get("call_volume"))
            put_volume = self._to_int(record.get("put_volume"))
            call_put_ratio = round(call_volume / max(put_volume, 1), 3) if (call_volume > 0 or put_volume > 0) else 0.0
            premium_ratio = round(call_premium / max(put_premium, 1.0), 3) if (call_premium > 0 or put_premium > 0) else 0.0
            bias = "neutral"
            if bullish_premium > bearish_premium * 1.1 or call_premium > put_premium * 1.1:
                bias = "bullish"
            elif bearish_premium > bullish_premium * 1.1 or put_premium > call_premium * 1.1:
                bias = "bearish"
            normalized.append(
                {
                    "date": str(record.get("date") or ""),
                    "call_volume": call_volume,
                    "put_volume": put_volume,
                    "call_volume_ask_side": self._to_int(record.get("call_volume_ask_side")),
                    "call_volume_bid_side": self._to_int(record.get("call_volume_bid_side")),
                    "put_volume_ask_side": self._to_int(record.get("put_volume_ask_side")),
                    "put_volume_bid_side": self._to_int(record.get("put_volume_bid_side")),
                    "call_premium": call_premium,
                    "put_premium": put_premium,
                    "bullish_premium": bullish_premium,
                    "bearish_premium": bearish_premium,
                    "call_put_ratio": call_put_ratio,
                    "premium_ratio": premium_ratio,
                    "call_open_interest": self._to_int(record.get("call_open_interest")),
                    "put_open_interest": self._to_int(record.get("put_open_interest")),
                    "bias": bias,
                    "raw": record,
                }
            )
        return normalized

    @staticmethod
    def summarize_option_screener(records: List[Dict]) -> List[Dict]:
        by_ticker: Dict[str, Dict] = {}
        for record in records or []:
            ticker = str(record.get("ticker", "")).upper()
            if not ticker:
                continue
            bucket = by_ticker.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "contracts": 0,
                    "total_premium": 0.0,
                    "bullish_premium": 0.0,
                    "bearish_premium": 0.0,
                    "avg_ask_side_pct": 0.0,
                    "avg_vol_to_oi": 0.0,
                    "new_position_contracts": 0,
                },
            )
            bucket["contracts"] += 1
            premium = float(record.get("premium", 0.0) or 0.0)
            bucket["total_premium"] += premium
            bucket["avg_ask_side_pct"] += float(record.get("ask_side_pct", 0.0) or 0.0)
            bucket["avg_vol_to_oi"] += float(record.get("vol_to_oi", 0.0) or 0.0)
            if record.get("is_new_position"):
                bucket["new_position_contracts"] += 1
            sentiment = str(record.get("sentiment") or "")
            if sentiment == "bullish":
                bucket["bullish_premium"] += premium
            elif sentiment == "bearish":
                bucket["bearish_premium"] += premium

        summaries = []
        for ticker, bucket in by_ticker.items():
            contracts = int(bucket["contracts"] or 0)
            avg_ask_side_pct = bucket["avg_ask_side_pct"] / max(contracts, 1)
            avg_vol_to_oi = bucket["avg_vol_to_oi"] / max(contracts, 1)
            bullish = bucket["bullish_premium"]
            bearish = bucket["bearish_premium"]
            bias = "neutral"
            if bullish > bearish * 1.1:
                bias = "bullish"
            elif bearish > bullish * 1.1:
                bias = "bearish"
            summaries.append(
                {
                    "ticker": ticker,
                    "contracts": contracts,
                    "total_premium": round(bucket["total_premium"], 2),
                    "bullish_premium": round(bullish, 2),
                    "bearish_premium": round(bearish, 2),
                    "avg_ask_side_pct": round(avg_ask_side_pct, 3),
                    "avg_vol_to_oi": round(avg_vol_to_oi, 3),
                    "new_position_contracts": bucket["new_position_contracts"],
                    "bias": bias,
                }
            )
        summaries.sort(
            key=lambda row: (
                float(row.get("total_premium", 0.0) or 0.0),
                int(row.get("contracts", 0) or 0),
            ),
            reverse=True,
        )
        return summaries

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

    def get_flow_recent(self, symbol: str, min_premium: int = 100_000, side: Optional[str] = None, limit: int = 100) -> List[Dict]:
        ticker = str(symbol or "").upper().strip()
        if not ticker:
            return []
        cache_key = ("flow_recent", ticker, int(min_premium or 0), str(side or "").lower(), int(limit or 100))
        cached = self._get_cached(cache_key, ttl_seconds=60)
        if cached is not None:
            return cached

        params = {"min_premium": int(min_premium)}
        if side:
            params["side"] = str(side).lower()
        records = self._request(f"/api/stock/{ticker}/flow-recent", params=params)
        normalized = self._normalize_flow_alerts(records)[: int(limit or 100)]
        return self._set_cached(cache_key, normalized)

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

    def summarize_recent_flow_for_symbol(self, symbol: str, min_premium: Optional[int] = None) -> Dict:
        flows = self.get_flow_recent(symbol, min_premium=min_premium or self._default_flow_premium, limit=100)
        bullish_premium = sum(a.get("premium", 0.0) for a in flows if a.get("sentiment") == "bullish")
        bearish_premium = sum(a.get("premium", 0.0) for a in flows if a.get("sentiment") == "bearish")
        bias = "neutral"
        if bullish_premium > bearish_premium * 1.1:
            bias = "bullish"
        elif bearish_premium > bullish_premium * 1.1:
            bias = "bearish"
        return {
            "ticker": str(symbol).upper(),
            "has_flow": bool(flows),
            "signals": flows,
            "count": len(flows),
            "bullish_premium": round(bullish_premium, 2),
            "bearish_premium": round(bearish_premium, 2),
            "bias": bias,
            "summary": (
                f"{len(flows)} recent flows; bullish ${bullish_premium:,.0f}; bearish ${bearish_premium:,.0f}; bias {bias}"
                if flows
                else "No recent ticker flow"
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

    def get_option_contract_screener(
        self,
        *,
        limit: int = 50,
        ticker_symbol: Optional[str] = None,
        is_otm: Optional[bool] = None,
        min_premium: Optional[int] = None,
        min_volume: Optional[int] = None,
        max_dte: Optional[int] = None,
        min_dte: Optional[int] = None,
        vol_greater_oi: Optional[bool] = None,
        issue_types: Optional[List[str]] = None,
        option_type: Optional[str] = None,
        exclude_ex_div_ticker: Optional[bool] = None,
    ) -> List[Dict]:
        cache_key = (
            "option_screener",
            int(limit or 50),
            str(ticker_symbol or "").upper(),
            bool(is_otm) if is_otm is not None else None,
            int(min_premium or 0),
            int(min_volume or 0),
            int(max_dte or 0),
            int(min_dte or 0),
            bool(vol_greater_oi) if vol_greater_oi is not None else None,
            tuple(issue_types or []),
            str(option_type or "").lower(),
            bool(exclude_ex_div_ticker) if exclude_ex_div_ticker is not None else None,
        )
        cached = self._get_cached(cache_key, ttl_seconds=60)
        if cached is not None:
            return cached

        params: Dict[str, object] = {"limit": int(limit or 50)}
        if ticker_symbol:
            params["ticker_symbol"] = str(ticker_symbol).upper()
        if is_otm is not None:
            params["is_otm"] = bool(is_otm)
        if min_premium is not None:
            params["min_premium"] = int(min_premium)
        if min_volume is not None:
            params["min_volume"] = int(min_volume)
        if max_dte is not None:
            params["max_dte"] = int(max_dte)
        if min_dte is not None:
            params["min_dte"] = int(min_dte)
        if vol_greater_oi is not None:
            params["vol_greater_oi"] = bool(vol_greater_oi)
        if issue_types:
            params["issue_types[]"] = issue_types
        if option_type:
            params["type"] = str(option_type).lower()
        if exclude_ex_div_ticker is not None:
            params["exclude_ex_div_ticker"] = bool(exclude_ex_div_ticker)

        records = self._request("/api/screener/option-contracts", params=params)
        return self._set_cached(cache_key, self._normalize_option_screener(records))

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

    def get_net_premium_ticks(self, symbol: str, date: Optional[str] = None) -> List[Dict]:
        ticker = str(symbol or "").upper().strip()
        if not ticker:
            return []
        cache_key = ("net_premium_ticks", ticker, str(date or ""))
        cached = self._get_cached(cache_key, ttl_seconds=60)
        if cached is not None:
            return cached
        params = {"date": date} if date else None
        records = self._request(f"/api/stock/{ticker}/net-prem-ticks", params=params)
        return self._set_cached(cache_key, self._normalize_net_premium_ticks(records))

    def summarize_net_premium_ticks(self, symbol: str, date: Optional[str] = None) -> Dict:
        ticks = self.get_net_premium_ticks(symbol, date=date)
        if not ticks:
            return {"ticker": str(symbol).upper(), "bias": "neutral", "summary": "No net premium ticks"}
        last = ticks[-1]
        call_premium = float(last.get("net_call_premium", 0.0) or 0.0)
        put_premium = float(last.get("net_put_premium", 0.0) or 0.0)
        net_delta = float(last.get("net_delta", 0.0) or 0.0)
        bias = "neutral"
        if call_premium > abs(put_premium) and net_delta > 0:
            bias = "bullish"
        elif abs(put_premium) > abs(call_premium) and net_delta < 0:
            bias = "bearish"
        return {
            "ticker": str(symbol).upper(),
            "bias": bias,
            "net_call_premium": round(call_premium, 2),
            "net_put_premium": round(put_premium, 2),
            "net_delta": round(net_delta, 2),
            "latest_tick": last,
            "summary": (
                f"net premium bias {bias}; call ${call_premium:,.0f}; "
                f"put ${put_premium:,.0f}; delta {net_delta:,.0f}"
            ),
        }

    def get_interpolated_iv(self, symbol: str, date: Optional[str] = None) -> List[Dict]:
        ticker = str(symbol or "").upper().strip()
        if not ticker:
            return []
        cache_key = ("interpolated_iv", ticker, str(date or ""))
        cached = self._get_cached(cache_key, ttl_seconds=300)
        if cached is not None:
            return cached
        params = {"date": date} if date else None
        records = self._request(f"/api/stock/{ticker}/interpolated-iv", params=params)
        return self._set_cached(cache_key, self._normalize_interpolated_iv(records))

    def summarize_interpolated_iv(self, symbol: str, target_days: int = 30, date: Optional[str] = None) -> Dict:
        profile = self.get_interpolated_iv(symbol, date=date)
        if not profile:
            return {"ticker": str(symbol).upper(), "iv_context": "unavailable", "summary": "No interpolated IV"}
        target = min(profile, key=lambda row: abs(int(row.get("days", 0) or 0) - int(target_days)))
        percentile = float(target.get("percentile", 0.0) or 0.0)
        context = "normal"
        if percentile >= 0.85:
            context = "elevated"
        elif 0 < percentile <= 0.25:
            context = "cheap"
        return {
            "ticker": str(symbol).upper(),
            "days": target.get("days", 0),
            "percentile": round(percentile, 3),
            "volatility": round(float(target.get("volatility", 0.0) or 0.0), 3),
            "implied_move_pct": round(float(target.get("implied_move_pct", 0.0) or 0.0), 4),
            "iv_context": context,
            "profile": profile,
            "summary": f"{target.get('days', 0)}D IV percentile {percentile:.2f} ({context})",
        }

    def get_options_volume(self, symbol: str, limit: int = 1) -> List[Dict]:
        ticker = str(symbol or "").upper().strip()
        if not ticker:
            return []
        cache_key = ("options_volume", ticker, int(limit or 1))
        cached = self._get_cached(cache_key, ttl_seconds=300)
        if cached is not None:
            return cached
        records = self._request(f"/api/stock/{ticker}/options-volume", params={"limit": int(limit or 1)})
        return self._set_cached(cache_key, self._normalize_options_volume(records))

    def summarize_options_volume(self, symbol: str, limit: int = 1) -> Dict:
        rows = self.get_options_volume(symbol, limit=limit)
        if not rows:
            return {"ticker": str(symbol).upper(), "bias": "neutral", "summary": "No options volume snapshot"}
        row = rows[0]
        return {
            "ticker": str(symbol).upper(),
            "bias": row.get("bias", "neutral"),
            "call_volume": row.get("call_volume", 0),
            "put_volume": row.get("put_volume", 0),
            "call_put_ratio": row.get("call_put_ratio", 0.0),
            "call_premium": row.get("call_premium", 0.0),
            "put_premium": row.get("put_premium", 0.0),
            "bullish_premium": row.get("bullish_premium", 0.0),
            "bearish_premium": row.get("bearish_premium", 0.0),
            "summary": (
                f"options volume bias {row.get('bias', 'neutral')}; "
                f"call/put vol {row.get('call_put_ratio', 0.0):.2f}; "
                f"call prem ${row.get('call_premium', 0.0):,.0f}; "
                f"put prem ${row.get('put_premium', 0.0):,.0f}"
            ),
        }
