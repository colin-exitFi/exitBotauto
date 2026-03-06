#!/usr/bin/env python3
"""
Velox - Autonomous Velocity Trading Engine
Main loop: scan → filter → enter → monitor → exit
AI layers: observe → advise → tune → manage positions
"""

import asyncio
import json
import signal
import sys
import time
from pathlib import Path

from loguru import logger
from dotenv import load_dotenv

from config import settings
from src.broker.alpaca_client import AlpacaClient
from src.data.polygon_client import PolygonClient
from src.scanner.scanner import Scanner
from src.sentiment.sentiment_analyzer import SentimentAnalyzer
from src.signals.stocktwits import StockTwitsClient
from src.signals.twitter import TwitterSentimentClient
from src.signals.pharma_catalyst import PharmaCatalystScanner
from src.signals.fade_runner import FadeRunnerScanner
from src.signals.watchlist import DynamicWatchlist
from src.signals.edgar import EdgarScanner
from src.signals.earnings import EarningsScanner
from src.signals.unusual_options import UnusualOptionsScanner
from src.signals.congress import CongressScanner
from src.signals.short_interest import ShortInterestScanner
from src.signals.sector_rotation import SectorRotationModel
from src.streams.market_stream import MarketStream
from src.streams.trade_stream import TradeStream
from src.dashboard.dashboard import log_activity
from src import persistence
from src.entry.entry_manager import EntryManager
from src.exit.exit_manager import ExitManager
from src.exit.extended_hours_guard import ExtendedHoursGuard
from src.risk.risk_manager import RiskManager
from src.ai.observer import Observer
from src.ai.advisor import Advisor
from src.ai.tuner import Tuner
from src.ai.game_film import GameFilm
from src.ai.position_manager import PositionManager
from src.ai import trade_history
from src.ai.consensus import ConsensusEngine
from src.agents.orchestrator import Orchestrator
from src.dashboard.dashboard import start_dashboard
from src.data.trade_schema import normalize_trade_record
from src.data.signal_attribution import extract_signal_sources, derive_strategy_tag


class TradingBot:
    """Main trading bot orchestrator."""

    def __init__(self):
        self.running = False
        self.paused = False
        self.start_time = time.time()
        self._breakout_queue = asyncio.Queue(maxsize=20)

        # Components (initialized in initialize())
        self.alpaca_client: AlpacaClient = None
        self.polygon_client: PolygonClient = None
        self.scanner: Scanner = None
        self.sentiment_analyzer: SentimentAnalyzer = None
        self.stocktwits_client: StockTwitsClient = None
        self.twitter_client: TwitterSentimentClient = None
        self.entry_manager: EntryManager = None
        self.exit_manager: ExitManager = None
        self.risk_manager: RiskManager = None

        # AI layers
        self.observer: Observer = None
        self.advisor: Advisor = None
        self.tuner: Tuner = None
        self.game_film: GameFilm = None
        self.position_manager: PositionManager = None
        self.ai_layers: dict = {}  # shared state for dashboard

        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

    def _signal_handler(self, sig, frame):
        logger.warning("Shutdown signal received")
        self.stop()

    async def initialize(self):
        """Initialize all components."""
        logger.info("⚡ Initializing Velox...")

        # Alpaca broker
        self.alpaca_client = AlpacaClient()
        if not self.alpaca_client.initialize():
            logger.warning("Alpaca init failed — running in monitor-only mode")
            self.alpaca_client = None

        # Polygon market data
        self.polygon_client = PolygonClient()
        if not self.polygon_client.initialize():
            logger.error("Polygon init failed — cannot scan. Exiting.")
            sys.exit(1)

        # Wire Alpaca as primary data source for Polygon failover
        if self.alpaca_client:
            self.polygon_client.set_alpaca_client(self.alpaca_client)

        # Signal sources
        self.stocktwits_client = StockTwitsClient()
        self.twitter_client = TwitterSentimentClient()

        # Risk manager
        self.risk_manager = RiskManager()

        # Sync equity from Alpaca on startup
        if self.alpaca_client:
            try:
                acct = self.alpaca_client.get_account()
                self.risk_manager.update_equity(acct.get("equity", settings.TOTAL_CAPITAL))
            except Exception:
                pass

        # Sentiment analyzer
        self.sentiment_analyzer = SentimentAnalyzer()

        # Pharma catalyst scanner (FDA PDUFA dates)
        self.pharma_scanner = PharmaCatalystScanner()

        # Fade runner scanner (short yesterday's big runners)
        self.fade_scanner = FadeRunnerScanner(polygon_client=self.polygon_client)

        # EDGAR SEC filing scanner (free, no auth)
        self.edgar_scanner = EdgarScanner()

        # Earnings calendar scanner
        self.earnings_scanner = EarningsScanner()

        # Unusual options activity scanner
        self.options_scanner = UnusualOptionsScanner()

        # Congressional trading scanner
        self.congress_scanner = CongressScanner()

        # Short interest / squeeze detector
        self.short_scanner = ShortInterestScanner()

        # Sector rotation model
        self.sector_model = SectorRotationModel(polygon_client=self.polygon_client)

        # Real-time WebSocket streams
        self.market_stream = MarketStream()
        self.trade_stream = TradeStream()

        # Dynamic watchlist (built overnight, used during trading)
        self.watchlist = DynamicWatchlist()

        # Grok X/Twitter trending scanner
        from src.signals.grok_x_trending import GrokXTrending
        self.grok_x_trending = GrokXTrending()

        # Scanner (with StockTwits + Pharma + Fade + Grok X)
        self.scanner = Scanner(
            polygon_client=self.polygon_client,
            sentiment_analyzer=self.sentiment_analyzer,
            stocktwits_client=self.stocktwits_client,
            alpaca_client=self.alpaca_client,
            pharma_scanner=self.pharma_scanner,
            fade_scanner=self.fade_scanner,
            grok_x_trending=self.grok_x_trending,
        )

        # Entry manager
        self.entry_manager = EntryManager(
            alpaca_client=self.alpaca_client,
            polygon_client=self.polygon_client,
            risk_manager=self.risk_manager,
        )

        # Exit manager
        self.exit_manager = ExitManager(
            alpaca_client=self.alpaca_client,
            polygon_client=self.polygon_client,
            risk_manager=self.risk_manager,
            entry_manager=self.entry_manager,
        )

        # Extended hours guard (dynamic limit sells when trailing stops don't work)
        self.extended_guard = ExtendedHoursGuard(
            alpaca_client=self.alpaca_client,
            polygon_client=self.polygon_client,
        )

        # Consensus engine (legacy — kept for fallback/dashboard compat)
        self.consensus_engine = ConsensusEngine()

        # Options engine
        from src.options.options_engine import OptionsEngine
        self.options_engine = None
        options_enabled = getattr(settings, "OPTIONS_ENABLED", False)
        if options_enabled and self.alpaca_client:
            self.options_engine = OptionsEngine(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                base_url=getattr(settings, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            )
            logger.info("🎯 Options trading ENABLED")
        else:
            logger.info("Options trading disabled (set OPTIONS_ENABLED=true to enable)")

        # Specialized Agent Orchestrator (new architecture)
        self.orchestrator = Orchestrator(
            broker=self.alpaca_client,
            entry_manager=self.entry_manager,
            risk_manager=self.risk_manager,
        )

        # AI layers
        self.observer = Observer()
        self.advisor = Advisor()
        self.tuner = Tuner()
        self.game_film = GameFilm()
        self.position_manager = PositionManager()
        self.ai_layers = {
            "last_observation": None,
            "last_advice": None,
            "last_tuner_changes": None,
            "last_game_film_summary": None,
            "last_position_manager": None,
            "last_consensus": None,
        }

        # ── Restore persisted state ─────────────────────────────────
        # Positions: merge disk state with Alpaca reality
        saved_positions = persistence.load_positions()
        if saved_positions:
            for sym, pos in saved_positions.items():
                if sym not in self.entry_manager.positions:
                    self.entry_manager.positions[sym] = pos
            logger.info(f"📦 Merged {len(saved_positions)} persisted positions")

        # P&L state
        self.pnl_state = persistence.load_pnl_state()
        from datetime import datetime
        today = datetime.now().strftime("%Y-%m-%d")
        if self.pnl_state.get("today_date") != today:
            self.pnl_state["today_realized_pnl"] = 0.0
            self.pnl_state["today_date"] = today

        # AI layer state
        saved_ai = persistence.load_ai_state()
        if saved_ai:
            self.ai_layers.update(saved_ai)

        # Dashboard
        start_dashboard(bot=self)

        # ── WebSocket streams ─────────────────────────────────────
        # Market data stream: real-time prices + breakout detection
        self.market_stream.set_breakout_callback(self._on_breakout_detected)
        await self.market_stream.start()

        # Trade updates stream: instant order fill detection
        self.trade_stream.set_stop_callback(self._on_trailing_stop_filled)
        await self.trade_stream.start()

        # Fetch initial earnings calendar
        try:
            earnings = await self.earnings_scanner.refresh()
            today_earnings = await self.earnings_scanner.get_today()
            if today_earnings:
                tickers = [e["ticker"] for e in today_earnings[:10]]
                logger.info(f"📅 Today's earnings: {', '.join(tickers)}")
                log_activity("research", f"📅 Earnings today: {', '.join(tickers)}")
        except Exception as e:
            logger.debug(f"Earnings calendar fetch failed: {e}")

        logger.success("✅ All components initialized")

    async def run(self):
        """Main trading loop with AI layers running as concurrent tasks."""
        self.running = True
        self.start_time = time.time()
        logger.info("🚀 Velox LIVE")

        # Launch AI layers as background tasks
        ai_task = asyncio.create_task(self._ai_loop())

        # Start Exit Agent monitoring loop
        await self.orchestrator.start_exit_agent()

        scan_interval = settings.SCAN_INTERVAL_SECONDS
        self.scan_regime = "mixed"
        self.scan_regime_raw = "mixed"
        self._scan_regime_history = []
        self.ai_layers["scan_regime"] = self.scan_regime
        self.ai_layers["scan_regime_raw"] = self.scan_regime_raw
        self.ai_layers["scan_interval_seconds"] = scan_interval
        monitor_interval = 5
        last_scan = 0
        last_equity_sync = 0
        last_state_save = 0

        try:
            while self.running:
                now = time.time()

                # Sync equity from Alpaca every 60s
                if now - last_equity_sync >= 60 and self.alpaca_client:
                    last_equity_sync = now
                    try:
                        acct = self.alpaca_client.get_account()
                        self.risk_manager.update_equity(acct.get("equity", self.risk_manager.equity))
                        # Update open risk
                        positions = self.entry_manager.get_positions() if self.entry_manager else []
                        self.risk_manager.update_open_risk(positions)
                    except Exception as e:
                        logger.debug(f"Equity sync error: {e}")

                market_open = self.entry_manager.is_market_open()
                if not market_open:
                    # Still scan during extended hours (pre-market 4AM-9:30AM, after-hours 4PM-8PM ET)
                    # but at a slower cadence. Skip scanning only during dead hours (8PM-4AM ET).
                    from datetime import datetime as dt
                    import pytz
                    et = dt.now(pytz.timezone('US/Eastern'))
                    extended_hours = (4 <= et.hour < 9) or (et.hour == 9 and et.minute < 30) or (16 <= et.hour < 21)
                    if not extended_hours:
                        # OVERNIGHT STRATEGY SESSION — formulate next day's plan
                        # But STILL monitor positions for protection
                        positions = self.entry_manager.get_positions()
                        if positions:
                            try:
                                await self._monitor_positions()
                            except Exception as e:
                                logger.debug(f"Overnight monitor error: {e}")
                        await self._overnight_session(et)
                        await asyncio.sleep(300)  # 5 min between overnight cycles
                        continue
                    # Extended hours: scan AND trade (earnings, FDA, filings drop in AH/PM)
                    logger.debug(f"📡 Extended hours active ({et.strftime('%H:%M')} ET) — scanning + trading")

                if self.paused:
                    await asyncio.sleep(5)
                    continue

                # ── PERSIST STATE (every 30s) ──────────────────────
                if now - last_state_save >= 30:
                    last_state_save = now
                    persistence.save_positions(self.entry_manager.positions)
                    persistence.save_ai_state(self.ai_layers)
                    persistence.save_pnl_state(self.pnl_state)

                # ── SCAN ───────────────────────────────────────────
                if now - last_scan >= scan_interval:
                    last_scan = now
                    try:
                        # Update sector rotation for scanning focus
                        if self.sector_model:
                            await self.sector_model.update()
                        candidates = await self.scanner.scan()
                        # Subscribe to real-time data for top candidates
                        if candidates and self.market_stream:
                            top_symbols = [c["symbol"] for c in candidates[:10]]
                            # Also keep streaming positions
                            pos_symbols = [p["symbol"] for p in self.entry_manager.get_positions()]
                            # Feed prev_close data for accurate daily % in breakout alerts
                            prev_closes = {c["symbol"]: c.get("prev_close", 0) for c in candidates if c.get("prev_close", 0) > 0}
                            self.market_stream.set_prev_closes(prev_closes)
                            await self.market_stream.subscribe(top_symbols + pos_symbols)
                        await self._process_candidates(candidates)

                        raw_regime = self.scanner.get_last_market_regime() if self.scanner else "mixed"
                        effective_regime = self._smooth_scan_regime(raw_regime)
                        new_scan_interval = self._determine_scan_interval(effective_regime)
                        if (
                            raw_regime != self.scan_regime_raw
                            or effective_regime != self.scan_regime
                            or new_scan_interval != scan_interval
                        ):
                            logger.info(
                                f"⏱️ Adaptive scan cadence: raw={raw_regime}, regime={effective_regime}, interval={new_scan_interval}s"
                            )
                            log_activity(
                                "scan",
                                f"Adaptive cadence: raw={raw_regime}, regime={effective_regime}, interval={new_scan_interval}s",
                            )
                        self.scan_regime_raw = raw_regime
                        self.scan_regime = effective_regime
                        scan_interval = new_scan_interval
                        self.ai_layers["scan_regime_raw"] = self.scan_regime_raw
                        self.ai_layers["scan_regime"] = self.scan_regime
                        self.ai_layers["scan_interval_seconds"] = scan_interval
                    except Exception as e:
                        logger.error(f"Scan error: {e}")

                # ── MONITOR pending orders (adjust stale limits) ──
                try:
                    await self._monitor_pending_orders()
                except Exception as e:
                    logger.debug(f"Pending order monitor error: {e}")

                # ── MONITOR positions ──────────────────────────────
                try:
                    await self._monitor_positions()
                except Exception as e:
                    logger.error(f"Monitor error: {e}")

                # ── EXTENDED HOURS GUARD ──────────────────────────
                # Ensure every position has protection (trailing stop OR dynamic limit)
                try:
                    positions = self.entry_manager.get_positions()
                    if positions:
                        guard_actions = await self.extended_guard.protect_positions(positions)
                        for sym, action in guard_actions.items():
                            log_activity("trade", f"🛡️ {sym}: {action}")
                except Exception as e:
                    logger.error(f"Extended guard error: {e}")

                # ── FAST-PATH BREAKOUT EVALUATION ─────────────────
                # Process any breakouts queued by WebSocket stream
                try:
                    await self._process_breakout_queue()
                except Exception as e:
                    logger.error(f"Breakout queue error: {e}")

                await asyncio.sleep(monitor_interval)

        except Exception as e:
            logger.error(f"Fatal error in main loop: {e}")
            raise
        finally:
            ai_task.cancel()
            await self.shutdown()

    async def _overnight_session(self, et):
        """
        Overnight strategy session (8PM - 4AM ET).
        Instead of sleeping, the bot thinks and prepares:
          1. Review today's performance (game film)
          2. Analyze overnight futures/crypto for market direction
          3. Scan global news for tomorrow's catalysts
          4. Refresh pharma PDUFA calendar
          5. Build tomorrow's watchlist and thesis
          6. Update fade runner candidates
        Runs every 5 min during overnight, but each task has its own throttle.
        """
        import json
        from pathlib import Path

        state_file = Path(__file__).parent.parent / "data" / "overnight_state.json"
        
        # Load state
        state = {}
        try:
            if state_file.exists():
                with open(state_file) as f:
                    state = json.load(f)
        except Exception:
            pass

        last_review = state.get("last_game_film", 0)
        last_thesis = state.get("last_thesis", 0)
        last_pharma = state.get("last_pharma_refresh", 0)
        last_news = state.get("last_news_scan", 0)
        now = time.time()
        hour = et.hour

        tasks_run = []

        # ── GAME FILM: Review today's trades (once per night, after 9PM ET) ──
        if hour >= 21 and (now - last_review > 6 * 3600):
            try:
                if hasattr(self, 'game_film') and self.game_film:
                    logger.info("🎬 Overnight: Running game film review...")
                    await self.game_film.run(bot=self)
                    state["last_game_film"] = now
                    tasks_run.append("game_film")
            except Exception as e:
                logger.debug(f"Game film review failed: {e}")

        # ── PHARMA: Refresh PDUFA calendar (every 6 hours) ──
        if now - last_pharma > 6 * 3600:
            try:
                if self.pharma_scanner:
                    await self.pharma_scanner._refresh_pdufa_calendar()
                    state["last_pharma_refresh"] = now
                    tasks_run.append("pharma_calendar")
            except Exception as e:
                logger.debug(f"Pharma refresh failed: {e}")

        # ── WATCHLIST + THESIS: Full overnight research (once per night, after 10PM ET) ──
        if hour >= 22 and (now - last_thesis > 8 * 3600):
            try:
                # 1. Get Perplexity market thesis + stock picks
                thesis = await self._build_overnight_thesis()
                perplexity_picks = thesis.get("watchlist", []) if thesis else []
                
                # 2. Get StockTwits trending with sentiment
                stocktwits_data = []
                if self.stocktwits_client:
                    try:
                        trending = await asyncio.get_event_loop().run_in_executor(
                            None, self.stocktwits_client.get_trending)
                        for t in trending:
                            sym = t.get("symbol", "")
                            if sym and sym.isalpha() and len(sym) <= 5:
                                sent = await asyncio.get_event_loop().run_in_executor(
                                    None, self.stocktwits_client.get_sentiment, sym)
                                stocktwits_data.append({
                                    "symbol": sym,
                                    "trending_score": t.get("trending_score", 0),
                                    "sentiment_score": sent.get("score", 0),
                                    "bullish": sent.get("bullish", 0),
                                    "bearish": sent.get("bearish", 0),
                                })
                        logger.info(f"📊 StockTwits overnight: {len(stocktwits_data)} tickers with sentiment")
                    except Exception as e:
                        logger.debug(f"StockTwits overnight failed: {e}")

                # 3. Get Twitter/X mentions (if available)
                twitter_data = []
                if hasattr(self, 'twitter_client') and self.twitter_client:
                    try:
                        # Get sentiment for top StockTwits tickers on Twitter too
                        for st in stocktwits_data[:10]:
                            sent = await asyncio.get_event_loop().run_in_executor(
                                None, self.twitter_client.get_sentiment, st["symbol"])
                            if sent and sent.get("count", 0) > 0:
                                twitter_data.append(sent)
                    except Exception:
                        pass

                # 4. Get pharma catalysts
                pharma_signals = []
                if self.pharma_scanner:
                    pharma_signals = await self.pharma_scanner.scan()

                # 5. Get fade candidates
                fade_candidates = []
                if self.fade_scanner:
                    fade_candidates = self.fade_scanner.get_fade_candidates()

                # 5b. Get earnings-driven candidates
                earnings_signals = []
                if self.earnings_scanner:
                    earnings_signals = await self.earnings_scanner.scan()
                    for es in earnings_signals:
                        self.watchlist.add(
                            es["ticker"], side="long", conviction=es.get("conviction", 0.4),
                            source="earnings", reason=es.get("reason", "earnings catalyst")
                        )
                    if earnings_signals:
                        logger.info(f"📅 Added {len(earnings_signals)} earnings plays to watchlist")

                # 5c. Unusual options activity
                uoa_signals = []
                if self.options_scanner:
                    uoa_signals = await self.options_scanner.scan()
                    for sig in uoa_signals:
                        side = "long" if sig.get("bias") == "bullish" else "short"
                        self.watchlist.add(
                            sig["ticker"], side=side, conviction=sig.get("conviction", 0.5),
                            source="options_flow", reason=sig.get("reason", "unusual options activity")
                        )
                    if uoa_signals:
                        logger.info(f"🎯 Added {len(uoa_signals)} unusual options signals to watchlist")
                        log_activity("research", f"🎯 Unusual options: {len(uoa_signals)} signals — {', '.join(s['ticker'] for s in uoa_signals[:5])}")

                # 5d. Congressional trading
                congress_trades = []
                if self.congress_scanner:
                    congress_trades = await self.congress_scanner.scan()
                    buy_signals = self.congress_scanner.get_buy_signals()
                    for sig in buy_signals[:5]:
                        self.watchlist.add(
                            sig["ticker"], side="long", conviction=0.4 + (0.1 * min(sig["count"], 3)),
                            source="congress", reason=f"{sig['count']} congress members buying"
                        )
                    if buy_signals:
                        logger.info(f"🏛️ Congress buys: {', '.join(s['ticker'] for s in buy_signals[:5])}")
                        log_activity("research", f"🏛️ Congress buying: {', '.join(s['ticker'] for s in buy_signals[:5])}")

                # 5e. Short interest / squeeze candidates
                si_stocks = []
                if self.short_scanner:
                    si_stocks = await self.short_scanner.scan()
                    squeeze_candidates = self.short_scanner.get_squeeze_candidates()
                    for sc in squeeze_candidates[:5]:
                        self.watchlist.add(
                            sc["ticker"], side="long", conviction=sc.get("conviction", 0.4),
                            source="short_squeeze", reason=sc.get("reason", "high short interest")
                        )
                    if squeeze_candidates:
                        logger.info(f"🩳 Squeeze candidates: {', '.join(s['ticker'] for s in squeeze_candidates[:5])}")
                        log_activity("research", f"🩳 Squeeze candidates: {', '.join(s['ticker'] for s in squeeze_candidates[:5])}")

                # 6. REBUILD WATCHLIST from all sources
                self.watchlist.rebuild_overnight(
                    stocktwits_trending=stocktwits_data,
                    twitter_mentions=twitter_data,
                    perplexity_picks=perplexity_picks,
                    pharma_catalysts=pharma_signals,
                    fade_candidates=fade_candidates,
                )

                # Save thesis
                if thesis:
                    thesis_file = Path(__file__).parent.parent / "data" / "tomorrow_thesis.json"
                    with open(thesis_file, "w") as f:
                        json.dump(thesis, f, indent=2)

                # 7. POST-EARNINGS REACTION CHECK
                # Check AH price action for today's earnings — remove/flip bad reactions
                try:
                    today_earnings = await self.earnings_scanner.get_today()
                    if today_earnings:
                        for earn in today_earnings:
                            ticker = earn.get("ticker", "")
                            if not ticker:
                                continue
                            try:
                                snapshot = self.scanner._get_alpaca_snapshot(ticker)
                                if snapshot:
                                    close = snapshot.get("prev_close", 0)
                                    latest = snapshot.get("price", 0)
                                    if close and latest and close > 0:
                                        ah_change_pct = snapshot.get("change_pct", 0)
                                        if ah_change_pct <= -2.0:
                                            # Bad earnings reaction — remove from LONG or flip to SHORT
                                            self.watchlist.remove(ticker)
                                            logger.warning(
                                                f"📉 POST-EARNINGS FLUSH: {ticker} down {ah_change_pct:.1f}% AH — removed from watchlist"
                                            )
                                            log_activity("research", f"📉 {ticker} post-earnings: {ah_change_pct:+.1f}% AH — removed")
                                        elif ah_change_pct >= 3.0:
                                            # Good earnings reaction — boost conviction
                                            self.watchlist.add(
                                                ticker, side="long",
                                                conviction=min(0.95, 0.7 + ah_change_pct / 50),
                                                source="earnings_reaction",
                                                reason=f"Earnings beat: {ah_change_pct:+.1f}% AH gap up"
                                            )
                                            logger.info(f"📈 POST-EARNINGS GAP: {ticker} up {ah_change_pct:+.1f}% AH — boosted")
                                            log_activity("research", f"📈 {ticker} post-earnings: {ah_change_pct:+.1f}% AH — boosted")
                            except Exception:
                                pass
                except Exception as e:
                    logger.debug(f"Post-earnings reaction check failed: {e}")

                # 8. PRICE VALIDATION — cross-reference entire watchlist against real prices
                try:
                    all_tickers = self.watchlist.get_tickers()
                    if all_tickers:
                        import requests as _req
                        _headers = {
                            'APCA-API-KEY-ID': settings.ALPACA_API_KEY,
                            'APCA-API-SECRET-KEY': settings.ALPACA_SECRET_KEY,
                        }
                        syms = ','.join(all_tickers)
                        _r = _req.get(
                            f'https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}&feed=iex',
                            headers=_headers, timeout=10
                        )
                        if _r.status_code == 200:
                            raw_snaps = _r.json()
                            # Convert to format watchlist expects
                            price_snaps = {}
                            for sym, data in raw_snaps.items():
                                lt = data.get('latestTrade', {})
                                pb = data.get('prevDailyBar', {})
                                price = lt.get('p', 0)
                                prev = pb.get('c', 0)
                                chg = ((price - prev) / prev * 100) if prev > 0 else 0
                                price_snaps[sym] = {"price": price, "change_pct": round(chg, 2), "prev_close": prev}
                            result = self.watchlist.validate_with_prices(price_snaps)
                            if result["removed"]:
                                log_activity("research", f"🔍 Price validation removed: {', '.join(result['removed'])}")
                            logger.info(f"🔍 Price validation complete: {len(result['removed'])} removed, {len(result['adjusted'])} adjusted")
                except Exception as e:
                    logger.debug(f"Price validation failed: {e}")

                state["last_thesis"] = now
                tasks_run.append("watchlist_rebuild")
                tasks_run.append("thesis")
                logger.success(f"📋 Tomorrow's thesis: bias={thesis.get('market_bias', '?')}, watchlist={len(self.watchlist)} tickers")
                log_activity("research", f"Watchlist rebuilt: {len(self.watchlist)} tickers, market bias: {thesis.get('market_bias', '?')}", 
                            {"watchlist_count": len(self.watchlist), "bias": thesis.get("market_bias", "?")})
            except Exception as e:
                logger.debug(f"Overnight thesis/watchlist failed: {e}")

        # ── EDGAR: Scan SEC filings for material events (every 30 min) ──
        last_edgar = state.get("last_edgar_scan", 0)
        if now - last_edgar > 1800:
            try:
                filings = await self.edgar_scanner.scan_recent_filings()
                if filings:
                    state["last_edgar_scan"] = now
                    tasks_run.append("edgar")
                    for f in filings[:5]:
                        ticker = f.get("ticker", "?")
                        form = f.get("form_type", "?")
                        log_activity("research", f"📋 SEC {form}: {ticker} — {f.get('description', '')[:80]}")
                        # Add 8-K filers to watchlist as potential catalysts
                        if form == "8-K" and ticker:
                            self.watchlist.add(ticker, side="long", conviction=0.5,
                                              source="edgar", reason=f"8-K filing: {f.get('description', '')[:50]}")
            except Exception as e:
                logger.debug(f"EDGAR scan failed: {e}")

        # ── NEWS: Scan overnight news for market-moving events (every 2 hours) ──
        if now - last_news > 2 * 3600:
            try:
                news = await self._scan_overnight_news()
                if news:
                    state["last_news_scan"] = now
                    state["overnight_news"] = news[:5]
                    tasks_run.append("news")
                    for headline in news[:3]:
                        log_activity("research", f"📰 {headline[:120]}")
            except Exception as e:
                logger.debug(f"News scan failed: {e}")

        # Save state
        if tasks_run:
            try:
                Path(state_file).parent.mkdir(parents=True, exist_ok=True)
                with open(state_file, "w") as f:
                    json.dump(state, f, indent=2)
                logger.info(f"🌙 Overnight tasks: {', '.join(tasks_run)}")
            except Exception:
                pass
        else:
            log_activity("thinking", f"Overnight idle ({et.strftime('%H:%M')} ET) — waiting for next research cycle")
            logger.debug(f"🌙 Overnight idle ({et.strftime('%H:%M')} ET) — next thesis at 10PM, news in {max(0, int(2*3600 - (now - last_news)))//60}min")

    async def _build_overnight_thesis(self) -> dict:
        """Use AI to build tomorrow's trading thesis based on today's data."""
        import httpx
        
        pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if not pplx_key:
            return {}

        try:
            # Get real-time market context from Perplexity
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {pplx_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro'),
                        "max_tokens": 1500,
                        "messages": [{"role": "user", "content":
                            "Give me a brief overnight market analysis for tomorrow's US stock trading session. Include: "
                            "1. S&P 500 futures direction and key levels "
                            "2. Any major overnight news (earnings, geopolitics, Fed) "
                            "3. Sectors likely to move tomorrow "
                            "4. Top 5 specific stock tickers to watch tomorrow and why "
                            "5. Overall market bias (bullish/bearish/neutral) "
                            "Format as JSON with keys: sp500_futures, overnight_news, hot_sectors, watchlist (array of {ticker, reason}), market_bias"}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                
                # Try to parse as JSON
                import re
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                if json_match:
                    import json
                    return json.loads(json_match.group())
                else:
                    return {"raw_thesis": text, "market_bias": "unknown", "watchlist": []}
        except Exception as e:
            logger.debug(f"Thesis build failed: {e}")
            return {}

    async def _scan_overnight_news(self) -> list:
        """Scan for market-moving overnight news via Perplexity."""
        import httpx
        
        pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if not pplx_key:
            return []

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {pplx_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro'),
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content":
                            "What are the most important market-moving news events in the last 4 hours that could "
                            "affect US stock prices tomorrow? List the top 5 with affected tickers if applicable. "
                            "One per line, brief."}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]
                headlines = [line.strip().lstrip("•-123456789. ") for line in text.strip().split("\n") if line.strip() and len(line.strip()) > 10]
                if headlines:
                    logger.info(f"📰 Overnight news: {len(headlines)} market-moving headlines")
                    for h in headlines[:3]:
                        logger.info(f"  📰 {h[:100]}")
                return headlines[:10]
        except Exception as e:
            logger.debug(f"News scan failed: {e}")
            return []

    async def _ai_loop(self):
        """Run AI layers on their own intervals, concurrently with trading."""
        while self.running:
            try:
                # Observer (every 10 min)
                obs = await self.observer.run(self)
                if obs:
                    obs_text = obs.get("market_assessment", str(obs)[:200])
                    self.ai_layers["last_observation"] = obs_text
                    log_activity("ai", f"🔭 Observer: {obs_text[:150]}")

                # Advisor (every 30 min)
                adv = await self.advisor.run(self, self.observer.get_last_output())
                if adv:
                    adv_text = adv.get("strategy", str(adv)[:200])
                    self.ai_layers["last_advice"] = adv_text
                    log_activity("ai", f"🎯 Advisor: {adv_text[:150]}")

                # Tuner (every 30 min)
                tun = await self.tuner.run(self, self.advisor.get_last_output())
                if tun and tun.get("applied"):
                    changes_str = ", ".join(f"{c['param']}:{c['old']}→{c['new']}" for c in tun["applied"])
                    self.ai_layers["last_tuner_changes"] = changes_str
                    log_activity("ai", f"🔧 Tuner: {changes_str}")

                # Game Film (every 60 min)
                gf = await self.game_film.run(self)
                if gf:
                    self.ai_layers["last_game_film_summary"] = (
                        f"{gf['total_trades']} trades, {gf['overall_win_rate_pct']}% WR, ${gf['total_pnl']:.2f}"
                    )

                # Position Manager (every 2 min)
                pm = await self.position_manager.run(self)
                if pm:
                    health = pm.get("portfolio_health", "healthy")
                    exits = len(pm.get("emergency_exits", []))
                    vetoes = len(pm.get("vetoes", []))
                    self.ai_layers["last_position_manager"] = f"{health} | {exits} exits | {vetoes} vetoes"

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AI layer error: {e}")

            await asyncio.sleep(30)  # Check every 30s, layers self-throttle

    @staticmethod
    def _extract_signal_sources(candidate: dict) -> list:
        return extract_signal_sources(candidate)

    @staticmethod
    def _derive_strategy_tag(candidate: dict, direction: str) -> str:
        return derive_strategy_tag(candidate, direction)

    @staticmethod
    def _determine_scan_interval(self, regime: str) -> int:
        """
        Adaptive scan cadence:
          market hours + high-vol -> fast scans (60s)
          market hours + choppy   -> slow scans (300s)
          extended hours (4-9:30 AM, 4-8 PM ET) -> 300s
          overnight (8 PM - 4 AM ET) -> 600s
        """
        from datetime import datetime
        try:
            import zoneinfo
            et_hour = datetime.now(zoneinfo.ZoneInfo("US/Eastern")).hour
        except Exception:
            et_hour = 12

        # Overnight: minimal scanning (thesis building only)
        if et_hour >= 20 or et_hour < 4:
            return 600  # 10 min

        # Extended hours: slow scanning
        if et_hour < 9 or (et_hour == 9 and datetime.now(zoneinfo.ZoneInfo("US/Eastern")).minute < 30) or et_hour >= 16:
            return 300  # 5 min

        # Market hours: adaptive by regime
        fast = max(15, int(getattr(settings, "SCAN_INTERVAL_FAST_SECONDS", 60)))
        slow = max(fast, int(getattr(settings, "SCAN_INTERVAL_SLOW_SECONDS", 300)))
        baseline = max(fast, int(settings.SCAN_INTERVAL_SECONDS))

        if regime in ("risk_on", "risk_off"):
            return fast
        if regime == "choppy":
            return slow
        return baseline

    def _smooth_scan_regime(self, raw_regime: str) -> str:
        """
        Apply hysteresis to avoid cadence flapping when regime signal flickers.
        """
        history_window = max(1, int(getattr(settings, "SCAN_REGIME_HYSTERESIS_WINDOW", 3)))
        confirmations = max(1, int(getattr(settings, "SCAN_REGIME_MIN_CONFIRMATIONS", 2)))

        if not hasattr(self, "_scan_regime_history"):
            self._scan_regime_history = []

        self._scan_regime_history.append(raw_regime or "mixed")
        self._scan_regime_history = self._scan_regime_history[-history_window:]

        current = getattr(self, "scan_regime", "mixed") or "mixed"
        if raw_regime == current:
            return current

        votes = self._scan_regime_history.count(raw_regime)
        if votes >= confirmations:
            return raw_regime
        return current

    @staticmethod
    def _compute_entry_slippage_bps(entry_price: float, signal_price: float, side: str) -> float:
        """
        Compute signed entry slippage in bps vs signal price.
        Positive = adverse fill. Negative = favorable fill.
        """
        if not entry_price or not signal_price:
            return 0.0
        if side == "short":
            # Short adverse slippage means entry lower than signal.
            return ((signal_price - entry_price) / signal_price) * 10000
        # Long adverse slippage means entry higher than signal.
        return ((entry_price - signal_price) / signal_price) * 10000

    async def _process_breakout_queue(self):
        """Process breakouts detected by WebSocket for immediate evaluation.
        
        This is the FAST PATH — gets us into runners 10-15 minutes before
        the next scan cycle would find them.
        """
        if not hasattr(self, '_breakout_queue') or self._breakout_queue.empty():
            return
        
        if not self.risk_manager.can_trade():
            return

        # Process up to 3 breakouts per cycle to avoid flooding
        processed = 0
        while not self._breakout_queue.empty() and processed < 3:
            try:
                candidate = self._breakout_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            
            symbol = candidate["symbol"]
            
            # Double-check not already held (could have been bought since queue)
            held_symbols = {p.get("symbol") for p in self.entry_manager.get_positions()}
            if symbol in held_symbols:
                continue
            
            logger.info(f"⚡ FAST-PATH evaluating: {symbol} @ ${candidate['price']:.2f} ({candidate['change_pct']:+.1f}%, {candidate['volume_spike']:.1f}x vol)")
            
            # Run through the same orchestrator pipeline as normal candidates
            await self._process_candidates([candidate])
            processed += 1

    async def _process_candidates(self, candidates):
        """Evaluate scanner candidates for entry with position manager veto."""
        if not self.risk_manager.can_trade():
            return

        positions = self.entry_manager.get_positions()

        # Evaluate candidates — skip held tickers and recently-SKIPped to find FRESH opportunities
        evaluated = 0
        held_symbols = {p.get("symbol") for p in positions}
        for candidate in candidates[:20]:  # Look at top 20
            if evaluated >= 8:  # Evaluate up to 8 per cycle (more diversity)
                break
            symbol = candidate["symbol"]

            # Skip tickers we already hold (don't waste AI calls re-evaluating held positions)
            if symbol in held_symbols:
                continue

            sentiment_score = candidate.get("sentiment_score", 0)
            sentiment_data = dict(self.sentiment_analyzer.get_cached(symbol) or {"score": sentiment_score})
            signal_price = float(candidate.get("price", 0) or 0)
            signal_sources = self._extract_signal_sources(candidate)
            sentiment_data["signal_price"] = signal_price
            sentiment_data["decision_price"] = signal_price
            sentiment_data["signal_sources"] = signal_sources

            # Position manager veto check
            if self.position_manager and not self.position_manager.can_enter(symbol, positions, self.risk_manager):
                continue

            # Specialized Agent Orchestrator — 5 agents + jury
            if self.orchestrator:
                try:
                    verdict = await self.orchestrator.evaluate(
                        symbol=symbol,
                        price=candidate.get("price", 0),
                        signals_data=candidate,
                    )
                    # Only count as "evaluated" if we actually made AI calls (not cooldown skip)
                    if "cooldown" not in verdict.reasoning.lower():
                        evaluated += 1
                        # Stagger AI calls to avoid rate limits
                        if evaluated > 1:
                            await asyncio.sleep(1.5)
                    self.ai_layers["last_consensus"] = verdict.to_dict()
                    if verdict.decision not in ("BUY", "SHORT"):
                        if "cooldown" not in verdict.reasoning.lower():
                            logger.info(f"🗳️ Jury SKIP for {symbol}: {verdict.reasoning}")
                            log_activity("ai", f"🗳️ {symbol}: SKIP — {verdict.reasoning}")
                        continue
                    direction = verdict.decision
                    # Map jury sizing to consensus_size_modifier (0-1 range)
                    tier = self.risk_manager.get_risk_tier() if self.risk_manager else {}
                    tier_size = tier.get("size_pct", 2.0)
                    size_modifier = min(1.0, verdict.size_pct / tier_size) if tier_size > 0 else 1.0
                    log_activity("trade", f"🗳️ {symbol}: {direction} verdict! conf={verdict.confidence}% size={verdict.size_pct}% trail={verdict.trail_pct}%")
                    sentiment_data["consensus_size_modifier"] = size_modifier
                    sentiment_data["consensus_confidence"] = verdict.confidence
                    sentiment_data["consensus_direction"] = direction
                    sentiment_data["jury_trail_pct"] = verdict.trail_pct
                    sentiment_data["provider_used"] = getattr(verdict, "provider_used", "")
                    sentiment_data["strategy_tag"] = self._derive_strategy_tag(candidate, direction)
                except Exception as e:
                    logger.error(f"Orchestrator error for {symbol}: {e}")
                    continue  # Never trade without agent consensus

            logger.info(f"🔑 {symbol} REACHED ENTRY BLOCK (orchestrator={bool(self.orchestrator)})")
            direction = sentiment_data.get("consensus_direction", "BUY")
            sentiment_data.setdefault("provider_used", "")
            sentiment_data.setdefault("consensus_confidence", 0)
            sentiment_data.setdefault("strategy_tag", self._derive_strategy_tag(candidate, direction))
            logger.info(f"🔑 {symbol} pre-entry: direction={direction}, sentiment={sentiment_score:.2f}")
            # For SHORT trades, invert sentiment check (negative sentiment = good for shorts)
            check_sentiment = -sentiment_score if direction == "SHORT" else sentiment_score
            can = await self.entry_manager.can_enter(symbol, check_sentiment, positions)
            logger.info(f"🔑 {symbol} can_enter={can} (check_sent={check_sentiment:.2f})")
            if can:
                logger.info(f"{'📈' if direction == 'BUY' else '📉'} Entry signal: {symbol} {direction} (score={candidate['score']:.3f}, sent={sentiment_score:.2f})")
                
                # ── OPTIONS TRADE (if enabled) ──
                if self.options_engine:
                    confidence = sentiment_data.get("consensus_confidence", 0)
                    # High confidence trades (80%+) → options for leverage
                    # Lower confidence → shares (safer)
                    if confidence >= 80:
                        options_pct = float(getattr(settings, "OPTIONS_ALLOCATION_PCT", 50))
                    elif confidence >= 70:
                        options_pct = float(getattr(settings, "OPTIONS_ALLOCATION_PCT", 50)) * 0.5
                    else:
                        options_pct = 0  # Shares only for low confidence
                    
                    if options_pct > 0:
                        tier = self.risk_manager.get_risk_tier() if self.risk_manager else {}
                        equity = self.risk_manager.equity if self.risk_manager else 25000
                        total_budget = equity * tier.get("size_pct", 2.5) / 100
                        options_budget = total_budget * (options_pct / 100)
                        
                        opt_pos = await self.options_engine.execute_option_trade(
                            symbol=symbol,
                            price=candidate.get("price", 0),
                            direction=direction,
                            budget=options_budget,
                            sentiment_data=sentiment_data,
                        )
                        if opt_pos:
                            log_activity("trade", f"🎯 OPTIONS: {opt_pos['qty']}x {opt_pos['contract_symbol']} ({opt_pos['option_type']}) @ ${opt_pos['entry_premium']:.2f}")

                # ── SHARES TRADE (always, reduced size if options took some) ──
                if direction == "SHORT":
                    pos = await self.entry_manager.enter_short(symbol, sentiment_data)
                else:
                    pos = await self.entry_manager.enter_position(symbol, sentiment_data)
                if pos:
                    positions = self.entry_manager.get_positions()

    async def _monitor_pending_orders(self):
        """Monitor unfilled limit orders and adjust price if stale."""
        from functools import partial

        try:
            open_orders = await asyncio.get_event_loop().run_in_executor(
                None, self.alpaca_client.get_orders
            )
        except Exception as e:
            logger.debug(f"Pending order check failed: {e}")
            return

        for order in open_orders:
            if order.get("type") != "limit" or order.get("side") != "buy":
                continue
            if order.get("status") not in ("new", "accepted"):
                continue

            symbol = order.get("symbol", "")
            order_id = order.get("id", "")
            limit_price = float(order.get("limit_price", 0))
            created = order.get("created_at", "")

            # Check age — only adjust after 2 minutes
            try:
                from datetime import datetime, timezone
                if "T" in str(created):
                    created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
                    age_seconds = (datetime.now(timezone.utc) - created_dt).total_seconds()
                else:
                    age_seconds = 0
            except Exception:
                age_seconds = 0

            if age_seconds < 120:
                continue  # Give it 2 minutes to fill

            # Get current price
            try:
                snapshot = await asyncio.get_event_loop().run_in_executor(
                    None, partial(self.alpaca_client.get_latest_price, symbol)
                )
                current_price = snapshot if isinstance(snapshot, (int, float)) else 0
            except Exception:
                continue

            if current_price <= 0:
                continue

            # If price moved more than 0.3% from our limit, adjust
            price_diff_pct = abs(current_price - limit_price) / limit_price * 100
            if price_diff_pct > 0.3:
                # Set new limit slightly above current ask (0.15% above for buys)
                new_limit = round(current_price * 1.0015, 2)
                logger.info(f"📝 Adjusting stale order for {symbol}: ${limit_price:.2f} → ${new_limit:.2f} (price moved {price_diff_pct:.1f}%, age={int(age_seconds)}s)")
                log_activity("trade", f"📝 {symbol}: limit ${limit_price:.2f} → ${new_limit:.2f} (stale {int(age_seconds)}s)")

                result = await asyncio.get_event_loop().run_in_executor(
                    None, partial(self.alpaca_client.replace_order, order_id, new_limit)
                )
                if result:
                    # Update our tracked position entry price
                    if symbol in self.entry_manager.positions:
                        self.entry_manager.positions[symbol]["entry_price"] = new_limit
                else:
                    # If replace fails (e.g. order already filled), cancel and let next cycle re-enter
                    logger.warning(f"Replace failed for {symbol} — cancelling stale order")
                    await asyncio.get_event_loop().run_in_executor(
                        None, partial(self.alpaca_client.cancel_order, order_id)
                    )
                    if symbol in self.entry_manager.positions:
                        self.entry_manager.remove_position(symbol)

            elif age_seconds > 600:
                # 10 minutes stale and price hasn't moved much — cancel, thesis may be dead
                logger.info(f"⏰ Cancelling stale order for {symbol} — {int(age_seconds)}s old, price near limit but no fill")
                log_activity("trade", f"⏰ {symbol}: cancelled stale order after {int(age_seconds//60)}min")
                await asyncio.get_event_loop().run_in_executor(
                    None, partial(self.alpaca_client.cancel_order, order_id)
                )
                if symbol in self.entry_manager.positions:
                    self.entry_manager.remove_position(symbol)

    def _record_realized_exit(self, trade_record: dict):
        """
        Record a fully closed trade exactly once across history, risk, P&L state, and persistence.
        Centralized to avoid drift between polling and websocket exit paths.
        """
        trade_record = normalize_trade_record(trade_record)

        pnl = float(trade_record.get("pnl", 0))
        symbol = trade_record.get("symbol", "")

        trade_history.record_trade(trade_record)
        if self.entry_manager and symbol:
            self.entry_manager.remove_position(symbol)
        if self.risk_manager:
            self.risk_manager.record_trade(trade_record)

        self.pnl_state["total_realized_pnl"] = self.pnl_state.get("total_realized_pnl", 0) + pnl
        self.pnl_state["today_realized_pnl"] = self.pnl_state.get("today_realized_pnl", 0) + pnl
        self.pnl_state["total_trades"] = self.pnl_state.get("total_trades", 0) + 1
        if pnl >= 0:
            self.pnl_state["winning_trades"] = self.pnl_state.get("winning_trades", 0) + 1
        else:
            self.pnl_state["losing_trades"] = self.pnl_state.get("losing_trades", 0) + 1
        self.pnl_state["best_trade"] = max(self.pnl_state.get("best_trade", 0), pnl)
        self.pnl_state["worst_trade"] = min(self.pnl_state.get("worst_trade", 0), pnl)

        persistence.save_pnl_state(self.pnl_state)
        persistence.save_positions(self.entry_manager.positions if self.entry_manager else {})
        persistence.save_trades([trade_record])

    async def _monitor_positions(self):
        """
        Monitor positions — but DO NOT exit them.
        Trailing stop % on Alpaca is the ONLY exit strategy.
        This method only:
          1. Verifies trailing stops exist (retries if missing)
          2. Syncs positions with Alpaca (detect fills from trailing stops)
          3. Records completed trades to history
        """
        positions = self.entry_manager.get_positions()
        if not positions:
            return

        # Get actual Alpaca positions to detect trailing stop fills
        try:
            alpaca_positions = await asyncio.get_event_loop().run_in_executor(
                None, self.alpaca_client.get_positions
            )
            alpaca_symbols = {p["symbol"] for p in alpaca_positions}
        except Exception as e:
            logger.debug(f"Alpaca position sync error: {e}")
            return

        # Also get open orders to detect pending (unfilled) entries
        try:
            open_orders = await asyncio.get_event_loop().run_in_executor(
                None, self.alpaca_client.get_orders
            )
            pending_buy_symbols = {o["symbol"] for o in open_orders
                                   if o.get("side") == "buy" and o.get("status") in ("new", "accepted", "pending_new", "partially_filled")}
        except Exception:
            pending_buy_symbols = set()

        for pos in list(positions):
            symbol = pos["symbol"]
            try:
                # ── DETECT TRAILING STOP FILLS ──
                # If we're tracking it but Alpaca no longer has it → trailing stop fired
                # BUT: if there's still a pending buy order, the position hasn't opened yet
                if symbol in pending_buy_symbols:
                    logger.debug(f"{symbol}: pending buy order still open — waiting for fill")
                    continue

                if symbol not in alpaca_symbols:
                    if pos.get("_exit_recorded") or pos.get("_exit_recording"):
                        logger.debug(f"{symbol}: trailing stop exit already being/been recorded — skipping duplicate")
                        continue
                    pos["_exit_recording"] = True
                    try:
                        entry_price = pos.get("entry_price", 0)
                        side = pos.get("side", "long")
                        # Get the latest matching trailing-stop fill price from closed orders
                        exit_price = entry_price  # default
                        try:
                            orders = await asyncio.get_event_loop().run_in_executor(
                                None, self.alpaca_client.get_orders, "closed"
                            )
                            expected_side = "sell" if side == "long" else "buy"
                            latest = None
                            latest_key = ""
                            for o in orders:
                                if o.get("symbol") != symbol:
                                    continue
                                if o.get("type") != "trailing_stop":
                                    continue
                                if o.get("side") and o.get("side") != expected_side:
                                    continue
                                if not o.get("filled_avg_price"):
                                    continue
                                ts_key = str(o.get("filled_at") or o.get("updated_at") or o.get("submitted_at") or "")
                                if ts_key >= latest_key:
                                    latest_key = ts_key
                                    latest = o
                            if latest:
                                exit_price = float(latest.get("filled_avg_price", entry_price))
                        except Exception:
                            pass

                        qty = pos.get("quantity", 0)
                        if side == "short":
                            pnl = (entry_price - exit_price) * qty
                        else:
                            pnl = (exit_price - entry_price) * qty
                        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
                        if side == "short":
                            pnl_pct = -pnl_pct

                        trade_record = {
                            "symbol": symbol,
                            "side": "sell" if side == "long" else "buy_to_cover",
                            "entry_price": entry_price,
                            "exit_price": exit_price,
                            "quantity": qty,
                            "pnl": pnl,
                            "pnl_pct": pnl_pct,
                            "reason": "trailing_stop",
                            "hold_seconds": time.time() - pos.get("entry_time", time.time()),
                            "entry_time": pos.get("entry_time", 0),
                            "exit_time": time.time(),
                            "sentiment_at_entry": pos.get("sentiment_at_entry", 0),
                            "conviction_level": pos.get("conviction_level", "normal"),
                            "risk_tier": self.risk_manager.get_risk_tier().get("name", "?"),
                            "strategy_tag": pos.get("strategy_tag", "unknown"),
                            "signal_sources": pos.get("signal_sources", ["unknown"]),
                            "decision_confidence": pos.get("decision_confidence", 0),
                            "provider_used": pos.get("provider_used", ""),
                            "signal_price": pos.get("signal_price", entry_price),
                            "decision_price": pos.get("decision_price", entry_price),
                            "fill_price": exit_price,
                            "slippage_bps": self._compute_entry_slippage_bps(
                                entry_price, pos.get("signal_price", entry_price), side
                            ),
                        }
                        self._record_realized_exit(trade_record)

                        pos["_exit_recorded"] = True
                        emoji = "✅" if pnl >= 0 else "❌"
                        logger.info(f"{emoji} TRAILING STOP EXIT: {symbol} P&L: ${pnl:.2f} ({pnl_pct:+.1f}%)")
                        log_activity("trade", f"{emoji} {symbol} stopped out: ${pnl:.2f} ({pnl_pct:+.1f}%)")
                    finally:
                        pos.pop("_exit_recording", None)
                    continue

                # ── VERIFY TRAILING STOP EXISTS ──
                if pos.get("_trail_adjusting"):
                    logger.debug(f"⏳ {symbol} trail being adjusted by Exit Agent — skipping monitor check")
                    continue
                if not pos.get("has_trailing_stop"):
                    # During extended hours, trailing stops don't work on Alpaca.
                    # ExtendedHoursGuard handles protection. Don't retry or emergency sell.
                    from datetime import datetime
                    try:
                        import zoneinfo
                        _et_now = datetime.now(zoneinfo.ZoneInfo("US/Eastern"))
                        _et_h, _et_m = _et_now.hour, _et_now.minute
                        _in_regular_hours = (_et_h == 9 and _et_m >= 30) or (10 <= _et_h < 16)
                    except Exception:
                        _in_regular_hours = True  # assume regular if can't determine

                    if not _in_regular_hours:
                        if not pos.get("_ext_hours_logged"):
                            logger.info(f"🌙 {symbol} — extended hours, skipping trailing stop (guard handles protection)")
                            pos["_ext_hours_logged"] = True
                        continue

                    # First check if Alpaca already has a trailing stop for this symbol
                    try:
                        open_orders = await asyncio.get_event_loop().run_in_executor(
                            None, self.alpaca_client.get_orders, "open"
                        )
                        for order in open_orders:
                            if order.get("symbol") == symbol and order.get("type") == "trailing_stop":
                                pos["has_trailing_stop"] = True
                                pos["trailing_stop_order_id"] = order.get("id", "")
                                logger.info(f"🔗 Found existing trailing stop for {symbol}: {order.get('id', '')[:8]}")
                                break
                    except Exception:
                        pass

                    if not pos.get("has_trailing_stop"):
                        retry_count = pos.get("_trail_retry_count", 0) + 1
                        pos["_trail_retry_count"] = retry_count
                        logger.warning(f"⚠️ {symbol} has NO trailing stop — attempt {retry_count}/5")
                        qty = int(float(pos.get("quantity", 0)))
                        trail_pct = pos.get("trail_pct", 3.0)
                        side = pos.get("side", "long")
                        trail_fn = self.alpaca_client.place_trailing_stop_short if side == "short" and hasattr(self.alpaca_client, "place_trailing_stop_short") else self.alpaca_client.place_trailing_stop
                        if qty >= 1:
                            stop_order = await asyncio.get_event_loop().run_in_executor(
                                None, trail_fn, symbol, qty, trail_pct
                            )
                            if stop_order:
                                pos["has_trailing_stop"] = True
                                pos["trailing_stop_order_id"] = stop_order.get("id")
                                pos["_trail_retry_count"] = 0
                                logger.success(f"📈 Trailing stop placed: {symbol} trail={trail_pct}%")
                            elif retry_count >= 5:
                                # Only emergency sell after 5 failed attempts across 5 scan cycles
                                logger.error(f"🚨 TRAILING STOP FAILED 5x for {symbol} — FORCED MARKET EXIT for protection")
                                emergency_fn = self.alpaca_client.place_market_buy if side == "short" else self.alpaca_client.place_market_sell
                                await asyncio.get_event_loop().run_in_executor(
                                    None, emergency_fn, symbol, qty
                                )
                                self.entry_manager.remove_position(symbol)
                                log_activity("alert", f"🚨 Emergency market exit: {symbol} — trailing stop failed 5x")
                            else:
                                logger.warning(f"⚠️ Trailing stop failed for {symbol} — will retry next cycle ({retry_count}/5)")

            except Exception as e:
                logger.error(f"Monitor error for {symbol}: {e}")

    def _on_breakout_detected(self, symbol: str, price: float, volume_spike: float, pct_change: float):
        """Called by market stream when a breakout is detected."""
        direction = "🚀" if pct_change > 0 else "💥"
        logger.info(f"{direction} BREAKOUT: {symbol} {pct_change:+.1f}% @ ${price:.2f} (vol {volume_spike:.1f}x)")
        log_activity("scan", f"{direction} Breakout: {symbol} {pct_change:+.1f}% vol={volume_spike:.1f}x")

        # ── FAST-PATH: Queue breakout for immediate evaluation ──
        # Only if: significant move, not already held, not recently evaluated
        if abs(pct_change) < 5.0 or volume_spike < 1.5:
            return  # Too small — let normal scan handle it

        held_symbols = {p.get("symbol") for p in self.entry_manager.get_positions()}
        if symbol in held_symbols:
            return  # Already in this one

        # Check orchestrator skip cache (don't re-evaluate SKIPs within 5 min)
        if self.orchestrator and self.orchestrator._skip_cache.get(symbol):
            skip_ts = self.orchestrator._skip_cache[symbol]
            if time.time() - skip_ts < 300:
                return

        # Queue for async evaluation (can't await from sync callback)
        if not hasattr(self, '_breakout_queue'):
            self._breakout_queue = asyncio.Queue()
        candidate = {
            "symbol": symbol,
            "price": price,
            "change_pct": pct_change,
            "volume_spike": volume_spike,
            "sentiment_score": 0.5,  # neutral default, agents will assess
            "score": abs(pct_change) / 100 + volume_spike / 10,  # rough priority score
            "source": "breakout_stream",
            "spread_pct": 0,
        }
        try:
            self._breakout_queue.put_nowait(candidate)
            logger.info(f"⚡ FAST-PATH: {symbol} queued for immediate evaluation ({pct_change:+.1f}%, {volume_spike:.1f}x vol)")
            log_activity("scan", f"⚡ Fast-path: {symbol} {pct_change:+.1f}% queued for immediate eval")
        except asyncio.QueueFull:
            pass  # Queue full, skip this one

    def _on_trailing_stop_filled(self, symbol: str, fill_price: float, qty: float):
        """Called by trade stream when a trailing stop order fills."""
        pos = self.entry_manager.positions.get(symbol)
        if not pos:
            logger.warning(f"Trailing stop filled for {symbol} but no tracked position")
            return
        if pos.get("_exit_recorded") or pos.get("_exit_recording"):
            logger.debug(f"{symbol}: trailing stop exit already being/been recorded — skipping duplicate callback")
            return
        pos["_exit_recording"] = True
        try:
            entry_price = pos.get("entry_price", fill_price)
            side = pos.get("side", "long")
            if side == "short":
                pnl = (entry_price - fill_price) * qty
            else:
                pnl = (fill_price - entry_price) * qty
            pnl_pct = ((fill_price - entry_price) / entry_price * 100) if entry_price else 0
            if side == "short":
                pnl_pct = -pnl_pct

            # Record trade
            trade_record = {
                "symbol": symbol, "side": "sell" if side == "long" else "buy_to_cover",
                "entry_price": entry_price, "exit_price": fill_price,
                "quantity": qty, "pnl": pnl, "pnl_pct": pnl_pct,
                "reason": "trailing_stop_ws", "hold_seconds": time.time() - pos.get("entry_time", time.time()),
                "entry_time": pos.get("entry_time", 0), "exit_time": time.time(),
                "strategy_tag": pos.get("strategy_tag", "unknown"),
                "signal_sources": pos.get("signal_sources", ["unknown"]),
                "decision_confidence": pos.get("decision_confidence", 0),
                "provider_used": pos.get("provider_used", ""),
                "signal_price": pos.get("signal_price", entry_price),
                "decision_price": pos.get("decision_price", entry_price),
                "fill_price": fill_price,
                "slippage_bps": self._compute_entry_slippage_bps(
                    entry_price, pos.get("signal_price", entry_price), side
                ),
            }
            self._record_realized_exit(trade_record)

            pos["_exit_recorded"] = True
            emoji = "✅" if pnl >= 0 else "❌"
            logger.info(f"{emoji} WS TRAILING STOP: {symbol} @ ${fill_price:.2f} P&L: ${pnl:.2f} ({pnl_pct:+.1f}%)")
            log_activity("trade", f"{emoji} {symbol} stopped out (WS): ${pnl:.2f} ({pnl_pct:+.1f}%)")
        finally:
            pos.pop("_exit_recording", None)

    def _send_trade_alert(self, pos: dict, direction: str):
        """Send Slack webhook alert on trade entry."""
        try:
            import httpx
            webhook_url = getattr(settings, 'SLACK_WEBHOOK_URL', None)
            if not webhook_url:
                return
            symbol = pos.get("symbol", "?")
            price = pos.get("entry_price", 0)
            notional = pos.get("notional", 0)
            trail = pos.get("trail_pct", 3.0)
            emoji = "📈" if direction == "BUY" else "📉"
            text = (
                f"{emoji} *Velox {direction}*: `{symbol}` @ ${price:.2f}\n"
                f"Size: ${notional:.2f} | Trail: {trail}% | "
                f"Conviction: {pos.get('conviction_level', '?')} | "
                f"{'🛡️ Trailing stop active' if pos.get('has_trailing_stop') else '⚠️ NO STOP'}"
            )
            httpx.post(webhook_url, json={"text": text}, timeout=5)
        except Exception as e:
            logger.debug(f"Slack alert failed: {e}")

    def _send_exit_alert(self, symbol: str, pnl: float, pnl_pct: float, trail_pct: float):
        """Send Slack webhook alert on trade exit."""
        try:
            import httpx
            webhook_url = getattr(settings, 'SLACK_WEBHOOK_URL', None)
            if not webhook_url:
                return
            emoji = "✅" if pnl >= 0 else "❌"
            text = (
                f"{emoji} *Velox EXIT*: `{symbol}` — P&L: ${pnl:.2f} ({pnl_pct:+.1f}%)\n"
                f"Trail: {trail_pct}% | Reason: trailing stop"
            )
            httpx.post(webhook_url, json={"text": text}, timeout=5)
        except Exception as e:
            logger.debug(f"Slack alert failed: {e}")

    async def shutdown(self):
        """Graceful shutdown."""
        logger.info("🛑 Shutting down...")
        # Stop exit agent
        if hasattr(self, 'orchestrator') and self.orchestrator:
            await self.orchestrator.stop_exit_agent()
        # Stop streams
        if self.market_stream:
            await self.market_stream.stop()
        if self.trade_stream:
            await self.trade_stream.stop()
        # Save final state
        persistence.save_positions(self.entry_manager.positions if self.entry_manager else {})
        persistence.save_pnl_state(getattr(self, 'pnl_state', {}))
        persistence.save_ai_state(self.ai_layers)
        positions = self.entry_manager.get_positions() if self.entry_manager else []
        if positions and self.exit_manager:
            logger.warning(f"Closing {len(positions)} positions on shutdown")
            await self.exit_manager.close_all(positions, "shutdown")
        logger.success("✅ Shutdown complete")

    def stop(self):
        self.running = False


async def main():
    load_dotenv()

    logger.remove()
    logger.add(
        sys.stdout,
        format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <level>{message}</level>",
        level=settings.LOG_LEVEL,
    )
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    logger.add(
        str(log_dir / "bot_{time:YYYY-MM-DD}.log"),
        rotation="1 day",
        retention="30 days",
        level="DEBUG",
    )

    bot = TradingBot()
    await bot.initialize()
    await bot.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
