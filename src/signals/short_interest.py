"""
Short Interest & Squeeze Detection - Find the next GME/AMC.

High short interest + catalyst + volume = squeeze potential.
When shorts HAVE to cover, they buy at any price -> feedback loop -> massive move.

Signals:
  - Short interest >20% of float -> squeeze candidate
  - Short interest increasing + stock rising -> squeeze building
  - Days to cover >5 -> shorts trapped, can't exit quickly
  - Cost to borrow spiking -> shorts under pressure

Sources:
  1. FINRA consolidated short interest (official, structured, if current)
  2. Finviz screener (fallback scrape)
  3. Perplexity for real-time squeeze chatter
"""

import asyncio
import csv
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import httpx
from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "short_interest.json"
FINRA_SHORT_INTEREST_URL = "https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest"


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

        # Source 1: FINRA short interest (only if the public partition is current)
        try:
            finra = await self._fetch_finra()
            stocks.extend(finra)
        except Exception as e:
            logger.debug(f"FINRA short interest failed: {e}")

        # Source 2: Finviz high short interest screener
        try:
            finviz = await self._fetch_finviz()
            stocks.extend(finviz)
        except Exception as e:
            logger.debug(f"Finviz short interest failed: {e}")

        # Source 3: Perplexity for squeeze chatter
        if not stocks:
            try:
                pplx = await self._fetch_via_perplexity()
                stocks.extend(pplx)
            except Exception as e:
                logger.debug(f"Perplexity short interest failed: {e}")

        if stocks:
            deduped: Dict[str, Dict] = {}
            for stock in stocks:
                ticker = str(stock.get("ticker", "")).upper().strip()
                if not ticker:
                    continue
                existing = deduped.get(ticker)
                if not existing:
                    deduped[ticker] = stock
                    continue
                existing_dtc = float(existing.get("days_to_cover", 0) or 0)
                incoming_dtc = float(stock.get("days_to_cover", 0) or 0)
                existing_si = float(existing.get("short_float_pct", 0) or 0)
                incoming_si = float(stock.get("short_float_pct", 0) or 0)
                if incoming_dtc > existing_dtc or incoming_si > existing_si:
                    deduped[ticker] = stock
            self._data = sorted(
                deduped.values(),
                key=lambda row: (
                    float(row.get("days_to_cover", 0) or 0),
                    float(row.get("short_float_pct", 0) or 0),
                    float(row.get("conviction", 0) or 0),
                ),
                reverse=True,
            )
            self._last_fetch = now
            self._save_cache()
            logger.info(f"🩳 Short interest: {len(self._data)} high-SI stocks found")

        return self._data

    @staticmethod
    def _parse_finra_csv(text: str) -> List[Dict]:
        return [{str(k): str(v) for k, v in row.items()} for row in csv.DictReader(text.splitlines())]

    @staticmethod
    def _is_recent_settlement(settlement_date: str, max_age_days: int = 60) -> bool:
        try:
            dt = datetime.strptime(str(settlement_date), "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return False
        return dt >= (datetime.now(timezone.utc) - timedelta(days=max_age_days))

    async def _fetch_finra(self) -> List[Dict]:
        """
        Query FINRA consolidated short interest.
        The public partition can be stale; discard it when the latest settlement date is too old.
        """
        stocks: List[Dict] = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    FINRA_SHORT_INTEREST_URL,
                    headers={"Content-Type": "application/json"},
                    json={
                        "limit": 200,
                        "fields": [
                            "symbolCode",
                            "issueName",
                            "currentShortPositionQuantity",
                            "averageDailyVolumeQuantity",
                            "daysToCoverQuantity",
                            "changePercent",
                            "settlementDate",
                        ],
                    },
                )
                if resp.status_code != 200:
                    return []
                rows = self._parse_finra_csv(resp.text)
                if not rows:
                    return []

                settlement_date = str(rows[0].get("settlementDate", "") or "")
                if not self._is_recent_settlement(settlement_date):
                    logger.info(f"FINRA short interest ignored: public data is stale ({settlement_date or 'unknown'})")
                    return []

                for row in rows:
                    ticker = str(row.get("symbolCode", "")).upper().strip()
                    if not ticker or not ticker.isalpha() or len(ticker) > 5:
                        continue
                    try:
                        days_to_cover = float(row.get("daysToCoverQuantity", 0) or 0)
                    except Exception:
                        days_to_cover = 0.0
                    try:
                        short_qty = int(float(row.get("currentShortPositionQuantity", 0) or 0))
                    except Exception:
                        short_qty = 0
                    try:
                        avg_vol = int(float(row.get("averageDailyVolumeQuantity", 0) or 0))
                    except Exception:
                        avg_vol = 0
                    try:
                        change_pct = float(row.get("changePercent", 0) or 0)
                    except Exception:
                        change_pct = 0.0

                    if days_to_cover < 5 and short_qty < 5_000_000:
                        continue

                    conviction = 0.35
                    if days_to_cover >= 10:
                        conviction += 0.20
                    elif days_to_cover >= 5:
                        conviction += 0.10
                    if change_pct >= 15:
                        conviction += 0.10

                    stocks.append(
                        {
                            "ticker": ticker,
                            "short_float_pct": 0.0,
                            "days_to_cover": round(days_to_cover, 2),
                            "short_interest_shares": short_qty,
                            "avg_daily_volume": avg_vol,
                            "change_pct": round(change_pct, 2),
                            "source": "finra",
                            "signal": "high_short_interest",
                            "conviction": min(conviction, 0.8),
                            "reason": (
                                f"FINRA short interest: DTC {days_to_cover:.1f}, "
                                f"short qty {short_qty:,}, change {change_pct:+.1f}%"
                            ),
                            "settlement_date": settlement_date,
                        }
                    )
        except Exception as e:
            logger.debug(f"FINRA short interest error: {e}")
        return stocks

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
        return [
            s
            for s in self._data
            if float(s.get("short_float_pct", 0) or 0) >= min_si_pct
            or float(s.get("days_to_cover", 0) or 0) >= 5.0
        ]

    def is_squeeze_candidate(self, ticker: str) -> bool:
        """Check if a specific ticker is a squeeze candidate."""
        return any(s["ticker"] == ticker.upper() for s in self._data)
