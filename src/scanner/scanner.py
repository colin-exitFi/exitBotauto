"""
Scanner - Find high-momentum stocks using Polygon gainers/losers + snapshots.
Scores candidates by volume spike, momentum, and sentiment.
"""

import asyncio
import time
from typing import Dict, List, Optional
from loguru import logger

import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings


class Scanner:
    """
    Stock scanner: Polygon gainers/losers → filter → score → rank.
    
    Filters:
      - Price: $5–$500
      - Avg daily volume: >1M
      - Momentum: >2% intraday change
    
    Score = volume_spike*0.3 + momentum*0.3 + sentiment*0.4
    """

    def __init__(self, polygon_client=None, sentiment_analyzer=None, stocktwits_client=None):
        self.polygon = polygon_client
        self.sentiment = sentiment_analyzer
        self.stocktwits = stocktwits_client

        self.min_price = settings.MIN_PRICE
        self.max_price = settings.MAX_PRICE
        self.min_volume = settings.MIN_VOLUME
        self.volume_spike_mult = settings.VOLUME_SPIKE_MULTIPLIER
        self.min_momentum = settings.MIN_MOMENTUM_PCT

        self._cache: List[Dict] = []
        self._news_cache: Dict[str, tuple] = {}  # symbol -> (timestamp, headlines)
        logger.info("Scanner initialized")

    async def scan(self) -> List[Dict]:
        """Run a full scan cycle. Returns ranked candidate list."""
        logger.info("🔍 Running stock scan...")

        if not self.polygon:
            logger.warning("No Polygon client – returning empty scan")
            return []

        # 1. Fetch gainers (most promising pool)
        raw = await asyncio.get_event_loop().run_in_executor(None, self.polygon.get_gainers)
        if not raw:
            logger.warning("Gainers endpoint returned nothing, trying all snapshots")
            raw = await asyncio.get_event_loop().run_in_executor(None, self.polygon.get_all_snapshots)

        logger.debug(f"Raw Polygon candidates: {len(raw)}")

        # 1b. Merge StockTwits trending
        if self.stocktwits:
            try:
                trending = await asyncio.get_event_loop().run_in_executor(
                    None, self.stocktwits.get_trending
                )
                existing_symbols = {s.get("symbol") for s in raw}
                for t in trending:
                    sym = t.get("symbol", "")
                    if sym and sym not in existing_symbols:
                        raw.append({
                            "symbol": sym,
                            "price": 0,
                            "change_pct": 0,
                            "volume": 0,
                            "stocktwits_trending_score": t.get("trending_score", 0),
                        })
                    else:
                        # Add trending score to existing
                        for s in raw:
                            if s.get("symbol") == sym:
                                s["stocktwits_trending_score"] = t.get("trending_score", 0)
                                break
                logger.debug(f"After StockTwits merge: {len(raw)} candidates")
            except Exception as e:
                logger.warning(f"StockTwits trending failed: {e}")

        # 2. Filter
        filtered = [s for s in raw if self._passes_filter(s)]
        logger.info(f"After filter: {len(filtered)} candidates")

        # 3. Enrich with avg volume + sentiment
        candidates = []
        for stock in filtered[:20]:  # cap API calls
            enriched = await self._enrich(stock)
            if enriched:
                candidates.append(enriched)

        # 4. Score & sort
        for c in candidates:
            c["score"] = self._calculate_score(c)
        candidates.sort(key=lambda x: x["score"], reverse=True)

        self._cache = candidates[:10]
        logger.success(f"Scan complete: {len(self._cache)} ranked candidates")
        for c in self._cache[:5]:
            logger.info(f"  {c['symbol']:6s} price=${c['price']:.2f}  chg={c['change_pct']:+.1f}%  vol_spike={c.get('volume_spike',0):.1f}x  sent={c.get('sentiment_score',0):.2f}  score={c['score']:.3f}")

        return self._cache

    def get_cached_candidates(self) -> List[Dict]:
        return list(self._cache)

    # ── Filtering ──────────────────────────────────────────────────

    def _passes_filter(self, s: Dict) -> bool:
        price = s.get("price", 0)
        if price < self.min_price or price > self.max_price:
            return False
        change = abs(s.get("change_pct", 0))
        if change < self.min_momentum:
            return False
        vol = s.get("volume", 0)
        if vol < self.min_volume:
            return False
        # Skip non-standard tickers (warrants, units, etc)
        sym = s.get("symbol", "")
        if not sym or len(sym) > 5 or not sym.isalpha():
            return False
        return True

    # ── Enrichment ─────────────────────────────────────────────────

    async def _enrich(self, stock: Dict) -> Optional[Dict]:
        symbol = stock["symbol"]
        try:
            # Volume spike vs 20-day avg
            avg_vol = await asyncio.get_event_loop().run_in_executor(
                None, self.polygon.get_avg_volume, symbol, 20
            )
            vol_spike = stock.get("volume", 0) / avg_vol if avg_vol > 0 else 0

            if avg_vol > 0 and avg_vol < self.min_volume:
                return None  # filter on avg volume

            # Sentiment (if available)
            sentiment_score = 0.0
            if self.sentiment:
                try:
                    sent = await self.sentiment.analyze(symbol)
                    sentiment_score = sent.get("score", 0.0)
                except Exception as e:
                    logger.debug(f"Sentiment failed for {symbol}: {e}")

            # Perplexity news headlines
            news_headlines = await self._get_news(symbol)

            return {
                **stock,
                "avg_volume": avg_vol,
                "volume_spike": vol_spike,
                "sentiment_score": sentiment_score,
                "news_headlines": news_headlines,
            }
        except Exception as e:
            logger.debug(f"Enrich failed for {symbol}: {e}")
            return None

    # ── News via Perplexity ──────────────────────────────────────────

    async def _get_news(self, symbol: str) -> List[str]:
        """Get recent news headlines via Perplexity API. Cached 10 min."""
        # Check cache
        cached = self._news_cache.get(symbol)
        if cached and (time.time() - cached[0]) < 600:
            return cached[1]

        api_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if not api_key:
            return []

        model = getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro')
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 300,
                        "messages": [{"role": "user", "content":
                            f"What are the latest news headlines about {symbol} stock in the last 4 hours? "
                            f"List only the headlines, one per line. No commentary."}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                headlines = [line.strip().lstrip("•-123456789. ") for line in text.strip().split("\n") if line.strip()]
                headlines = [h for h in headlines if len(h) > 10][:5]
                self._news_cache[symbol] = (time.time(), headlines)
                return headlines
        except Exception as e:
            logger.debug(f"Perplexity news failed for {symbol}: {e}")
            return []

    # ── Scoring ────────────────────────────────────────────────────

    def _calculate_score(self, c: Dict) -> float:
        """
        Composite score:
          volume_spike (0.3): capped at 5x → normalized 0–1
          momentum     (0.3): change_pct / 10, capped 0–1
          sentiment    (0.4): (-1 to 1) → shifted to 0–1
        """
        vol_score = min(c.get("volume_spike", 0) / 5.0, 1.0)
        mom_score = min(abs(c.get("change_pct", 0)) / 10.0, 1.0)
        sent_raw = c.get("sentiment_score", 0)
        sent_score = (sent_raw + 1.0) / 2.0  # map -1..1 → 0..1
        # StockTwits trending bonus (0-30 range → 0-1)
        twits_score = min(c.get("stocktwits_trending_score", 0) / 30.0, 1.0)

        return vol_score * 0.25 + mom_score * 0.25 + sent_score * 0.35 + twits_score * 0.15
