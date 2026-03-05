"""
Pharma Catalyst Scanner - FDA PDUFA dates, clinical trial approvals, and drug catalysts.

Sources:
  1. Perplexity AI (real-time web) — upcoming PDUFA dates & catalysts
  2. OpenFDA API — recent drug approvals (catches new approvals fast)
  3. ClinicalTrials.gov API — trial status changes (APPROVED_FOR_MARKETING)

Strategy:
  - Maintain watchlist of tickers with upcoming PDUFA dates
  - Monitor FDA API for new approvals every scan cycle
  - Flag catalysts as HIGH PRIORITY signals to the scanner
  - Pre-PDUFA positioning (2-5 days before date)
  - Post-approval momentum catching (within minutes of FDA decision)
"""

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import httpx
import requests

from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "pharma_catalysts.json"


class PharmaCatalystScanner:
    """
    Tracks FDA catalysts and generates high-priority trading signals.
    
    Signals:
      - PRE_CATALYST: 1-5 days before PDUFA date (position building)
      - APPROVAL: FDA just approved a drug (momentum play)
      - REJECTION: FDA rejected (short or avoid)
      - ADCOM_POSITIVE: Advisory committee voted favorably
    """

    def __init__(self):
        self._pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        self._catalysts: List[Dict] = []
        self._last_pplx_refresh = 0
        self._last_fda_check = 0
        self._known_approvals: set = set()  # Track already-seen approvals
        self._load_cache()
        logger.info(f"Pharma catalyst scanner initialized ({len(self._catalysts)} cached catalysts)")

    def _load_cache(self):
        """Load cached catalysts from disk."""
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._catalysts = data.get("catalysts", [])
                    self._known_approvals = set(data.get("known_approvals", []))
                    self._last_pplx_refresh = data.get("last_pplx_refresh", 0)
        except Exception as e:
            logger.debug(f"Cache load failed: {e}")

    def _save_cache(self):
        """Save catalysts to disk."""
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({
                    "catalysts": self._catalysts,
                    "known_approvals": list(self._known_approvals),
                    "last_pplx_refresh": self._last_pplx_refresh,
                    "updated_at": datetime.now().isoformat(),
                }, f, indent=2)
        except Exception as e:
            logger.debug(f"Cache save failed: {e}")

    async def scan(self) -> List[Dict]:
        """
        Run pharma catalyst scan. Returns list of actionable signals.
        
        Each signal: {
            ticker, company, drug, indication, catalyst_date,
            catalyst_type, signal_type, days_until, priority, score
        }
        """
        now = time.time()
        signals = []

        # 1. Refresh PDUFA calendar via Perplexity (every 6 hours)
        if now - self._last_pplx_refresh > 6 * 3600:
            await self._refresh_pdufa_calendar()

        # 2. Check FDA for NEW approvals (every 5 minutes)
        if now - self._last_fda_check > 300:
            new_approvals = await self._check_fda_approvals()
            for approval in new_approvals:
                signals.append({
                    **approval,
                    "signal_type": "APPROVAL",
                    "priority": "CRITICAL",
                    "score": 0.95,  # Highest priority
                })

        # 3. Generate pre-catalyst signals from watchlist
        today = datetime.now().date()
        for cat in self._catalysts:
            try:
                cat_date = datetime.strptime(cat.get("date", ""), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            ticker = cat.get("ticker", "")
            if not ticker or ticker == "?" or len(ticker) > 5:
                continue

            days_until = (cat_date - today).days

            if days_until < 0:
                continue  # Past catalyst, skip
            
            if days_until == 0:
                # CATALYST DAY — highest priority
                signals.append({
                    "ticker": ticker,
                    "company": cat.get("company", ""),
                    "drug": cat.get("drug", ""),
                    "indication": cat.get("indication", ""),
                    "catalyst_date": cat.get("date", ""),
                    "catalyst_type": cat.get("type", ""),
                    "signal_type": "CATALYST_DAY",
                    "days_until": 0,
                    "priority": "CRITICAL",
                    "score": 0.90,
                })
            elif days_until <= 5:
                # PRE-CATALYST — position building window
                signals.append({
                    "ticker": ticker,
                    "company": cat.get("company", ""),
                    "drug": cat.get("drug", ""),
                    "indication": cat.get("indication", ""),
                    "catalyst_date": cat.get("date", ""),
                    "catalyst_type": cat.get("type", ""),
                    "signal_type": "PRE_CATALYST",
                    "days_until": days_until,
                    "priority": "HIGH",
                    "score": 0.70 + (0.04 * (5 - days_until)),  # Closer = higher score
                })
            elif days_until <= 14:
                # WATCHLIST — on radar
                signals.append({
                    "ticker": ticker,
                    "company": cat.get("company", ""),
                    "drug": cat.get("drug", ""),
                    "indication": cat.get("indication", ""),
                    "catalyst_date": cat.get("date", ""),
                    "catalyst_type": cat.get("type", ""),
                    "signal_type": "WATCHLIST",
                    "days_until": days_until,
                    "priority": "MEDIUM",
                    "score": 0.50 + (0.01 * (14 - days_until)),
                })

        # Sort by score
        signals.sort(key=lambda x: x["score"], reverse=True)

        if signals:
            logger.info(f"💊 Pharma catalysts: {len(signals)} active signals")
            for s in signals[:5]:
                logger.info(
                    f"  {s['signal_type']:14s} {s['ticker']:6s} "
                    f"{s['drug'][:20]:20s} {s['catalyst_type']:5s} "
                    f"in {s['days_until']}d  score={s['score']:.2f}"
                )

        return signals

    async def _refresh_pdufa_calendar(self):
        """Use Perplexity to get upcoming PDUFA dates (real-time web access)."""
        if not self._pplx_key:
            logger.debug("No Perplexity key — skipping PDUFA refresh")
            return

        logger.info("💊 Refreshing PDUFA calendar via Perplexity...")
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._pplx_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro'),
                        "max_tokens": 2000,
                        "messages": [{"role": "user", "content":
                            f"Today is {today}. List ALL upcoming FDA PDUFA dates, AdCom meetings, "
                            f"and major clinical trial data readouts for the next 60 days. "
                            f"For each, provide: stock ticker, company name, drug name, indication, "
                            f"exact date (YYYY-MM-DD), and catalyst type (NDA, BLA, sNDA, AdCom, Phase3). "
                            f"Format EXACTLY as: TICKER|Company|Drug|Indication|YYYY-MM-DD|Type "
                            f"One per line. Only confirmed/scheduled dates. No commentary."}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                
                catalysts = []
                for line in text.strip().split("\n"):
                    line = line.strip().lstrip("•-123456789. ")
                    parts = [p.strip() for p in line.split("|")]
                    if len(parts) >= 6:
                        ticker = parts[0].replace("$", "").strip()
                        catalysts.append({
                            "ticker": ticker,
                            "company": parts[1],
                            "drug": parts[2],
                            "indication": parts[3],
                            "date": parts[4],
                            "type": parts[5],
                        })
                
                if catalysts:
                    self._catalysts = catalysts
                    self._last_pplx_refresh = time.time()
                    self._save_cache()
                    logger.success(f"💊 Loaded {len(catalysts)} upcoming pharma catalysts")
                else:
                    logger.warning("Perplexity returned no parseable catalysts")

        except Exception as e:
            logger.warning(f"PDUFA calendar refresh failed: {e}")

    async def _check_fda_approvals(self):
        """Check OpenFDA API for new drug approvals in the last 24 hours."""
        self._last_fda_check = time.time()
        new_approvals = []

        try:
            # Check for recent NDA/BLA original approvals (not supplements/labeling)
            today = datetime.now().strftime("%Y%m%d")
            yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
            
            url = (
                f"https://api.fda.gov/drug/drugsfda.json?"
                f"search=submissions.submission_status:\"AP\""
                f"+AND+submissions.submission_type:\"ORIG\""
                f"&limit=10"
            )

            resp = requests.get(url, timeout=10)
            if resp.status_code == 200:
                results = resp.json().get("results", [])
                for drug in results:
                    sponsor = drug.get("sponsor_name", "")
                    brand = drug.get("openfda", {}).get("brand_name", [""])[0] if drug.get("openfda") else ""
                    generic = drug.get("openfda", {}).get("generic_name", [""])[0] if drug.get("openfda") else ""
                    app_no = drug.get("application_number", "")
                    
                    # Find the most recent approval submission
                    for sub in drug.get("submissions", []):
                        if sub.get("submission_status") == "AP" and sub.get("submission_type") == "ORIG":
                            status_date = sub.get("submission_status_date", "")
                            # Check if this is recent (within last 2 days)
                            if status_date >= yesterday:
                                approval_key = f"{app_no}_{status_date}"
                                if approval_key not in self._known_approvals:
                                    self._known_approvals.add(approval_key)
                                    new_approvals.append({
                                        "ticker": "",  # Need to map company → ticker
                                        "company": sponsor,
                                        "drug": brand or generic,
                                        "indication": "",
                                        "catalyst_date": f"{status_date[:4]}-{status_date[4:6]}-{status_date[6:8]}",
                                        "catalyst_type": "NDA_APPROVAL",
                                        "days_until": 0,
                                    })
                                    logger.info(f"🚨 NEW FDA APPROVAL: {sponsor} - {brand or generic} ({status_date})")

                if new_approvals:
                    self._save_cache()

        except Exception as e:
            logger.debug(f"FDA approval check failed: {e}")

        return new_approvals

    def get_watchlist_tickers(self) -> List[str]:
        """Return list of tickers with upcoming catalysts (for scanner priority)."""
        today = datetime.now().date()
        tickers = []
        for cat in self._catalysts:
            try:
                cat_date = datetime.strptime(cat.get("date", ""), "%Y-%m-%d").date()
                days_until = (cat_date - today).days
                ticker = cat.get("ticker", "")
                if 0 <= days_until <= 14 and ticker and ticker != "?" and ticker.isalpha():
                    tickers.append(ticker)
            except (ValueError, TypeError):
                continue
        return list(set(tickers))

    def get_catalysts(self) -> List[Dict]:
        """Return all cached catalysts."""
        return list(self._catalysts)
