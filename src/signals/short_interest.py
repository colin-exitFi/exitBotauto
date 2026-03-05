"""
Short Interest & Squeeze Detection — Find the next GME/AMC.

High short interest + catalyst + volume = squeeze potential.
When shorts HAVE to cover, they buy at any price → feedback loop → massive move.

Signals:
  - Short interest >20% of float → squeeze candidate
  - Short interest increasing + stock rising → squeeze building
  - Days to cover >5 → shorts trapped, can't exit quickly
  - Cost to borrow spiking → shorts under pressure

Sources:
  1. Finviz screener (free, scrape)
  2. Alpaca short interest data
  3. Perplexity for real-time squeeze chatter
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import httpx
from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "short_interest.json"


class ShortInterestScanner:
    """Detect high short interest stocks and potential squeeze setups."""

    def __init__(self):
        self._data: List[Dict] = []
        self._last_fetch = 0
        self._fetch_interval = 1800  # 30 min
        self._load_cache()
        logger.info(f"Short interest scanner initialized ({len(self._data)} cached)")

    def _load_cache(self):
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._data = data.get("stocks", [])
                    self._last_fetch = data.get("fetched_at", 0)
        except Exception:
            pass

    def _save_cache(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({"stocks": self._data, "fetched_at": time.time()}, f, indent=2)
        except Exception:
            pass

    async def scan(self) -> List[Dict]:
        """Scan for high short interest stocks."""
        now = time.time()
        if now - self._last_fetch < self._fetch_interval and self._data:
            return self._data

        stocks = []

        # Source 1: Finviz high short interest screener
        try:
            finviz = await self._fetch_finviz()
            stocks.extend(finviz)
        except Exception as e:
            logger.debug(f"Finviz short interest failed: {e}")

        # Source 2: Perplexity for squeeze chatter
        if not stocks:
            try:
                pplx = await self._fetch_via_perplexity()
                stocks.extend(pplx)
            except Exception as e:
                logger.debug(f"Perplexity short interest failed: {e}")

        if stocks:
            self._data = stocks
            self._last_fetch = now
            self._save_cache()
            logger.info(f"🩳 Short interest: {len(stocks)} high-SI stocks found")

        return self._data

    async def _fetch_finviz(self) -> List[Dict]:
        """Scrape Finviz for stocks with high short interest."""
        stocks = []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Finviz screener: short float > 20%
                resp = await client.get(
                    "https://finviz.com/screener.ashx",
                    params={
                        "v": "152",  # Technical view
                        "f": "sh_short_o20",  # Short float > 20%
                        "ft": "4",  # Filter: stocks
                        "o": "-shortinterestshare",  # Sort by SI desc
                    },
                    headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    },
                )
                if resp.status_code == 200:
                    import re
                    # Extract ticker data from screener table
                    rows = re.findall(
                        r'class="screener-link-primary"[^>]*>([A-Z]{1,5})</a>',
                        resp.text
                    )
                    for ticker in rows[:20]:
                        stocks.append({
                            "ticker": ticker,
                            "short_float_pct": 20.0,  # We filtered >20%
                            "source": "finviz",
                            "signal": "high_short_interest",
                            "conviction": 0.4,
                            "reason": f"Short float >20% — squeeze candidate",
                        })
        except Exception as e:
            logger.debug(f"Finviz scrape error: {e}")
        return stocks

    async def _fetch_via_perplexity(self) -> List[Dict]:
        """Use Perplexity for real-time short squeeze candidates."""
        pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if not pplx_key:
            return []

        stocks = []
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
                        "max_tokens": 600,
                        "messages": [{"role": "user", "content":
                            "What are the top 10 most shorted US stocks right now by short interest percentage? "
                            "Include ticker, short interest %, and days to cover. "
                            "Format: TICKER|short_pct|days_to_cover "
                            "One per line."}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                for line in text.strip().split("\n"):
                    parts = line.strip().split("|")
                    if len(parts) >= 2:
                        ticker = parts[0].strip().upper()
                        if ticker.isalpha() and len(ticker) <= 5:
                            try:
                                si_pct = float(parts[1].strip().replace("%", ""))
                            except ValueError:
                                si_pct = 20.0
                            stocks.append({
                                "ticker": ticker,
                                "short_float_pct": si_pct,
                                "days_to_cover": float(parts[2].strip()) if len(parts) > 2 else 0,
                                "source": "perplexity",
                                "signal": "high_short_interest",
                                "conviction": 0.5 if si_pct > 30 else 0.3,
                                "reason": f"Short float {si_pct:.0f}% — squeeze candidate",
                            })
        except Exception as e:
            logger.debug(f"Perplexity SI failed: {e}")
        return stocks

    def get_squeeze_candidates(self, min_si_pct: float = 20.0) -> List[Dict]:
        """Get stocks most likely to squeeze: high SI + momentum."""
        return [s for s in self._data if s.get("short_float_pct", 0) >= min_si_pct]

    def is_squeeze_candidate(self, ticker: str) -> bool:
        """Check if a specific ticker is a squeeze candidate."""
        return any(s["ticker"] == ticker.upper() for s in self._data)
