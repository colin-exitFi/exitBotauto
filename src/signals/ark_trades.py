"""
ARK Invest daily trade notifications.

ARK publishes a daily workbook of ETF buys and sells after the close. These
are next-day contextual signals, not intraday execution events.
"""

import json
import time
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Dict, List

import pandas as pd
import requests
from loguru import logger


DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "ark_trades.json"


class ArkTradesScanner:
    """Fetch and summarize ARK's latest daily trade file."""

    TRADE_FILE_URLS = [
        "https://etfs.ark-funds.com/hubfs/idt/trades/ARK_Trades.xls",
        "https://www.ark-funds.com/hubfs/idt/trades/ARK_Trades.xls",
    ]

    def __init__(self):
        self._trades: List[Dict] = []
        self._last_fetch = 0.0
        self._fetch_interval = 3600
        self._load_cache()
        logger.info(f"ARK trades scanner initialized ({len(self._trades)} cached trades)")

    def _load_cache(self):
        try:
            if CACHE_FILE.exists():
                payload = json.loads(CACHE_FILE.read_text())
                self._trades = payload.get("trades", [])
                self._last_fetch = float(payload.get("fetched_at", 0) or 0)
        except Exception:
            self._trades = []
            self._last_fetch = 0.0

    def _save_cache(self):
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(
                json.dumps(
                    {"trades": self._trades, "fetched_at": self._last_fetch},
                    indent=2,
                )
            )
        except Exception as e:
            logger.debug(f"ARK trades cache save failed: {e}")

    @staticmethod
    def _to_int(value) -> int:
        try:
            return int(float(value))
        except Exception:
            return 0

    @staticmethod
    def _to_float(value) -> float:
        try:
            return float(value)
        except Exception:
            return 0.0

    @staticmethod
    def _parse_date(value) -> str:
        text = str(value or "").strip()
        for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
            except Exception:
                continue
        return text

    def get_recent_trades(self) -> List[Dict]:
        if self._trades and (time.time() - self._last_fetch) < self._fetch_interval:
            return self._trades

        for url in self.TRADE_FILE_URLS:
            try:
                response = requests.get(
                    url,
                    headers={"User-Agent": "Velox Trading Bot support@exitfi.ai"},
                    timeout=20,
                )
                response.raise_for_status()
                trades = self._parse_workbook(response.content)
                if trades:
                    self._trades = trades
                    self._last_fetch = time.time()
                    self._save_cache()
                    return self._trades
            except Exception as e:
                logger.debug(f"ARK trade file fetch failed for {url}: {e}")

        return self._trades

    def _parse_workbook(self, content: bytes) -> List[Dict]:
        try:
            frame = pd.read_excel(
                BytesIO(content),
                engine="xlrd",
                skiprows=3,
                names=["fund", "date", "direction", "ticker", "isin", "name", "shares", "weight_pct"],
            )
        except Exception as e:
            logger.debug(f"ARK workbook parse failed: {e}")
            return []

        trades = []
        for row in frame.to_dict(orient="records"):
            ticker = str(row.get("ticker", "") or "").upper().strip()
            if not ticker or not ticker.isalpha() or len(ticker) > 5:
                continue
            direction = str(row.get("direction", "") or "").strip().lower()
            if direction not in ("buy", "sell"):
                continue
            trades.append(
                {
                    "fund": str(row.get("fund", "") or "").strip(),
                    "date": self._parse_date(row.get("date", "")),
                    "direction": direction,
                    "ticker": ticker,
                    "isin": str(row.get("isin", "") or "").strip(),
                    "name": str(row.get("name", "") or "").strip(),
                    "shares": self._to_int(row.get("shares", 0)),
                    "weight_pct": round(self._to_float(row.get("weight_pct", 0) or 0), 4),
                    "source": "ark_trades",
                }
            )
        trades.sort(key=lambda row: (row.get("date", ""), row.get("ticker", ""), row.get("fund", "")), reverse=True)
        return trades

    def _latest_trade_date(self) -> str:
        trades = self.get_recent_trades()
        if not trades:
            return ""
        return max(str(trade.get("date", "") or "") for trade in trades)

    def _group_signals(self, direction: str) -> List[Dict]:
        latest_date = self._latest_trade_date()
        if not latest_date:
            return []
        grouped: Dict[str, Dict] = {}
        for trade in self.get_recent_trades():
            if trade.get("date") != latest_date or trade.get("direction") != direction:
                continue
            ticker = trade["ticker"]
            bucket = grouped.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "date": latest_date,
                    "direction": direction,
                    "company": trade.get("name", ""),
                    "funds": set(),
                    "shares": 0,
                    "weight_pct": 0.0,
                },
            )
            bucket["funds"].add(trade.get("fund", ""))
            bucket["shares"] += int(trade.get("shares", 0) or 0)
            bucket["weight_pct"] += float(trade.get("weight_pct", 0) or 0.0)

        signals = []
        for bucket in grouped.values():
            fund_count = len(bucket["funds"])
            weight_pct = float(bucket["weight_pct"] or 0.0)
            shares = int(bucket["shares"] or 0)
            conviction = 0.32 + min(0.18, 0.06 * fund_count)
            if weight_pct >= 0.05:
                conviction += 0.08
            elif weight_pct >= 0.02:
                conviction += 0.04
            if shares >= 100_000:
                conviction += 0.05
            signals.append(
                {
                    "ticker": bucket["ticker"],
                    "date": latest_date,
                    "direction": direction,
                    "company": bucket["company"],
                    "fund_count": fund_count,
                    "funds": sorted(bucket["funds"]),
                    "shares": shares,
                    "weight_pct": round(weight_pct, 4),
                    "conviction": round(min(0.8, conviction), 3),
                    "reason": (
                        f"ARK {direction} across {fund_count} fund(s), "
                        f"{shares:,} shares, {weight_pct:.4f}% total ETF weight"
                    ),
                }
            )
        signals.sort(key=lambda row: (row.get("fund_count", 0), row.get("weight_pct", 0.0), row.get("shares", 0)), reverse=True)
        return signals

    def get_buy_signals(self) -> List[Dict]:
        return self._group_signals("buy")

    def get_sell_signals(self) -> List[Dict]:
        return self._group_signals("sell")
