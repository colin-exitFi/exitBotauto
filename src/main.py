#!/usr/bin/env python3
"""
Velox - Autonomous Velocity Trading Engine
Main loop: scan → filter → enter → monitor → exit
AI layers: observe → advise → tune → manage positions
"""

import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path
from datetime import datetime
from typing import Dict, Optional

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
from src.signals.copy_trader import CopyTraderMonitor
from src.signals.watchlist import DynamicWatchlist
from src.signals.edgar import EdgarScanner
from src.signals.earnings import EarningsScanner
from src.signals.ark_trades import ArkTradesScanner
from src.signals.finnhub import FinnhubClient
from src.signals.fred import FredClient
from src.signals.human_intel import HumanIntelStore
from src.signals.unusual_options import UnusualOptionsScanner
from src.signals.congress import CongressScanner
from src.signals.unusual_whales import UnusualWhalesClient
from src.signals.short_interest import ShortInterestScanner
from src.signals.sector_rotation import SectorRotationModel
from src.streams.market_stream import MarketStream
from src.streams.trade_stream import TradeStream
from src.streams.unusual_whales_stream import UnusualWhalesStream
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
from src.data.strategy_playbook import (
    annotate_candidate,
    bias_matches_direction,
    extract_watchlist_symbols,
    normalize_bias_label,
    score_directional_biases,
)
from src.data.technicals import get_cached_rsi
from src.options.options_monitor import OptionsMonitor
from src.reconciliation.reconciler import Reconciler


class TradingBot:
    """Main trading bot orchestrator."""

    copy_trader_monitor = None
    _processed_copy_trader_exit_ids = None
    human_intel_store = None
    fred_client = None
    finnhub_client = None
    pharma_scanner = None
    fade_scanner = None
    edgar_scanner = None
    earnings_scanner = None
    ark_trades = None
    unusual_whales = None
    unusual_whales_stream = None
    options_scanner = None
    congress_scanner = None
    short_scanner = None
    sector_model = None
    market_stream = None
    trade_stream = None
    watchlist = None
    grok_x_trending = None
    extended_guard = None
    reconciler = None
    _recorded_realized_keys = None

    def __init__(self):
        self.running = False
        self.paused = False
        self.start_time = time.time()
        self._breakout_queue = asyncio.Queue(maxsize=20)
        self._uw_signal_queue = asyncio.Queue(maxsize=50)
        self._fast_path_pending = set()
        self._jury_vetoed_symbols: Dict[str, float] = {}
        self._fast_path_eval_queue = asyncio.Queue(maxsize=50)
        self._recent_uw_signal_keys: Dict[str, float] = {}
        self._last_daily_reset_date = None
        self._processed_copy_trader_exit_ids = set()
        self._recorded_realized_keys = set()
        self._tomorrow_thesis_cache = None
        self._tomorrow_thesis_cache_at = 0.0

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
        self.options_monitor: OptionsMonitor = None
        self.reconciler: Optional[Reconciler] = None

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
        self.copy_trader_monitor = CopyTraderMonitor()
        if (
            getattr(self.copy_trader_monitor, "start_stream", None)
            and str(getattr(self.copy_trader_monitor, "_mode", "auto")) in ("auto", "stream")
        ):
            self.copy_trader_monitor.start_stream()
            logger.info("📡 X copy trader stream started")
        self.human_intel_store = HumanIntelStore()
        self.fred_client = FredClient()
        self.finnhub_client = FinnhubClient()

        # Risk manager
        self.risk_manager = RiskManager()

        # Sync equity from Alpaca on startup
        if self.alpaca_client:
            try:
                acct = self.alpaca_client.get_account()
                self.risk_manager.update_equity(
                    acct.get("equity", settings.TOTAL_CAPITAL),
                    daytrade_count=acct.get("daytrade_count"),
                )
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

        # ARK daily trade notifications
        self.ark_trades = ArkTradesScanner()

        # Unusual Whales REST client
        self.unusual_whales = UnusualWhalesClient()

        # Unusual options activity scanner
        self.options_scanner = UnusualOptionsScanner(uw_client=self.unusual_whales)

        # Congressional trading scanner
        self.congress_scanner = CongressScanner(uw_client=self.unusual_whales)

        # Unusual Whales realtime stream
        self.unusual_whales_stream = UnusualWhalesStream(rest_client=self.unusual_whales)

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
            unusual_whales_client=self.unusual_whales,
            unusual_whales_stream=self.unusual_whales_stream,
            human_intel_store=self.human_intel_store,
            watchlist_provider=self.watchlist,
            copy_trader_monitor=self.copy_trader_monitor,
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
        self.options_monitor = None
        options_enabled = getattr(settings, "OPTIONS_ENABLED", False)
        if options_enabled and self.alpaca_client:
            self.options_engine = OptionsEngine(
                api_key=settings.ALPACA_API_KEY,
                secret_key=settings.ALPACA_SECRET_KEY,
                base_url=getattr(settings, "ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
            )
            self.options_monitor = OptionsMonitor(self)
            logger.info("🎯 Options trading ENABLED")
        else:
            logger.info("Options trading disabled (set OPTIONS_ENABLED=true to enable)")
        if self.alpaca_client:
            self.reconciler = Reconciler(
                self.alpaca_client,
                entry_manager=self.entry_manager,
                options_engine=self.options_engine,
            )

        # Specialized Agent Orchestrator (new architecture)
        self.orchestrator = Orchestrator(
            broker=self.alpaca_client,
            entry_manager=self.entry_manager,
            risk_manager=self.risk_manager,
            fred_client=self.fred_client,
            finnhub_client=self.finnhub_client,
            human_intel_store=self.human_intel_store,
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
            "short_verdicts_blocked": 0,
            "last_short_block_reason": None,
            "last_uw_stream_signal": None,
        }

        # ── Fail-Closed Startup: broker is canonical ──────────────────
        self._broker_ready = False
        saved_positions = persistence.load_positions()
        broker_symbols = {p.get("symbol") for p in (self.entry_manager.get_positions() or [])}
        ghost_count = 0
        restored_count = 0
        if saved_positions:
            for sym, pos in saved_positions.items():
                if sym in broker_symbols:
                    if sym not in self.entry_manager.positions:
                        self.entry_manager.positions[sym] = pos
                        restored_count += 1
                else:
                    ghost_count += 1
                    logger.warning(f"GHOST POSITION REMOVED: {sym} on disk but not on broker — tombstoning")
                    try:
                        from src.data.entry_controls import tombstone_symbol
                        tombstone_symbol(sym, reason="ghost_position_startup_cleanup")
                    except Exception:
                        pass
            if restored_count:
                logger.info(f"Restored {restored_count} broker-confirmed positions from disk")
            if ghost_count:
                logger.warning(f"Tombstoned {ghost_count} ghost positions not found on broker")

        # Options positions: restore + reconcile with broker snapshot
        if self.options_engine:
            saved_options = persistence.load_options_positions()
            if saved_options:
                self.options_engine.load_positions(saved_options)
                logger.info(f"📦 Restored {len(saved_options)} options positions")
            try:
                recon = await asyncio.get_event_loop().run_in_executor(
                    None, self.options_engine.reconcile_with_broker
                )
                if recon.get("removed", 0) or recon.get("added", 0):
                    logger.info(
                        f"🔄 Options reconcile: removed={recon.get('removed', 0)} added={recon.get('added', 0)}"
                    )
            except Exception as e:
                logger.debug(f"Options reconcile failed: {e}")
            if self.risk_manager:
                self.risk_manager.update_options_exposure(self.options_engine.get_options_positions())

        # P&L state
        self.pnl_state = persistence.load_pnl_state()
        self.pnl_state.setdefault("options_total_realized_pnl", 0.0)
        self.pnl_state.setdefault("options_total_trades", 0)
        self.pnl_state.setdefault("options_winning_trades", 0)
        self.pnl_state.setdefault("options_losing_trades", 0)
        self._roll_daily_state_if_needed()

        # AI layer state
        saved_ai = persistence.load_ai_state()
        if saved_ai:
            self.ai_layers.update(saved_ai)

        # Dashboard
        start_dashboard(bot=self)

        # ── WebSocket streams ─────────────────────────────────────
        # Market data stream: real-time prices + breakout detection
        self.market_stream.set_breakout_callback(self._on_breakout_detected)
        self.market_stream.set_halt_callback(self._on_halt_status)
        self.market_stream.set_luld_callback(self._on_luld_status)
        await self.market_stream.start()

        # Trade updates stream: instant order fill detection
        self.trade_stream.set_fill_callback(self._on_trade_update_fill)
        self.trade_stream.set_stop_callback(self._on_trailing_stop_filled)
        await self.trade_stream.start()

        # Unusual Whales realtime stream: live flow alerts + dark pool prints
        self.unusual_whales_stream.set_signal_callback(self._on_unusual_whales_signal)
        await self.unusual_whales_stream.start()

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

        # Validate broker health before enabling entries
        try:
            _acct = await asyncio.get_event_loop().run_in_executor(None, self.alpaca_client.get_account)
            if _acct and _acct.get("equity"):
                self._broker_ready = True
                logger.success("Broker health validated — entries enabled")
            else:
                logger.error("Broker health check returned empty account — entries BLOCKED")
        except Exception as _be:
            logger.error(f"Broker health check failed: {_be} — entries BLOCKED until next cycle")

        persistence.clear_shutdown_marker()
        logger.success("All components initialized")

    async def run(self):
        """Main trading loop with AI layers running as concurrent tasks."""
        self.running = True
        self.start_time = time.time()
        logger.info("🚀 Velox LIVE")

        # Launch AI layers as background tasks
        ai_task = asyncio.create_task(self._ai_loop())
        options_task = asyncio.create_task(self._options_monitor_loop()) if self.options_engine else None

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
        last_reconciliation = 0

        try:
            while self.running:
                now = time.time()
                session_type = "regular"
                self._roll_daily_state_if_needed()

                # Sync equity from Alpaca every 60s
                if now - last_equity_sync >= 60 and self.alpaca_client:
                    last_equity_sync = now
                    try:
                        acct = self.alpaca_client.get_account()
                        self.risk_manager.update_equity(
                            acct.get("equity", self.risk_manager.equity),
                            daytrade_count=acct.get("daytrade_count"),
                        )
                        # Update open risk
                        positions = self.entry_manager.get_positions() if self.entry_manager else []
                        self.risk_manager.update_open_risk(positions)
                        if self.options_engine:
                            self.risk_manager.update_options_exposure(self.options_engine.get_options_positions())
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
                        session_type = "overnight"
                        # OVERNIGHT STRATEGY SESSION — formulate next day's plan
                        # But STILL monitor positions for protection
                        positions = self.entry_manager.get_positions()
                        if positions:
                            try:
                                await self._monitor_positions()
                            except Exception as e:
                                logger.debug(f"Overnight monitor error: {e}")
                        await self._overnight_session(et)
                        # Faster cycle during pre-market ramp (midnight-4AM ET)
                        if 0 <= et.hour < 4:
                            overnight_sleep = max(60, int(getattr(settings, "SCAN_INTERVAL_PREMARKET_SECONDS", 300)))
                        else:
                            overnight_sleep = max(60, int(getattr(settings, "SCAN_INTERVAL_OVERNIGHT_SECONDS", 600)))
                        await asyncio.sleep(overnight_sleep)
                        continue
                    # Extended hours: scan AND trade (earnings, FDA, filings drop in AH/PM)
                    session_type = "extended"
                    logger.debug(f"📡 Extended hours active ({et.strftime('%H:%M')} ET) — scanning + trading")

                if self.paused:
                    await asyncio.sleep(5)
                    continue

                # ── PERSIST STATE (every 30s) ──────────────────────
                if now - last_state_save >= 30:
                    last_state_save = now
                    persistence.save_positions(self.entry_manager.positions)
                    if self.options_engine:
                        persistence.save_options_positions(self.options_engine.positions)
                    persistence.save_ai_state(self.ai_layers)
                    persistence.save_pnl_state(self.pnl_state)

                if now - last_reconciliation >= 60 and self.reconciler:
                    last_reconciliation = now
                    try:
                        recon_state = self.reconciler.snapshot()
                        self.ai_layers["reconciliation"] = recon_state.get("reconciliation", {})
                        self.ai_layers["broker_truth"] = recon_state.get("broker", {})
                    except Exception as e:
                        logger.debug(f"Reconciliation snapshot error: {e}")

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
                        new_scan_interval = self._determine_scan_interval(effective_regime, session=session_type)
                        if (
                            raw_regime != self.scan_regime_raw
                            or effective_regime != self.scan_regime
                            or new_scan_interval != scan_interval
                        ):
                            logger.info(
                                f"⏱️ Adaptive scan cadence: session={session_type}, raw={raw_regime}, regime={effective_regime}, interval={new_scan_interval}s"
                            )
                            log_activity(
                                "scan",
                                f"Adaptive cadence: session={session_type}, raw={raw_regime}, regime={effective_regime}, interval={new_scan_interval}s",
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

                # ── COPY-TRADER EXIT SIGNALS ─────────────────────
                try:
                    await self._process_copy_trader_exit_signals()
                except Exception as e:
                    logger.error(f"Copy trader exit handling error: {e}")

                # ── UNUSUAL WHALES REALTIME SIGNALS ──────────────
                try:
                    await self._process_unusual_whales_signal_queue()
                except Exception as e:
                    logger.error(f"UW realtime handling error: {e}")
                if getattr(self, "unusual_whales_stream", None):
                    self.ai_layers["uw_stream"] = self.unusual_whales_stream.get_stats()
                if getattr(self, "unusual_whales", None):
                    self.ai_layers["uw_api"] = self.unusual_whales.get_usage_stats()

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

                # ── FAST-PATH SCOUT EVALUATION ────────────────────
                # Evaluate held scout positions on 5-second cadence.
                try:
                    await self._evaluate_fast_path_scouts()
                except Exception as e:
                    logger.error(f"Fast-path scout eval error: {e}")

                await asyncio.sleep(monitor_interval)

        except Exception as e:
            logger.error(f"Fatal error in main loop: {e}")
            raise
        finally:
            ai_task.cancel()
            if options_task:
                options_task.cancel()
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
          7. Pre-market ramp: intensify research as market open approaches
          8. Sunday night: Friday close analysis + weekend gap setup
        Runs every 5 min during overnight, but each task has its own throttle.
        Pre-market ramp (midnight-4AM ET) uses tighter intervals.
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
        last_premarket_scan = state.get("last_premarket_scan", 0)
        now = time.time()
        hour = et.hour

        # Pre-market ramp: midnight-4AM ET = tighter research intervals
        premarket_ramp = (0 <= hour < 4)
        # Sunday night = day of week 6 (Sunday) after 8PM ET, or Monday before 4AM ET
        is_sunday_night = (et.weekday() == 6 and hour >= 20) or (et.weekday() == 0 and hour < 4)

        # Dynamic throttles based on phase
        thesis_interval = 2 * 3600 if premarket_ramp else 8 * 3600  # 2h vs 8h
        news_interval = 30 * 60 if premarket_ramp else 2 * 3600     # 30m vs 2h

        tasks_run = []

        # ── SUNDAY NIGHT: Friday close + weekend gap analysis (once per weekend) ──
        last_sunday_analysis = state.get("last_sunday_analysis", 0)
        if is_sunday_night and (now - last_sunday_analysis > 12 * 3600):
            try:
                log_activity("research", "🗓️ Sunday night: Analyzing Friday close + weekend setup for Monday open...")
                sunday_thesis = await self._build_sunday_analysis()
                if sunday_thesis:
                    state["last_sunday_analysis"] = now
                    state["sunday_thesis"] = sunday_thesis
                    tasks_run.append("sunday_analysis")

                    # Add Sunday picks to watchlist
                    for pick in sunday_thesis.get("monday_watchlist", []):
                        ticker = pick.get("ticker", "")
                        if ticker:
                            side = "short" if pick.get("bias", "").lower() == "bearish" else "long"
                            self.watchlist.add(
                                ticker, side=side,
                                conviction=min(0.9, pick.get("conviction", 0.5)),
                                source="sunday_analysis",
                                reason=pick.get("reason", "Monday open setup")[:80],
                            )

                    bias = sunday_thesis.get("market_bias", "?")
                    gap = sunday_thesis.get("expected_gap", "?")
                    count = len(sunday_thesis.get("monday_watchlist", []))
                    logger.success(f"🗓️ Sunday analysis: bias={bias}, expected_gap={gap}, {count} Monday plays")
                    log_activity("research", f"🗓️ Monday outlook: {bias} bias, gap {gap} | {count} tickers staged")

                    # Log top picks
                    for pick in sunday_thesis.get("monday_watchlist", [])[:5]:
                        log_activity("research", f"  📌 {pick.get('ticker','?')}: {pick.get('reason','')[:300]}")
            except Exception as e:
                logger.debug(f"Sunday analysis failed: {e}")

        # ── PRE-MARKET RAMP: Overnight movers + futures check (every 30 min, midnight-4AM) ──
        if premarket_ramp and (now - last_premarket_scan > 30 * 60):
            try:
                log_activity("research", f"🌅 Pre-market ramp ({et.strftime('%H:%M')} ET): Scanning overnight movers + futures...")
                premarket_intel = await self._scan_premarket_movers()
                if premarket_intel:
                    state["last_premarket_scan"] = now
                    tasks_run.append("premarket_scan")

                    # Add AH/PM movers to watchlist
                    for mover in premarket_intel.get("movers", []):
                        ticker = mover.get("ticker", "")
                        if ticker:
                            side = "short" if mover.get("direction") == "down" else "long"
                            self.watchlist.add(
                                ticker, side=side,
                                conviction=min(0.85, mover.get("conviction", 0.5)),
                                source="premarket_scan",
                                reason=mover.get("reason", "overnight mover")[:80],
                            )

                    futures = premarket_intel.get("futures_signal", "neutral")
                    mover_count = len(premarket_intel.get("movers", []))
                    log_activity("research", f"🌅 Futures: {futures} | {mover_count} overnight movers identified")
            except Exception as e:
                logger.debug(f"Pre-market scan failed: {e}")

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

        # ── WATCHLIST + THESIS: Full overnight research (dynamic interval) ──
        # Pre-market ramp: every 2h. Normal overnight: every 8h after 10PM ET.
        thesis_ready = (premarket_ramp and (now - last_thesis > thesis_interval)) or \
                       (not premarket_ramp and hour >= 22 and (now - last_thesis > thesis_interval))
        if thesis_ready:
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

                # 5f. Insider cluster buys via Unusual Whales
                insider_signals = []
                if self.unusual_whales and self.unusual_whales.is_configured():
                    try:
                        insider_trades = await asyncio.get_event_loop().run_in_executor(
                            None, self.unusual_whales.get_insider_trades, None, 100
                        )
                        by_ticker = {}
                        for trade in insider_trades or []:
                            ticker = str(trade.get("ticker", "")).upper().strip()
                            if not ticker:
                                continue
                            bucket = by_ticker.setdefault(ticker, {"ticker": ticker, "buy_count": 0, "buy_value": 0.0})
                            if trade.get("transaction") == "buy":
                                bucket["buy_count"] += 1
                                bucket["buy_value"] += float(trade.get("value", 0) or 0)
                        insider_signals = sorted(
                            [row for row in by_ticker.values() if row["buy_count"] >= 2],
                            key=lambda row: (row["buy_count"], row["buy_value"]),
                            reverse=True,
                        )
                        for sig in insider_signals[:5]:
                            conviction = min(0.8, 0.45 + 0.1 * min(sig["buy_count"], 3))
                            self.watchlist.add(
                                sig["ticker"],
                                side="long",
                                conviction=conviction,
                                source="insider",
                                reason=f"{sig['buy_count']} insider buys (${sig['buy_value']:,.0f})",
                            )
                        if insider_signals:
                            logger.info(f"👔 Insider buys: {', '.join(s['ticker'] for s in insider_signals[:5])}")
                            log_activity("research", f"👔 Insider buying: {', '.join(s['ticker'] for s in insider_signals[:5])}")
                    except Exception as e:
                        logger.debug(f"Insider trades scan failed: {e}")

                # 5g. ARK daily trades (next-day watchlist signal)
                ark_buy_signals = []
                ark_sell_signals = []
                if self.ark_trades:
                    try:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self.ark_trades.get_recent_trades
                        )
                        ark_buy_signals = self.ark_trades.get_buy_signals()
                        ark_sell_signals = self.ark_trades.get_sell_signals()
                        for sig in ark_buy_signals[:8]:
                            self.watchlist.add(
                                sig["ticker"],
                                side="long",
                                conviction=sig.get("conviction", 0.4),
                                source="ark_buy",
                                reason=sig.get("reason", "ARK buy signal"),
                            )
                        for sig in ark_sell_signals[:5]:
                            self.watchlist.add(
                                sig["ticker"],
                                side="short",
                                conviction=max(0.3, sig.get("conviction", 0.35) - 0.05),
                                source="ark_sell",
                                reason=sig.get("reason", "ARK sell signal"),
                            )
                        if ark_buy_signals or ark_sell_signals:
                            logger.info(
                                f"🏛️ ARK trades: {len(ark_buy_signals)} buys, {len(ark_sell_signals)} sells"
                            )
                            leaders = [sig["ticker"] for sig in (ark_buy_signals[:3] + ark_sell_signals[:2])]
                            log_activity("research", f"🏛️ ARK trades: {', '.join(leaders)}")
                    except Exception as e:
                        logger.debug(f"ARK trades scan failed: {e}")

                # 5g. Finnhub macro calendar + IPO calendar
                economic_calendar = {}
                ipo_calendar = []
                if self.finnhub_client and self.finnhub_client.is_configured():
                    try:
                        economic_calendar = await asyncio.get_event_loop().run_in_executor(
                            None, self.finnhub_client.summarize_economic_calendar, 7
                        )
                        if economic_calendar.get("events"):
                            state["economic_calendar"] = economic_calendar.get("events", [])
                            logger.info(
                                "🗓️ Macro calendar: "
                                + ", ".join(event.get("event", "") for event in economic_calendar.get("events", [])[:3])
                            )
                            log_activity("research", f"🗓️ Macro calendar: {economic_calendar.get('summary', '')}")
                    except Exception as e:
                        logger.debug(f"Finnhub economic calendar failed: {e}")
                    try:
                        ipo_calendar = await asyncio.get_event_loop().run_in_executor(
                            None, self.finnhub_client.get_ipo_calendar
                        )
                        for ipo in ipo_calendar[:8]:
                            self.watchlist.add(
                                ipo["symbol"],
                                side="long",
                                conviction=0.35,
                                source="ipo_calendar",
                                reason=f"IPO watch: {ipo.get('name', ipo['symbol'])} listing {ipo.get('date', '')}",
                            )
                        if ipo_calendar:
                            logger.info(f"🆕 IPO calendar: {', '.join(ipo['symbol'] for ipo in ipo_calendar[:5])}")
                            log_activity("research", f"🆕 IPO watch: {', '.join(ipo['symbol'] for ipo in ipo_calendar[:5])}")
                    except Exception as e:
                        logger.debug(f"Finnhub IPO calendar failed: {e}")

                # 6. REBUILD WATCHLIST from all sources
                self.watchlist.rebuild_overnight(
                    stocktwits_trending=stocktwits_data,
                    twitter_mentions=twitter_data,
                    perplexity_picks=perplexity_picks,
                    pharma_catalysts=pharma_signals,
                    fade_candidates=fade_candidates,
                )

                # 6b. Operator-guided context
                if self.human_intel_store:
                    human_candidates = self.human_intel_store.get_watchlist_candidates(limit=12)
                    for intel in human_candidates:
                        ticker = intel.get("ticker", "")
                        if not ticker:
                            continue
                        side = "short" if intel.get("bias") == "bearish" else "long"
                        conviction = min(0.95, 0.35 + float(intel.get("avg_confidence", 0.5) or 0.5) * 0.5)
                        self.watchlist.add(
                            ticker,
                            side=side,
                            conviction=conviction,
                            source="human_intel",
                            reason=intel.get("summary", "operator context"),
                        )
                    if human_candidates:
                        logger.info(f"🧠 Human intel watchlist: {', '.join(i['ticker'] for i in human_candidates[:5])}")
                        log_activity("research", f"🧠 Human intel: {', '.join(i['ticker'] for i in human_candidates[:5])}")

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

                # ── POPULATE SCANNER CANDIDATES from watchlist for dashboard ──
                # During overnight, scanner.scan() doesn't run, so the dashboard
                # shows "No candidates yet". Feed watchlist items into scanner cache
                # so the operator can see what the bot is researching.
                try:
                    watchlist_items = self.watchlist.get_all()
                    if watchlist_items and self.scanner:
                        dashboard_candidates = []
                        for item in watchlist_items[:20]:
                            ticker = item.get("ticker", "")
                            # Try to get price data from the snapshot we already fetched
                            snap = locals().get("price_snaps", {}).get(ticker, {})
                            dashboard_candidates.append({
                                "symbol": ticker,
                                "price": snap.get("price", 0),
                                "change_pct": snap.get("change_pct", 0),
                                "volume_spike": 0,
                                "sentiment_score": item.get("conviction", 0),
                                "score": item.get("conviction", 0),
                                "source": item.get("sources", "overnight"),
                                "side": item.get("side", "long"),
                                "reason": item.get("reason", "")[:80],
                            })
                        self.scanner._cache = dashboard_candidates
                        logger.info(f"📊 Dashboard candidates: {len(dashboard_candidates)} from overnight watchlist")
                except Exception as e:
                    logger.debug(f"Dashboard candidate sync failed: {e}")

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
                        log_activity("research", f"📋 SEC {form}: {ticker} — {f.get('description', '')[:300]}")
                        # Add 8-K filers to watchlist as potential catalysts
                        if form == "8-K" and ticker:
                            self.watchlist.add(ticker, side="long", conviction=0.5,
                                              source="edgar", reason=f"8-K filing: {f.get('description', '')[:50]}")
                        if form == "4" and ticker:
                            insider = await self.edgar_scanner.get_insider_trades(ticker, filings=[f])
                            signal = insider.get("signal")
                            if signal in ("bullish", "bearish"):
                                self.watchlist.add(
                                    ticker,
                                    side="long" if signal == "bullish" else "short",
                                    conviction=0.45 if signal == "bullish" else 0.4,
                                    source="edgar_form4",
                                    reason=insider.get("summary", "Form 4 insider activity"),
                                )
            except Exception as e:
                logger.debug(f"EDGAR scan failed: {e}")

        # ── NEWS: Scan overnight news (30min during pre-market ramp, 2h otherwise) ──
        if now - last_news > news_interval:
            try:
                news = await self._scan_overnight_news()
                if news:
                    state["last_news_scan"] = now
                    state["overnight_news"] = news[:5]
                    tasks_run.append("news")
                    for headline in news[:3]:
                        log_activity("research", f"📰 {headline[:300]}")
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
            phase = "pre-market ramp 🌅" if premarket_ramp else ("sunday prep 🗓️" if is_sunday_night else "overnight")
            next_news_min = max(0, int(news_interval - (now - last_news)) // 60)
            next_thesis_min = max(0, int(thesis_interval - (now - last_thesis)) // 60)
            log_activity("thinking", f"{phase} ({et.strftime('%H:%M')} ET) — news in {next_news_min}m, thesis in {next_thesis_min}m")
            logger.debug(f"🌙 {phase} ({et.strftime('%H:%M')} ET) — news in {next_news_min}m, thesis refresh in {next_thesis_min}m")

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

    async def _build_sunday_analysis(self) -> dict:
        """Sunday night special: Analyze Friday's close, weekend news, and Monday setup."""
        import httpx

        pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if not pplx_key:
            return {}

        try:
            async with httpx.AsyncClient(timeout=45) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {pplx_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro'),
                        "max_tokens": 2000,
                        "messages": [{"role": "user", "content":
                            "It's Sunday night. I need a comprehensive Monday stock market prep. Analyze: "
                            "1. FRIDAY CLOSE: How did major indices close? What sectors led/lagged? Any notable Friday selloff or rally? "
                            "2. FRIDAY AFTER-HOURS: Any earnings beats/misses after Friday close? Major AH movers? "
                            "3. WEEKEND NEWS: Geopolitics, Fed commentary, economic data, corporate news over the weekend "
                            "4. FUTURES/CRYPTO: Current S&P/Nasdaq futures direction, Bitcoin/crypto moves as risk sentiment proxy "
                            "5. MONDAY CATALYSTS: Earnings before open, economic data releases, FDA decisions, IPOs "
                            "6. GAP ANALYSIS: Which stocks are likely to gap up/down Monday based on AH + weekend news? "
                            "7. TOP 10 MONDAY PLAYS: Specific tickers with entry thesis (momentum runners, gap fills, earnings reactions, sector rotations) "
                            "Format as JSON with keys: friday_close_summary, ah_movers (array of {ticker, change_pct, reason}), "
                            "weekend_catalysts, futures_signal (bullish/bearish/neutral), expected_gap (up/down/flat), "
                            "market_bias, monday_watchlist (array of {ticker, bias (bullish/bearish), conviction (0-1), reason})"}],
                    },
                )
                resp.raise_for_status()
                text = resp.json()["choices"][0]["message"]["content"]

                import re, json as _json
                json_match = re.search(r'\{.*\}', text, re.DOTALL)
                if json_match:
                    return _json.loads(json_match.group())
                else:
                    return {"raw_analysis": text, "market_bias": "unknown", "monday_watchlist": []}
        except Exception as e:
            logger.debug(f"Sunday analysis failed: {e}")
            return {}

    async def _scan_premarket_movers(self) -> dict:
        """Pre-market ramp: scan for overnight movers, futures, and early pre-market activity."""
        import httpx

        result = {"movers": [], "futures_signal": "neutral"}

        # 1. Check Alpaca for any AH/PM price moves on our watchlist
        try:
            all_tickers = self.watchlist.get_tickers()
            if all_tickers:
                import requests as _req
                _headers = {
                    'APCA-API-KEY-ID': settings.ALPACA_API_KEY,
                    'APCA-API-SECRET-KEY': settings.ALPACA_SECRET_KEY,
                }
                syms = ','.join(all_tickers[:50])
                _r = _req.get(
                    f'https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}&feed=iex',
                    headers=_headers, timeout=10
                )
                if _r.status_code == 200:
                    raw_snaps = _r.json()
                    for sym, data in raw_snaps.items():
                        lt = data.get('latestTrade', {})
                        pb = data.get('prevDailyBar', {})
                        price = lt.get('p', 0)
                        prev = pb.get('c', 0)
                        if prev > 0 and price > 0:
                            chg_pct = (price - prev) / prev * 100
                            if abs(chg_pct) >= 3.0:  # 3%+ movers
                                direction = "up" if chg_pct > 0 else "down"
                                result["movers"].append({
                                    "ticker": sym,
                                    "change_pct": round(chg_pct, 1),
                                    "direction": direction,
                                    "conviction": min(0.8, 0.4 + abs(chg_pct) / 20),
                                    "reason": f"Overnight {direction} {abs(chg_pct):.1f}% (${price:.2f} vs prev ${prev:.2f})",
                                })
                    result["movers"].sort(key=lambda x: abs(x["change_pct"]), reverse=True)
                    if result["movers"]:
                        logger.info(f"🌅 Pre-market movers: {', '.join(m['ticker'] + ' ' + str(m['change_pct']) + '%' for m in result['movers'][:5])}")
        except Exception as e:
            logger.debug(f"Pre-market price scan failed: {e}")

        # 2. Get futures/macro direction from Perplexity
        pplx_key = getattr(settings, 'PERPLEXITY_API_KEY', None)
        if pplx_key:
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
                                "Quick pre-market check: What are S&P 500 futures, Nasdaq futures, and Bitcoin doing right now? "
                                "Any breaking overnight news? Give me the direction (bullish/bearish/neutral) and top 3 stocks "
                                "with unusual pre-market volume or big moves. Keep it brief, one paragraph."}],
                        },
                    )
                    resp.raise_for_status()
                    text = resp.json()["choices"][0]["message"]["content"]
                    if "bullish" in text.lower():
                        result["futures_signal"] = "bullish"
                    elif "bearish" in text.lower():
                        result["futures_signal"] = "bearish"
                    result["futures_summary"] = text[:300]
                    log_activity("research", f"🌅 Futures: {text[:300]}")
            except Exception as e:
                logger.debug(f"Pre-market futures check failed: {e}")

        return result

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
                    log_activity("ai", f"🔭 Observer: {obs_text[:300]}")

                # Advisor (every 30 min)
                adv = await self.advisor.run(self, self.observer.get_last_output())
                if adv:
                    adv_text = adv.get("strategy", str(adv)[:200])
                    self.ai_layers["last_advice"] = adv_text
                    log_activity("ai", f"🎯 Advisor: {adv_text[:300]}")

                exit_agent = getattr(getattr(self, "orchestrator", None), "exit_agent", None)
                if self.advisor and exit_agent and self.entry_manager:
                    try:
                        await exit_agent._check_advisor_recommendations(
                            self.entry_manager.get_positions(),
                            self.advisor,
                        )
                    except Exception as e:
                        logger.debug(f"Advisor exit check failed: {e}")

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
                pm = await self.position_manager.run(self, self.advisor.get_last_output())
                if pm:
                    health = pm.get("portfolio_health", "healthy")
                    exits = len(pm.get("emergency_exits", [])) + len(pm.get("strategic_exits", []))
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

    def _load_tomorrow_thesis(self) -> Dict:
        now = time.time()
        if self._tomorrow_thesis_cache is not None and (now - self._tomorrow_thesis_cache_at) < 300:
            return dict(self._tomorrow_thesis_cache)

        thesis_file = Path(__file__).parent.parent / "data" / "tomorrow_thesis.json"
        thesis: Dict = {}
        try:
            if thesis_file.exists():
                thesis = json.loads(thesis_file.read_text())
        except Exception as e:
            logger.debug(f"Tomorrow thesis load failed: {e}")
            thesis = {}

        self._tomorrow_thesis_cache = dict(thesis)
        self._tomorrow_thesis_cache_at = now
        return dict(thesis)

    @staticmethod
    def _candidate_has_uw_confirmation(candidate: Dict, direction: str) -> bool:
        biases = score_directional_biases(
            [
                candidate.get("uw_flow_sentiment"),
                candidate.get("uw_recent_flow_bias"),
                candidate.get("uw_net_premium_bias"),
                candidate.get("uw_options_volume_bias"),
                candidate.get("uw_chain_bias"),
                candidate.get("uw_news_bias"),
                candidate.get("market_tide_bias"),
            ]
        )
        if str(direction or "BUY").upper() == "SHORT":
            return biases["bearish"] >= 2 and biases["bearish"] > biases["bullish"]
        return biases["bullish"] >= 2 and biases["bullish"] > biases["bearish"]

    def _evaluate_trade_gate(self, candidate: Dict, direction: str) -> Dict:
        annotated = annotate_candidate(candidate)
        strategy_tag = str(
            annotated.get("strategy_tag")
            or self._derive_strategy_tag(annotated, direction)
            or "unknown"
        )
        annotated["strategy_tag"] = strategy_tag
        annotated.update(annotate_candidate(annotated))

        regime = str(
            annotated.get("market_regime")
            or getattr(self, "scan_regime", "")
            or getattr(self, "scan_regime_raw", "")
            or "mixed"
        ).lower()
        if not annotated.get("playbook_live", False):
            return {"allowed": False, "reason": "playbook_disabled", "candidate": annotated}

        allowed_regimes = {
            str(r).strip().lower() for r in (annotated.get("playbook_allowed_regimes") or []) if str(r).strip()
        }
        if allowed_regimes and regime not in allowed_regimes:
            return {"allowed": False, "reason": "regime_block", "candidate": annotated}

        raw_signal_ts = annotated.get("signal_timestamp", None)
        signal_ts = None if raw_signal_ts in (None, "") else float(raw_signal_ts)
        signal_age = None if signal_ts is None else max(0.0, time.time() - signal_ts)
        min_signal_age = max(0, int(annotated.get("playbook_min_signal_age_seconds", 0) or 0))
        if signal_age is not None and signal_age < min_signal_age:
            return {"allowed": False, "reason": "awaiting_confirmation", "candidate": annotated}

        if annotated.get("playbook_requires_uw_confirmation") and not self._candidate_has_uw_confirmation(annotated, direction):
            return {"allowed": False, "reason": "uw_unconfirmed", "candidate": annotated}

        thesis_mode = str(annotated.get("playbook_thesis_mode", "intraday") or "intraday").lower()
        if thesis_mode == "required":
            thesis = self._load_tomorrow_thesis()
            bias = normalize_bias_label(thesis.get("market_bias"))
            watchlist = extract_watchlist_symbols(thesis)
            if bias == "unknown" and not watchlist:
                return {"allowed": False, "reason": "thesis_not_actionable", "candidate": annotated}
            if annotated.get("playbook_watchlist_only") and watchlist:
                if str(annotated.get("symbol", "")).upper() not in watchlist:
                    return {"allowed": False, "reason": "not_in_watchlist", "candidate": annotated}
            if bias in ("bullish", "bearish") and not bias_matches_direction(bias, direction):
                return {"allowed": False, "reason": "thesis_bias_conflict", "candidate": annotated}

        return {"allowed": True, "reason": "ok", "candidate": annotated}

    def _determine_options_allocation_pct(self, candidate: Dict, direction: str, confidence: float) -> float:
        annotated = annotate_candidate(candidate)
        strategy_tag = str(
            annotated.get("strategy_tag")
            or self._derive_strategy_tag(annotated, direction)
            or "unknown"
        )
        annotated["strategy_tag"] = strategy_tag
        annotated.update(annotate_candidate(annotated))

        options_mode = str(annotated.get("playbook_options_mode", "off") or "off").lower()
        if options_mode != "prefer":
            return 0.0
        if not self._candidate_has_uw_confirmation(annotated, direction):
            return 0.0

        base_pct = float(getattr(settings, "OPTIONS_ALLOCATION_PCT", 50) or 50)
        confidence = float(confidence or 0)
        if confidence >= 85:
            return base_pct
        if confidence >= 75:
            return base_pct * 0.5
        return 0.0

    @staticmethod
    def _determine_scan_interval(regime: str, session: str = "regular") -> int:
        """
        Adaptive scan cadence by regime:
          risk_on / risk_off -> fast
          choppy             -> slow
          mixed              -> baseline
        """
        fast = max(15, int(getattr(settings, "SCAN_INTERVAL_FAST_SECONDS", 60)))
        slow = max(fast, int(getattr(settings, "SCAN_INTERVAL_SLOW_SECONDS", 300)))
        baseline = max(fast, int(settings.SCAN_INTERVAL_SECONDS))

        interval = baseline
        if regime in ("risk_on", "risk_off"):
            interval = fast
        elif regime == "choppy":
            interval = slow

        session_name = (session or "regular").lower()
        if session_name == "extended":
            extended_floor = max(fast, int(getattr(settings, "SCAN_INTERVAL_EXTENDED_SECONDS", 300)))
            return max(interval, extended_floor)
        if session_name == "overnight":
            overnight_floor = max(fast, int(getattr(settings, "SCAN_INTERVAL_OVERNIGHT_SECONDS", 600)))
            return max(interval, overnight_floor)
        return interval

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

    def _get_operating_guardrails(self) -> dict:
        risk_status = self.risk_manager.get_status() if self.risk_manager and hasattr(self.risk_manager, "get_status") else {}
        reconciliation_state = {}
        if getattr(self, "reconciler", None):
            try:
                reconciliation_state = self.reconciler.snapshot()
            except Exception:
                reconciliation_state = {}
        recon = (reconciliation_state.get("reconciliation", {}) or {}) if isinstance(reconciliation_state, dict) else {}
        trust = (reconciliation_state.get("trust", {}) or {}) if isinstance(reconciliation_state, dict) else {}
        positions = self.entry_manager.get_positions() if getattr(self, "entry_manager", None) else []
        unprotected = []
        protection_failed = []
        for pos in positions or []:
            symbol = str(pos.get("symbol", "") or "")
            if pos.get("protection_failed"):
                protection_failed.append(symbol)
            if not pos.get("has_trailing_stop") and not pos.get("swing_only"):
                unprotected.append(symbol)
        reasons = []
        allow_new_entries = True
        if recon.get("status") == "critical_mismatch" or trust.get("broker_only_mode"):
            allow_new_entries = False
            reasons.append("critical_reconciliation")
        if protection_failed:
            allow_new_entries = False
            reasons.append("protection_failed")
        if len(unprotected) > 0:
            allow_new_entries = False
            reasons.append("unprotected_positions")
        if risk_status.get("trading_halted"):
            allow_new_entries = False
            reasons.append("risk_halted")
        return {
            "allow_new_entries": allow_new_entries,
            "reconciliation_status": recon.get("status", "unknown"),
            "broker_only_mode": bool(trust.get("broker_only_mode")),
            "unprotected_symbols": unprotected,
            "protection_failed_symbols": protection_failed,
            "reasons": reasons,
        }

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

    def _prune_uw_signal_dedupe(self):
        cutoff = time.time() - max(
            60,
            int(getattr(settings, "UW_STREAM_SIGNAL_WINDOW_SECONDS", 300) or 300),
        )
        self._recent_uw_signal_keys = {
            key: ts for key, ts in getattr(self, "_recent_uw_signal_keys", {}).items() if ts >= cutoff
        }

    @staticmethod
    def _summarize_uw_flow_event(event: Dict) -> str:
        premium = float(event.get("premium", 0.0) or 0.0)
        sentiment = str(event.get("sentiment", "neutral") or "neutral")
        option_type = str(event.get("type", "unknown") or "unknown")
        contract = str(event.get("contract_symbol", "") or "")
        return (
            f"live UW flow {sentiment} {option_type} "
            f"${premium:,.0f} {contract}".strip()
        )

    @staticmethod
    def _summarize_uw_dark_pool_event(event: Dict) -> str:
        premium = float(event.get("premium", 0.0) or 0.0)
        price = float(event.get("price", 0.0) or 0.0)
        size = float(event.get("size", 0.0) or 0.0)
        sentiment = str(event.get("sentiment", "neutral") or "neutral")
        return (
            f"live UW dark pool {sentiment} ${premium:,.0f} "
            f"({size:,.0f} @ ${price:.2f})"
        )

    @staticmethod
    def _build_uw_signal_key(symbol: str, event_type: str, side: str) -> str:
        return f"{str(symbol).upper()}:{event_type}:{side}"

    def _build_uw_candidate(self, event: Dict) -> Dict:
        symbol = str(event.get("ticker", "") or event.get("symbol", "") or "").upper().strip()
        if not symbol:
            return {}

        event_type = str(event.get("event_type", "") or "")
        signal_price = float(event.get("underlying_price") or event.get("price") or 0.0)
        premium = float(event.get("premium", 0.0) or 0.0)

        if event_type == "flow_alert":
            if premium < float(getattr(settings, "UW_STREAM_MIN_FLOW_PREMIUM", 100000.0) or 100000.0):
                return {}
            side = "short" if str(event.get("sentiment", "")).lower() == "bearish" else "long"
            sentiment_score = -0.55 if side == "short" else 0.65
            context = self._summarize_uw_flow_event(event)
            return {
                "symbol": symbol,
                "price": signal_price,
                "change_pct": 0.0,
                "volume": float(event.get("volume", 0.0) or 0.0),
                "source": "unusual_whales_stream",
                "side": side,
                "sentiment_score": sentiment_score,
                "score": 1.0,
                "priority": 1,
                "signal_timestamp": time.time(),
                "signal_sources": ["unusual_whales", "unusual_whales_stream"],
                "uw_flow_sentiment": event.get("sentiment", "neutral"),
                "uw_total_premium": premium,
                "uw_flow_alerts": 1,
                "unusual_options": context,
                "uw_stream_channel": event.get("stream_channel", "flow-alerts"),
                "uw_stream_event": dict(event),
            }

        if event_type == "dark_pool":
            if premium < float(getattr(settings, "UW_STREAM_MIN_DARK_POOL_PREMIUM", 250000.0) or 250000.0):
                return {}
            sentiment = str(event.get("sentiment", "neutral") or "neutral").lower()
            if sentiment == "neutral":
                return {}
            side = "short" if sentiment == "bearish" else "long"
            sentiment_score = -0.45 if side == "short" else 0.45
            context = self._summarize_uw_dark_pool_event(event)
            return {
                "symbol": symbol,
                "price": signal_price,
                "change_pct": 0.0,
                "volume": float(event.get("size", 0.0) or 0.0),
                "source": "unusual_whales_stream",
                "side": side,
                "sentiment_score": sentiment_score,
                "score": 0.9,
                "priority": 1,
                "signal_timestamp": time.time(),
                "signal_sources": ["unusual_whales", "unusual_whales_stream"],
                "uw_dark_pool_bias": sentiment,
                "dark_pool": context,
                "uw_stream_channel": event.get("stream_channel", "off_lit_trades"),
                "uw_stream_event": dict(event),
            }

        return {}

    async def _on_unusual_whales_signal(self, event: Dict):
        event_type = str(event.get("event_type", "") or "")
        if event_type == "market_tide":
            return

        candidate = self._build_uw_candidate(event)
        if not candidate:
            return

        symbol = candidate["symbol"]
        side = candidate.get("side", "long")
        dedupe_key = self._build_uw_signal_key(symbol, event_type, side)
        self._prune_uw_signal_dedupe()
        if dedupe_key in self._recent_uw_signal_keys:
            return
        self._recent_uw_signal_keys[dedupe_key] = time.time()

        queue = getattr(self, "_uw_signal_queue", None)
        if not queue:
            return
        try:
            queue.put_nowait(candidate)
        except asyncio.QueueFull:
            logger.debug(f"UW signal queue full — dropping {symbol} {event_type}")
            return

        summary = candidate.get("unusual_options") or candidate.get("dark_pool") or event_type
        self.ai_layers["last_uw_stream_signal"] = f"{symbol} {summary}"
        logger.info(f"🐋 Queued UW realtime candidate: {symbol} {event_type} {side}")
        log_activity(
            "scan",
            f"🐋 UW realtime: {symbol} {side} — {summary}",
            {"symbol": symbol, "event_type": event_type, "side": side},
        )

    async def _process_unusual_whales_signal_queue(self):
        queue = getattr(self, "_uw_signal_queue", None)
        if not queue or queue.empty():
            return
        if not self.risk_manager.can_trade():
            return

        processed = 0
        while not queue.empty() and processed < 3:
            try:
                candidate = queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            symbol = str(candidate.get("symbol", "") or "").upper()
            held_symbols = {p.get("symbol") for p in self.entry_manager.get_positions()}
            if symbol in held_symbols:
                continue

            logger.info(
                f"🐋 Processing UW realtime candidate: {symbol} "
                f"({candidate.get('side', 'long')}, src={candidate.get('uw_stream_channel', '?')})"
            )
            await self._process_candidates([candidate])
            processed += 1

    @staticmethod
    def _parse_iso_ts(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _current_trading_day() -> str:
        try:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")
            return datetime.now(et).strftime("%Y-%m-%d")
        except Exception:
            try:
                from pytz import timezone as tz
                return datetime.now(tz("US/Eastern")).strftime("%Y-%m-%d")
            except Exception:
                return datetime.now().strftime("%Y-%m-%d")

    def _roll_daily_state_if_needed(self):
        """Reset per-day P&L and risk stats once when trading day changes."""
        if not isinstance(getattr(self, "pnl_state", None), dict):
            return

        today = self._current_trading_day()
        if self._last_daily_reset_date == today and self.pnl_state.get("today_date") == today:
            return

        if self.pnl_state.get("today_date") != today:
            self.pnl_state["today_realized_pnl"] = 0.0
            self.pnl_state["today_date"] = today
            if self.risk_manager:
                self.risk_manager.reset_daily()
            try:
                persistence.save_pnl_state(self.pnl_state)
            except Exception:
                pass
            logger.info(f"📅 Daily trading state rolled to {today}")

        self._last_daily_reset_date = today

    @staticmethod
    def _compute_signal_latency_fields(position: dict) -> dict:
        signal_ts = position.get("signal_timestamp")
        order_ts = position.get("entry_order_timestamp")
        fill_ts = position.get("fill_timestamp")
        signal_to_order_ms = None
        signal_to_fill_ms = None
        try:
            if signal_ts is not None and order_ts is not None:
                signal_to_order_ms = max(0, int((float(order_ts) - float(signal_ts)) * 1000))
        except Exception:
            signal_to_order_ms = None
        try:
            if signal_ts is not None and fill_ts is not None:
                signal_to_fill_ms = max(0, int((float(fill_ts) - float(signal_ts)) * 1000))
        except Exception:
            signal_to_fill_ms = None
        return {
            "signal_timestamp": signal_ts,
            "entry_order_timestamp": order_ts,
            "fill_timestamp": fill_ts,
            "fill_timestamp_source": position.get("fill_timestamp_source", "unknown"),
            "signal_to_order_ms": signal_to_order_ms,
            "signal_to_fill_ms": signal_to_fill_ms,
        }

    @staticmethod
    def _directional_move_pct(position: dict, current_price: float) -> float:
        entry_price = float(position.get("entry_price", 0) or 0)
        if entry_price <= 0 or current_price <= 0:
            return 0.0
        if position.get("side", "long") == "short":
            return ((entry_price - current_price) / entry_price) * 100.0
        return ((current_price - entry_price) / entry_price) * 100.0

    def _update_position_trade_telemetry(
        self,
        position: dict,
        current_price: float,
        now_ts: Optional[float] = None,
    ):
        if not position:
            return
        entry_price = float(position.get("entry_price", 0) or 0)
        entry_time = float(position.get("entry_time", 0) or 0)
        current_price = float(current_price or 0)
        if entry_price <= 0 or entry_time <= 0 or current_price <= 0:
            return

        if now_ts is None:
            now_ts = time.time()
        elapsed = max(0.0, float(now_ts) - entry_time)
        move_pct = self._directional_move_pct(position, current_price)
        position["current_price"] = current_price

        for seconds, field in ((60, "price_at_1m"), (180, "price_at_3m"), (300, "price_at_5m")):
            if elapsed >= seconds and position.get(field) is None:
                position[field] = current_price

        if move_pct > 0 and position.get("time_to_green_seconds") is None:
            position["time_to_green_seconds"] = int(round(elapsed))

        current_mfe = position.get("mfe_pct")
        if current_mfe is None or move_pct > float(current_mfe):
            position["mfe_pct"] = round(move_pct, 4)
            position["time_to_peak_seconds"] = int(round(elapsed))

        current_mae = position.get("mae_pct")
        if current_mae is None or move_pct < float(current_mae):
            position["mae_pct"] = round(move_pct, 4)

    @staticmethod
    def _merge_anomaly_flags(*sources) -> list:
        merged = []
        seen = set()
        for source in sources:
            values = source
            if isinstance(values, str):
                values = [v.strip() for v in values.split(",") if v.strip()]
            if not isinstance(values, list):
                continue
            for flag in values:
                key = str(flag or "").strip()
                if not key or key in seen:
                    continue
                seen.add(key)
                merged.append(key)
        return merged

    def _passes_fast_path_deterministic_screen(self, symbol: str, price: float, pct_change: float, volume_spike: float):
        if not getattr(settings, "FAST_PATH_ENABLED", True):
            return False, "disabled"
        if (
            self.risk_manager
            and getattr(settings, "SWING_MODE_DISABLE_FAST_PATH", True)
            and self.risk_manager.is_swing_mode()
        ):
            return False, "swing_mode_disabled"
        if symbol in self._fast_path_pending:
            return False, "already_pending"
        self._prune_jury_vetoes()
        jury_vetoes = getattr(self, "_jury_vetoed_symbols", {})
        vetoed_at = jury_vetoes.get(symbol)
        if vetoed_at and (time.time() - float(vetoed_at)) < 3600:
            return False, "jury_vetoed"
        if price <= 0 or price < 5 or price > 500:
            return False, "price_out_of_range"
        min_change = float(getattr(settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0))
        if pct_change < min_change:
            return False, "insufficient_change"
        min_vol_spike = float(getattr(settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0))
        if volume_spike < min_vol_spike:
            return False, "insufficient_volume"

        positions = self.entry_manager.get_positions() if self.entry_manager else []
        held_symbols = {p.get("symbol") for p in positions}
        if symbol in held_symbols:
            return False, "already_held"

        if self.risk_manager:
            if self.risk_manager.is_wash_sale(symbol):
                return False, "wash_sale"
            if not self.risk_manager.can_open_position(positions, symbol=symbol):
                return False, "risk_open_position_block"
            if not self.risk_manager.can_enter_sector(symbol, positions):
                return False, "sector_block"

        cached_rsi = get_cached_rsi(symbol)
        if cached_rsi is not None:
            rsi_min = float(getattr(settings, "FAST_PATH_RSI_MIN", 40))
            rsi_max = float(getattr(settings, "FAST_PATH_RSI_MAX", 85))
            if cached_rsi < rsi_min or cached_rsi > rsi_max:
                return False, f"rsi_block_{cached_rsi:.1f}"

        return True, "ok"

    def _prune_jury_vetoes(self):
        jury_vetoes = getattr(self, "_jury_vetoed_symbols", None)
        if not jury_vetoes:
            return
        cutoff = time.time() - 3600
        stale_symbols = [symbol for symbol, ts in jury_vetoes.items() if float(ts or 0) < cutoff]
        for symbol in stale_symbols:
            jury_vetoes.pop(symbol, None)

    def _record_jury_veto(self, symbol: str):
        jury_vetoes = getattr(self, "_jury_vetoed_symbols", None)
        if jury_vetoes is None:
            jury_vetoes = {}
            self._jury_vetoed_symbols = jury_vetoes
        jury_vetoes[symbol] = time.time()

    def _clear_jury_veto(self, symbol: str):
        jury_vetoes = getattr(self, "_jury_vetoed_symbols", None)
        if jury_vetoes is not None:
            jury_vetoes.pop(symbol, None)

    def _record_short_verdict_block(self, symbol: str, reason: str, stage: str):
        reason_text = f"{stage}:{reason or 'unknown'}"
        self.ai_layers["short_verdicts_blocked"] = int(self.ai_layers.get("short_verdicts_blocked", 0) or 0) + 1
        self.ai_layers["last_short_block_reason"] = f"{symbol} {reason_text}"
        logger.warning(f"🩳 SHORT blocked for {symbol}: {reason_text}")

    @staticmethod
    def _summarize_brief_for_trace(brief: dict) -> str:
        if not isinstance(brief, dict) or not brief:
            return "n/a"
        if brief.get("error"):
            return "unavailable"
        if "signal" in brief:
            return f"{brief.get('signal')}:{brief.get('confidence', 0)}"
        if "score" in brief:
            return f"score={brief.get('score', 0)}"
        if "approved" in brief:
            return f"approved={brief.get('approved')} size={brief.get('max_size_pct', 0)}"
        if "regime" in brief:
            return f"{brief.get('regime')}:{brief.get('confidence', 0)}"
        return str(brief)[:80]

    @staticmethod
    def _make_realized_trade_key(trade_record: dict) -> tuple:
        return (
            str(trade_record.get("asset_type", "equity") or "equity").lower(),
            str(trade_record.get("symbol", "") or "").upper(),
            round(float(trade_record.get("entry_time", 0) or 0), 3),
            round(float(trade_record.get("quantity", 0) or 0), 6),
            str(trade_record.get("reason", "") or ""),
            str(trade_record.get("exit_order_id", trade_record.get("order_id", "")) or ""),
        )

    def _build_confirmed_exit_trade(
        self,
        position: dict,
        fill_price: float,
        qty: float,
        reason: str,
        exit_time: Optional[float] = None,
        order: Optional[dict] = None,
        fill_source: str = "broker",
    ) -> dict:
        entry_price = float(position.get("entry_price", fill_price) or fill_price or 0)
        side = position.get("side", "long")
        quantity = float(qty or position.get("quantity", 0) or 0)
        if side == "short":
            pnl = (entry_price - fill_price) * quantity
        else:
            pnl = (fill_price - entry_price) * quantity
        pnl_pct = ((fill_price - entry_price) / entry_price * 100) if entry_price else 0
        if side == "short":
            pnl_pct = -pnl_pct
        confirmed_exit_time = float(exit_time or time.time())
        trade_record = {
            "symbol": position.get("symbol", ""),
            "side": "sell" if side == "long" else "buy_to_cover",
            "entry_price": entry_price,
            "exit_price": float(fill_price or 0),
            "quantity": quantity,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "hold_seconds": confirmed_exit_time - float(position.get("entry_time", confirmed_exit_time) or confirmed_exit_time),
            "entry_time": position.get("entry_time", 0),
            "exit_time": confirmed_exit_time,
            "sentiment_at_entry": position.get("sentiment_at_entry", 0),
            "conviction_level": position.get("conviction_level", "normal"),
            "risk_tier": self.risk_manager.get_risk_tier().get("name", "?") if self.risk_manager else "?",
            "strategy_tag": position.get("strategy_tag", "unknown"),
            "signal_sources": position.get("signal_sources", ["unknown"]),
            "decision_confidence": position.get("decision_confidence", 0),
            "provider_used": position.get("provider_used", ""),
            "signal_price": position.get("signal_price", entry_price),
            "decision_price": position.get("decision_price", entry_price),
            "fill_price": float(fill_price or 0),
            "fill_timestamp": confirmed_exit_time,
            "fill_timestamp_source": fill_source,
            "exit_order_id": position.get("exit_order_id"),
            "slippage_bps": self._compute_entry_slippage_bps(
                entry_price, position.get("signal_price", entry_price), side
            ),
            **self._compute_signal_latency_fields(position),
        }
        if isinstance(order, dict):
            trade_record["order"] = order
            if order.get("id") and not trade_record.get("exit_order_id"):
                trade_record["exit_order_id"] = order.get("id")
        return trade_record

    def _handle_fast_path_breakout(self, symbol: str, price: float, pct_change: float, volume_spike: float):
        """Synchronous callback-safe breakout handler (zero network, in-memory checks only)."""
        passes, reason = self._passes_fast_path_deterministic_screen(symbol, price, pct_change, volume_spike)
        if not passes:
            logger.debug(f"⚡ FAST-PATH reject {symbol}: {reason}")
            return

        signal_timestamp = time.time()
        candidate = {
            "symbol": symbol,
            "price": price,
            "change_pct": pct_change,
            "volume_spike": volume_spike,
            "source": "breakout_stream",
            "score": abs(pct_change) / 100 + volume_spike / 10,
            "signal_timestamp": signal_timestamp,
            "strategy_tag": "breakout_fast_path",
        }

        self._fast_path_pending.add(symbol)
        log_activity(
            "scan",
            f"⚡ Fast-path scout: {symbol} {pct_change:+.1f}% vol={volume_spike:.1f}x (signal)",
            {"signal_timestamp": signal_timestamp},
        )
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._execute_fast_path_scout_entry(candidate))
        except RuntimeError:
            self._fast_path_pending.discard(symbol)

    async def _execute_fast_path_scout_entry(self, candidate: dict):
        """Async scout entry path (can use network; launched from sync callback via create_task)."""
        if not getattr(self, "_broker_ready", False):
            return
        symbol = candidate["symbol"]
        price = float(candidate.get("price", 0) or 0)
        pct_change = float(candidate.get("change_pct", 0) or 0)
        volume_spike = float(candidate.get("volume_spike", 0) or 0)
        signal_timestamp = float(candidate.get("signal_timestamp", time.time()) or time.time())
        try:
            if (
                self.risk_manager
                and getattr(settings, "SWING_MODE_DISABLE_FAST_PATH", True)
                and self.risk_manager.is_swing_mode()
            ):
                logger.info(f"⚡ FAST-PATH skipped in swing mode: {symbol}")
                return
            min_entry_sent = float(getattr(settings, "MIN_ENTRY_SENTIMENT", 0.3) or 0.3)
            scout_score = max(0.75, min(1.0, min_entry_sent + 0.05))
            positions = self.entry_manager.get_positions()
            if symbol in getattr(self.entry_manager, "positions", {}):
                logger.info(f"⚡ FAST-PATH duplicate position blocked: {symbol}")
                return
            can = await self.entry_manager.can_enter(symbol, scout_score, positions)
            if not can:
                logger.info(f"⚡ FAST-PATH blocked by entry checks: {symbol}")
                return

            scout_mult = float(getattr(settings, "FAST_PATH_SIZE_MULTIPLIER", 0.4))
            sentiment_data = {
                "score": scout_score,
                "consensus_direction": "BUY",
                "consensus_confidence": 0,
                "consensus_size_modifier": 1.0,
                "share_notional_multiplier": scout_mult,
                "strategy_tag": "breakout_fast_path",
                "signal_sources": ["breakout_stream"],
                "provider_used": "fast_path_v1",
                "signal_price": price,
                "decision_price": price,
                "signal_timestamp": signal_timestamp,
                "entry_path": "fast_path",
                "anomaly_flags": [],
                "change_pct": pct_change,
                "volume_spike": volume_spike,
            }
            pos = await self.entry_manager.enter_position(symbol, sentiment_data)
            if not pos:
                logger.info(f"⚡ FAST-PATH scout rejected by broker/size: {symbol}")
                return

            pos["strategy_tag"] = "breakout_fast_path"
            pos["scout_escalated"] = False
            pos["signal_timestamp"] = signal_timestamp

            eval_payload = dict(candidate)
            eval_payload["attempts"] = 0
            eval_payload["first_enqueued_at"] = time.time()
            eval_payload["last_eval_at"] = 0.0
            try:
                self._fast_path_eval_queue.put_nowait(eval_payload)
            except asyncio.QueueFull:
                logger.warning(f"⚡ FAST-PATH eval queue full, dropping scout eval for {symbol}")
            log_activity(
                "trade",
                f"⚡ FAST-PATH scout entered: {symbol} @ ${pos.get('entry_price', price):.2f}",
                {"signal_timestamp": signal_timestamp},
            )
        finally:
            self._fast_path_pending.discard(symbol)

    @staticmethod
    def _get_fast_path_eval_limits():
        max_cycles = max(1, int(getattr(settings, "FAST_PATH_EVAL_MAX_CYCLES", 6)))
        max_age_s = max(10, int(getattr(settings, "FAST_PATH_EVAL_MAX_AGE_SECONDS", 90)))
        return max_cycles, max_age_s

    def _requeue_fast_path_scout(self, scout_candidate: dict, attempts: int):
        scout_candidate = dict(scout_candidate)
        scout_candidate["attempts"] = attempts
        scout_candidate["last_eval_at"] = time.time()
        try:
            self._fast_path_eval_queue.put_nowait(scout_candidate)
            return True
        except asyncio.QueueFull:
            logger.warning(
                f"⚡ FAST-PATH eval queue full; dropping requeue for {scout_candidate.get('symbol', '?')}"
            )
            return False

    async def _evaluate_fast_path_scouts(self):
        """Tier-2 AI evaluation for held fast-path scouts (runs every ~5s)."""
        if not hasattr(self, "_fast_path_eval_queue") or self._fast_path_eval_queue.empty():
            return
        if not self.orchestrator:
            return

        max_cycles, max_age_s = self._get_fast_path_eval_limits()
        processed = 0
        seen_symbols = set()
        while not self._fast_path_eval_queue.empty() and processed < 5:
            try:
                scout_candidate = self._fast_path_eval_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

            symbol = scout_candidate.get("symbol", "")
            if symbol in seen_symbols:
                # Preserve 5-second cadence: never evaluate same symbol twice in one loop tick.
                self._requeue_fast_path_scout(
                    scout_candidate, int(scout_candidate.get("attempts", 0) or 0)
                )
                break
            seen_symbols.add(symbol)
            pos = self.entry_manager.positions.get(symbol)
            if not pos:
                continue
            if pos.get("strategy_tag") != "breakout_fast_path":
                continue
            if pos.get("scout_escalated"):
                continue
            attempts = int(scout_candidate.get("attempts", 0) or 0)
            first_enqueued_at = float(
                scout_candidate.get("first_enqueued_at", scout_candidate.get("signal_timestamp", time.time()))
                or time.time()
            )
            age_s = max(0.0, time.time() - first_enqueued_at)
            if attempts >= max_cycles or age_s > max_age_s:
                log_activity(
                    "trade",
                    f"⚡ FAST-PATH timeout hold: {symbol} (attempts={attempts}, age={int(age_s)}s)",
                )
                processed += 1
                continue
            if pos.get("order_status") != "filled":
                next_attempt = attempts + 1
                self._requeue_fast_path_scout(scout_candidate, next_attempt)
                processed += 1
                continue

            try:
                verdict = await self.orchestrator.evaluate(
                    symbol=symbol,
                    price=float(pos.get("entry_price", scout_candidate.get("price", 0)) or 0),
                    signals_data=scout_candidate,
                )
                self.ai_layers["last_consensus"] = verdict.to_dict()
            except Exception as e:
                logger.error(f"Fast-path scout jury error for {symbol}: {e}")
                continue

            if verdict.decision == "BUY":
                tier = self.risk_manager.get_risk_tier() if self.risk_manager else {}
                tier_size = tier.get("size_pct", 2.0)
                size_modifier = min(1.0, verdict.size_pct / tier_size) if tier_size > 0 else 1.0
                sentiment_data = {
                    "score": pos.get("sentiment_at_entry", 0),
                    "consensus_size_modifier": size_modifier,
                    "consensus_confidence": verdict.confidence,
                    "provider_used": getattr(verdict, "provider_used", ""),
                    "jury_trail_pct": verdict.trail_pct,
                    "signal_timestamp": pos.get("signal_timestamp"),
                }
                added = await self.entry_manager.add_to_scout(symbol, sentiment_data)
                if added:
                    log_activity("trade", f"⚡ FAST-PATH escalate: {symbol} scout -> full")
                else:
                    next_attempt = attempts + 1
                    requeued = self._requeue_fast_path_scout(scout_candidate, next_attempt)
                    status = "recheck queued" if requeued else "recheck dropped"
                    log_activity("trade", f"⚡ FAST-PATH hold scout: {symbol} (add blocked, {status})")
            elif verdict.decision == "SHORT":
                await self._exit_fast_path_scout(symbol, reason="fast_path_thesis_rejected")
                log_activity("trade", f"⚡ FAST-PATH exit: {symbol} thesis rejected")
            else:
                # SKIP maps to HOLD for scout positions.
                current_trail = float(pos.get("trail_pct", 3.0) or 3.0)
                advised = float(getattr(verdict, "trail_pct", current_trail) or current_trail)
                tightened = max(1.0, min(current_trail, advised))
                pos["trail_pct"] = tightened
                next_attempt = attempts + 1
                requeued = self._requeue_fast_path_scout(scout_candidate, next_attempt)
                status = "recheck queued" if requeued else "recheck dropped"
                log_activity(
                    "trade",
                    f"⚡ FAST-PATH hold: {symbol} scout maintained (trail={tightened:.1f}%, {status})",
                )
            processed += 1

    async def _exit_fast_path_scout(self, symbol: str, reason: str = "fast_path_exit"):
        pos = self.entry_manager.positions.get(symbol)
        if not pos:
            return
        qty = float(pos.get("quantity", 0) or 0)
        if qty <= 0:
            return
        side = pos.get("side", "long")
        close_fn = self.alpaca_client.place_market_buy if side == "short" else self.alpaca_client.place_market_sell
        order = await asyncio.get_event_loop().run_in_executor(None, close_fn, symbol, qty)
        if not order:
            return
        exit_price = float(order.get("filled_avg_price", pos.get("entry_price", 0)) or pos.get("entry_price", 0))
        entry_price = float(pos.get("entry_price", exit_price) or exit_price)
        pnl = (entry_price - exit_price) * qty if side == "short" else (exit_price - entry_price) * qty
        pnl_pct = ((exit_price - entry_price) / entry_price * 100) if entry_price else 0
        if side == "short":
            pnl_pct = -pnl_pct
        trade_record = {
            "symbol": symbol,
            "side": "buy_to_cover" if side == "short" else "sell",
            "entry_price": entry_price,
            "exit_price": exit_price,
            "quantity": qty,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "reason": reason,
            "hold_seconds": time.time() - pos.get("entry_time", time.time()),
            "entry_time": pos.get("entry_time", 0),
            "exit_time": time.time(),
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
            **self._compute_signal_latency_fields(pos),
        }
        self._record_realized_exit(trade_record)

    async def _process_candidates(self, candidates):
        """Evaluate scanner candidates for entry with position manager veto."""
        if not getattr(self, "_broker_ready", False):
            return
        if not self.risk_manager.can_trade():
            return
        guardrails = self._get_operating_guardrails()
        if not guardrails.get("allow_new_entries", True):
            logger.warning(f"⛔ Entry pipeline blocked by operating guardrails: {','.join(guardrails.get('reasons', []))}")
            return

        self._prune_jury_vetoes()
        positions = self.entry_manager.get_positions()
        congress_scanner = getattr(self, "congress_scanner", None)
        human_intel_store = getattr(self, "human_intel_store", None)
        edgar_scanner = getattr(self, "edgar_scanner", None)

        # Evaluate candidates — skip held tickers and recently-SKIPped to find FRESH opportunities
        evaluated = 0
        held_symbols = {p.get("symbol") for p in positions}
        evaluated_symbols = set()
        for candidate in candidates[:20]:  # Look at top 20
            if evaluated >= 8:  # Evaluate up to 8 per cycle (more diversity)
                break
            symbol = candidate["symbol"]

            # Skip tickers we already hold (don't waste AI calls re-evaluating held positions)
            if symbol in held_symbols:
                continue
            if symbol in evaluated_symbols:
                logger.debug(f"Skipping duplicate candidate in same cycle: {symbol}")
                continue
            evaluated_symbols.add(symbol)

            sentiment_score = candidate.get("sentiment_score", 0)
            sentiment_data = dict(self.sentiment_analyzer.get_cached(symbol) or {"score": sentiment_score})
            signal_price = float(candidate.get("price", 0) or 0)
            signal_timestamp = float(candidate.get("signal_timestamp", time.time()) or time.time())
            signal_sources = self._extract_signal_sources(candidate)
            sentiment_data["signal_price"] = signal_price
            sentiment_data["decision_price"] = signal_price
            sentiment_data["signal_sources"] = signal_sources
            sentiment_data["signal_timestamp"] = signal_timestamp
            sentiment_data["entry_path"] = "jury"
            sentiment_data["anomaly_flags"] = list(sentiment_data.get("anomaly_flags", []) or [])
            if congress_scanner and not candidate.get("congress_trades"):
                related_congress = [
                    trade for trade in getattr(congress_scanner, "_trades", [])
                    if str(trade.get("ticker", "")).upper() == symbol
                ][:3]
                if related_congress:
                    candidate["congress_trades"] = "; ".join(
                        f"{trade.get('member', 'Unknown')} {trade.get('transaction', 'trade')} {trade.get('amount', '')}".strip()
                        for trade in related_congress
                    )
            if human_intel_store and not candidate.get("human_intel"):
                human_intel = human_intel_store.summarize_for_symbol(symbol)
                if human_intel.get("count"):
                    candidate["human_intel"] = human_intel.get("summary", "")
                    candidate["human_intel_bias"] = human_intel.get("bias", "neutral")
            if edgar_scanner and (not candidate.get("edgar_filings") or not candidate.get("insider_activity")):
                edgar_filings = await edgar_scanner.check_ticker(symbol)
                if edgar_filings and not candidate.get("edgar_filings"):
                    candidate["edgar_filings"] = "; ".join(
                        f"{filing.get('form_type', '?')} {filing.get('filed', '')}".strip()
                        for filing in edgar_filings[:3]
                    )
                if not candidate.get("insider_activity"):
                    insider_activity = await edgar_scanner.get_insider_trades(symbol, filings=edgar_filings)
                    if insider_activity.get("form4_count"):
                        candidate["insider_activity"] = insider_activity.get("summary", "")
                        candidate["insider_signal"] = insider_activity.get("signal", "watch")

            # Position manager veto check
            if self.position_manager and not self.position_manager.can_enter(symbol, positions, self.risk_manager):
                logger.info(
                    f"🧭 ENTRY TRACE {symbol}: blocked before jury by position_manager "
                    f"strategy={candidate.get('strategy_tag', 'unknown')} side={candidate.get('side', 'long')}"
                )
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
                    consensus_detail = getattr(verdict, "consensus_detail", {}) or {}
                    votes = consensus_detail.get("votes", {})
                    briefs = getattr(verdict, "briefs", {}) or {}
                    logger.info(
                        f"🧭 JURY TRACE {symbol}: decision={verdict.decision} conf={verdict.confidence:.1f}% "
                        f"agreement={consensus_detail.get('agreement', 'unknown')} "
                        f"votes={votes} degraded={consensus_detail.get('degraded', False)} "
                        f"rate_limited={consensus_detail.get('rate_limited_providers', [])}"
                    )
                    logger.info(
                        f"🧭 BRIEFS {symbol}: tech={self._summarize_brief_for_trace(briefs.get('technical', {}))} "
                        f"sent={self._summarize_brief_for_trace(briefs.get('sentiment', {}))} "
                        f"cat={self._summarize_brief_for_trace(briefs.get('catalyst', {}))} "
                        f"risk={self._summarize_brief_for_trace(briefs.get('risk', {}))} "
                        f"macro={self._summarize_brief_for_trace(briefs.get('macro', {}))}"
                    )
                    if verdict.decision not in ("BUY", "SHORT"):
                        if "cooldown" not in verdict.reasoning.lower():
                            self._record_jury_veto(symbol)
                            from src.data.entry_controls import record_jury_veto as _persist_veto
                            _persist_veto(symbol)
                            logger.info(f"Jury SKIP for {symbol}: {verdict.reasoning}")
                            log_activity("ai", f"{symbol}: SKIP — {verdict.reasoning}")
                        continue
                    min_conf = float(getattr(settings, "MIN_JURY_CONFIDENCE", 40) or 40)
                    if verdict.confidence < min_conf:
                        logger.warning(f"Jury {verdict.decision} for {symbol} below confidence floor ({verdict.confidence:.0f}% < {min_conf:.0f}%) — forcing SKIP")
                        log_activity("ai", f"{symbol}: {verdict.decision} blocked — confidence {verdict.confidence:.0f}% < {min_conf:.0f}% threshold")
                        self._record_jury_veto(symbol)
                        continue
                    direction = verdict.decision
                    if direction in ("BUY", "SHORT"):
                        self._clear_jury_veto(symbol)
                        from src.data.entry_controls import clear_jury_veto as _clear_persist_veto
                        _clear_persist_veto(symbol)
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
                    sentiment_data["consensus_agreement"] = (
                        (getattr(verdict, "consensus_detail", {}) or {}).get("agreement", "")
                    )
                    sentiment_data["strategy_tag"] = self._derive_strategy_tag(candidate, direction)
                except Exception as e:
                    logger.error(f"Orchestrator error for {symbol}: {e}")
                    continue  # Never trade without agent consensus

            logger.info(f"🔑 {symbol} REACHED ENTRY BLOCK (orchestrator={bool(self.orchestrator)})")
            direction = sentiment_data.get("consensus_direction", "BUY")
            sentiment_data.setdefault("provider_used", "")
            sentiment_data.setdefault("consensus_confidence", 0)
            sentiment_data.setdefault("strategy_tag", self._derive_strategy_tag(candidate, direction))
            sentiment_data.setdefault("share_notional_multiplier", 1.0)
            sentiment_data.setdefault("signal_timestamp", signal_timestamp)
            gate = self._evaluate_trade_gate(
                {**candidate, "strategy_tag": sentiment_data.get("strategy_tag", "unknown")},
                direction,
            )
            candidate = dict(gate.get("candidate", candidate) or candidate)
            sentiment_data["strategy_tag"] = candidate.get("strategy_tag", sentiment_data.get("strategy_tag", "unknown"))
            sentiment_data["playbook_label"] = candidate.get("playbook_label", "")
            sentiment_data["playbook_options_mode"] = candidate.get("playbook_options_mode", "off")
            if not gate.get("allowed", False):
                reason = gate.get("reason", "playbook_block")
                logger.info(f"⛔ PLAYBOOK GATE {symbol}: {reason} strategy={sentiment_data['strategy_tag']} direction={direction}")
                log_activity("trade", f"⛔ PLAYBOOK GATE: {symbol} {direction} blocked ({reason})")
                if direction == "SHORT":
                    self._record_short_verdict_block(symbol, reason, "playbook")
                continue
            if candidate.get("copy_trader_context"):
                sentiment_data["copy_trader_context"] = candidate.get("copy_trader_context", "")
                sentiment_data["copy_trader_handles"] = list(candidate.get("copy_trader_handles", []) or [])
                sentiment_data["copy_trader_signal_count"] = int(candidate.get("copy_trader_signal_count", 0) or 0)
                sentiment_data["copy_trader_convergence"] = int(candidate.get("copy_trader_convergence", 0) or 0)
                sentiment_data["copy_trader_weight"] = float(candidate.get("copy_trader_weight", 1.0) or 1.0)
                sentiment_data["copy_trader_size_multiplier"] = float(
                    candidate.get("copy_trader_size_multiplier", 1.0) or 1.0
                )
            raw_sentiment_score = float(sentiment_score or 0)
            effective_sentiment_score = raw_sentiment_score
            if direction == "SHORT":
                effective_sentiment_score = -abs(raw_sentiment_score) if raw_sentiment_score != 0 else -0.1
                sentiment_data["raw_sentiment_score"] = raw_sentiment_score
                sentiment_data["score"] = effective_sentiment_score
            else:
                sentiment_data["score"] = raw_sentiment_score
            logger.info(f"🔑 {symbol} pre-entry: direction={direction}, sentiment={sentiment_score:.2f}")
            # For SHORT trades, invert sentiment check (negative sentiment = good for shorts)
            check_sentiment = -effective_sentiment_score if direction == "SHORT" else effective_sentiment_score
            can = await self.entry_manager.can_enter(symbol, check_sentiment, positions)
            gate_reason = (getattr(self.entry_manager, "last_gate", {}) or {}).get("reason", "unknown")
            risk_status = {}
            if self.risk_manager and hasattr(self.risk_manager, "get_status"):
                try:
                    risk_status = self.risk_manager.get_status() or {}
                except Exception:
                    risk_status = {}
            logger.info(
                f"🧭 ENTRY GATE {symbol}: allowed={can} reason={gate_reason} "
                f"direction={direction} conf={float(sentiment_data.get('consensus_confidence', 0) or 0):.1f}% "
                f"raw_sent={raw_sentiment_score:.2f} check_sent={check_sentiment:.2f} "
                f"pdt_raw={risk_status.get('alpaca_daytrade_count', 0)} "
                f"pdt_effective={risk_status.get('effective_daytrade_count', 0)} "
                f"swing_mode={risk_status.get('swing_mode', False)}"
            )
            if direction == "SHORT" and not can:
                self._record_short_verdict_block(symbol, gate_reason, "gate")
            if can:
                logger.info(f"{'📈' if direction == 'BUY' else '📉'} Entry signal: {symbol} {direction} (score={candidate['score']:.3f}, sent={sentiment_score:.2f})")

                # ── OPTIONS TRADE (if enabled) ──
                options_budget = 0.0
                options_pct = 0.0
                options_engine = getattr(self, "options_engine", None)
                if options_engine:
                    confidence = sentiment_data.get("consensus_confidence", 0)
                    options_pct = self._determine_options_allocation_pct(candidate, direction, confidence)

                    if options_pct > 0:
                        tier = self.risk_manager.get_risk_tier() if self.risk_manager else {}
                        equity = self.risk_manager.equity if self.risk_manager else 25000
                        total_budget = equity * tier.get("size_pct", 2.5) / 100
                        options_budget = total_budget * (options_pct / 100)

                        can_open_options = True
                        if self.risk_manager:
                            can_open_options = self.risk_manager.can_open_options(options_budget)
                        if can_open_options:
                            sentiment_data["change_pct"] = candidate.get("change_pct", 0)
                            sentiment_data["volume_spike"] = candidate.get("volume_spike", 1.0)
                            opt_pos = await options_engine.execute_option_trade(
                                symbol=symbol,
                                price=candidate.get("price", 0),
                                direction=direction,
                                budget=options_budget,
                                sentiment_data=sentiment_data,
                            )
                            if opt_pos:
                                options_cost = float(opt_pos.get("total_cost", 0) or 0)
                                share_mult = 1.0
                                if total_budget > 0:
                                    share_mult = max(0.0, 1.0 - (options_cost / total_budget))
                                sentiment_data["share_notional_multiplier"] = share_mult
                                sentiment_data["options_budget_used"] = options_cost
                                if self.risk_manager:
                                    self.risk_manager.update_options_exposure(options_engine.get_options_positions())
                                log_activity(
                                    "options",
                                    f"🎯 OPTIONS ENTRY: {opt_pos['qty']}x {opt_pos['contract_symbol']} @ ${opt_pos['entry_premium']:.2f}",
                                )
                            else:
                                sentiment_data["share_notional_multiplier"] = 1.0
                        else:
                            log_activity(
                                "options",
                                f"⛔ OPTIONS BLOCKED: {symbol} would exceed portfolio premium cap",
                            )

                # ── SHARES TRADE (always, reduced size if options took some) ──
                if direction == "SHORT":
                    pos = await self.entry_manager.enter_short(symbol, sentiment_data)
                else:
                    pos = await self.entry_manager.enter_position(symbol, sentiment_data)
                if direction == "SHORT" and not pos:
                    order_reason = getattr(self.entry_manager, "last_order_error", "") or "entry_execution_failed"
                    self._record_short_verdict_block(symbol, order_reason, "execution")
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
        symbol = str(trade_record.get("symbol", "") or "")
        asset_type = str(trade_record.get("asset_type", "equity") or "equity").lower()
        position = None
        if self.entry_manager and symbol and asset_type != "option":
            position = self.entry_manager.positions.get(symbol)
            if position and position.get("exit_recorded"):
                return

        trade_key = self._make_realized_trade_key(trade_record)
        recorded_keys = getattr(self, "_recorded_realized_keys", None)
        if recorded_keys is None:
            self._recorded_realized_keys = set()
            recorded_keys = self._recorded_realized_keys
        if trade_key in recorded_keys:
            return

        try:
            for existing in trade_history.load_all():
                if self._make_realized_trade_key(existing) == trade_key:
                    recorded_keys.add(trade_key)
                    if position:
                        position["exit_recorded"] = True
                        position["exit_finalized_at"] = float(
                            trade_record.get("exit_time", time.time()) or time.time()
                        )
                    return
        except Exception:
            pass

        pnl = float(trade_record.get("pnl", 0))
        if position:
            position["exit_recorded"] = True
            position["exit_finalized_at"] = float(trade_record.get("exit_time", time.time()) or time.time())
            position["exit_pending"] = False
            position["exit_fill_qty"] = float(trade_record.get("quantity", 0) or 0)
            if position:
                exit_price = float(
                    trade_record.get("exit_price", trade_record.get("fill_price", position.get("current_price", 0)))
                    or 0
                )
                exit_time = float(trade_record.get("exit_time", time.time()) or time.time())
                if exit_price > 0:
                    self._update_position_trade_telemetry(position, exit_price, now_ts=exit_time)
                merge_fields = (
                    "entry_path",
                    "intended_notional",
                    "actual_notional",
                    "intended_qty",
                    "actual_qty",
                    "price_at_1m",
                    "price_at_3m",
                    "price_at_5m",
                    "time_to_green_seconds",
                    "time_to_peak_seconds",
                    "mfe_pct",
                    "mae_pct",
                    "copy_trader_context",
                    "copy_trader_handles",
                    "copy_trader_signal_count",
                    "copy_trader_convergence",
                    "copy_trader_weight",
                )
                for field in merge_fields:
                    pos_value = position.get(field)
                    current_value = trade_record.get(field)
                    if pos_value in (None, "", [], {}):
                        continue
                    if field == "entry_path":
                        if current_value in (None, "", "unknown"):
                            trade_record[field] = pos_value
                    elif field in ("intended_notional", "actual_notional", "intended_qty", "actual_qty"):
                        if current_value in (None, "") or float(current_value or 0) <= 0:
                            trade_record[field] = pos_value
                    elif field in ("copy_trader_signal_count", "copy_trader_convergence"):
                        if current_value in (None, "") or int(current_value or 0) <= 0:
                            trade_record[field] = pos_value
                    elif field == "copy_trader_weight":
                        if current_value in (None, "") or float(current_value or 0) == 0:
                            trade_record[field] = pos_value
                    elif current_value in (None, "", [], {}):
                        trade_record[field] = pos_value
                trade_record["anomaly_flags"] = self._merge_anomaly_flags(
                    position.get("anomaly_flags", []),
                    trade_record.get("anomaly_flags", []),
                )

        recorded_keys.add(trade_key)
        trade_history.record_trade(trade_record)
        if self.entry_manager and symbol and asset_type != "option":
            # For partial exits (TP1), preserve the position with remaining quantity
            is_partial = (position or {}).get("exit_scope") == "partial" or \
                         str(trade_record.get("reason", "") or "").endswith("_1")
            remaining_qty = float((position or {}).get("quantity", 0) or 0) - float(trade_record.get("quantity", 0) or 0)
            if is_partial and remaining_qty > 0:
                if position:
                    position["quantity"] = remaining_qty
                    position["partial_exit"] = True
                    position.pop("exit_recorded", None)  # allow future exit recording
            else:
                self.entry_manager.remove_position(symbol)
        if self.risk_manager:
            self.risk_manager.record_trade(trade_record)

        if symbol and asset_type != "option":
            try:
                from src.data import entry_controls
                exit_time = float(trade_record.get("exit_time", time.time()) or time.time())
                entry_controls.set_cooldown(symbol, exit_confirmed_at=exit_time)
                anomaly_flags = trade_record.get("anomaly_flags", []) or []
                reason_str = str(trade_record.get("reason", "") or "").lower()
                if "statistical_poison" in str(anomaly_flags).lower() or "blacklist" in reason_str:
                    entry_controls.blacklist_symbol(symbol, reason=reason_str, source="exit_recording")
            except Exception as ec_err:
                logger.debug(f"Entry controls update failed for {symbol}: {ec_err}")
        copy_trader_monitor = getattr(self, "copy_trader_monitor", None)
        if copy_trader_monitor and (
            trade_record.get("copy_trader_handles")
            or "copy_trader" in (trade_record.get("signal_sources", []) or [])
        ):
            copy_trader_monitor.record_trade_result(trade_record)

        self.pnl_state["total_realized_pnl"] = self.pnl_state.get("total_realized_pnl", 0) + pnl
        self.pnl_state["today_realized_pnl"] = self.pnl_state.get("today_realized_pnl", 0) + pnl
        self.pnl_state["total_trades"] = self.pnl_state.get("total_trades", 0) + 1
        if pnl > 0:
            self.pnl_state["winning_trades"] = self.pnl_state.get("winning_trades", 0) + 1
        elif pnl < 0:
            self.pnl_state["losing_trades"] = self.pnl_state.get("losing_trades", 0) + 1
        # pnl == 0 → breakeven, not counted as win or loss
        self.pnl_state["best_trade"] = max(self.pnl_state.get("best_trade", 0), pnl)
        self.pnl_state["worst_trade"] = min(self.pnl_state.get("worst_trade", 0), pnl)

        if asset_type == "option":
            self.pnl_state["options_total_realized_pnl"] = self.pnl_state.get("options_total_realized_pnl", 0) + pnl
            self.pnl_state["options_total_trades"] = self.pnl_state.get("options_total_trades", 0) + 1
            if pnl > 0:
                self.pnl_state["options_winning_trades"] = self.pnl_state.get("options_winning_trades", 0) + 1
            elif pnl < 0:
                self.pnl_state["options_losing_trades"] = self.pnl_state.get("options_losing_trades", 0) + 1

        persistence.save_pnl_state(self.pnl_state)
        persistence.save_positions(self.entry_manager.positions if self.entry_manager else {})
        options_engine = getattr(self, "options_engine", None)
        if options_engine:
            persistence.save_options_positions(options_engine.positions)
            if self.risk_manager:
                self.risk_manager.update_options_exposure(options_engine.get_options_positions())
        persistence.save_trades([trade_record])

    @staticmethod
    def _position_is_copy_trader_influenced(position: dict) -> bool:
        handles = position.get("copy_trader_handles") or []
        if handles:
            return True
        sources = position.get("signal_sources", []) or []
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",") if s.strip()]
        return "copy_trader" in sources

    async def _refresh_position_trailing_stop(self, position: dict, new_trail_pct: float) -> bool:
        pos = position or {}
        symbol = pos.get("symbol", "")
        qty = int(float(pos.get("quantity", 0) or 0))
        side = pos.get("side", "long")
        if not symbol or qty < 1 or pos.get("swing_only"):
            pos["trail_pct"] = new_trail_pct
            return False
        broker = getattr(self, "alpaca_client", None)
        entry_manager = getattr(self, "entry_manager", None)
        if not broker or not entry_manager:
            pos["trail_pct"] = new_trail_pct
            return False

        cancel_fn = None
        if side == "short" and hasattr(broker, "cancel_open_buys_for_symbol"):
            cancel_fn = broker.cancel_open_buys_for_symbol
        elif side != "short" and hasattr(broker, "cancel_open_sells_for_symbol"):
            cancel_fn = broker.cancel_open_sells_for_symbol
        try:
            if cancel_fn:
                await asyncio.get_event_loop().run_in_executor(None, cancel_fn, symbol)
            trail_order, protection_failed = await entry_manager._place_entry_protection_order(
                symbol, qty, new_trail_pct, side
            )
            pos["trail_pct"] = new_trail_pct
            pos["protection_failed"] = bool(protection_failed)
            if trail_order:
                pos["has_trailing_stop"] = True
                pos["trailing_stop_order_id"] = trail_order.get("id", pos.get("trailing_stop_order_id"))
                return True
        except Exception as e:
            logger.warning(f"Could not refresh trailing stop for {symbol}: {e}")
        return False

    async def _process_copy_trader_exit_signals(self):
        monitor = getattr(self, "copy_trader_monitor", None)
        entry_manager = getattr(self, "entry_manager", None)
        if not monitor or not entry_manager:
            return

        try:
            exit_signals = list(monitor.get_exit_signals() or [])
        except Exception as e:
            logger.debug(f"Copy trader exit fetch failed: {e}")
            return
        if not exit_signals:
            return

        processed_ids = getattr(self, "_processed_copy_trader_exit_ids", None)
        if processed_ids is None:
            processed_ids = set()
            self._processed_copy_trader_exit_ids = processed_ids

        tighten_mult = max(0.1, float(getattr(settings, "COPY_TRADER_EXIT_TIGHTEN_MULT", 0.6) or 0.6))
        min_trail = max(0.5, float(getattr(settings, "COPY_TRADER_EXIT_MIN_TRAIL_PCT", 1.5) or 1.5))
        auto_exit_enabled = bool(getattr(settings, "COPY_TRADER_AUTO_EXIT_STRONG", False))
        strong_exit_min = max(2, int(getattr(settings, "COPY_TRADER_STRONG_EXIT_MIN_SIGNALS", 2) or 2))

        for signal in exit_signals:
            tweet_ids = [tid for tid in signal.get("copy_trader_exit_tweet_ids", []) if tid]
            new_ids = [tid for tid in tweet_ids if tid not in processed_ids]
            if tweet_ids and not new_ids:
                continue

            symbol = str(signal.get("symbol", "") or "").upper()
            if not symbol:
                processed_ids.update(new_ids)
                continue

            pos = entry_manager.positions.get(symbol)
            if not pos or not self._position_is_copy_trader_influenced(pos):
                processed_ids.update(new_ids)
                continue

            signal_handles = {str(h).lower() for h in signal.get("copy_trader_exit_handles", []) if h}
            position_handles = {
                str(h).lower()
                for h in (pos.get("copy_trader_handles", []) or [])
                if h
            }
            if position_handles and signal_handles and not (position_handles & signal_handles):
                processed_ids.update(new_ids)
                continue

            current_trail = max(0.5, float(pos.get("trail_pct", 3.0) or 3.0))
            tightened_trail = max(min_trail, min(current_trail, current_trail * tighten_mult))
            refreshed = False
            if tightened_trail < current_trail:
                refreshed = await self._refresh_position_trailing_stop(pos, tightened_trail)
            pos["copy_trader_exit_action"] = signal.get("copy_trader_exit_action", "trim")
            pos["copy_trader_exit_count"] = int(signal.get("copy_trader_exit_count", 0) or 0)
            pos["copy_trader_exit_handles"] = list(signal.get("copy_trader_exit_handles", []) or [])
            pos["copy_trader_exit_context"] = signal.get("copy_trader_exit_context", "")
            pos["copy_trader_exit_at"] = time.time()
            self.ai_layers["last_copy_trader_exit_signal"] = f"{symbol} {pos['copy_trader_exit_context']}"
            log_activity(
                "trade",
                f"📣 {symbol}: copy-trader {pos['copy_trader_exit_action']} signal"
                f" ({pos['copy_trader_exit_count']} handles) -> trail {tightened_trail:.1f}%",
                {
                    "symbol": symbol,
                    "handles": pos["copy_trader_exit_handles"],
                    "refreshed": refreshed,
                },
            )

            if (
                auto_exit_enabled
                and pos.get("copy_trader_exit_action") == "exit"
                and pos.get("copy_trader_exit_count", 0) >= strong_exit_min
                and not pos.get("swing_only")
            ):
                pos["copy_trader_auto_exit_ready"] = True
                exit_manager = getattr(self, "exit_manager", None)
                polygon_client = getattr(self, "polygon_client", None)
                if exit_manager and polygon_client:
                    try:
                        current_price = await asyncio.get_event_loop().run_in_executor(
                            None, polygon_client.get_price, symbol
                        )
                    except Exception:
                        current_price = float(pos.get("entry_price", 0) or 0)
                    entry_price = float(pos.get("entry_price", 0) or 0)
                    pnl_pct = 0.0
                    if entry_price and current_price:
                        if pos.get("side", "long") == "short":
                            pnl_pct = ((entry_price - current_price) / entry_price) * 100
                        else:
                            pnl_pct = ((current_price - entry_price) / entry_price) * 100
                    qty = float(pos.get("quantity", 0) or 0)
                    if qty > 0:
                        await exit_manager._execute_exit(
                            pos,
                            qty,
                            current_price,
                            "copy_trader_exit_signal",
                            pnl_pct,
                        )

            processed_ids.update(new_ids)

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
            if self.entry_manager and hasattr(self.entry_manager, "sync_positions_from_brokerage"):
                self.entry_manager.sync_positions_from_brokerage(alpaca_positions)
                positions = self.entry_manager.get_positions()
            alpaca_symbols = {p["symbol"] for p in alpaca_positions}
            alpaca_position_map = {p["symbol"]: p for p in alpaca_positions}
        except Exception as e:
            logger.debug(f"Alpaca position sync error: {e}")
            return

        # Also get open orders to detect pending (unfilled) entries.
        pending_entry_order_keys = set()
        try:
            open_orders = await asyncio.get_event_loop().run_in_executor(
                None, self.alpaca_client.get_orders
            )
            pending_entry_order_keys = {
                (o.get("symbol", ""), o.get("side", ""))
                for o in open_orders
                if o.get("status") in ("new", "accepted", "pending_new", "partially_filled")
                and o.get("type") != "trailing_stop"
            }
        except Exception:
            pending_entry_order_keys = set()

        # Fetch closed orders once for reconciliation and confirmed-exit checks.
        needs_fill_backfill = any(not p.get("fill_timestamp") for p in positions)
        symbols_missing_from_broker = any(
            p.get("symbol", "") and p.get("symbol") not in alpaca_symbols for p in positions
        )
        closed_orders = []
        if needs_fill_backfill or symbols_missing_from_broker:
            try:
                closed_orders = await asyncio.get_event_loop().run_in_executor(
                    None, self.alpaca_client.get_orders, "closed"
                )
            except Exception:
                closed_orders = []

        for pos in positions:
            if pos.get("fill_timestamp"):
                continue
            symbol = pos.get("symbol", "")
            if not symbol:
                continue
            side = pos.get("side", "long")
            expected_entry_side = "sell" if side == "short" else "buy"
            order_id = str(pos.get("order_id", "") or "")
            best_fill_ts = None
            best_fill_price = None
            for order in closed_orders:
                if order.get("symbol") != symbol:
                    continue
                if order.get("side") and order.get("side") != expected_entry_side:
                    continue
                if order_id and order.get("id") and str(order.get("id")) != order_id:
                    continue
                fill_ts = self._parse_iso_ts(order.get("filled_at"))
                if fill_ts is None:
                    continue
                if best_fill_ts is None or fill_ts > best_fill_ts:
                    best_fill_ts = fill_ts
                    try:
                        best_fill_price = float(order.get("filled_avg_price", 0) or 0)
                    except Exception:
                        best_fill_price = None
            if best_fill_ts is not None:
                pos["fill_timestamp"] = best_fill_ts
                pos["fill_timestamp_source"] = "reconciliation"
                if best_fill_price and best_fill_price > 0:
                    pos["fill_price"] = best_fill_price
                pos["order_status"] = "filled"

        for pos in list(positions):
            symbol = pos["symbol"]
            try:
                broker_pos = alpaca_position_map.get(symbol)
                if broker_pos:
                    broker_qty = float(broker_pos.get("quantity", 0) or 0)
                    if abs(float(pos.get("quantity", 0) or 0) - broker_qty) > 1e-6:
                        pos["quantity"] = broker_qty
                        pos["actual_qty"] = broker_qty
                        pos["actual_notional"] = float(pos.get("entry_price", 0) or 0) * broker_qty
                    broker_price = float(
                        broker_pos.get("current_price", pos.get("current_price", 0))
                        or pos.get("current_price", 0)
                    )
                    if broker_price <= 0:
                        broker_price = float(
                            broker_pos.get("current_price", broker_pos.get("avg_entry_price", pos.get("entry_price", 0)))
                            or pos.get("entry_price", 0)
                        )
                    if broker_price > 0:
                        self._update_position_trade_telemetry(pos, broker_price)
                if pos.get("halted"):
                    logger.debug(f"{symbol}: market halted — skipping monitor checks")
                    continue
                # ── DETECT TRAILING STOP FILLS ──
                # If we're tracking it but Alpaca no longer has it → trailing stop fired
                # BUT: if there's still a pending entry order, the position hasn't opened yet.
                side = pos.get("side", "long")
                expected_entry_side = "sell" if side == "short" else "buy"
                if (symbol, expected_entry_side) in pending_entry_order_keys:
                    logger.debug(f"{symbol}: pending {expected_entry_side} entry order still open — waiting for fill")
                    continue

                if symbol not in alpaca_symbols:
                    expected_exit_side = "buy" if side == "short" else "sell"
                    latest = None
                    latest_fill_ts = None
                    latest_key = ""
                    latest_fill_price = None
                    session_start_ts = float(getattr(self, "start_time", 0) or 0)
                    for o in closed_orders:
                        if o.get("symbol") != symbol:
                            continue
                        if o.get("side") and o.get("side") != expected_exit_side:
                            continue
                        ts_key = str(o.get("filled_at") or o.get("updated_at") or o.get("submitted_at") or "")
                        fill_ts = self._parse_iso_ts(ts_key)
                        if fill_ts is None:
                            continue
                        # Guard against matching stale historical exits from before this bot session.
                        if session_start_ts and fill_ts + 1 < session_start_ts:
                            continue
                        try:
                            fill_price = float(o.get("filled_avg_price", 0) or 0)
                        except Exception:
                            fill_price = 0
                        if fill_price <= 0:
                            continue
                        if latest_fill_ts is None or fill_ts > latest_fill_ts:
                            latest = o
                            latest_fill_ts = fill_ts
                            latest_fill_price = fill_price
                            latest_key = ts_key

                    if not latest:
                        # Broker snapshots can transiently fail at open; never force local exits
                        # without a confirmed closed fill.
                        if not pos.get("_missing_broker_warned"):
                            logger.warning(
                                f"⚠️ {symbol} missing from broker snapshot but no confirmed exit fill yet — keeping position"
                            )
                            pos["_missing_broker_warned"] = True
                        continue

                    if pos.get("_exit_recorded") or pos.get("_exit_recording"):
                        logger.debug(f"{symbol}: trailing stop exit already being/been recorded — skipping duplicate")
                        continue
                    pos["_exit_recording"] = True
                    try:
                        exit_price = float(latest_fill_price or pos.get("entry_price", 0) or 0)
                        logger.info(
                            f"📊 {symbol} exit fill found: ${exit_price:.2f} "
                            f"(type={latest.get('type')}, filled_at={latest_key[:19]})"
                        )
                        qty = float(pos.get("exit_fill_qty") or pos.get("quantity", 0) or 0)
                        if latest.get("filled_qty"):
                            try:
                                qty = float(latest.get("filled_qty", qty) or qty)
                            except Exception:
                                qty = float(pos.get("quantity", 0) or 0)
                        reason = str(pos.get("last_exit_reason") or "trailing_stop")
                        trade_record = self._build_confirmed_exit_trade(
                            pos,
                            fill_price=exit_price,
                            qty=qty,
                            reason=reason,
                            exit_time=latest_fill_ts,
                            order=latest,
                            fill_source="reconciliation",
                        )
                        self._record_realized_exit(trade_record)

                        pos["_exit_recorded"] = True
                        pnl = float(trade_record.get("pnl", 0) or 0)
                        pnl_pct = float(trade_record.get("pnl_pct", 0) or 0)
                        emoji = "✅" if pnl >= 0 else "❌"
                        logger.info(f"{emoji} EXIT CONFIRMED: {symbol} P&L: ${pnl:.2f} ({pnl_pct:+.1f}%)")
                        log_activity("trade", f"{emoji} {symbol} exit confirmed: ${pnl:.2f} ({pnl_pct:+.1f}%)")
                        if reason.startswith("trailing_stop"):
                            await self._close_paired_options(symbol, reason="underlying_trailing_stop")
                    finally:
                        pos.pop("_exit_recording", None)
                    continue
                else:
                    pos.pop("_missing_broker_warned", None)

                # ── VERIFY TRAILING STOP EXISTS ──
                if pos.get("_trail_adjusting"):
                    logger.debug(f"⏳ {symbol} trail being adjusted by Exit Agent — skipping monitor check")
                    continue
                if not pos.get("has_trailing_stop"):
                    if self.risk_manager and hasattr(self.risk_manager, "can_exit_position"):
                        if not self.risk_manager.can_exit_position(pos, reason="trailing_stop", log_block=False):
                            if not pos.get("_swing_trail_deferred_logged"):
                                logger.info(f"🌙 {symbol} swing-only same-day position — trailing stop deferred")
                                pos["_swing_trail_deferred_logged"] = True
                            continue
                        pos.pop("_swing_trail_deferred_logged", None)
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
                        raw_qty = float(pos.get("quantity", 0) or 0)
                        qty = int(raw_qty)
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
                            elif retry_count >= 2:
                                logger.error(f"TRAILING STOP FAILED {retry_count}x for {symbol} — FORCED EXIT via exit_manager")
                                entry_price = float(pos.get("entry_price", 0) or 0)
                                if side == "short":
                                    _pnl_pct = ((entry_price - float(pos.get("current_price", entry_price))) / entry_price * 100) if entry_price else 0
                                else:
                                    _pnl_pct = ((float(pos.get("current_price", entry_price)) - entry_price) / entry_price * 100) if entry_price else 0
                                await self.exit_manager._execute_exit(pos, raw_qty, float(pos.get("current_price", entry_price)), "emergency_trail_failure", _pnl_pct)
                                log_activity("alert", f"Emergency exit: {symbol} — trailing stop failed {retry_count}x")
                            else:
                                logger.warning(f"Trailing stop failed for {symbol} — will retry next cycle ({retry_count}/2)")
                        elif pos.get("_dust_remainder") and raw_qty > 0:
                            logger.warning(f"{symbol} fractional {raw_qty:.4f} — liquidating dust via exit_manager")
                            entry_price = float(pos.get("entry_price", 0) or 0)
                            if side == "short":
                                _pnl_pct = ((entry_price - float(pos.get("current_price", entry_price))) / entry_price * 100) if entry_price else 0
                            else:
                                _pnl_pct = ((float(pos.get("current_price", entry_price)) - entry_price) / entry_price * 100) if entry_price else 0
                            await self.exit_manager._execute_exit(pos, raw_qty, float(pos.get("current_price", entry_price)), "dust_liquidation", _pnl_pct)
                            log_activity("trade", f"Dust exit: {symbol} {raw_qty:.4f} shares")

            except Exception as e:
                logger.error(f"Monitor error for {symbol}: {e}")

    def _on_breakout_detected(self, symbol: str, price: float, volume_spike: float, pct_change: float):
        """Called by market stream when a breakout is detected."""
        direction = "🚀" if pct_change > 0 else "💥"
        logger.info(f"{direction} BREAKOUT: {symbol} {pct_change:+.1f}% @ ${price:.2f} (vol {volume_spike:.1f}x)")
        log_activity("scan", f"{direction} Breakout: {symbol} {pct_change:+.1f}% vol={volume_spike:.1f}x")

        # v1 fast-path is long-only deterministic scout entry.
        if pct_change <= 0:
            return
        self._handle_fast_path_breakout(
            symbol=symbol,
            price=price,
            pct_change=pct_change,
            volume_spike=volume_spike,
        )

    def _on_halt_status(self, symbol: str, status_code: str, reason: str, halted: bool):
        """Pause active monitoring on halted positions until trading resumes."""
        if not self.entry_manager:
            return
        if not hasattr(self.entry_manager, "_halted_symbols"):
            self.entry_manager._halted_symbols = set()
        pos = self.entry_manager.positions.get(symbol)
        if halted:
            self.entry_manager._halted_symbols.add(symbol)
        else:
            self.entry_manager._halted_symbols.discard(symbol)
        if pos is not None:
            pos["halted"] = bool(halted)
            pos["market_status_code"] = status_code
            pos["market_status_reason"] = reason
            pos["market_status_updated_at"] = time.time()
        if halted:
            log_activity("alert", f"🚨 {symbol} HALTED — monitor paused ({reason or status_code})")
            logger.warning(f"{symbol} halted while held — pausing monitor checks")
        else:
            log_activity("alert", f"✅ {symbol} RESUMED — monitor restored")
            logger.info(f"{symbol} resumed trading — monitor restored")

    def _on_luld_status(self, symbol: str, band_data: dict):
        """Track active LULD bands for held positions."""
        if not self.entry_manager:
            return
        pos = self.entry_manager.positions.get(symbol)
        if not pos:
            return
        pos["luld_state"] = band_data.get("band_state") or band_data.get("indicator") or "active"
        pos["luld_upper_band"] = band_data.get("upper_band")
        pos["luld_lower_band"] = band_data.get("lower_band")
        pos["luld_updated_at"] = time.time()
        side = pos.get("side", "long")
        entry_price = float(pos.get("entry_price", 0) or 0)
        lower = float(band_data.get("lower_band", 0) or 0)
        upper = float(band_data.get("upper_band", 0) or 0)
        pos["luld_at_risk"] = False
        if side == "long" and lower > 0 and entry_price > 0:
            distance_pct = ((entry_price - lower) / entry_price) * 100
            if distance_pct < 3.0:
                pos["luld_at_risk"] = True
                logger.warning(f"⚠️ {symbol} LULD lower band ${lower:.2f} is {distance_pct:.1f}% from entry ${entry_price:.2f}")
        elif side == "short" and upper > 0 and entry_price > 0:
            distance_pct = ((upper - entry_price) / entry_price) * 100
            if distance_pct < 3.0:
                pos["luld_at_risk"] = True
                logger.warning(f"⚠️ {symbol} LULD upper band ${upper:.2f} is {distance_pct:.1f}% from short entry ${entry_price:.2f}")
        log_activity(
            "alert",
            f"⚠️ {symbol} LULD bands: {band_data.get('lower_band')} - {band_data.get('upper_band')}",
        )

    async def _on_trade_update_fill(self, data: dict, event: str):
        """Capture entry fill timestamps from trade-update events."""
        if event != "fill":
            return
        order = data.get("order", {})
        symbol = order.get("symbol", "")
        if not symbol or order.get("type") == "trailing_stop":
            return
        if not self.entry_manager:
            return
        pos = self.entry_manager.positions.get(symbol)
        if not pos:
            return

        filled_at = self._parse_iso_ts(order.get("filled_at"))
        order_side = str(order.get("side", "") or "").lower()
        expected_exit_side = "buy" if pos.get("side", "long") == "short" else "sell"
        if pos.get("exit_pending") and order_side == expected_exit_side:
            if pos.get("exit_recorded"):
                return
            order_id = str(order.get("id", "") or "")
            pending_order_id = str(pos.get("exit_order_id", "") or "")
            if pending_order_id and order_id and pending_order_id != order_id:
                return
            fill_price = float(order.get("filled_avg_price", 0) or 0)
            filled_qty = float(order.get("filled_qty", pos.get("quantity", 0)) or pos.get("quantity", 0) or 0)
            if fill_price <= 0 or filled_qty <= 0:
                return
            pos["exit_fill_qty"] = filled_qty
            trade_record = self._build_confirmed_exit_trade(
                pos,
                fill_price=fill_price,
                qty=filled_qty,
                reason=str(pos.get("last_exit_reason", "broker_exit_fill") or "broker_exit_fill"),
                exit_time=filled_at,
                order=order,
                fill_source="trade_update",
            )
            self._record_realized_exit(trade_record)
            return
        if filled_at and not pos.get("fill_timestamp"):
            pos["fill_timestamp"] = filled_at
            pos["fill_timestamp_source"] = "trade_update"
        fill_price = float(order.get("filled_avg_price", 0) or 0)
        if fill_price > 0:
            pos["fill_price"] = fill_price
        pos["order_status"] = "filled"

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
                **self._compute_signal_latency_fields(pos),
            }
            self._record_realized_exit(trade_record)

            pos["_exit_recorded"] = True
            emoji = "✅" if pnl >= 0 else "❌"
            logger.info(f"{emoji} WS TRAILING STOP: {symbol} @ ${fill_price:.2f} P&L: ${pnl:.2f} ({pnl_pct:+.1f}%)")
            log_activity("trade", f"{emoji} {symbol} stopped out (WS): ${pnl:.2f} ({pnl_pct:+.1f}%)")
            options_engine = getattr(self, "options_engine", None)
            if options_engine:
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(self._close_paired_options(symbol, reason="underlying_trailing_stop_ws"))
                except RuntimeError:
                    pass
        finally:
            pos.pop("_exit_recording", None)

    def _get_options_monitor(self):
        monitor = getattr(self, "options_monitor", None)
        if not monitor and getattr(self, "options_engine", None):
            monitor = OptionsMonitor(self)
            self.options_monitor = monitor
        return monitor

    @staticmethod
    def _is_regular_market_hours() -> bool:
        return OptionsMonitor.is_regular_market_hours()

    async def _close_paired_options(self, underlying_symbol: str, reason: str = "underlying_exit"):
        monitor = self._get_options_monitor()
        if not monitor:
            return
        await monitor.close_paired_options(underlying_symbol, reason=reason)

    async def _execute_option_exit_action(self, contract_symbol: str, action: dict) -> bool:
        monitor = self._get_options_monitor()
        if not monitor:
            return False
        return await monitor.execute_exit_action(contract_symbol, action)

    async def _monitor_options_once(self):
        monitor = self._get_options_monitor()
        if not monitor:
            return
        await monitor.monitor_once()

    async def _options_monitor_loop(self):
        monitor = self._get_options_monitor()
        if not monitor:
            return
        await monitor.monitor_loop()

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
        if getattr(self, "unusual_whales_stream", None):
            await self.unusual_whales_stream.stop()
        if getattr(self, "copy_trader_monitor", None) and getattr(self.copy_trader_monitor, "stop_stream", None):
            self.copy_trader_monitor.stop_stream()
        # Save final state
        persistence.save_positions(self.entry_manager.positions if self.entry_manager else {})
        if getattr(self, "options_engine", None):
            persistence.save_options_positions(self.options_engine.positions)
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
    # ── Single-instance guard via PID file ──
    import fcntl
    _lock_path = Path(__file__).parent.parent / "data" / "velox.lock"
    _lock_path.parent.mkdir(parents=True, exist_ok=True)
    _lock_fd = open(_lock_path, "w")
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fd.write(str(os.getpid()))
        _lock_fd.flush()
    except (IOError, OSError):
        print(f"⚠️  Velox is already running (lock: {_lock_path}). Exiting duplicate.")
        sys.exit(0)

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stopped by user")
    except Exception as e:
        logger.error(f"Fatal: {e}")
        sys.exit(1)
    finally:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            _lock_fd.close()
            _lock_path.unlink(missing_ok=True)
        except Exception:
            pass
