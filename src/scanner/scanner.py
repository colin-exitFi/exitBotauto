"""
Scanner - Find high-momentum stocks from MULTIPLE sources.
Sources: Polygon gainers + StockTwits trending + enrichment via Polygon snapshots.
"""

import asyncio
import time
from typing import Dict, List, Optional, Set
from loguru import logger

import httpx

from config import settings
from src.data import strategy_controls
from src.data.signal_attribution import extract_signal_sources, derive_strategy_tag
from src.data.technicals import compute_technicals


class Scanner:
    """
    Multi-source stock scanner:
      1. Polygon gainers (price movers with volume)
      2. StockTwits trending (social momentum)
      3. Merge + enrich ALL with Polygon snapshot data
      4. Filter on enriched data (not raw)
      5. Score & rank
    """

    def __init__(self, polygon_client=None, sentiment_analyzer=None, stocktwits_client=None, alpaca_client=None, pharma_scanner=None, fade_scanner=None, grok_x_trending=None):
        self.polygon = polygon_client
        self.sentiment = sentiment_analyzer
        self.stocktwits = stocktwits_client
        self.alpaca = alpaca_client
        self.pharma = pharma_scanner
        self.fade = fade_scanner
        self.grok_x = grok_x_trending

        self.min_price = settings.MIN_PRICE
        self.max_price = settings.MAX_PRICE
        self.min_volume = settings.MIN_VOLUME
        self.volume_spike_mult = settings.VOLUME_SPIKE_MULTIPLIER
        self.min_momentum = settings.MIN_MOMENTUM_PCT

        self._cache: List[Dict] = []
        self._news_cache: Dict[str, tuple] = {}
        self._performance_cache: Dict = {}
        self._performance_cache_at = 0.0
        self._performance_cache_ttl = 300  # 5 min
        self._index_context_cache: Dict = {}
        self._index_context_cache_at = 0.0
        self._index_context_cache_ttl = 60  # 1 min
        self._disabled_strategies_cache: Set[str] = set()
        self._disabled_strategies_cache_at = 0.0
        self._disabled_strategies_cache_ttl = 60  # 1 min
        self._signal_first_seen: Dict[str, float] = {}
        self._signal_first_seen_ttl = max(
            300,
            int(getattr(settings, "SCANNER_SIGNAL_FIRST_SEEN_TTL_SECONDS", 3600)),
        )
        self._last_market_regime = "mixed"
        logger.info("Scanner initialized")

    async def scan(self) -> List[Dict]:
        """Run a full scan cycle. Returns ranked candidate list."""
        logger.info("🔍 Running stock scan...")
        cutoff = time.time() - self._signal_first_seen_ttl
        self._signal_first_seen = {s: ts for s, ts in self._signal_first_seen.items() if ts >= cutoff}

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

        # ── SOURCE 5: Grok X/Twitter trending (real-time social) ───
        grok_x_candidates = []
        if self.grok_x:
            try:
                grok_tickers = await self.grok_x.scan()
                for t in grok_tickers:
                    ticker = t.get("ticker", "")
                    if ticker and ticker.isalpha() and len(ticker) <= 5:
                        buzz_score = {"extreme": 1.0, "high": 0.8, "medium": 0.5}.get(t.get("buzz_level", "medium"), 0.5)
                        sent_map = {"bullish": 0.7, "bearish": -0.5, "mixed": 0.1}
                        grok_x_candidates.append({
                            "symbol": ticker,
                            "price": 0,
                            "change_pct": 0,
                            "volume": 0,
                            "source": "grok_x",
                            "grok_x_reason": t.get("reason", ""),
                            "grok_x_sentiment": t.get("sentiment", "mixed"),
                            "grok_x_buzz": buzz_score,
                            "sentiment_score": sent_map.get(t.get("sentiment", "mixed"), 0.1),
                            "side": "short" if t.get("sentiment") == "bearish" else "long",
                        })
                if grok_x_candidates:
                    logger.info(f"🐦 Grok X trending: {len(grok_x_candidates)} tickers")
            except Exception as e:
                logger.warning(f"Grok X trending scan failed: {e}")

        # ── MERGE: deduplicate by symbol ───────────────────────────
        seen = {}
        for s in polygon_candidates:
            sym = s.get("symbol", "")
            if sym:
                seen[sym] = s

        for s in stocktwits_candidates:
            sym = s["symbol"]
            if sym in seen:
                seen[sym]["stocktwits_trending_score"] = s.get("stocktwits_trending_score", 0)
                seen[sym]["source"] = "both"
            else:
                seen[sym] = s

        for s in grok_x_candidates:
            sym = s["symbol"]
            if sym in seen:
                seen[sym]["grok_x_reason"] = s.get("grok_x_reason", "")
                seen[sym]["grok_x_sentiment"] = s.get("grok_x_sentiment", "mixed")
                seen[sym]["grok_x_buzz"] = s.get("grok_x_buzz", 0.5)
                if seen[sym]["source"] not in ("both",):
                    seen[sym]["source"] = seen[sym]["source"] + "+grok_x"
            else:
                seen[sym] = s

        all_symbols = list(seen.values())
        logger.info(f"Merged candidates: {len(all_symbols)} unique symbols")

        # ── ENRICH ALL with Polygon snapshot data ──────────────────
        # This fills in price, change_pct, volume for StockTwits-only tickers
        candidates = []
        for stock in all_symbols[:50]:  # enrich more candidates for diversity
            enriched = await self._enrich(stock)
            if enriched:
                candidates.append(enriched)

        # ── FILTER on enriched data ────────────────────────────────
        filtered = [c for c in candidates if self._passes_filter(c)]
        logger.info(f"After filter: {len(filtered)} candidates (from {len(candidates)} enriched)")

        # Derive strategy metadata before expensive technical calls.
        for c in filtered:
            c["strategy_tag"] = self._derive_strategy_tag(c)
            c["signal_sources"] = self._extract_signal_sources(c)

        # Hard disable losing strategies before bar fetches.
        disabled_strategies = self._load_disabled_strategies()
        active_candidates: List[Dict] = []
        disabled_count = 0
        for c in filtered:
            if c["strategy_tag"] in disabled_strategies:
                disabled_count += 1
                c["score"] = 0.0
                continue
            active_candidates.append(c)
        if disabled_count:
            logger.info(f"🚫 Strategy controls skipped {disabled_count} candidates this cycle")

        # Compute technicals only for post-filter survivors.
        technical_fetch_delay = max(
            0.15,
            float(getattr(settings, "SCANNER_TECHNICAL_FETCH_DELAY_SECONDS", 0.26) or 0.26),
        )
        for idx, c in enumerate(active_candidates):
            snapshot = {
                "day_high": c.get("day_high", c.get("high", 0)),
                "day_low": c.get("day_low", c.get("low", 0)),
            }
            technicals = await compute_technicals(
                symbol=c.get("symbol", ""),
                price=float(c.get("price", 0) or 0),
                polygon_client=self.polygon,
                snapshot=snapshot,
            )
            if technicals:
                c.update(technicals)
            # Keep minute-bar fetches under free-tier rate limits.
            if idx < (len(active_candidates) - 1):
                await asyncio.sleep(technical_fetch_delay)

        # ── SCORE & RANK (adaptive by regime + recent hit-rate) ───
        index_context = await self._load_index_context()
        regime = self._detect_market_regime(active_candidates, index_context=index_context)
        self._last_market_regime = regime
        performance = self._load_performance_snapshot()
        if index_context.get("count", 0) >= 2:
            logger.info(
                f"Market regime: {regime} (indices avg {index_context.get('avg_change_pct', 0.0):+.2f}%: "
                f"SPY {index_context.get('SPY', 0.0):+.2f}% "
                f"QQQ {index_context.get('QQQ', 0.0):+.2f}% "
                f"DIA {index_context.get('DIA', 0.0):+.2f}%)"
            )
        else:
            logger.info(f"Market regime: {regime}")
        for c in active_candidates:
            c["market_regime"] = regime
            c["index_avg_change_pct"] = round(float(index_context.get("avg_change_pct", 0.0) or 0.0), 2)
            c["strategy_win_rate_pct"] = round(
                self._estimate_strategy_hit_rate(c["strategy_tag"], performance) * 100, 1
            )
            c["source_win_rate_pct"] = round(
                self._estimate_source_hit_rate(c["signal_sources"], performance) * 100, 1
            )
            c["score_multiplier"] = round(self._performance_multiplier(c, performance), 3)
            c["score"] = self._calculate_score(c, regime=regime, performance=performance)
        active_candidates.sort(key=lambda x: x["score"], reverse=True)

        self._cache = active_candidates[:20]  # Keep more candidates for orchestrator diversity

        # Record today's big runners for tomorrow's fade watchlist
        if self.fade:
            try:
                self.fade.record_todays_runners(active_candidates)
            except Exception:
                pass

        logger.success(f"Scan complete: {len(self._cache)} ranked candidates ({regime})")
        
        # Log to dashboard activity feed
        try:
            from src.dashboard.dashboard import log_activity
            top = [f"{c['symbol']} ({c['change_pct']:+.1f}%)" for c in self._cache[:5]]
            log_activity("scan", f"Found {len(self._cache)} candidates: {', '.join(top)}")
        except Exception:
            pass
        for c in self._cache[:8]:
            src = c.get("source", "?")
            bull = c.get("st_bullish", 0)
            bear = c.get("st_bearish", 0)
            logger.info(
                f"  {c['symbol']:6s} ${c['price']:.2f}  chg={c['change_pct']:+.1f}%  "
                f"vol={c.get('volume_spike',0):.1f}x  "
                f"social={c.get('sentiment_score',0):+.2f}({bull}🟢/{bear}🔴)  "
                f"score={c['score']:.3f}({c.get('score_multiplier',1.0):.2f}x)  [{src}]"
            )

        return self._cache

    def get_cached_candidates(self) -> List[Dict]:
        return list(self._cache)

    def get_last_market_regime(self) -> str:
        return self._last_market_regime

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
                # ALWAYS prefer Alpaca change_pct — Polygon's todaysChangePerc is stale/inaccurate
                # especially during extended hours. Alpaca calculates from prev_close to latest trade.
                if alpaca_snap.get("change_pct", 0) != 0:
                    stock["change_pct"] = alpaca_snap.get("change_pct", 0)
                if stock.get("volume", 0) == 0:
                    stock["volume"] = alpaca_snap.get("volume", 0)
                if stock.get("prev_volume", 0) == 0:
                    stock["prev_volume"] = alpaca_snap.get("prev_volume", 0)
                stock["prev_close"] = alpaca_snap.get("prev_close", 0)
                stock["high"] = alpaca_snap.get("high", stock.get("high", 0))
                stock["low"] = alpaca_snap.get("low", stock.get("low", 0))
                stock["day_high"] = alpaca_snap.get("day_high", stock.get("day_high", 0))
                stock["day_low"] = alpaca_snap.get("day_low", stock.get("day_low", 0))

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
                    stock["high"] = snapshot.get("high", stock.get("high", 0))
                    stock["low"] = snapshot.get("low", stock.get("low", 0))
                    stock["day_high"] = snapshot.get("high", stock.get("day_high", 0))
                    stock["day_low"] = snapshot.get("low", stock.get("day_low", 0))

            # Skip if no price
            if stock.get("price", 0) <= 0:
                return None

            # Preserve earliest first-seen timestamp across scan cycles.
            first_seen = self._signal_first_seen.setdefault(symbol, stock.get("signal_timestamp", time.time()))
            stock["signal_timestamp"] = first_seen

            # Volume spike vs previous day volume (Alpaca) or 20-day avg (Polygon fallback)
            prev_vol = stock.get("prev_volume", 0)
            if prev_vol <= 0 and self.polygon:
                prev_vol = await asyncio.get_event_loop().run_in_executor(
                    None, self.polygon.get_avg_volume, symbol, 20
                )
            avg_vol = prev_vol if prev_vol > 0 else 1
            cur_vol = stock.get("volume", 0)
            vol_spike = cur_vol / avg_vol if avg_vol > 0 else 0
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
                lq = data.get('latestQuote', {})
                mb = data.get('minuteBar', {})
                db = data.get('dailyBar', {})
                pb = data.get('prevDailyBar', {})
                prev_close = pb.get('c', 0)
                cur_price = lt.get('p', 0)
                chg = ((cur_price - prev_close) / prev_close * 100) if prev_close > 0 else 0

                # Use today's daily bar volume if available, otherwise previous day
                # During pre-market, dailyBar is yesterday — use prevDailyBar for avg comparison
                today_vol = db.get('v', 0)
                prev_vol = pb.get('v', 0)
                avg_vol = prev_vol if prev_vol > 0 else 1

                return {
                    "price": cur_price,
                    "change_pct": round(chg, 2),
                    "volume": today_vol,
                    "prev_close": prev_close,
                    "prev_volume": prev_vol,
                    "high": db.get("h", 0),
                    "low": db.get("l", 0),
                    "day_high": db.get("h", 0),
                    "day_low": db.get("l", 0),
                    "bid": lq.get('bp', 0),
                    "ask": lq.get('ap', 0),
                    "spread_pct": round((lq.get('ap', 0) - lq.get('bp', 0)) / cur_price * 100, 2) if cur_price > 0 else 0,
                    "minute_vol": mb.get('v', 0),
                    "minute_vwap": mb.get('vw', 0),
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

    @staticmethod
    def _extract_signal_sources(candidate: Dict) -> List[str]:
        return extract_signal_sources(candidate)

    @staticmethod
    def _derive_strategy_tag(candidate: Dict) -> str:
        return derive_strategy_tag(candidate, candidate.get("side", "long"))

    @staticmethod
    def _clamp(value: float, low: float, high: float) -> float:
        return max(low, min(high, value))

    async def _load_index_context(self) -> Dict:
        """
        Load broad-market context from SPY/QQQ/DIA snapshots.
        Cached briefly to avoid repeated index requests within tight loops.
        """
        now = time.time()
        if self._index_context_cache and (now - self._index_context_cache_at) < self._index_context_cache_ttl:
            return self._index_context_cache

        context = {"SPY": 0.0, "QQQ": 0.0, "DIA": 0.0, "avg_change_pct": 0.0, "count": 0}
        changes = []
        try:
            if not self.polygon:
                return context

            loop = asyncio.get_event_loop()
            for sym in ("SPY", "QQQ", "DIA"):
                snap = await loop.run_in_executor(None, self.polygon.get_snapshot, sym)
                chg = float((snap or {}).get("change_pct", 0) or 0)
                if not chg and self.alpaca:
                    # Fall back to Alpaca snapshot when Polygon day-change is stale or absent.
                    alp = await loop.run_in_executor(None, self._get_alpaca_snapshot, sym)
                    chg = float((alp or {}).get("change_pct", 0) or 0)
                context[sym] = chg
                if chg:
                    changes.append(chg)

            if changes:
                context["count"] = len(changes)
                context["avg_change_pct"] = sum(changes) / len(changes)
        except Exception as e:
            logger.debug(f"Index context unavailable: {e}")

        self._index_context_cache = context
        self._index_context_cache_at = now
        return context

    @staticmethod
    def _derive_index_regime(index_context: Dict) -> str:
        count = int(index_context.get("count", 0) or 0)
        if count < 2:
            return "mixed"

        avg_change = float(index_context.get("avg_change_pct", 0.0) or 0.0)
        if avg_change >= 0.35:
            return "risk_on"
        if avg_change <= -0.35:
            return "risk_off"
        if abs(avg_change) <= 0.15:
            return "choppy"
        return "mixed"

    def _detect_market_regime(self, candidates: List[Dict], index_context: Optional[Dict] = None) -> str:
        """
        Lightweight regime inference from:
          1) candidate tape internals
          2) broad index direction (SPY/QQQ/DIA) when available.
        Returns: risk_on, risk_off, choppy, mixed.
        """
        if not candidates:
            return self._derive_index_regime(index_context or {})

        moves = [float(c.get("change_pct", 0) or 0) for c in candidates]
        avg_abs_move = sum(abs(m) for m in moves) / max(1, len(moves))
        advancers = sum(1 for m in moves if m > 0.5)
        decliners = sum(1 for m in moves if m < -0.5)
        breadth_total = max(1, advancers + decliners)
        breadth = advancers / breadth_total

        tape_regime = "mixed"
        if avg_abs_move >= 3.0 and breadth >= 0.62:
            tape_regime = "risk_on"
        elif avg_abs_move >= 3.0 and breadth <= 0.38:
            tape_regime = "risk_off"
        elif avg_abs_move <= 1.2:
            tape_regime = "choppy"

        index_regime = self._derive_index_regime(index_context or {})

        # Agreement between tape and index is high confidence.
        if tape_regime in ("risk_on", "risk_off") and tape_regime == index_regime:
            return tape_regime

        # If index strongly trends while tape is mixed/choppy, trust broad market.
        if tape_regime in ("mixed", "choppy") and index_regime in ("risk_on", "risk_off"):
            return index_regime

        # If tape looks directional but index is flat/choppy, soften to mixed.
        if tape_regime in ("risk_on", "risk_off") and index_regime == "choppy":
            return "mixed"

        # Conflicting directional signals: avoid overfitting to a biased candidate set.
        if tape_regime in ("risk_on", "risk_off") and index_regime in ("risk_on", "risk_off"):
            return "mixed"

        # Prefer whichever is more informative when one side is mixed.
        if tape_regime == "mixed":
            return index_regime
        if index_regime == "mixed":
            return tape_regime

        return tape_regime

    def _load_performance_snapshot(self) -> Dict:
        """
        Pull recent realized performance attribution from trade history.
        Cached to avoid file reads every scan.
        """
        now = time.time()
        if self._performance_cache and (now - self._performance_cache_at) < self._performance_cache_ttl:
            return self._performance_cache

        snapshot = {"by_strategy": {}, "by_source": {}}
        try:
            from src.ai import trade_history

            analytics = trade_history.get_analytics() or {}
            by_strategy = analytics.get("by_strategy_tag", {}) or {}
            by_source = analytics.get("by_signal_source", {}) or {}

            for tag, bucket in by_strategy.items():
                snapshot["by_strategy"][tag] = self._normalize_perf_bucket(bucket)
            for src, bucket in by_source.items():
                snapshot["by_source"][src] = self._normalize_perf_bucket(bucket)
        except Exception as e:
            logger.debug(f"Scanner performance snapshot unavailable: {e}")

        self._performance_cache = snapshot
        self._performance_cache_at = now
        return snapshot

    def _load_disabled_strategies(self) -> Set[str]:
        """Load persistent strategy controls with a short in-memory cache."""
        now = time.time()
        if (now - self._disabled_strategies_cache_at) < self._disabled_strategies_cache_ttl:
            return set(self._disabled_strategies_cache)
        try:
            controls = strategy_controls.load_controls()
            disabled = strategy_controls.get_effective_disabled(controls)
        except Exception:
            disabled = set()
        self._disabled_strategies_cache = set(disabled)
        self._disabled_strategies_cache_at = now
        return set(disabled)

    def _normalize_perf_bucket(self, bucket: Dict) -> Dict:
        trades = int(bucket.get("trades", 0) or 0)
        win_rate_raw = float(bucket.get("win_rate", bucket.get("win_rate_pct", 0)) or 0)
        win_rate = win_rate_raw / 100.0 if win_rate_raw > 1 else win_rate_raw
        pnl = float(bucket.get("pnl", 0) or 0)
        pnl_per_trade = (pnl / trades) if trades > 0 else 0.0
        pnl_score = self._clamp(0.5 + (pnl_per_trade / 25.0), 0.0, 1.0)
        blended_score = self._clamp((win_rate * 0.7) + (pnl_score * 0.3), 0.0, 1.0)
        return {"trades": trades, "win_rate": self._clamp(win_rate, 0.0, 1.0), "score": blended_score}

    def _estimate_strategy_hit_rate(self, strategy_tag: str, performance: Dict) -> float:
        if not performance:
            return 0.5
        bucket = (performance.get("by_strategy", {}) or {}).get(strategy_tag)
        if not bucket:
            return 0.5
        if bucket.get("trades", 0) < 5:
            return 0.5
        return float(bucket.get("win_rate", 0.5))

    def _estimate_source_hit_rate(self, signal_sources: List[str], performance: Dict) -> float:
        if not performance:
            return 0.5
        by_source = performance.get("by_source", {}) or {}
        scores = []
        for src in signal_sources or []:
            bucket = by_source.get(src)
            if bucket and bucket.get("trades", 0) >= 5:
                scores.append(float(bucket.get("win_rate", 0.5)))
        if not scores:
            return 0.5
        return sum(scores) / len(scores)

    def _performance_multiplier(self, c: Dict, performance: Dict) -> float:
        """
        Convert realized strategy/source performance into a bounded multiplier.
        Neutral when history is sparse.
        """
        if not performance:
            return 1.0

        strategy_tag = c.get("strategy_tag", "unknown")
        signal_sources = c.get("signal_sources", [])
        by_strategy = performance.get("by_strategy", {}) or {}
        by_source = performance.get("by_source", {}) or {}

        multipliers = []

        strat_bucket = by_strategy.get(strategy_tag)
        if strat_bucket and strat_bucket.get("trades", 0) >= 8:
            strat_score = float(strat_bucket.get("score", 0.5))
            multipliers.append(0.85 + (strat_score * 0.40))  # 0.85x..1.25x

        source_scores = []
        for src in signal_sources:
            bucket = by_source.get(src)
            if bucket and bucket.get("trades", 0) >= 8:
                source_scores.append(float(bucket.get("score", 0.5)))
        if source_scores:
            avg_source_score = sum(source_scores) / len(source_scores)
            multipliers.append(0.85 + (avg_source_score * 0.40))

        if not multipliers:
            return 1.0
        avg_mult = sum(multipliers) / len(multipliers)
        return self._clamp(avg_mult, 0.85, 1.25)

    def _regime_weights(self, regime: str, side: str) -> Dict[str, float]:
        # Base profile
        weights = {
            "volume": 0.15,
            "momentum": 0.15,
            "sentiment": 0.25,
            "trending": 0.15,
            "pharma": 0.20,
            "news": 0.10,
        }

        if regime == "risk_on":
            weights = {
                "volume": 0.24,
                "momentum": 0.24,
                "sentiment": 0.20,
                "trending": 0.16,
                "pharma": 0.10,
                "news": 0.06,
            }
            if side == "short":
                weights["momentum"] = 0.18
                weights["sentiment"] = 0.26
                weights["pharma"] = 0.14

        elif regime == "risk_off":
            if side == "short":
                weights = {
                    "volume": 0.22,
                    "momentum": 0.22,
                    "sentiment": 0.24,
                    "trending": 0.14,
                    "pharma": 0.12,
                    "news": 0.06,
                }
            else:
                weights = {
                    "volume": 0.14,
                    "momentum": 0.11,
                    "sentiment": 0.30,
                    "trending": 0.16,
                    "pharma": 0.20,
                    "news": 0.09,
                }

        elif regime == "choppy":
            weights = {
                "volume": 0.12,
                "momentum": 0.10,
                "sentiment": 0.28,
                "trending": 0.18,
                "pharma": 0.22,
                "news": 0.10,
            }

        total = sum(weights.values()) or 1.0
        return {k: v / total for k, v in weights.items()}

    def _calculate_score(self, c: Dict, regime: str = "mixed", performance: Optional[Dict] = None) -> float:
        """
        Composite score from:
          1) feature scores (volume/momentum/sentiment/trending/pharma/news),
          2) regime-specific dynamic weights,
          3) bounded multiplier from recent realized hit-rate attribution.
        """
        vol_score = min(c.get("volume_spike", 0) / 5.0, 1.0)
        mom_score = min(abs(c.get("change_pct", 0)) / 10.0, 1.0)
        sent_raw = c.get("sentiment_score", 0)
        sent_score = (sent_raw + 1.0) / 2.0  # map -1..1 → 0..1
        twits_score = min(c.get("stocktwits_trending_score", 0) / 30.0, 1.0)
        news_score = 1.0 if c.get("news_headlines") else 0.3

        # Pharma catalyst bonus — upcoming FDA decisions are HUGE signals
        pharma_score = c.get("pharma_score", 0)

        side = str(c.get("side", "long")).lower()
        weights = self._regime_weights(regime, side)
        base_score = (
            vol_score * weights["volume"]
            + mom_score * weights["momentum"]
            + sent_score * weights["sentiment"]
            + twits_score * weights["trending"]
            + pharma_score * weights["pharma"]
            + news_score * weights["news"]
        )

        multiplier = self._performance_multiplier(c, performance or {})
        return base_score * multiplier
