"""
Earnings Calendar Scanner — Track upcoming earnings for momentum plays.

Sources:
  1. Yahoo Finance earnings calendar (free, no auth)
  2. Nasdaq earnings calendar API (free)

Strategies:
  - Pre-earnings run-up: Stocks tend to run 3-5 days before earnings
  - Post-earnings drift: Surprise direction continues for days
  - Earnings gap plays: Big gaps on earnings → momentum trade

Data cached and refreshed every 6 hours.
"""

import asyncio
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import httpx


DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "earnings_calendar.json"


class EarningsScanner:
    """Scan for upcoming earnings to find momentum catalysts."""

    def __init__(self):
        self._calendar: List[Dict] = []
        self._last_fetch = 0
        self._fetch_interval = 6 * 3600  # 6 hours
        self._load_cache()
        logger.info(f"Earnings scanner initialized ({len(self._calendar)} cached earnings)")

    def _load_cache(self):
        """Load cached earnings calendar."""
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._calendar = data.get("earnings", [])
                    self._last_fetch = data.get("fetched_at", 0)
        except Exception:
            pass

    def _save_cache(self):
        """Save earnings calendar to disk."""
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({
                    "earnings": self._calendar,
                    "fetched_at": time.time(),
                }, f, indent=2)
        except Exception:
            pass

    async def refresh(self) -> List[Dict]:
        """Fetch upcoming earnings calendar. Returns list of earnings events."""
        now = time.time()
        if now - self._last_fetch < self._fetch_interval and self._calendar:
            return self._calendar

        earnings = []

        # Source 1: Yahoo Finance
        try:
            yahoo_earnings = await self._fetch_yahoo_earnings()
            earnings.extend(yahoo_earnings)
        except Exception as e:
            logger.debug(f"Yahoo earnings fetch failed: {e}")

        # Source 2: Nasdaq API fallback
        if not earnings:
            try:
                nasdaq_earnings = await self._fetch_nasdaq_earnings()
                earnings.extend(nasdaq_earnings)
            except Exception as e:
                logger.debug(f"Nasdaq earnings fetch failed: {e}")

        if earnings:
            self._calendar = earnings
            self._last_fetch = now
            self._save_cache()
            logger.info(f"📅 Earnings calendar refreshed: {len(earnings)} upcoming earnings")

        return self._calendar

    async def get_upcoming(self, days: int = 7) -> List[Dict]:
        """Get earnings happening in the next N days."""
        await self.refresh()
        cutoff = (datetime.utcnow() + timedelta(days=days)).strftime("%Y-%m-%d")
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return [e for e in self._calendar if today <= e.get("date", "") <= cutoff]

    async def get_today(self) -> List[Dict]:
        """Get earnings reporting today (before/after market)."""
        await self.refresh()
        today = datetime.utcnow().strftime("%Y-%m-%d")
        return [e for e in self._calendar if e.get("date", "") == today]

    async def get_tomorrow(self) -> List[Dict]:
        """Get earnings reporting tomorrow."""
        await self.refresh()
        tomorrow = (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d")
        return [e for e in self._calendar if e.get("date", "") == tomorrow]

    async def check_ticker(self, ticker: str) -> Optional[Dict]:
        """Check if a specific ticker has upcoming earnings."""
        await self.refresh()
        for e in self._calendar:
            if e.get("ticker", "").upper() == ticker.upper():
                return e
        return None

    def get_pre_earnings_candidates(self) -> List[Dict]:
        """
        Get stocks 2-5 days before earnings — pre-earnings run-up play.
        These tend to drift upward as traders position for the event.
        """
        today = datetime.utcnow()
        candidates = []
        for e in self._calendar:
            try:
                earn_date = datetime.strptime(e["date"], "%Y-%m-%d")
                days_until = (earn_date - today).days
                if 2 <= days_until <= 5:
                    e["days_until_earnings"] = days_until
                    e["strategy"] = "pre_earnings_runup"
                    candidates.append(e)
            except (ValueError, KeyError):
                continue
        return candidates

    # ── Data Sources ──────────────────────────────────────────────

    async def _fetch_yahoo_earnings(self) -> List[Dict]:
        """Fetch earnings from Yahoo Finance."""
        earnings = []
        today = datetime.utcnow()

        async with httpx.AsyncClient(timeout=15) as client:
            for day_offset in range(7):
                date = today + timedelta(days=day_offset)
                date_str = date.strftime("%Y-%m-%d")

                try:
                    # Yahoo earnings calendar API
                    resp = await client.get(
                        "https://finance.yahoo.com/calendar/earnings",
                        params={"day": date_str},
                        headers={
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                        },
                    )
                    if resp.status_code == 200:
                        # Parse HTML for earnings data
                        text = resp.text
                        import re
                        # Look for ticker symbols in the earnings table
                        tickers = re.findall(r'data-symbol="([A-Z]{1,5})"', text)
                        for ticker in set(tickers):
                            earnings.append({
                                "ticker": ticker,
                                "date": date_str,
                                "source": "yahoo",
                                "timing": "unknown",  # BMO/AMC
                            })
                except Exception:
                    continue

                await asyncio.sleep(0.5)  # Rate limit

        return earnings

    async def _fetch_nasdaq_earnings(self) -> List[Dict]:
        """Fetch earnings from Nasdaq API."""
        earnings = []
        today = datetime.utcnow()

        async with httpx.AsyncClient(timeout=15) as client:
            for day_offset in range(7):
                date = today + timedelta(days=day_offset)
                date_str = date.strftime("%Y-%m-%d")

                try:
                    resp = await client.get(
                        f"https://api.nasdaq.com/api/calendar/earnings",
                        params={"date": date_str},
                        headers={
                            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                            "Accept": "application/json",
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        rows = data.get("data", {}).get("rows", [])
                        for row in rows:
                            ticker = row.get("symbol", "")
                            if ticker and ticker.isalpha() and len(ticker) <= 5:
                                earnings.append({
                                    "ticker": ticker,
                                    "company": row.get("name", ""),
                                    "date": date_str,
                                    "timing": row.get("time", "unknown"),  # BMO/AMC
                                    "eps_estimate": row.get("epsForecast", None),
                                    "source": "nasdaq",
                                })
                except Exception:
                    continue

                await asyncio.sleep(0.5)

        return earnings

    async def scan(self) -> List[Dict]:
        """
        Return actionable earnings signals for the scanner.
        Combines pre-earnings run-up candidates + today's earnings.
        """
        signals = []

        # Pre-earnings run-up (2-5 days out)
        pre = self.get_pre_earnings_candidates()
        for e in pre:
            signals.append({
                "ticker": e["ticker"],
                "signal": "pre_earnings",
                "days_until": e.get("days_until_earnings", 0),
                "conviction": 0.4,
                "reason": f"Earnings in {e.get('days_until_earnings', '?')} days — pre-earnings run-up play",
            })

        # Today's earnings (position for the gap)
        today = await self.get_today()
        for e in today:
            timing = e.get("timing", "unknown")
            if timing in ("AMC", "amc", "After Market Close"):
                signals.append({
                    "ticker": e["ticker"],
                    "signal": "earnings_today_amc",
                    "conviction": 0.3,
                    "reason": f"Reports after close today — potential gap tomorrow",
                })

        return signals
