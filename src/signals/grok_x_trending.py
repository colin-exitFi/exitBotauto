"""
Grok X Trending - Use Grok's real-time X/Twitter access to find trending tickers.
Grok has native access to X data — no Twitter API needed.
"""

import json
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional
from loguru import logger

import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings


class GrokXTrending:
    """Ask Grok for trending stock tickers on X with real-time context."""

    CACHE_TTL = 600  # 10 min cache — don't spam Grok
    TIMEOUT = 60  # Grok-4 reasoning can take time

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

        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        prompt = f"""Current time: {now_utc}

What are the top 25 most-discussed stock tickers on X/Twitter RIGHT NOW today, and why is each one trending?

Focus on:
- Stocks with unusual volume of mentions in the last few hours
- Earnings reactions, FDA decisions, M&A rumors, short squeezes, meme momentum
- Include the approximate sentiment (bullish/bearish/mixed) for each

Return ONLY valid JSON array (no markdown, no explanation):
[
  {{"ticker": "AAPL", "reason": "brief reason for trending", "sentiment": "bullish", "buzz_level": "high"}},
  ...
]

buzz_level should be: "extreme", "high", "medium"
sentiment should be: "bullish", "bearish", "mixed"
Only include real US stock tickers (no crypto, no ETFs unless they're actually trending)."""

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
                    })

                self._cache = results
                self._cache_ts = time.time()
                logger.info(f"🐦 Grok X trending: {len(results)} tickers — {', '.join(r['ticker'] for r in results[:10])}...")
                return results

        except Exception as e:
            logger.warning(f"Grok X trending scan failed ({type(e).__name__}): {e}")
            return self._cache or []
