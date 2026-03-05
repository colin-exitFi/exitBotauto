"""
Grok X Trending - Use Grok's real-time X/Twitter access to find trending tickers.
Two scans: (1) Big-cap movers (2) Under-the-radar small/mid-cap plays.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from loguru import logger

import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings


BIGCAP_PROMPT = """Current time: {now}

What are the top 15 most-discussed LARGE-CAP stock tickers (market cap > $10B) on X/Twitter RIGHT NOW today, and why is each one trending?

Focus on:
- Stocks with unusual volume of mentions in the last few hours
- Earnings reactions, analyst upgrades/downgrades, macro moves, sector rotation
- Include the approximate sentiment (bullish/bearish/mixed) for each

Return ONLY valid JSON array (no markdown, no explanation):
[
  {{"ticker": "AAPL", "reason": "brief reason for trending", "sentiment": "bullish", "buzz_level": "high", "cap": "large"}},
  ...
]

buzz_level: "extreme", "high", "medium"
sentiment: "bullish", "bearish", "mixed"
Only real US stock tickers (no crypto, no ETFs unless actually trending)."""

SMALLCAP_PROMPT = """Current time: {now}

What are the top 15 UNDER-THE-RADAR small-cap and mid-cap stocks (market cap < $10B) getting unusual buzz on X/Twitter RIGHT NOW today?

I'm looking for stocks that retail traders are discovering — the ones that haven't hit mainstream yet but are picking up momentum on X. Think:
- Biotech/pharma with FDA catalysts or trial results
- Small caps with short squeeze potential (high short interest + rising mentions)
- Penny stocks or micro-caps suddenly getting unusual mention volume
- SPACs, de-SPACs, or recent IPOs with growing chatter
- Stocks where retail is piling in before institutions notice
- Any ticker where X mention volume spiked 3x+ in the last few hours vs normal

DO NOT include obvious mega-caps (TSLA, NVDA, AAPL, MSFT, GOOGL, AMZN, META, etc.)

Return ONLY valid JSON array (no markdown, no explanation):
[
  {{"ticker": "PDYN", "reason": "why this small cap is buzzing on X right now", "sentiment": "bullish", "buzz_level": "high", "cap": "small"}},
  ...
]

buzz_level: "extreme", "high", "medium"
sentiment: "bullish", "bearish", "mixed"
Only real US stock tickers. Quality over quantity — if you can only find 8 legit ones, return 8."""


class GrokXTrending:
    """Ask Grok for trending stock tickers on X — big caps + under-the-radar small caps."""

    CACHE_TTL = 600  # 10 min cache
    TIMEOUT = 90  # grok-4 reasoning + X search can take time

    def __init__(self):
        self._cache: Optional[List[Dict]] = None
        self._cache_ts: float = 0

    async def scan(self) -> List[Dict]:
        """Get trending tickers from Grok's X data. Returns list of ticker dicts."""
        if not settings.XAI_API_KEY:
            return []

        # Cache check
        if self._cache is not None and (time.time() - self._cache_ts) < self.CACHE_TTL:
            logger.debug(f"Grok X trending: cache hit ({len(self._cache)} tickers)")
            return self._cache

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC (%A)")

        # Run both prompts in parallel
        bigcap_task = self._query_grok(BIGCAP_PROMPT.format(now=now_utc), "big-cap")
        smallcap_task = self._query_grok(SMALLCAP_PROMPT.format(now=now_utc), "small-cap")
        bigcap_results, smallcap_results = await asyncio.gather(bigcap_task, smallcap_task)

        results = bigcap_results + smallcap_results

        self._cache = results
        self._cache_ts = time.time()

        big_tickers = [r['ticker'] for r in bigcap_results[:8]]
        small_tickers = [r['ticker'] for r in smallcap_results[:8]]
        logger.info(f"🐦 Grok X trending: {len(bigcap_results)} big-cap ({', '.join(big_tickers)}...) "
                     f"+ {len(smallcap_results)} small-cap ({', '.join(small_tickers)}...)")
        return results

    async def _query_grok(self, prompt: str, label: str) -> List[Dict]:
        """Send a single query to Grok and parse the response."""
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.XAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": getattr(settings, 'XAI_MODEL', 'grok-4-0709'),
                        "max_tokens": 2000,
                        "temperature": 0.3,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]

                # Parse JSON from response
                text = text.strip()
                if "```" in text:
                    text = text.split("```json")[-1].split("```")[0] if "```json" in text else text.split("```")[1].split("```")[0]
                text = text.strip()

                start = text.find("[")
                end = text.rfind("]") + 1
                if start >= 0 and end > start:
                    tickers = json.loads(text[start:end])
                else:
                    tickers = json.loads(text)

                results = []
                for t in tickers:
                    ticker = t.get("ticker", "").upper().strip()
                    if not ticker or not ticker.isalpha() or len(ticker) > 5:
                        continue
                    results.append({
                        "ticker": ticker,
                        "reason": t.get("reason", ""),
                        "sentiment": t.get("sentiment", "mixed"),
                        "buzz_level": t.get("buzz_level", "medium"),
                        "cap": t.get("cap", "unknown"),
                    })

                logger.debug(f"Grok X {label}: {len(results)} tickers")
                return results

        except Exception as e:
            # Retry once on 504/timeout
            if "504" in str(e) or "timeout" in str(e).lower():
                logger.warning(f"Grok X {label}: timeout, retrying in 10s...")
                await asyncio.sleep(10)
                try:
                    async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                        resp = await client.post(
                            "https://api.x.ai/v1/chat/completions",
                            headers={"Authorization": f"Bearer {settings.XAI_API_KEY}", "Content-Type": "application/json"},
                            json={"model": getattr(settings, 'XAI_MODEL', 'grok-4-0709'), "max_tokens": 2000, "temperature": 0.3,
                                  "messages": [{"role": "user", "content": prompt}]},
                        )
                        resp.raise_for_status()
                        text = resp.json()["choices"][0]["message"]["content"].strip()
                        if "```" in text:
                            text = text.split("```json")[-1].split("```")[0] if "```json" in text else text.split("```")[1].split("```")[0]
                        start, end = text.find("["), text.rfind("]") + 1
                        tickers = json.loads(text[start:end]) if start >= 0 and end > start else json.loads(text)
                        return [{"ticker": t.get("ticker","").upper().strip(), "reason": t.get("reason",""),
                                 "sentiment": t.get("sentiment","mixed"), "buzz_level": t.get("buzz_level","medium"),
                                 "cap": t.get("cap","unknown")} for t in tickers
                                if t.get("ticker","").strip().isalpha() and len(t.get("ticker","").strip()) <= 5]
                except Exception:
                    pass
            logger.warning(f"Grok X {label} scan failed ({type(e).__name__}): {e}")
            return []
