"""
Congressional Trading Scanner — Follow the smart money in government.

Members of Congress trade on inside information. Their disclosures are public.
Even with 45-day delay, congressional trades outperform S&P by 6%+ annually.

Sources:
  1. Capitol Trades API (capitoltrades.com)
  2. Quiver Quantitative API (free tier)
  3. House/Senate disclosure RSS feeds

Key insight: When multiple members buy the same stock → very strong signal.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import httpx
from config import settings
from src.signals.unusual_whales import UnusualWhalesClient

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "congress_trades.json"


class CongressScanner:
    """Track congressional stock trades for alpha signals."""

    def __init__(self, uw_client: Optional[UnusualWhalesClient] = None):
        self._trades: List[Dict] = []
        self._last_fetch = 0
        self._fetch_interval = 3600  # 1 hour (trades update slowly)
        self.uw = uw_client or UnusualWhalesClient()
        self._load_cache()
        logger.info(f"Congress scanner initialized ({len(self._trades)} cached trades)")

    def _load_cache(self):
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._trades = data.get("trades", [])
                    self._last_fetch = data.get("fetched_at", 0)
        except Exception:
            pass

    def _save_cache(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({"trades": self._trades, "fetched_at": time.time()}, f, indent=2)
        except Exception:
            pass

    async def scan(self) -> List[Dict]:
        """Fetch recent congressional trades. Returns actionable signals."""
        now = time.time()
        if now - self._last_fetch < self._fetch_interval and self._trades:
            return self._trades

        trades = []

        # Source 1: Unusual Whales Congress feed
        if self.uw and self.uw.is_configured():
            try:
                trades = await asyncio.get_event_loop().run_in_executor(
                    None, self.uw.get_congress_trades, 100
                )
            except Exception as e:
                logger.debug(f"Unusual Whales congress fetch failed: {e}")

        # Source 2: Quiver Quantitative (free tier)
        if not trades:
            try:
                quiver = await self._fetch_quiver()
                trades.extend(quiver)
            except Exception as e:
                logger.debug(f"Quiver congress fetch failed: {e}")

        # Source 3: Perplexity for recent congressional trades
        if not trades:
            try:
                pplx = await self._fetch_via_perplexity()
                trades.extend(pplx)
            except Exception as e:
                logger.debug(f"Perplexity congress fetch failed: {e}")

        if trades:
            self._trades = trades
            self._last_fetch = now
            self._save_cache()
            logger.info(f"🏛️ Congress scanner: {len(trades)} recent trades")

        return self._trades

    async def _fetch_quiver(self) -> List[Dict]:
        """Fetch from Quiver Quantitative free API."""
        trades = []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    "https://api.quiverquant.com/beta/live/congresstrading",
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Velox Trading Bot",
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for t in data[:50]:
                        ticker = t.get("Ticker", "")
                        if not ticker or not ticker.isalpha():
                            continue
                        trades.append({
                            "ticker": ticker,
                            "member": t.get("Representative", "Unknown"),
                            "party": t.get("Party", ""),
                            "transaction": t.get("Transaction", "").lower(),
                            "amount": t.get("Range", ""),
                            "date": t.get("TransactionDate", ""),
                            "disclosure_date": t.get("DisclosureDate", ""),
                            "source": "quiver",
                        })
                elif resp.status_code == 429:
                    logger.debug("Quiver rate limited")
        except Exception as e:
            logger.debug(f"Quiver fetch error: {e}")
        return trades

    async def _fetch_via_perplexity(self) -> List[Dict]:
        """Use Perplexity to find recent congressional trades."""
        pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if not pplx_key:
            return []

        trades = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {pplx_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "sonar-pro",
                        "max_tokens": 800,
                        "messages": [{"role": "user", "content":
                            "What are the most notable recent US Congressional stock trades disclosed in the last 2 weeks? "
                            "Include: member name, ticker, buy/sell, approximate amount. "
                            "Format: MEMBER|TICKER|buy_or_sell|amount "
                            "One per line, top 10 most significant."}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                for line in text.strip().split("\n"):
                    parts = line.strip().split("|")
                    if len(parts) >= 3:
                        ticker = parts[1].strip().upper()
                        if ticker.isalpha() and len(ticker) <= 5:
                            trades.append({
                                "ticker": ticker,
                                "member": parts[0].strip(),
                                "transaction": parts[2].strip().lower(),
                                "amount": parts[3].strip() if len(parts) > 3 else "",
                                "source": "perplexity",
                            })
        except Exception as e:
            logger.debug(f"Perplexity congress failed: {e}")
        return trades

    def get_buy_signals(self) -> List[Dict]:
        """Get tickers that congress members are buying."""
        buys = [t for t in self._trades if "purchase" in t.get("transaction", "") or "buy" in t.get("transaction", "")]
        # Count how many members bought the same ticker
        ticker_counts = {}
        for t in buys:
            ticker = t["ticker"]
            if ticker not in ticker_counts:
                ticker_counts[ticker] = {"ticker": ticker, "members": [], "count": 0}
            ticker_counts[ticker]["members"].append(t.get("member", "Unknown"))
            ticker_counts[ticker]["count"] += 1
        # Sort by number of members buying (multiple members = stronger signal)
        signals = sorted(ticker_counts.values(), key=lambda x: -x["count"])
        return signals

    def get_sell_signals(self) -> List[Dict]:
        """Get tickers that congress members are selling."""
        sells = [t for t in self._trades if "sale" in t.get("transaction", "") or "sell" in t.get("transaction", "")]
        ticker_counts = {}
        for t in sells:
            ticker = t["ticker"]
            if ticker not in ticker_counts:
                ticker_counts[ticker] = {"ticker": ticker, "members": [], "count": 0}
            ticker_counts[ticker]["members"].append(t.get("member", "Unknown"))
            ticker_counts[ticker]["count"] += 1
        return sorted(ticker_counts.values(), key=lambda x: -x["count"])
