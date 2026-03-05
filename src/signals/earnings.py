"""
Earnings Calendar Scanner — Track upcoming earnings for momentum plays.

Source: Nasdaq earnings calendar API (free, no auth, reliable dates)
  GET https://api.nasdaq.com/api/calendar/earnings?date=YYYY-MM-DD

Strategies:
  - Pre-earnings run-up: Stocks tend to run 3-5 days before earnings
  - Post-earnings drift: Surprise direction continues for days
  - Earnings gap plays: Big gaps on earnings → momentum trade
  - After-hours earnings: Position before close for the gap

Data cached per-date and refreshed every 6 hours.
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
    """Scan for upcoming earnings using Nasdaq calendar API."""

    NASDAQ_URL = "https://api.nasdaq.com/api/calendar/earnings"
    HEADERS = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    }

    def __init__(self):
        self._calendar: List[Dict] = []
        self._last_fetch = 0
        self._fetch_interval = 6 * 3600  # 6 hours
        self._load_cache()
        logger.info(f"Earnings scanner initialized ({len(self._calendar)} cached earnings)")

    def _load_cache(self):
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._calendar = data.get("earnings", [])
                    self._last_fetch = data.get("fetched_at", 0)
        except Exception:
            pass

    def _save_cache(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({"earnings": self._calendar, "fetched_at": time.time()}, f, indent=2)
        except Exception:
            pass

    async def refresh(self) -> List[Dict]:
        """Fetch upcoming earnings calendar from Nasdaq API."""
        now = time.time()
        if now - self._last_fetch < self._fetch_interval and self._calendar:
            return self._calendar

        earnings = []
        today = datetime.now()

        async with httpx.AsyncClient(timeout=15, headers=self.HEADERS) as client:
            # Fetch next 7 trading days
            for day_offset in range(10):
                date = today + timedelta(days=day_offset)
                # Skip weekends
                if date.weekday() >= 5:
                    continue
                date_str = date.strftime("%Y-%m-%d")

                try:
                    resp = await client.get(self.NASDAQ_URL, params={"date": date_str})
                    if resp.status_code == 200:
                        data = resp.json()
                        rows = data.get("data", {}).get("rows", [])
                        if rows:
                            for row in rows:
                                ticker = row.get("symbol", "")
                                if not ticker or not ticker.replace(".", "").isalpha():
                                    continue
                                # Skip ADRs with dots (PBR.A etc)
                                if "." in ticker:
                                    continue

                                timing_raw = row.get("time", "")
                                if "pre" in timing_raw:
                                    timing = "BMO"  # Before Market Open
                                elif "after" in timing_raw:
                                    timing = "AMC"  # After Market Close
                                else:
                                    timing = "unknown"

                                earnings.append({
                                    "ticker": ticker,
                                    "company": row.get("name", ""),
                                    "date": date_str,
                                    "timing": timing,
                                    "eps_estimate": row.get("epsForecast", ""),
                                    "market_cap": row.get("marketCap", ""),
                                    "source": "nasdaq",
                                })
                            logger.debug(f"📅 {date_str}: {len(rows)} earnings")
                    await asyncio.sleep(0.3)  # Rate limit
                except Exception as e:
                    logger.debug(f"Nasdaq earnings fetch failed for {date_str}: {e}")
                    continue

        if earnings:
            self._calendar = earnings
            self._last_fetch = now
            self._save_cache()
            # Log summary
            today_str = today.strftime("%Y-%m-%d")
            today_count = sum(1 for e in earnings if e["date"] == today_str)
            logger.info(f"📅 Earnings calendar refreshed: {len(earnings)} total, {today_count} today")

        return self._calendar

    async def get_upcoming(self, days: int = 7) -> List[Dict]:
        """Get earnings happening in the next N days."""
        await self.refresh()
        cutoff = (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")
        return [e for e in self._calendar if today <= e.get("date", "") <= cutoff]

    async def get_today(self) -> List[Dict]:
        """Get earnings reporting today."""
        await self.refresh()
        today = datetime.now().strftime("%Y-%m-%d")
        return [e for e in self._calendar if e.get("date", "") == today]

    async def get_tomorrow(self) -> List[Dict]:
        """Get earnings reporting tomorrow."""
        await self.refresh()
        tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
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
        """
        today = datetime.now()
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

    async def scan(self) -> List[Dict]:
        """Return actionable earnings signals for the scanner."""
        signals = []

        pre = self.get_pre_earnings_candidates()
        for e in pre:
            signals.append({
                "ticker": e["ticker"],
                "signal": "pre_earnings",
                "days_until": e.get("days_until_earnings", 0),
                "conviction": 0.4,
                "reason": f"Earnings in {e.get('days_until_earnings', '?')} days ({e.get('timing', '?')}) — pre-earnings run-up",
            })

        today = await self.get_today()
        for e in today:
            if e.get("timing") == "AMC":
                signals.append({
                    "ticker": e["ticker"],
                    "signal": "earnings_today_amc",
                    "conviction": 0.3,
                    "reason": f"Reports after close today — potential gap tomorrow",
                })

        return signals
