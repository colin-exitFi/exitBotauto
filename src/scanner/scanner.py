"""
Scanner - Find high-momentum stocks from MULTIPLE sources.
Sources: Polygon gainers + StockTwits trending + enrichment via Polygon snapshots.
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
    Multi-source stock scanner:
      1. Polygon gainers (price movers with volume)
      2. StockTwits trending (social momentum)
      3. Merge + enrich ALL with Polygon snapshot data
      4. Filter on enriched data (not raw)
      5. Score & rank
    """

    def __init__(self, polygon_client=None, sentiment_analyzer=None, stocktwits_client=None, alpaca_client=None, pharma_scanner=None, fade_scanner=None):
        self.polygon = polygon_client
        self.sentiment = sentiment_analyzer
        self.stocktwits = stocktwits_client
        self.alpaca = alpaca_client
        self.pharma = pharma_scanner
        self.fade = fade_scanner

        self.min_price = settings.MIN_PRICE
        self.max_price = settings.MAX_PRICE
        self.min_volume = settings.MIN_VOLUME
        self.volume_spike_mult = settings.VOLUME_SPIKE_MULTIPLIER
        self.min_momentum = settings.MIN_MOMENTUM_PCT

        self._cache: List[Dict] = []
        self._news_cache: Dict[str, tuple] = {}
        logger.info("Scanner initialized")

    async def scan(self) -> List[Dict]:
        """Run a full scan cycle. Returns ranked candidate list."""
        logger.info("🔍 Running stock scan...")

        # ── SOURCE 1: Polygon gainers ──────────────────────────────
        polygon_candidates = []
        if self.polygon:
            raw = await asyncio.get_event_loop().run_in_executor(None, self.polygon.get_gainers)
            if raw:
                for s in raw:
                    s["source"] = "polygon"
                polygon_candidates = raw
            logger.info(f"Polygon gainers: {len(polygon_candidates)}")

        # ── SOURCE 2: StockTwits trending ──────────────────────────
        stocktwits_candidates = []
        if self.stocktwits:
            try:
                trending = await asyncio.get_event_loop().run_in_executor(
                    None, self.stocktwits.get_trending
                )
                for t in trending:
                    sym = t.get("symbol", "")
                    # Skip crypto tickers (have .X suffix)
                    if not sym or "." in sym or not sym.isalpha() or len(sym) > 5:
                        continue
                    stocktwits_candidates.append({
                        "symbol": sym,
                        "price": 0,  # will be enriched
                        "change_pct": 0,  # will be enriched
                        "volume": 0,  # will be enriched
                        "stocktwits_trending_score": t.get("trending_score", 0),
                        "source": "stocktwits",
                    })
                logger.info(f"StockTwits trending: {len(stocktwits_candidates)} valid tickers")
            except Exception as e:
                logger.warning(f"StockTwits trending failed: {e}")

        # ── SOURCE 3: Pharma catalysts (FDA PDUFA dates) ───────────
        pharma_signals = []
        pharma_tickers = set()
        if self.pharma:
            try:
                pharma_signals = await self.pharma.scan()
                for sig in pharma_signals:
                    ticker = sig.get("ticker", "")
                    if ticker and ticker.isalpha() and len(ticker) <= 5:
                        pharma_tickers.add(ticker)
                        stocktwits_candidates.append({
                            "symbol": ticker,
                            "price": 0,
                            "change_pct": 0,
                            "volume": 0,
                            "source": "pharma",
                            "pharma_signal": sig.get("signal_type", ""),
                            "pharma_score": sig.get("score", 0),
                            "pharma_drug": sig.get("drug", ""),
                            "pharma_days_until": sig.get("days_until", 99),
                            "pharma_catalyst_type": sig.get("catalyst_type", ""),
                        })
                if pharma_tickers:
                    logger.info(f"💊 Pharma catalysts: {len(pharma_tickers)} tickers ({', '.join(sorted(pharma_tickers))})")
            except Exception as e:
                logger.warning(f"Pharma catalyst scan failed: {e}")

        # ── SOURCE 4: Fade yesterday's runners (SHORT signals) ─────
        fade_signals = []
        if self.fade:
            try:
                fade_signals = await self.fade.scan()
                for sig in fade_signals:
                    ticker = sig.get("symbol", "")
                    if ticker and ticker.isalpha() and len(ticker) <= 5:
                        stocktwits_candidates.append({
                            "symbol": ticker,
                            "price": sig.get("current_price", 0),
                            "change_pct": sig.get("price_change_from_run", 0),
                            "volume": 0,
                            "source": "fade",
                            "side": "short",
                            "fade_signal": sig.get("signal_type", ""),
                            "fade_score": sig.get("score", 0),
                            "fade_run_pct": sig.get("run_change_pct", 0),
                        })
                if fade_signals:
                    logger.info(f"📉 Fade candidates: {len(fade_signals)} short setups")
            except Exception as e:
                logger.warning(f"Fade runner scan failed: {e}")

        # ── MERGE: deduplicate by symbol ───────────────────────────
        seen = {}
        for s in polygon_candidates:
            sym = s.get("symbol", "")
            if sym:
                seen[sym] = s

        for s in stocktwits_candidates:
            sym = s["symbol"]
            if sym in seen:
                # Merge trending score into polygon data
                seen[sym]["stocktwits_trending_score"] = s.get("stocktwits_trending_score", 0)
                seen[sym]["source"] = "both"
            else:
                seen[sym] = s

        all_symbols = list(seen.values())
        logger.info(f"Merged candidates: {len(all_symbols)} unique symbols")

        # ── ENRICH ALL with Polygon snapshot data ──────────────────
        # This fills in price, change_pct, volume for StockTwits-only tickers
        candidates = []
        for stock in all_symbols[:30]:  # cap at 30 to manage API calls
            enriched = await self._enrich(stock)
            if enriched:
                candidates.append(enriched)

        # ── FILTER on enriched data ────────────────────────────────
        filtered = [c for c in candidates if self._passes_filter(c)]
        logger.info(f"After filter: {len(filtered)} candidates (from {len(candidates)} enriched)")

        # ── SCORE & RANK ───────────────────────────────────────────
        for c in filtered:
            c["score"] = self._calculate_score(c)
        filtered.sort(key=lambda x: x["score"], reverse=True)

        self._cache = filtered[:10]

        # Record today's big runners for tomorrow's fade watchlist
        if self.fade:
            try:
                self.fade.record_todays_runners(filtered)
            except Exception:
                pass

        logger.success(f"Scan complete: {len(self._cache)} ranked candidates")
        for c in self._cache[:8]:
            src = c.get("source", "?")
            bull = c.get("st_bullish", 0)
            bear = c.get("st_bearish", 0)
            logger.info(
                f"  {c['symbol']:6s} ${c['price']:.2f}  chg={c['change_pct']:+.1f}%  "
                f"vol={c.get('volume_spike',0):.1f}x  "
                f"social={c.get('sentiment_score',0):+.2f}({bull}🟢/{bear}🔴)  "
                f"score={c['score']:.3f}  [{src}]"
            )

        return self._cache

    def get_cached_candidates(self) -> List[Dict]:
        return list(self._cache)

    # ── Filtering (on ENRICHED data) ───────────────────────────────

    def _passes_filter(self, s: Dict) -> bool:
        """Filter on enriched data — all tickers have real price/volume now."""
        price = s.get("price", 0)
        if price < self.min_price or price > self.max_price:
            return False

        # Skip non-standard tickers
        sym = s.get("symbol", "")
        if not sym or len(sym) > 5 or not sym.isalpha():
            return False

        # For StockTwits-sourced tickers, be more lenient on momentum
        # (they're trending for a reason — social momentum IS momentum)
        is_social = s.get("source") in ("stocktwits", "both")
        min_mom = 0.5 if is_social else self.min_momentum  # 0.5% vs 2%

        change = abs(s.get("change_pct", 0))
        if change < min_mom:
            return False

        # Volume check — require some volume but lower bar for social tickers
        min_vol = 200_000 if is_social else self.min_volume
        vol = s.get("volume", 0)
        avg_vol = s.get("avg_volume", 0)
        effective_vol = max(vol, avg_vol)
        if effective_vol < min_vol:
            return False

        return True

    # ── Enrichment ─────────────────────────────────────────────────

    async def _enrich(self, stock: Dict) -> Optional[Dict]:
        """Enrich with Alpaca snapshot (extended hours!) + StockTwits sentiment."""
        symbol = stock["symbol"]
        try:
            # ── PRICE DATA: Try Alpaca first (has extended hours), fallback Polygon ──
            alpaca_snap = None
            if self.alpaca:
                try:
                    alpaca_snap = await asyncio.get_event_loop().run_in_executor(
                        None, self._get_alpaca_snapshot, symbol
                    )
                except Exception:
                    pass

            if alpaca_snap:
                if stock.get("price", 0) <= 0:
                    stock["price"] = alpaca_snap.get("price", 0)
                if stock.get("change_pct", 0) == 0:
                    stock["change_pct"] = alpaca_snap.get("change_pct", 0)
                if stock.get("volume", 0) == 0:
                    stock["volume"] = alpaca_snap.get("volume", 0)
                stock["prev_close"] = alpaca_snap.get("prev_close", 0)

            # Fallback to Polygon if Alpaca didn't fill
            if stock.get("price", 0) <= 0 and self.polygon:
                snapshot = await asyncio.get_event_loop().run_in_executor(
                    None, self.polygon.get_snapshot, symbol
                )
                if snapshot:
                    if stock.get("price", 0) <= 0:
                        stock["price"] = snapshot.get("price", 0)
                    if stock.get("change_pct", 0) == 0:
                        stock["change_pct"] = snapshot.get("change_pct", 0)
                    if stock.get("volume", 0) == 0:
                        stock["volume"] = snapshot.get("volume", 0)

            # Skip if no price
            if stock.get("price", 0) <= 0:
                return None

            # Volume spike vs 20-day avg
            avg_vol = 0
            if self.polygon:
                avg_vol = await asyncio.get_event_loop().run_in_executor(
                    None, self.polygon.get_avg_volume, symbol, 20
                )
            vol_spike = stock.get("volume", 0) / avg_vol if avg_vol > 0 else 0
            stock["avg_volume"] = avg_vol
            stock["volume_spike"] = vol_spike

            # ── SENTIMENT: StockTwits per-ticker (real social data!) ──
            sentiment_score = 0.0
            if self.stocktwits:
                try:
                    st_sent = await asyncio.get_event_loop().run_in_executor(
                        None, self.stocktwits.get_sentiment, symbol
                    )
                    sentiment_score = st_sent.get("score", 0.0)
                    stock["st_bullish"] = st_sent.get("bullish", 0)
                    stock["st_bearish"] = st_sent.get("bearish", 0)
                    stock["st_messages"] = st_sent.get("total_messages", 0)
                except Exception:
                    pass
            stock["sentiment_score"] = sentiment_score

            # News — fetched at consensus stage (too slow for 30 tickers)
            stock["news_headlines"] = []

            return stock
        except Exception as e:
            logger.debug(f"Enrich failed for {symbol}: {e}")
            return None

    def _get_alpaca_snapshot(self, symbol: str) -> Optional[Dict]:
        """Get Alpaca snapshot — includes extended hours data."""
        import requests as req
        try:
            headers = {
                'APCA-API-KEY-ID': getattr(self.alpaca, 'api_key', settings.ALPACA_API_KEY),
                'APCA-API-SECRET-KEY': getattr(self.alpaca, 'secret_key', settings.ALPACA_SECRET_KEY),
            }
            r = req.get(
                f'https://data.alpaca.markets/v2/stocks/snapshots?symbols={symbol}&feed=iex',
                headers=headers, timeout=5
            )
            if r.status_code == 200:
                data = r.json().get(symbol, {})
                lt = data.get('latestTrade', {})
                db = data.get('dailyBar', {})
                pb = data.get('prevDailyBar', {})
                prev_close = pb.get('c', 0)
                cur_price = lt.get('p', 0)
                chg = ((cur_price - prev_close) / prev_close * 100) if prev_close > 0 else 0
                return {
                    "price": cur_price,
                    "change_pct": round(chg, 2),
                    "volume": db.get('v', 0),
                    "prev_close": prev_close,
                }
        except Exception:
            pass
        return None

    # ── News via Perplexity ──────────────────────────────────────────

    async def _get_news(self, symbol: str) -> List[str]:
        """Get recent news headlines via Perplexity API. Cached 10 min."""
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
        Composite score from multiple signals:
          volume_spike (0.15): capped at 5x → 0-1
          momentum     (0.15): change_pct / 10, capped 0-1
          sentiment    (0.25): (-1 to 1) → 0-1
          trending     (0.15): StockTwits score / 30, capped 0-1
          pharma       (0.20): catalyst proximity and type
          news         (0.10): has headlines = 1.0, none = 0.3
        """
        vol_score = min(c.get("volume_spike", 0) / 5.0, 1.0)
        mom_score = min(abs(c.get("change_pct", 0)) / 10.0, 1.0)
        sent_raw = c.get("sentiment_score", 0)
        sent_score = (sent_raw + 1.0) / 2.0  # map -1..1 → 0..1
        twits_score = min(c.get("stocktwits_trending_score", 0) / 30.0, 1.0)
        news_score = 1.0 if c.get("news_headlines") else 0.3

        # Pharma catalyst bonus — upcoming FDA decisions are HUGE signals
        pharma_score = c.get("pharma_score", 0)

        return (vol_score * 0.15 + mom_score * 0.15 + sent_score * 0.25 +
                twits_score * 0.15 + pharma_score * 0.20 + news_score * 0.10)
