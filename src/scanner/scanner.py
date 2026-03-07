"""
Scanner - Find high-momentum stocks from MULTIPLE sources.
Sources: Alpaca movers + Polygon gainers + StockTwits trending + enrichment via snapshots.
"""

import asyncio
import time
from datetime import datetime
from typing import Dict, List, Optional, Set
from loguru import logger

import httpx

from config import settings
from src.data import strategy_controls
from src.data.signal_attribution import extract_signal_sources, derive_strategy_tag
from src.data.technicals import compute_technicals
from src.signals.live_indicators import compute_live_signals, get_consensus


class Scanner:
    """
    Multi-source stock scanner:
      1. Alpaca movers (fast, free with account)
      2. Polygon gainers (price movers with volume)
      3. StockTwits trending (social momentum)
      4. Merge + enrich ALL with snapshot data
      5. Filter on enriched data (not raw)
      6. Score & rank
    """

    def __init__(self, polygon_client=None, sentiment_analyzer=None, stocktwits_client=None, alpaca_client=None, pharma_scanner=None, fade_scanner=None, grok_x_trending=None, unusual_whales_client=None, human_intel_store=None, watchlist_provider=None, copy_trader_monitor=None):
        self.polygon = polygon_client
        self.sentiment = sentiment_analyzer
        self.stocktwits = stocktwits_client
        self.alpaca = alpaca_client
        self.pharma = pharma_scanner
        self.fade = fade_scanner
        self.grok_x = grok_x_trending
        self.unusual_whales = unusual_whales_client
        self.human_intel = human_intel_store
        self.watchlist = watchlist_provider
        self.copy_trader = copy_trader_monitor

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
        self._last_runners_record_date = ""
        self._last_market_regime = "mixed"
        logger.info("Scanner initialized")

    async def scan(self) -> List[Dict]:
        """Run a full scan cycle. Returns ranked candidate list."""
        logger.info("🔍 Running stock scan...")
        cutoff = time.time() - self._signal_first_seen_ttl
        self._signal_first_seen = {s: ts for s, ts in self._signal_first_seen.items() if ts >= cutoff}

        # ── SOURCE 0: Alpaca movers ────────────────────────────────
        alpaca_candidates = []
        if self.alpaca:
            try:
                raw = await asyncio.get_event_loop().run_in_executor(None, self.alpaca.get_movers, 20, "stocks")
                for s in raw or []:
                    s.setdefault("source", "alpaca_movers")
                alpaca_candidates = list(raw or [])
                logger.info(f"Alpaca movers: {len(alpaca_candidates)}")
            except Exception as e:
                logger.debug(f"Alpaca movers failed: {e}")

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
                            "run_volume": sig.get("run_volume", 0),
                            "run_close": sig.get("run_close", 0),
                            "price_change_from_run": sig.get("price_change_from_run", 0),
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

        # ── SOURCE 6: Tier-1 copy trader signals ─────────────────
        copy_trader_candidates = []
        if self.copy_trader and getattr(self.copy_trader, "is_configured", lambda: False)():
            try:
                copy_trader_candidates = list(self.copy_trader.get_candidate_signals() or [])
                if copy_trader_candidates:
                    logger.info(f"📣 Copy trader source: {len(copy_trader_candidates)} candidates")
            except Exception as e:
                logger.warning(f"Copy trader source failed: {e}")

        # ── SOURCE 7: Overnight watchlist (curated for the next day) ───────
        watchlist_candidates = []
        if self.watchlist:
            try:
                for item in self.watchlist.get_all()[:25]:
                    symbol = str(item.get("ticker", "")).upper().strip()
                    if not symbol:
                        continue
                    watchlist_candidates.append({
                        "symbol": symbol,
                        "price": 0,
                        "change_pct": 0,
                        "volume": 0,
                        "source": str(item.get("sources", "watchlist") or "watchlist"),
                        "side": item.get("side", "long"),
                        "watchlist_reason": item.get("reason", ""),
                        "watchlist_conviction": float(item.get("conviction", 0) or 0),
                        "priority": 1,
                    })
                if watchlist_candidates:
                    logger.info(f"📋 Watchlist source: {len(watchlist_candidates)} curated tickers")
            except Exception as e:
                logger.warning(f"Watchlist source failed: {e}")

        # ── SOURCE 8: Human intel / operator context ───────────────
        human_candidates = []
        if self.human_intel:
            try:
                for intel in self.human_intel.get_watchlist_candidates(limit=15):
                    symbol = str(intel.get("ticker", "")).upper().strip()
                    if not symbol or symbol == "MARKET":
                        continue
                    human_candidates.append({
                        "symbol": symbol,
                        "price": 0,
                        "change_pct": 0,
                        "volume": 0,
                        "source": "human_intel",
                        "side": "short" if intel.get("bias") == "bearish" else "long",
                        "human_intel": intel.get("summary", ""),
                        "human_intel_bias": intel.get("bias", "neutral"),
                        "human_intel_confidence": intel.get("avg_confidence", 0.5),
                        "human_intel_score_adjustment": intel.get("score_adjustment", 0.0),
                        "priority": 1,
                    })
                if human_candidates:
                    logger.info(f"🧠 Human intel: {len(human_candidates)} guided tickers")
            except Exception as e:
                logger.warning(f"Human intel source failed: {e}")

        # ── MERGE: deduplicate by symbol ───────────────────────────
        seen = {}
        for s in alpaca_candidates:
            sym = s.get("symbol", "")
            if sym:
                seen[sym] = s

        for s in polygon_candidates:
            sym = s.get("symbol", "")
            if sym:
                if sym in seen:
                    self._merge_candidate(seen[sym], s)
                else:
                    seen[sym] = s

        for s in stocktwits_candidates:
            sym = s["symbol"]
            if sym in seen:
                self._merge_candidate(seen[sym], s)
            else:
                seen[sym] = s

        for s in grok_x_candidates:
            sym = s["symbol"]
            if sym in seen:
                self._merge_candidate(seen[sym], s)
            else:
                seen[sym] = s

        for s in copy_trader_candidates:
            sym = s["symbol"]
            if sym in seen:
                self._merge_candidate(seen[sym], s)
            else:
                seen[sym] = s

        for s in watchlist_candidates:
            sym = s["symbol"]
            if sym in seen:
                self._merge_candidate(seen[sym], s)
            else:
                seen[sym] = s

        for s in human_candidates:
            sym = s["symbol"]
            if sym in seen:
                self._merge_candidate(seen[sym], s)
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
            # Compute validated indicator signals from backtest winners
            if self.polygon:
                try:
                    live_sigs = await compute_live_signals(
                        symbol=c.get("symbol", ""),
                        price=float(c.get("price", 0) or 0),
                        polygon_client=self.polygon,
                    )
                    if live_sigs:
                        c["validated_indicator_signals"] = live_sigs
                        consensus = get_consensus(live_sigs)
                        c["indicator_consensus_bias"] = consensus["bias"]
                        c["indicator_consensus_strength"] = consensus["strength"]
                        c["indicator_consensus_agreement"] = consensus["agreement"]
                except Exception as e:
                    logger.debug(f"Live indicator signals failed for {c.get('symbol', '?')}: {e}")
            self._apply_strategy_context(c)
            # Keep minute-bar fetches under free-tier rate limits.
            if idx < (len(active_candidates) - 1):
                await asyncio.sleep(technical_fetch_delay)

        market_tide = {}
        await self._apply_unusual_whales_enrichment(active_candidates)
        self._apply_human_intel_enrichment(active_candidates)
        if self.unusual_whales and getattr(self.unusual_whales, "is_configured", lambda: False)():
            try:
                market_tide = await asyncio.get_event_loop().run_in_executor(
                    None, self.unusual_whales.get_market_tide
                )
                market_tide_summary = (
                    f"{market_tide.get('bias', 'mixed')} p/c {float(market_tide.get('put_call_ratio', 0) or 0):.2f}; "
                    f"puts ${float(market_tide.get('net_put_premium', 0) or 0):,.0f}; "
                    f"calls ${float(market_tide.get('net_call_premium', 0) or 0):,.0f}"
                )
                for candidate in active_candidates:
                    candidate["market_tide"] = market_tide_summary
                    candidate["market_tide_bias"] = market_tide.get("bias", "mixed")
            except Exception as e:
                logger.debug(f"Market tide enrichment unavailable: {e}")

        # ── SCORE & RANK (adaptive by regime + recent hit-rate) ───
        index_context = await self._load_index_context()
        regime = self._detect_market_regime(active_candidates, index_context=index_context)
        regime = self._apply_market_tide_bias(regime, market_tide)
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
        self._maybe_record_todays_runners(active_candidates)

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

        # For social/manual-context tickers, be more lenient on momentum
        # (they're trending for a reason — social momentum IS momentum)
        source_parts = set(self._source_parts(s.get("source", "")))
        is_social = s.get("source") in ("stocktwits", "both") or "stocktwits" in source_parts
        is_human = "human_intel" in source_parts or bool(s.get("human_intel"))
        is_copy_trader = "copy_trader" in source_parts or bool(s.get("copy_trader_context"))
        is_watchlist = bool(s.get("watchlist_reason")) or "watchlist" in source_parts or bool(s.get("watchlist_conviction"))
        min_mom = 0.0 if (is_human or is_watchlist or is_copy_trader) else (0.5 if is_social else self.min_momentum)

        change = abs(s.get("change_pct", 0))
        if change < min_mom:
            return False

        # Volume check — require some volume but lower bar for social tickers
        min_vol = 100_000 if (is_human or is_watchlist or is_copy_trader) else (200_000 if is_social else self.min_volume)
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
    def _source_parts(source: str) -> List[str]:
        raw = str(source or "").strip().lower()
        if not raw:
            return []
        if raw == "both":
            return ["polygon", "stocktwits"]
        return [part.strip() for part in raw.split("+") if part.strip()]

    def _merge_sources(self, existing_source: str, incoming_source: str) -> str:
        merged: List[str] = []
        for raw in (existing_source, incoming_source):
            for part in self._source_parts(raw):
                if part not in merged:
                    merged.append(part)
        if merged == ["polygon", "stocktwits"]:
            return "both"
        return "+".join(merged)

    def _merge_candidate(self, existing: Dict, incoming: Dict):
        existing["source"] = self._merge_sources(existing.get("source", ""), incoming.get("source", ""))

        overlay_keys = {
            "stocktwits_trending_score",
            "pharma_signal",
            "pharma_score",
            "pharma_drug",
            "pharma_days_until",
            "pharma_catalyst_type",
            "fade_signal",
            "fade_score",
            "fade_run_pct",
            "run_volume",
            "run_close",
            "price_change_from_run",
            "current_price",
            "priority",
            "grok_x_reason",
            "grok_x_sentiment",
            "grok_x_buzz",
            "sentiment_score",
            "human_intel",
            "human_intel_bias",
            "human_intel_confidence",
            "human_intel_score_adjustment",
            "watchlist_reason",
            "watchlist_conviction",
            "copy_trader_signal_count",
            "copy_trader_handles",
            "copy_trader_context",
            "copy_trader_convergence",
            "copy_trader_weight",
            "copy_trader_size_multiplier",
            "copy_trader_score_adjustment",
        }

        for key, value in incoming.items():
            if key == "source":
                continue
            if key == "side":
                if str(value or "").strip().lower() == "short" or not existing.get("side"):
                    existing["side"] = value
                continue
            if key in overlay_keys:
                if value not in (None, "", [], {}):
                    existing[key] = value
                continue
            if key in ("price", "change_pct", "volume"):
                try:
                    existing_value = float(existing.get(key, 0) or 0)
                    incoming_value = float(value or 0)
                except Exception:
                    existing_value = 0.0
                    incoming_value = 0.0
                if existing_value <= 0 and incoming_value != 0:
                    existing[key] = value
                continue
            if key not in existing or existing.get(key) in (None, "", [], {}):
                existing[key] = value

    def _apply_strategy_context(self, candidate: Dict):
        if not candidate.get("fade_signal"):
            return

        candidate["side"] = "short"
        run_volume = float(candidate.get("run_volume", 0) or 0)
        day2_volume = float(candidate.get("volume", 0) or 0)
        rsi = float(candidate.get("rsi", 0) or 0)
        volume_declining = run_volume > 0 and day2_volume > 0 and day2_volume < run_volume
        candidate["fade_volume_declining"] = volume_declining
        candidate["fade_high_conviction"] = bool(rsi >= 80 and volume_declining)
        if candidate["fade_high_conviction"]:
            candidate["fade_score"] = max(float(candidate.get("fade_score", 0) or 0), 0.9)
        candidate["strategy_tag"] = self._derive_strategy_tag(candidate)
        candidate["signal_sources"] = self._extract_signal_sources(candidate)

    def _maybe_record_todays_runners(self, candidates: List[Dict]):
        if not self.fade:
            return
        try:
            import zoneinfo

            now_et = datetime.now(zoneinfo.ZoneInfo("US/Eastern"))
        except Exception:
            from pytz import timezone as tz

            now_et = datetime.now(tz("US/Eastern"))

        if now_et.weekday() >= 5:
            return
        if (now_et.hour, now_et.minute) < (15, 55):
            return

        today = now_et.strftime("%Y-%m-%d")
        if self._last_runners_record_date == today:
            return

        try:
            self.fade.record_todays_runners(candidates)
            self._last_runners_record_date = today
        except Exception:
            pass

    async def _apply_unusual_whales_enrichment(self, candidates: List[Dict]):
        if not candidates or not self.unusual_whales or not getattr(self.unusual_whales, "is_configured", lambda: False)():
            return
        try:
            flow_alerts = await asyncio.get_event_loop().run_in_executor(
                None, self.unusual_whales.get_flow_alerts, 100_000, None, 150
            )
            dark_pool = await asyncio.get_event_loop().run_in_executor(
                None, self.unusual_whales.get_dark_pool, None, 150
            )
        except Exception as e:
            logger.debug(f"Unusual Whales enrichment failed: {e}")
            return

        flow_by_ticker: Dict[str, List[Dict]] = {}
        for alert in flow_alerts or []:
            ticker = str(alert.get("ticker", "")).upper()
            if ticker:
                flow_by_ticker.setdefault(ticker, []).append(alert)

        dark_by_ticker: Dict[str, List[Dict]] = {}
        for trade in dark_pool or []:
            ticker = str(trade.get("ticker", "")).upper()
            if ticker:
                dark_by_ticker.setdefault(ticker, []).append(trade)

        for candidate in candidates[:20]:
            symbol = str(candidate.get("symbol", "")).upper()
            side = str(candidate.get("side", "long")).lower()
            alerts = flow_by_ticker.get(symbol, [])
            dark_trades = dark_by_ticker.get(symbol, [])
            score_adj = 0.0

            if alerts:
                bullish_premium = sum(a.get("premium", 0.0) for a in alerts if a.get("sentiment") == "bullish")
                bearish_premium = sum(a.get("premium", 0.0) for a in alerts if a.get("sentiment") == "bearish")
                dominant = "bullish" if bullish_premium > bearish_premium else "bearish" if bearish_premium > bullish_premium else "neutral"
                if dominant != "neutral":
                    if (side == "short" and dominant == "bearish") or (side != "short" and dominant == "bullish"):
                        score_adj += 0.15
                    else:
                        score_adj -= 0.20
                candidate["unusual_options"] = (
                    f"{len(alerts)} whale flow alerts; bull ${bullish_premium:,.0f}; "
                    f"bear ${bearish_premium:,.0f}; bias {dominant}"
                )
                candidate["uw_flow_sentiment"] = dominant

            if dark_trades:
                bullish_dark = sum(t.get("premium", 0.0) for t in dark_trades if t.get("sentiment") == "bullish")
                bearish_dark = sum(t.get("premium", 0.0) for t in dark_trades if t.get("sentiment") == "bearish")
                dark_bias = "bullish" if bullish_dark > bearish_dark else "bearish" if bearish_dark > bullish_dark else "neutral"
                if dark_bias == "bullish" and side != "short":
                    score_adj += 0.10
                elif dark_bias == "bearish" and side == "short":
                    score_adj += 0.10
                candidate["dark_pool"] = (
                    f"{len(dark_trades)} dark pool prints; bull ${bullish_dark:,.0f}; "
                    f"bear ${bearish_dark:,.0f}; bias {dark_bias}"
                )
                candidate["uw_dark_pool_bias"] = dark_bias

            candidate["uw_score_adjustment"] = round(score_adj, 3)

        # Gamma is per-symbol; keep it to the top few candidates to stay under rate limits.
        for candidate in candidates[:10]:
            symbol = str(candidate.get("symbol", "")).upper()
            if not symbol:
                continue
            try:
                gamma = await asyncio.get_event_loop().run_in_executor(
                    None, self.unusual_whales.get_gamma_exposure, symbol
                )
            except Exception as e:
                logger.debug(f"Gamma enrichment unavailable for {symbol}: {e}")
                continue

            if not gamma.get("levels"):
                continue
            candidate["gamma_support"] = gamma.get("support_strikes", [])
            candidate["gamma_resistance"] = gamma.get("resistance_strikes", [])
            candidate["gamma_max_strike"] = gamma.get("max_gamma_strike", 0)
            candidate["gamma_levels"] = gamma.get("levels", [])

    def _apply_human_intel_enrichment(self, candidates: List[Dict]):
        if not candidates or not self.human_intel:
            return
        for candidate in candidates[:20]:
            symbol = str(candidate.get("symbol", "")).upper()
            if not symbol:
                continue
            if candidate.get("human_intel"):
                continue
            intel = self.human_intel.summarize_for_symbol(symbol)
            if not intel.get("count"):
                continue
            candidate["human_intel"] = intel.get("summary", "")
            candidate["human_intel_bias"] = intel.get("bias", "neutral")
            candidate["human_intel_confidence"] = intel.get("avg_confidence", 0.0)
            candidate["human_intel_score_adjustment"] = intel.get("score_adjustment", 0.0)

    @staticmethod
    def _apply_market_tide_bias(regime: str, market_tide: Dict) -> str:
        bias = str((market_tide or {}).get("bias", "") or "").strip().lower()
        if bias == "risk_off" and regime in ("mixed", "choppy"):
            return "risk_off"
        if bias == "risk_on" and regime == "mixed":
            return "risk_on"
        return regime

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
        side = str(c.get("side", "long")).lower()
        if side == "short":
            sent_raw = -float(sent_raw or 0)
        sent_score = (sent_raw + 1.0) / 2.0  # map -1..1 → 0..1
        twits_score = min(c.get("stocktwits_trending_score", 0) / 30.0, 1.0)
        news_score = 1.0 if c.get("news_headlines") else 0.3

        # Pharma catalyst bonus — upcoming FDA decisions are HUGE signals
        pharma_score = c.get("pharma_score", 0)

        weights = self._regime_weights(regime, side)
        base_score = (
            vol_score * weights["volume"]
            + mom_score * weights["momentum"]
            + sent_score * weights["sentiment"]
            + twits_score * weights["trending"]
            + pharma_score * weights["pharma"]
            + news_score * weights["news"]
        )
        if c.get("fade_signal"):
            base_score += min(float(c.get("fade_score", 0) or 0), 1.0) * 0.12
            if c.get("fade_high_conviction"):
                base_score += 0.08
        base_score += float(c.get("uw_score_adjustment", 0.0) or 0.0)
        human_adj = float(c.get("human_intel_score_adjustment", 0.0) or 0.0)
        if side == "short" and human_adj:
            human_adj = -human_adj
        base_score += human_adj
        base_score += min(float(c.get("watchlist_conviction", 0) or 0), 1.0) * 0.15
        base_score += float(c.get("copy_trader_score_adjustment", 0.0) or 0.0)
        base_score += max(0.0, min(0.06, (float(c.get("copy_trader_weight", 1.0) or 1.0) - 1.0) * 0.15))

        multiplier = self._performance_multiplier(c, performance or {})
        return base_score * multiplier
