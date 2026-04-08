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
from functools import partial
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional

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
from src.signals.overnight_context import OvernightContext
from src.signals.unusual_whales import UnusualWhalesClient
from src.context.session_context import SessionContext
from src.data.symbol_state_machine import SymbolStateTracker
from src.data.state_store import StateStore
from src.execution.trigger_engine import TriggerEngine
from src.execution.pre_trade_cost import PreTradeCostEstimator
from src.execution.dust_policy import should_auto_liquidate
from src.risk.concentration_guard import ConcentrationGuard
from src.signals.short_interest import ShortInterestScanner
from src.signals.sector_rotation import SectorRotationModel, SECTOR_STOCKS
from src.streams.market_stream import MarketStream
from src.streams.trade_stream import TradeStream
from src.streams.unusual_whales_stream import UnusualWhalesStream
from src.dashboard.dashboard import log_activity
from src import persistence
from src.entry.entry_manager import EntryManager
from src.exit.exit_manager import ExitManager
from src.exit.extended_hours_guard import ExtendedHoursGuard
from src.exit.order_conflicts import cancel_conflicting_exit_orders
from src.exit.profit_ratchet import ProfitRatchet
from src.risk.risk_manager import RiskManager, SECTOR_MAP
from src.risk import book_allocator
from src.ai.observer import Observer
from src.ai.advisor import Advisor
from src.ai.tuner import Tuner
from src.ai.game_film import GameFilm
from src.ai.position_manager import PositionManager
from src.ai import trade_history
from src.ai.post_exit_tracker import check_post_exit_prices
from src.ai.provider_health import get_provider_health_tracker
from src.analytics.book_scoreboard import BookScoreboard
from src.analytics.daily_review import build_daily_review
from src.analytics.latency_tracker import LatencyTracker
from src.agents.jury import JuryVerdict
from src.agents import risk_agent as book_risk_agent
from src.agents.risk_agent import STRATEGY_MAX_POSITIONS
from src.agents.orchestrator import Orchestrator
from src.dashboard.dashboard import start_dashboard
from src.data import entry_controls
from src.data import strategy_controls
from src.data.bar_context import bar_context_is_stale
from src.data.trade_schema import normalize_trade_record
from src.data.pending_setups import get_pending_setup, list_pending_setups, remove_pending_setup, upsert_pending_setup
from src.data.setup_identity import build_material_change_signature, build_setup_id, normalize_symbol_state
from src.data.symbols import is_supported_trade_symbol, normalize_trade_symbol
from src.data.setup_snapshots import record_setup_snapshot
from src.data.signal_attribution import extract_signal_sources, derive_strategy_tag
from src.data.trading_calendar import EASTERN, is_market_hours, trading_session_day
from src.data.strategy_tags import normalize_strategy_tag
from src.data.strategy_playbook import (
    annotate_candidate,
    bias_matches_direction,
    extract_watchlist_symbols,
    normalize_bias_label,
    score_directional_biases,
)
from src.data.technicals import compute_technicals, get_cached_rsi
from src.signals.mode_classifier import (
    build_mode_features,
    classify_mode,
    mode_features_from_dict,
    normalize_direction_constraint,
    normalize_mode,
)
from src.signals.play_resolver import TriggerSpec, evaluate_trigger, resolve_play
from src.options.options_monitor import OptionsMonitor
from src.reconciliation.reconciler import Reconciler

_DATA_DIR = Path(__file__).parent.parent / "data"
_SHADOW_TRADES_FILE = _DATA_DIR / "shadow_trades.json"


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
        self._uw_signal_drain_task = None
        self._fast_path_pending = set()
        self._jury_vetoed_symbols: Dict[str, float] = {}
        self._fast_path_eval_queue = asyncio.Queue(maxsize=50)
        self._pending_live_refresh_queue = asyncio.Queue(maxsize=50)
        self._recent_uw_signal_keys: Dict[str, float] = {}
        self._pending_live_refresh_at: Dict[str, float] = {}
        self._candidate_processing_lock = asyncio.Lock()
        self._book_allocator_snapshot: Dict[str, Dict] = {}
        self._book_allocator_snapshot_key = None
        self._book_allocator_snapshot_at = 0.0
        self._last_daily_reset_date = None
        self._processed_copy_trader_exit_ids = set()
        self._recorded_realized_keys = set()
        self._symbol_reentry_cooldown_until: Dict[str, float] = {}
        self._latest_broker_position_symbols = set()
        self._latest_broker_positions_synced_at = 0.0
        self._tomorrow_thesis_cache = None
        self._tomorrow_thesis_cache_at = 0.0
        self._last_daily_review_date: Optional[str] = None
        self._last_book_scoreboard_refresh_at = 0.0

        # Components (initialized in initialize())
        self.alpaca_client: AlpacaClient = None
        self.polygon_client: PolygonClient = None
        self.scanner: Scanner = None
        self.sentiment_analyzer: SentimentAnalyzer = None
        self.stocktwits_client: StockTwitsClient = None
        self.twitter_client: TwitterSentimentClient = None
        self.entry_manager: EntryManager = None
        self.exit_manager: ExitManager = None
        self.profit_ratchet: ProfitRatchet = None
        self.risk_manager: RiskManager = None
        self.options_monitor: OptionsMonitor = None
        self.reconciler: Optional[Reconciler] = None
        self.overnight_context: Optional[OvernightContext] = None
        self.book_scoreboard: Optional[BookScoreboard] = None
        self.latency_tracker: Optional[LatencyTracker] = None
        self.provider_health = None

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

    async def _provider_health_check(self) -> Dict[str, Dict]:
        """
        Validate jury providers before entering the market loop.
        This surfaces configuration/quota/model failures explicitly at startup.
        """
        from src.agents.base_agent import call_claude, call_gpt, call_grok

        prompt = (
            'Return only JSON: {"decision":"SKIP","size_pct":0,"trail_pct":3,'
            '"reasoning":"health_check","confidence":1}'
        )
        checks = [("claude", call_claude), ("gpt", call_gpt), ("grok", call_grok)]
        status: Dict[str, Dict] = {}
        rate_limited_providers: List[str] = []

        def _is_rate_limited(msg: str) -> bool:
            text = str(msg or "").lower()
            return "429" in text or "rate limit" in text or "too many requests" in text

        for name, caller in checks:
            started = time.time()
            ok = False
            err = ""
            try:
                result = await asyncio.wait_for(caller(prompt, max_tokens=120), timeout=30)
                ok = isinstance(result, dict) and bool(result)
                if not ok:
                    err = "no_response_or_invalid_json"
                    if name == "gpt":
                        # GPT commonly warms up behind provider-side burst limits right after restart.
                        await asyncio.sleep(2.0)
                        retry = await asyncio.wait_for(caller(prompt, max_tokens=120), timeout=30)
                        ok = isinstance(retry, dict) and bool(retry)
                        if ok:
                            err = ""
            except Exception as e:
                err = str(e)
                if name == "gpt" and _is_rate_limited(err):
                    await asyncio.sleep(2.0)
                    try:
                        retry = await asyncio.wait_for(caller(prompt, max_tokens=120), timeout=30)
                        ok = isinstance(retry, dict) and bool(retry)
                        if ok:
                            err = ""
                    except Exception as retry_e:
                        err = str(retry_e)
            status[name] = {
                "ok": ok,
                "error": err,
                "latency_ms": int((time.time() - started) * 1000),
            }
            if not ok and _is_rate_limited(err):
                rate_limited_providers.append(name)
            if ok:
                logger.info(f"✅ Provider health: {name} ok ({status[name]['latency_ms']}ms)")
            else:
                log_fn = logger.warning if _is_rate_limited(err) else logger.error
                log_fn(
                    f"❌ Provider health: {name} failed "
                    f"({status[name]['latency_ms']}ms) err={err or 'unknown'}"
                )

        failed = [name for name, info in status.items() if not info.get("ok")]
        non_rate_limit_failures = [name for name in failed if name not in rate_limited_providers]
        if len(non_rate_limit_failures) >= 2:
            logger.critical(
                f"🚨 Provider panel degraded at startup: failed={failed}. "
                "Jury reliability is reduced until providers recover."
            )
        return status

    def _refresh_provider_health_layer(self):
        provider_health = getattr(self, "provider_health", None)
        if not provider_health or not isinstance(getattr(self, "ai_layers", None), dict):
            return
        try:
            self.ai_layers["provider_health"] = provider_health.get_dashboard_status()
            self.ai_layers["provider_health_policy"] = provider_health.get_policy()
        except Exception as e:
            logger.debug(f"Provider health layer refresh failed: {e}")

    def _refresh_book_scoreboard(self):
        scoreboard = getattr(self, "book_scoreboard", None)
        if not scoreboard:
            return
        try:
            trades = trade_history.load_all()
            positions = self.entry_manager.get_positions() if self.entry_manager else []
            funnel_summary = self.state_store.get_funnel_summary() if getattr(self, "state_store", None) else None
            scoreboard.refresh(trades, positions, funnel_summary)
            self._last_book_scoreboard_refresh_at = time.time()
        except Exception as e:
            logger.debug(f"Book scoreboard refresh failed: {e}")

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
        self.profit_ratchet = ProfitRatchet()

        # Extended hours guard (software-managed protection outside regular session)
        self.extended_guard = ExtendedHoursGuard(
            alpaca_client=self.alpaca_client,
            polygon_client=self.polygon_client,
        )

        self.overnight_context = OvernightContext(
            alpaca_client=self.alpaca_client,
            polygon_client=self.polygon_client,
        )

        # Session Context Stack (institutional morning stack equivalent)
        self.session_context = SessionContext(
            fred_client=self.fred_client,
            polygon_client=self.polygon_client,
            finnhub_client=self.finnhub_client,
            overnight_context=self.overnight_context,
            sector_model=self.sector_model,
        )

        # SQLite-backed state store — durable runtime persistence
        self.state_store = StateStore()
        self.session_context._state_store = self.state_store

        # Symbol state machine — enforces lifecycle transitions, prevents duplicates
        self.symbol_state_tracker = SymbolStateTracker(state_store=self.state_store)

        # Trigger engine — continuous background monitoring of pending setups
        self.trigger_engine = TriggerEngine(state_store=self.state_store)

        # Pre-trade cost estimator — Bloomberg TRA equivalent
        self.pre_trade_cost = PreTradeCostEstimator(
            broker_client=self.alpaca_client,
            polygon_client=self.polygon_client,
        )

        # Portfolio concentration guard — Bloomberg PORT equivalent (V1)
        self.concentration_guard = ConcentrationGuard(
            polygon_client=self.polygon_client,
            alpaca_client=self.alpaca_client,
        )
        self.provider_health = get_provider_health_tracker()
        self.book_scoreboard = BookScoreboard()
        self.latency_tracker = LatencyTracker()

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
            if bool(getattr(settings, "OPTIONS_PILOT_ENABLED", False)):
                logger.info("🎯 Options engine live — pilot entries enabled")
            else:
                logger.info("🎯 Options engine live — management only (pilot entries disabled)")
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
            "overnight_bias_summary": None,
            "overnight_bias": {},
            "short_verdicts_blocked": 0,
            "last_short_block_reason": None,
            "last_uw_stream_signal": None,
        }
        await self._provider_health_check()
        self._refresh_provider_health_layer()

        # ── Fail-Closed Startup: broker is canonical ──────────────────
        self._broker_ready = False
        saved_positions = persistence.load_positions()
        saved_recently_removed = persistence.load_recently_removed_positions()
        if saved_recently_removed:
            self.entry_manager._recently_removed_positions = self.entry_manager._prune_recently_removed_positions(
                saved_recently_removed
            )
        broker_symbols = {p.get("symbol") for p in (self.entry_manager.get_positions() or [])}
        ghost_count = 0
        restored_count = 0
        if saved_positions:
            _merge_fields = (
                "peak_price", "ratchet_floor_pct", "ratchet_peak_pnl_pct",
                "ratchet_limit_order_id", "ratchet_order_type",
                "hard_stop_order_id", "hard_stop_price", "hard_stop_pct", "hard_stop_flags",
                "entry_time", "signal_timestamp", "entry_order_timestamp",
                "fill_timestamp", "fill_timestamp_source", "fill_price",
                "entry_price", "strategy_tag", "signal_tier", "holding_horizon",
                "entry_quality", "overnight_context", "entry_reason_code",
                "allocator_status", "allocator_recommended_action", "allocator_control_state",
                "entry_model_votes", "risk_constraints_applied",
                "dead_money_tightened", "dead_money", "order_state",
                "mfe_pct", "mae_pct", "time_to_green_seconds", "time_to_peak_seconds",
            )
            for sym, pos in saved_positions.items():
                if sym in broker_symbols:
                    if sym not in self.entry_manager.positions:
                        self.entry_manager.positions[sym] = pos
                        restored_count += 1
                    else:
                        existing = self.entry_manager.positions[sym]
                        for field in _merge_fields:
                            if field in pos and pos[field] is not None:
                                existing.setdefault(field, pos[field])
                                if field == "peak_price" and pos[field] is not None:
                                    side = str(existing.get("side", "long") or "long").lower()
                                    saved_peak = float(pos[field] or 0)
                                    cur_peak = float(existing.get("peak_price", 0) or 0)
                                    if saved_peak > 0:
                                        if side == "short":
                                            existing["peak_price"] = min(saved_peak, cur_peak) if cur_peak > 0 else saved_peak
                                        else:
                                            existing["peak_price"] = max(saved_peak, cur_peak)
                                elif field == "entry_time" and pos[field]:
                                    existing[field] = pos[field]
                                elif field == "entry_price" and pos[field]:
                                    existing[field] = pos[field]
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
        self._cache_broker_position_symbols(self.entry_manager.get_positions())

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
            self._repair_last_consensus_snapshot()
        self._refresh_provider_health_layer()
        self._refresh_book_scoreboard()

        # Dashboard
        start_dashboard(bot=self)

        # ── WebSocket streams ─────────────────────────────────────
        # Market data stream: real-time prices + breakout detection
        self.market_stream.set_breakout_callback(self._on_breakout_detected)
        self.market_stream.set_trade_callback(self._on_market_trade)
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
        try:
            shorting_status = self._refresh_shorting_readiness()
            if shorting_status.get("ready"):
                logger.info(
                    f"🩳 Shorting readiness: ready "
                    f"(paper={shorting_status.get('paper')}, PDT={shorting_status.get('daytrade_count')}, "
                    f"equity=${float(shorting_status.get('equity', 0) or 0):,.2f})"
                )
            else:
                logger.warning(
                    "🩳 Shorting readiness: not ready "
                    f"reasons={','.join(shorting_status.get('reasons', []) or ['unknown'])}"
                )
        except Exception as e:
            logger.warning(f"Shorting readiness check failed: {e}")

        # Force a fresh reconciliation baseline at startup so we do not carry
        # stale critical state across restarts.
        if self.reconciler:
            try:
                recon_state = await asyncio.get_event_loop().run_in_executor(None, self.reconciler.snapshot)
                self.ai_layers["reconciliation"] = recon_state.get("reconciliation", {})
                self.ai_layers["broker_truth"] = recon_state.get("broker", {})
                recon = recon_state.get("reconciliation", {}) or {}
                logger.info(
                    f"🧭 Startup reconciliation baseline: {recon.get('status', 'unknown')} "
                    f"reasons={','.join(recon.get('reasons', []) or [])}"
                )
            except Exception as e:
                logger.warning(f"Startup reconciliation baseline failed: {e}")

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
        monitor_task = asyncio.create_task(self._monitor_positions_loop())
        trigger_task = asyncio.create_task(
            self.trigger_engine.start(market_stream=self.market_stream)
        )

        # Start Exit Agent monitoring loop
        await self.orchestrator.start_exit_agent()

        scan_interval = settings.SCAN_INTERVAL_SECONDS
        self.scan_regime = self._sanitize_scan_regime_label(
            self.ai_layers.get("scan_regime") or self.ai_layers.get("scan_regime_raw")
        )
        self.scan_regime_raw = self._sanitize_scan_regime_label(
            self.ai_layers.get("scan_regime_raw") or self.scan_regime
        )
        self._scan_regime_history = []
        self.ai_layers["scan_regime"] = self.scan_regime
        self.ai_layers["scan_regime_raw"] = self.scan_regime_raw
        self.ai_layers["scan_interval_seconds"] = scan_interval
        self._refresh_book_allocator_layer(self._current_session_type_label(self._entry_session_label()))
        self._refresh_background_scan_surface()
        monitor_interval = 5
        last_scan = 0
        last_equity_sync = 0
        last_state_save = 0
        last_reconciliation = 0

        try:
            while self.running:
                now = time.time()
                session_type = self._current_session_type_label(self._entry_session_label())
                self._roll_daily_state_if_needed()
                self._refresh_provider_health_layer()

                # Sync equity from Alpaca every 60s
                if now - last_equity_sync >= 60 and self.alpaca_client:
                    last_equity_sync = now
                    try:
                        acct = self.alpaca_client.get_account()
                        self.risk_manager.update_equity(
                            acct.get("equity", self.risk_manager.equity),
                            daytrade_count=acct.get("daytrade_count"),
                        )
                        self._refresh_shorting_readiness()
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
                        self._refresh_book_allocator_layer(session_type)
                        self._refresh_background_scan_surface()
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
                    persistence.save_recently_removed_positions(
                        getattr(self.entry_manager, "_recently_removed_positions", {}) if self.entry_manager else {}
                    )
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

                # ── SESSION CONTEXT (timeout-guarded to never block scan cycle) ──
                if self.session_context.is_stale():
                    try:
                        await asyncio.wait_for(self.session_context.refresh(), timeout=15.0)
                    except asyncio.TimeoutError:
                        logger.warning("SessionContext refresh timed out (15s) — using cached snapshot")
                    except Exception as e:
                        logger.debug(f"SessionContext refresh error: {e}")

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
                            stream_limit = max(1, int(getattr(settings, "STREAM_SUBSCRIBE_SYMBOL_LIMIT", 25) or 25))
                            top_symbols = [c["symbol"] for c in candidates[:stream_limit]]
                            # Also keep streaming positions
                            pos_symbols = [p["symbol"] for p in self.entry_manager.get_positions()]
                            pending_symbols = [row.get("symbol") for row in list_pending_setups(limit=stream_limit)]
                            # Feed prev_close data for accurate daily % in breakout alerts
                            prev_closes = {c["symbol"]: c.get("prev_close", 0) for c in candidates if c.get("prev_close", 0) > 0}
                            self.market_stream.set_prev_closes(prev_closes)
                            await self.market_stream.subscribe(top_symbols + pos_symbols + pending_symbols)
                        await self._process_candidates_serial(candidates)

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
                        self._refresh_book_allocator_layer(session_type)
                    except Exception as e:
                        logger.error(f"Scan error: {e}")

                # ── BREAKOUT FAST-PATH ROUTING ────────────────────
                try:
                    await self._process_breakout_queue()
                except Exception as e:
                    logger.error(f"Breakout queue error: {e}")

                try:
                    await self._process_pending_live_refresh_queue()
                except Exception as e:
                    logger.error(f"Pending live refresh queue error: {e}")

                # ── MONITOR pending orders (adjust stale limits) ──
                try:
                    await self._monitor_pending_orders()
                except Exception as e:
                    logger.debug(f"Pending order monitor error: {e}")

                # ── MONITOR positions (now runs in independent _monitor_positions_loop task) ──

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

                await asyncio.sleep(monitor_interval)

        except Exception as e:
            logger.error(f"Fatal error in main loop: {e}")
            raise
        finally:
            ai_task.cancel()
            if options_task:
                options_task.cancel()
            monitor_task.cancel()
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

        # ── POST-EXIT TRACKING: Check where prices went after exits (every 5 min) ──
        last_post_exit = float(state.get("last_post_exit_check", 0) or 0)
        if now - last_post_exit > 300:
            try:
                async def _get_price(symbol):
                    if self.polygon_client:
                        return await asyncio.get_event_loop().run_in_executor(
                            None, self.polygon_client.get_price, symbol
                        )
                    return 0
                await check_post_exit_prices(_get_price)
                state["last_post_exit_check"] = now
            except Exception as e:
                logger.debug(f"Post-exit tracking failed: {e}")

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
                self._refresh_provider_health_layer()
                if (time.time() - float(getattr(self, "_last_book_scoreboard_refresh_at", 0.0) or 0.0)) >= 300:
                    self._refresh_book_scoreboard()

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

                # Daily Operating Review at 4:15 PM ET (once per day)
                await self._maybe_run_daily_review()

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"AI layer error: {e}")

            await asyncio.sleep(30)  # Check every 30s, layers self-throttle

    async def _maybe_run_daily_review(self):
        """Run the daily operating review once at 4:15 PM ET."""
        try:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")
            now_et = datetime.now(et)
        except Exception:
            return
        if now_et.hour != 16 or now_et.minute < 15 or now_et.minute > 25:
            return
        today_str = trading_session_day()
        if self._last_daily_review_date == today_str:
            return
        self._last_daily_review_date = today_str
        try:
            session_snap = None
            if hasattr(self, "session_context") and self.session_context and hasattr(self.session_context, "snapshot") and self.session_context.snapshot:
                session_snap = self.session_context.snapshot.to_dict() if hasattr(self.session_context.snapshot, "to_dict") else {}
            today_trades = [
                t for t in trade_history.load_all()
                if trading_session_day(float(t.get("exit_time", t.get("recorded_at", 0)) or 0)) == today_str
            ]
            shadow_trades = self._load_shadow_trades()
            funnel_summary = None
            if hasattr(self, "state_store") and self.state_store:
                funnel_summary = self.state_store.get_funnel_summary()
            book_summary = {}
            if getattr(self, "book_scoreboard", None):
                self._refresh_book_scoreboard()
                book_summary = self.book_scoreboard.get_summary() or {}
            review = build_daily_review(
                date_str=today_str,
                session_snapshot=session_snap,
                funnel_summary=funnel_summary,
                book_scores=book_summary.get("books", []),
                mode_scores=book_summary.get("modes", []),
                trades_today=today_trades,
                shadow_trades=shadow_trades,
                provider_snapshot=self.provider_health.get_snapshot() if getattr(self, "provider_health", None) else None,
            )
            review_dir = _DATA_DIR / "daily_reviews"
            review_dir.mkdir(parents=True, exist_ok=True)
            review_path = review_dir / f"{today_str}.json"
            review_path.write_text(json.dumps(review.to_dict(), indent=2, default=str))
            logger.info(f"📋 Daily operating review saved → {review_path.name}")
        except Exception as e:
            logger.error(f"Daily review generation failed: {e}")

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

    @staticmethod
    def _entry_session_label(now: Optional[datetime] = None) -> str:
        try:
            import zoneinfo

            et = zoneinfo.ZoneInfo("US/Eastern")
            current = now.astimezone(et) if now else datetime.now(et)
        except Exception:
            current = now or datetime.now()
        if current.weekday() >= 5:
            return "closed"
        hour = current.hour
        minute = current.minute
        if (hour == 9 and minute >= 30) or (10 <= hour < 16):
            return "regular"
        if (4 <= hour < 9) or (hour == 9 and minute < 30):
            return "pre"
        if 16 <= hour < 20:
            return "after"
        return "overnight"

    def _derive_signal_tier(self, candidate: Dict) -> str:
        premium_candidates = [
            candidate.get("uw_total_premium"),
            candidate.get("premium"),
            candidate.get("bullish_premium"),
            candidate.get("bearish_premium"),
        ]
        max_premium = 0.0
        for value in premium_candidates:
            try:
                max_premium = max(max_premium, float(value or 0))
            except Exception:
                continue
        if max_premium >= 500000:
            return "tier_1"

        source = str(candidate.get("source", "") or "").lower()
        strategy_tag = str(candidate.get("strategy_tag", "") or "").lower()
        if any(tag in source for tag in ("stocktwits", "grok_x", "watchlist")):
            return "tier_3"
        if strategy_tag.startswith("copy_trader_"):
            return "tier_3"
        return "tier_2"

    @staticmethod
    def _derive_holding_horizon(candidate: Dict) -> str:
        if candidate.get("holding_horizon"):
            return str(candidate.get("holding_horizon") or "intraday")
        strategy_tag = str(candidate.get("strategy_tag", "") or "").lower()
        if any(tag in strategy_tag for tag in ("pharma", "copy_trader", "watchlist")):
            return "swing"
        if candidate.get("earnings") or candidate.get("earnings_date") or candidate.get("catalyst_date"):
            return "multiday"
        return "intraday"

    @staticmethod
    def _build_uw_flow_summary(candidate: Dict) -> str:
        premium = float(candidate.get("uw_total_premium", 0) or 0)
        direction = "SHORT" if str(candidate.get("side", "")).lower() == "short" else "LONG"
        sentiment = str(candidate.get("uw_flow_sentiment", "neutral") or "neutral")
        net_premium = str(candidate.get("uw_net_premium", "") or "None")
        net_bias = str(candidate.get("uw_net_premium_bias", "neutral") or "neutral")
        dark_pool_bias = str(candidate.get("uw_dark_pool_bias", "neutral") or "neutral")
        parts = [
            f"premium=${premium:,.0f}" if premium > 0 else "premium=None",
            f"direction={direction}",
            f"sentiment={sentiment}",
            f"net_premium_bias={net_bias}",
            f"dark_pool_bias={dark_pool_bias}",
        ]
        if net_premium and net_premium != "None":
            parts.append(f"net_premium={net_premium}")
        news = str(candidate.get("uw_news_summary", "") or "").strip()
        chain = str(candidate.get("uw_chain_summary", "") or "").strip()
        if news:
            parts.append(f"news={news}")
        if chain:
            parts.append(f"chain={chain}")
        return " | ".join(parts)

    def get_overnight_bias_context(self, refresh: bool = False) -> Dict:
        overnight_context = getattr(self, "overnight_context", None)
        if not overnight_context:
            return {}
        try:
            bias = overnight_context.get_bias(refresh=refresh) or {}
        except Exception as e:
            logger.debug(f"Overnight bias fetch failed: {e}")
            return {}
        ai_layers = getattr(self, "ai_layers", None)
        if isinstance(ai_layers, dict):
            ai_layers["overnight_bias"] = bias
            ai_layers["overnight_bias_summary"] = OvernightContext.format_summary(bias)
        return bias

    @staticmethod
    def _derive_entry_quality(candidate: Dict) -> str:
        try:
            range_pct = float(candidate.get("range_pct", 50) or 50)
        except Exception:
            range_pct = 50.0
        try:
            change_pct = float(candidate.get("change_pct", 0) or 0)
        except Exception:
            change_pct = 0.0
        if range_pct > 95:
            return "at_highs"
        if range_pct < 70 and change_pct > 3:
            return "pullback"
        return "neutral"

    @staticmethod
    def _current_session_type_label(session_label: str) -> str:
        if session_label == "pre":
            return "pre"
        if session_label == "after":
            return "after"
        if session_label == "regular":
            return "regular"
        return "overnight"

    @staticmethod
    def _sanitize_scan_regime_label(value) -> str:
        label = str(value or "").strip().lower()
        if label in {"risk_on", "risk_off", "mixed", "choppy"}:
            return label
        return "mixed"

    def _refresh_book_allocator_layer(self, session_type: str) -> None:
        if not bool(getattr(settings, "BOOK_ALLOCATOR_ENABLED", True)):
            return
        if not getattr(self, "entry_manager", None):
            return
        try:
            positions = self.entry_manager.get_positions() if self.entry_manager else []
            market_regime = str(
                getattr(self, "scan_regime", "")
                or getattr(self, "scan_regime_raw", "")
                or "mixed"
            )
            self._get_book_allocator_snapshot(market_regime, positions, session_type)
        except Exception as e:
            logger.debug(f"Book allocator refresh error: {e}")

    def _refresh_background_scan_surface(self) -> None:
        scanner = getattr(self, "scanner", None)
        if not scanner or not getattr(scanner, "record_background_research_cycle", None):
            return
        try:
            live_candidates = scanner.get_cached_candidates()
            research_rows = scanner.get_research_universe()
            if not live_candidates and not research_rows:
                return
            regime = str(
                getattr(self, "scan_regime", "")
                or getattr(self, "scan_regime_raw", "")
                or scanner.get_last_market_regime()
                or "mixed"
            )
            scanner.record_background_research_cycle(
                live_candidates=live_candidates,
                research_rows=research_rows,
                regime=regime,
            )
        except Exception as e:
            logger.debug(f"Background scan surface refresh error: {e}")

    def _repair_last_consensus_snapshot(self) -> None:
        """Repair stale persisted dashboard context from live open-position metadata."""
        snapshot = self.ai_layers.get("last_consensus")
        if not isinstance(snapshot, dict):
            return

        decision = str(snapshot.get("decision", "") or "").upper()
        if decision not in {"BUY", "SHORT"}:
            return

        symbol = str(snapshot.get("symbol", "") or "").upper()
        if not symbol or not getattr(self, "entry_manager", None):
            return

        position = None
        positions_map = getattr(self.entry_manager, "positions", {}) or {}
        if isinstance(positions_map, dict):
            position = positions_map.get(symbol)
        if not position and getattr(self.entry_manager, "get_positions", None):
            for row in self.entry_manager.get_positions() or []:
                if str(row.get("symbol", "") or "").upper() == symbol:
                    position = row
                    break
        if not isinstance(position, dict):
            return

        if str(snapshot.get("setup_mode", "") or "").lower() in {"", "invalid"}:
            repaired_mode = str(position.get("setup_mode", "") or "").strip()
            if repaired_mode:
                snapshot["setup_mode"] = repaired_mode
        if str(snapshot.get("timing_state", "") or "").lower() in {"", "mode_conflict"}:
            repaired_timing = str(position.get("timing_state", "") or "").strip()
            if repaired_timing:
                snapshot["timing_state"] = repaired_timing
        if not str(snapshot.get("best_play", "") or "").strip():
            repaired_play = str(position.get("best_play", "") or "").strip()
            if repaired_play:
                snapshot["best_play"] = repaired_play
        if str(snapshot.get("direction_constraint", "") or "").lower() in {"", "none"}:
            repaired_constraint = str(position.get("direction_constraint", "") or "").strip()
            if repaired_constraint:
                snapshot["direction_constraint"] = repaired_constraint
        if not str(snapshot.get("hold_style", "") or "").strip():
            repaired_hold_style = str(
                position.get("hold_style", position.get("holding_horizon", ""))
                or ""
            ).strip()
            if repaired_hold_style:
                snapshot["hold_style"] = repaired_hold_style
        if str(snapshot.get("timing_state", "") or "").lower() in {"", "mode_conflict"} and bool(snapshot.get("entry_now")):
            snapshot["timing_state"] = "enter_now"

    @staticmethod
    def _target_size_pct_for_candidate(
        candidate: Dict,
        verdict,
    ) -> float:
        signal_tier = str(candidate.get("signal_tier", "tier_2") or "tier_2").lower()
        agreement = str((getattr(verdict, "consensus_detail", {}) or {}).get("agreement", "") or "").lower()
        target = float(getattr(verdict, "size_pct", 0) or 0)
        if signal_tier == "tier_1" and agreement == "unanimous":
            target = max(target, 6.0)
        elif signal_tier == "tier_1" and agreement in {"majority", "majority_conflict", "majority_two_model"}:
            target = max(target, 5.0)
        elif signal_tier == "tier_1" and agreement == "tier1_probe":
            target = max(target, 2.5)
        elif signal_tier == "tier_2" and agreement in {"majority", "majority_conflict", "majority_two_model", "unanimous"}:
            target = max(target, 4.0)
        elif signal_tier == "tier_3":
            target = min(target, 1.5)
        return round(max(0.0, target), 3)

    @staticmethod
    def _compose_effective_size_pct(
        candidate: Dict,
        verdict,
        tier_size_pct: float,
        risk_cap_pct: float,
    ) -> float:
        target_size_pct = TradingBot._target_size_pct_for_candidate(candidate, verdict)
        risk_cap = max(0.0, float(risk_cap_pct or 0) or 0.0)
        tier_size = max(0.0, float(tier_size_pct or 0) or 0.0)
        strategy_tag = normalize_strategy_tag(candidate.get("strategy_tag", "unknown"), fallback="unknown")

        if strategy_tag == "momentum_long" and tier_size > 0:
            risk_scale = min(1.0, risk_cap / tier_size)
            return round(max(0.0, target_size_pct * risk_scale), 3)

        return round(max(0.0, min(target_size_pct, risk_cap)), 3)

    @staticmethod
    def _book_allocator_position_fingerprint(positions: List[Dict]) -> tuple:
        rows = []
        for pos in positions or []:
            tag = normalize_strategy_tag(pos.get("strategy_tag", "unknown"), fallback="unknown")
            symbol = str(pos.get("symbol", "") or "").upper()
            try:
                notional = float(
                    pos.get("actual_notional", pos.get("notional", pos.get("market_value", 0))) or 0
                )
            except Exception:
                notional = 0.0
            rows.append((tag, symbol, round(abs(notional), 2)))
        return tuple(sorted(rows))

    def _get_book_allocator_snapshot(self, market_regime: str, positions: List[Dict], session_type: str) -> Dict[str, Dict]:
        if not bool(getattr(settings, "BOOK_ALLOCATOR_ENABLED", True)):
            return {}

        if not hasattr(self, "_book_allocator_snapshot"):
            self._book_allocator_snapshot = {}
        if not hasattr(self, "_book_allocator_snapshot_key"):
            self._book_allocator_snapshot_key = None
        if not hasattr(self, "_book_allocator_snapshot_at"):
            self._book_allocator_snapshot_at = 0.0
        if not hasattr(self, "_book_allocator_analytics"):
            self._book_allocator_analytics = {}

        regime = str(market_regime or "mixed").strip().lower() or "mixed"
        session = str(session_type or "regular").strip().lower() or "regular"
        now = time.time()
        cache_seconds = float(getattr(settings, "BOOK_ALLOCATOR_CACHE_SECONDS", 30.0) or 30.0)
        fingerprint = self._book_allocator_position_fingerprint(positions)
        cache_key = (regime, session, fingerprint)

        if (
            cache_key == self._book_allocator_snapshot_key
            and (now - float(self._book_allocator_snapshot_at or 0.0)) < max(1.0, cache_seconds)
        ):
            return dict(self._book_allocator_snapshot)

        analytics = trade_history.get_analytics() or {}
        controls = strategy_controls.load_controls()
        effective_disabled = strategy_controls.get_effective_disabled(controls)
        equity = float(
            getattr(self.risk_manager, "equity", getattr(self.risk_manager, "_equity", 0.0))
            if self.risk_manager
            else 0.0
        )
        snapshot = book_allocator.build_snapshot(
            market_regime=regime,
            session_type=session,
            positions=positions,
            analytics=analytics,
            equity=equity,
        )
        self._book_allocator_analytics = dict(analytics or {})
        for tag, row in snapshot.items():
            row["disabled"] = tag in effective_disabled

        self._book_allocator_snapshot = dict(snapshot)
        self._book_allocator_snapshot_key = cache_key
        self._book_allocator_snapshot_at = now
        if isinstance(getattr(self, "ai_layers", None), dict):
            ranked = sorted(
                snapshot.values(),
                key=lambda row: (
                    float(row.get("allocator_score", 0) or 0),
                    float(row.get("effective_realized_pnl", row.get("realized_pnl", 0)) or 0),
                    -float(row.get("current_exposure_pct", 0) or 0),
                ),
                reverse=True,
            )
            play_report = dict((analytics or {}).get("play_report", {}) or {})
            self.ai_layers["book_allocator"] = {
                "market_regime": regime,
                "session_type": session,
                "books": ranked[:8],
                "play_summary": dict(play_report.get("summary", {}) or {}),
                "top_plays": list((play_report.get("plays", []) or [])[:8]),
                "generated_at": now,
            }
        return dict(snapshot)

    def _allocate_entry_size(
        self,
        *,
        candidate: Dict,
        direction: str,
        verdict,
        requested_size_pct: float,
        positions: List[Dict],
    ) -> Dict:
        if not bool(getattr(settings, "BOOK_ALLOCATOR_ENABLED", True)):
            return {
                "allowed": True,
                "reason": "allocator_disabled",
                "size_pct": round(float(requested_size_pct or 0.0), 3),
                "requested_size_pct": round(float(requested_size_pct or 0.0), 3),
                "size_multiplier": 1.0,
                "strategy_tag": normalize_strategy_tag(candidate.get("strategy_tag", "unknown"), fallback="unknown"),
                "market_regime": str(candidate.get("market_regime", "mixed") or "mixed"),
                "state": "neutral",
                "alignment": "neutral",
                "budget_pct": 0.0,
                "current_exposure_pct": 0.0,
                "remaining_budget_pct": 0.0,
                "utilization_pct": 0.0,
                "confidence": round(float(getattr(verdict, "confidence", 0.0) or 0.0), 1),
                "reason_codes": [],
            }

        market_regime = str(candidate.get("market_regime", "mixed") or "mixed")
        session_type = str(candidate.get("session_type", "regular") or "regular")
        setup_mode = str(candidate.get("setup_mode", "invalid") or "invalid")
        snapshot = self._get_book_allocator_snapshot(market_regime, positions, session_type)
        strategy_tag = normalize_strategy_tag(candidate.get("strategy_tag", "unknown"), fallback="unknown")
        row = dict(snapshot.get(strategy_tag, {}) or {})
        if bool(row.get("disabled")):
            return {
                "allowed": False,
                "reason": "strategy_disabled",
                "strategy_tag": strategy_tag,
                "market_regime": market_regime,
                "state": str(row.get("state", "neutral") or "neutral"),
                "alignment": str(row.get("alignment", "neutral") or "neutral"),
                "budget_pct": float(row.get("budget_pct", 0.0) or 0.0),
                "current_exposure_pct": float(row.get("current_exposure_pct", 0.0) or 0.0),
                "remaining_budget_pct": max(
                    0.0,
                    float(row.get("budget_pct", 0.0) or 0.0) - float(row.get("current_exposure_pct", 0.0) or 0.0),
                ),
                "requested_size_pct": round(float(requested_size_pct or 0.0), 3),
                "size_pct": 0.0,
                "size_multiplier": 0.0,
                "utilization_pct": float(row.get("utilization_pct", 0.0) or 0.0),
                "confidence": round(float(getattr(verdict, "confidence", 0.0) or 0.0), 1),
                "reason_codes": ["strategy_disabled"],
            }

        plan = book_allocator.plan_entry(
            strategy_tag=strategy_tag,
            setup_mode=setup_mode,
            market_regime=market_regime,
            session_type=session_type,
            confidence=float(getattr(verdict, "confidence", 0.0) or 0.0),
            requested_size_pct=float(requested_size_pct or 0.0),
            snapshot=snapshot,
            play_report=(self._book_allocator_analytics or {}).get("play_report", {}),
        )
        logger.info(
            f"🧮 ALLOCATOR {candidate.get('symbol', '?')}: book={strategy_tag} regime={market_regime} "
            f"mode={setup_mode} play={plan.get('play_status')} "
            f"state={plan.get('state')} align={plan.get('alignment')} "
            f"requested={float(plan.get('requested_size_pct', 0) or 0):.2f}% -> "
            f"{float(plan.get('size_pct', 0) or 0):.2f}% "
            f"exposure={float(plan.get('current_exposure_pct', 0) or 0):.2f}%/"
            f"{float(plan.get('budget_pct', 0) or 0):.2f}% "
            f"reason={plan.get('reason')} codes={plan.get('reason_codes', [])}"
        )
        return plan

    @staticmethod
    def _position_strategy_counts(positions: List[Dict]) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for pos in positions or []:
            strategy_tag = normalize_strategy_tag(pos.get("strategy_tag", "unknown"), fallback="unknown")
            counts[strategy_tag] = counts.get(strategy_tag, 0) + 1
        return counts

    @staticmethod
    def _sector_bucket(row: Dict) -> str:
        symbol = str(row.get("symbol", "") or "").upper()
        return str(row.get("sector") or SECTOR_MAP.get(symbol, "unknown") or "unknown").lower()

    @staticmethod
    def _extract_disabled_strategy(risk_brief: Dict) -> str:
        for flag in list((risk_brief or {}).get("constraint_flags", []) or []):
            text = str(flag or "").strip().lower()
            if text.startswith("strategy_disabled_"):
                return text.replace("strategy_disabled_", "", 1) or "unknown"
        return ""

    @staticmethod
    def _shadow_direction_for_strategy(strategy_tag: str) -> str:
        return "SHORT" if "short" in str(strategy_tag or "").lower() else "BUY"

    @staticmethod
    def _load_shadow_trades() -> List[Dict]:
        data = persistence.safe_load_json(_SHADOW_TRADES_FILE, default=list)
        if isinstance(data, list):
            return data
        return []

    @classmethod
    def _update_shadow_trade_extrema(cls, record: Dict) -> Dict:
        entry_price = float(record.get("signal_price", 0) or 0)
        if entry_price <= 0:
            return record
        direction = str(record.get("direction", "BUY") or "BUY").upper()
        returns: List[float] = []
        for key in ("price_1h", "price_4h", "price_eod"):
            price = record.get(key)
            if not isinstance(price, (int, float)):
                continue
            observed_price = float(price or 0)
            if observed_price <= 0:
                continue
            if direction == "SHORT":
                returns.append(((entry_price - observed_price) / entry_price) * 100.0)
            else:
                returns.append(((observed_price - entry_price) / entry_price) * 100.0)
        if returns:
            record["mfe"] = round(max(returns), 2)
            record["mae"] = round(min(returns), 2)
        return record

    @staticmethod
    def _shadow_eod_due(signal_ts: float, now_ts: Optional[float] = None) -> bool:
        if signal_ts <= 0:
            return False
        now_ts = float(now_ts or time.time())
        try:
            import zoneinfo

            et = zoneinfo.ZoneInfo("US/Eastern")
            signal_dt = datetime.fromtimestamp(signal_ts, et)
            now_dt = datetime.fromtimestamp(now_ts, et)
        except Exception:
            signal_dt = datetime.fromtimestamp(signal_ts)
            now_dt = datetime.fromtimestamp(now_ts)
        if now_dt.date() > signal_dt.date():
            return True
        return now_dt.date() == signal_dt.date() and (now_dt.hour, now_dt.minute) >= (16, 0)

    async def _fetch_shadow_price(self, symbol: str) -> float:
        if not symbol:
            return 0.0
        loop = asyncio.get_event_loop()
        alpaca_client = getattr(self, "alpaca_client", None)
        if alpaca_client:
            try:
                price = await loop.run_in_executor(None, alpaca_client.get_latest_price, symbol)
                price = float(price or 0)
                if price > 0:
                    return price
            except Exception as e:
                logger.debug(f"Shadow price lookup via Alpaca failed for {symbol}: {e}")
        polygon_client = getattr(self, "polygon_client", None)
        if polygon_client:
            try:
                price = await loop.run_in_executor(None, polygon_client.get_price, symbol)
                price = float(price or 0)
                if price > 0:
                    return price
            except Exception as e:
                logger.debug(f"Shadow price lookup via Polygon failed for {symbol}: {e}")
        logger.warning(f"👻 Shadow follow-up price unavailable for {symbol} — both Alpaca and Polygon returned 0")
        return 0.0

    def _persist_shadow_record(self, record: Dict):
        rows = self._load_shadow_trades()
        rows.append(self._update_shadow_trade_extrema(dict(record or {})))
        persistence.atomic_write_json(_SHADOW_TRADES_FILE, rows, indent=2)

    async def _refresh_shadow_records(self, limit: int = 20):
        rows = self._load_shadow_trades()
        if not rows:
            return
        now_ts = time.time()
        updated = False
        refreshed = 0
        price_cache: Dict[str, float] = {}

        for row in rows:
            if refreshed >= max(1, int(limit or 1)):
                break
            signal_ts = float(row.get("timestamp", 0) or 0)
            if signal_ts <= 0:
                continue
            symbol = str(row.get("symbol", "") or "").upper()
            if not symbol:
                continue

            age_seconds = max(0.0, now_ts - signal_ts)
            due_fields: List[str] = []
            if row.get("price_1h") is None and age_seconds >= 3600:
                due_fields.append("price_1h")
            if row.get("price_4h") is None and age_seconds >= 14400:
                due_fields.append("price_4h")
            if row.get("price_eod") is None and self._shadow_eod_due(signal_ts, now_ts=now_ts):
                due_fields.append("price_eod")
            if not due_fields:
                continue

            if symbol not in price_cache:
                price_cache[symbol] = await self._fetch_shadow_price(symbol)
            latest_price = float(price_cache.get(symbol, 0) or 0)
            if latest_price <= 0:
                continue

            for field in due_fields:
                row[field] = round(latest_price, 4)
            self._update_shadow_trade_extrema(row)
            updated = True
            refreshed += 1

        if updated:
            persistence.atomic_write_json(_SHADOW_TRADES_FILE, rows, indent=2)

    async def refresh_shadow_trades(self):
        await self._refresh_shadow_records()

    @staticmethod
    def _mode_classifier_enabled() -> bool:
        return bool(getattr(settings, "MODE_CLASSIFIER_ENABLED", True))

    @staticmethod
    def _mode_classifier_enforced() -> bool:
        return bool(getattr(settings, "MODE_CLASSIFIER_ENABLED", True)) and bool(
            getattr(settings, "MODE_CLASSIFIER_ENFORCE", False)
        )

    @staticmethod
    def _disabled_setup_modes() -> set:
        return {str(mode or "").strip().lower() for mode in getattr(settings, "DISABLED_SETUP_MODES", ()) if str(mode or "").strip()}

    def _refresh_shorting_readiness(self) -> Dict:
        account = self.alpaca_client.get_account() if self.alpaca_client else {}
        shorting_enabled = account.get("shorting_enabled")
        ready = bool(self.alpaca_client)
        reasons = []
        if shorting_enabled is False:
            ready = False
            reasons.append("shorting_disabled")
        elif shorting_enabled is None:
            ready = False
            reasons.append("shorting_status_unknown")
        if bool(account.get("trading_blocked")):
            ready = False
            reasons.append("trading_blocked")
        if bool(account.get("account_blocked")):
            ready = False
            reasons.append("account_blocked")
        if bool(account.get("trade_suspended_by_user")):
            ready = False
            reasons.append("trade_suspended_by_user")
        if self.risk_manager and getattr(self.risk_manager, "is_swing_mode", None) and self.risk_manager.is_swing_mode():
            ready = False
            reasons.append("pdt_swing_mode")
        status = {
            "ready": ready,
            "paper": bool(getattr(settings, "ALPACA_PAPER", True)),
            "shorting_enabled": shorting_enabled,
            "pattern_day_trader": bool(account.get("pattern_day_trader")),
            "daytrade_count": int(account.get("daytrade_count", 0) or 0),
            "equity": float(account.get("equity", 0) or 0),
            "multiplier": str(account.get("multiplier", "") or ""),
            "status": str(account.get("status", "") or ""),
            "reasons": reasons,
        }
        self.ai_layers["shorting_readiness"] = status
        return status

    def _shorting_ready(self) -> bool:
        status = dict(self.ai_layers.get("shorting_readiness", {}) or {})
        return bool(status.get("ready"))

    def _build_pending_setup_candidates(self, scan_candidates: List[Dict]) -> List[Dict]:
        if not self._mode_classifier_enabled():
            return []
        live_by_symbol = {
            str(row.get("symbol", "") or "").upper(): dict(row)
            for row in (scan_candidates or [])
            if str(row.get("symbol", "") or "").strip()
        }
        pending_rows = list_pending_setups(
            limit=max(1, int(getattr(settings, "MODE_CLASSIFIER_PENDING_REFRESH_LIMIT", 8) or 8))
        )
        self.ai_layers["pending_setup_count"] = len(pending_rows)
        pending_candidates = []
        for row in pending_rows:
            symbol = str(row.get("symbol", "") or "").upper()
            if not symbol:
                continue
            candidate = dict(row.get("candidate_snapshot", {}) or {})
            live = live_by_symbol.get(symbol)
            if live:
                candidate.update(live)
            candidate.update(
                {
                    "symbol": symbol,
                    "_pending_setup": True,
                    "_pending_setup_mode": row.get("mode"),
                    "_pending_setup_id": row.get("setup_id"),
                    "_pending_setup_shadow_mode": bool(row.get("shadow_mode")),
                    "setup_id": row.get("setup_id", candidate.get("setup_id")),
                    "setup_mode": row.get("mode", candidate.get("setup_mode")),
                    "direction_constraint": row.get("direction_constraint", candidate.get("direction_constraint")),
                    "timing_state": row.get("timing_state", candidate.get("timing_state")),
                    "best_play": row.get("best_play", candidate.get("best_play")),
                    "trigger": row.get("trigger", candidate.get("trigger")),
                    "trigger_spec": dict(row.get("trigger_spec", candidate.get("trigger_spec", {})) or {}),
                    "invalidation": row.get("invalidation", candidate.get("invalidation")),
                    "hold_style": row.get("hold_style", candidate.get("hold_style")),
                    "size_posture": row.get("size_posture", candidate.get("size_posture", "normal")),
                    "expires_at": row.get("expires_at", candidate.get("expires_at")),
                    "created_at": row.get("created_at", candidate.get("created_at")),
                    "last_refreshed_at": row.get("last_refreshed_at", candidate.get("last_refreshed_at")),
                    "feature_snapshot_id": row.get("feature_snapshot_id", candidate.get("feature_snapshot_id")),
                    "material_change_signature": row.get(
                        "material_change_signature", candidate.get("material_change_signature")
                    ),
                    "feature_quality_score": row.get(
                        "feature_quality_score", candidate.get("feature_quality_score", 0.0)
                    ),
                    "feature_quality": row.get("feature_quality", candidate.get("feature_quality", "")),
                    "mode_features": dict(row.get("mode_features", candidate.get("mode_features", {})) or {}),
                    "bar_context": dict(row.get("bar_context", candidate.get("bar_context", {})) or {}),
                }
            )
            pending_candidates.append(candidate)
        return pending_candidates

    @staticmethod
    def _apply_setup_fields_to_sentiment_data(
        sentiment_data: Dict,
        candidate: Dict,
        verdict=None,
        execution_gate: Optional[Dict] = None,
    ) -> Dict:
        sentiment_data = dict(sentiment_data or {})
        candidate = dict(candidate or {})
        sentiment_data["mode_constraint_active"] = bool(candidate.get("mode_constraint_active"))
        sentiment_data["setup_id"] = candidate.get("setup_id")
        sentiment_data["setup_mode"] = candidate.get("setup_mode", "invalid")
        sentiment_data["direction_constraint"] = candidate.get("direction_constraint", "none")
        sentiment_data["timing_state"] = candidate.get("timing_state", "mode_conflict")
        sentiment_data["best_play"] = candidate.get("best_play", "")
        sentiment_data["trigger"] = candidate.get("trigger", "")
        sentiment_data["trigger_spec"] = dict(candidate.get("trigger_spec", {}) or {})
        sentiment_data["invalidation"] = candidate.get("invalidation", "")
        sentiment_data["hold_style"] = candidate.get("hold_style", candidate.get("holding_horizon", "intraday"))
        sentiment_data["size_posture"] = candidate.get("size_posture", "normal")
        sentiment_data["no_trade_reason"] = candidate.get("no_trade_reason")
        sentiment_data["classifier_confidence"] = float(candidate.get("classifier_confidence", 0.0) or 0.0)
        sentiment_data["resolver_confidence"] = float(candidate.get("resolver_confidence", 0.0) or 0.0)
        sentiment_data["feature_snapshot_id"] = candidate.get("feature_snapshot_id")
        sentiment_data["feature_quality_score"] = float(candidate.get("feature_quality_score", 0.0) or 0.0)
        sentiment_data["feature_quality"] = candidate.get("feature_quality", "")
        sentiment_data["missing_fields"] = list(candidate.get("missing_fields", []) or [])
        sentiment_data["material_change_signature"] = candidate.get("material_change_signature")
        sentiment_data["symbol_state"] = candidate.get("symbol_state", "classified")
        sentiment_data["mode_features"] = dict(candidate.get("mode_features", {}) or {})
        sentiment_data["bar_context"] = dict(candidate.get("bar_context", {}) or {})
        sentiment_data["created_at"] = candidate.get("created_at")
        sentiment_data["last_refreshed_at"] = candidate.get("last_refreshed_at")
        sentiment_data["data_age_seconds"] = float(candidate.get("data_age_seconds", 0.0) or 0.0)
        if execution_gate:
            sentiment_data["execution_confidence"] = float(execution_gate.get("execution_confidence", 0.0) or 0.0)
        if verdict is not None:
            sentiment_data["jury_entry_now"] = bool(getattr(verdict, "entry_now", False))
            sentiment_data["jury_trigger"] = getattr(verdict, "trigger", "") or ""
            sentiment_data["jury_invalidation"] = getattr(verdict, "invalidation", "") or ""
            sentiment_data["jury_hold_style"] = getattr(verdict, "hold_style", "") or ""
            sentiment_data["jury_size_posture"] = getattr(verdict, "size_posture", "") or ""
            sentiment_data["jury_no_trade_reason"] = getattr(verdict, "no_trade_reason", "") or ""
        return sentiment_data

    def _build_provider_fallback_verdict(self, candidate: Dict, failed_verdict) -> Optional[JuryVerdict]:
        if not bool(getattr(settings, "MODE_CLASSIFIER_ALLOW_PROVIDER_FALLBACK", False)):
            return None
        if not self._mode_classifier_enforced():
            return None
        agreement = str((getattr(failed_verdict, "consensus_detail", {}) or {}).get("agreement", "") or "").lower()
        if agreement not in {"no_votes", "degraded_insufficient", "single_model_insufficient", "degraded_split"}:
            return None
        mode = str(candidate.get("setup_mode", "") or "").strip().lower()
        if mode not in {"continuation_long", "continuation_short"}:
            return None
        if str(candidate.get("timing_state", "") or "").strip().lower() != "enter_now":
            return None
        if float(candidate.get("feature_quality_score", 0.0) or 0.0) < 0.9:
            return None
        if float(candidate.get("classifier_confidence", 0.0) or 0.0) < 0.75:
            return None
        if float(candidate.get("resolver_confidence", 0.0) or 0.0) < 0.7:
            return None
        if float(candidate.get("spread_pct", 0.0) or 0.0) > 0.5:
            return None
        decision = "SHORT" if str(candidate.get("direction_constraint", "none") or "").lower() == "short_only" else "BUY"
        if decision == "SHORT" and not self._shorting_ready():
            return None
        confidence = min(
            70.0,
            max(
                45.0,
                (float(candidate.get("classifier_confidence", 0.0) or 0.0) * 100.0 + float(candidate.get("resolver_confidence", 0.0) or 0.0) * 100.0) / 2.0,
            ),
        )
        return JuryVerdict(
            symbol=str(candidate.get("symbol", "") or ""),
            decision=decision,
            size_pct=0.5,
            trail_pct=round(float(getattr(settings, "PROFIT_RATCHET_TRAIL_PCT", 2.0) or 2.0), 3),
            reasoning="Deterministic fallback: provider panel degraded but setup remained high-quality and live.",
            confidence=round(confidence, 2),
            provider_used="deterministic_fallback",
            consensus_detail={
                "agreement": "deterministic_fallback",
                "votes": {},
                "total_models": 0,
                "degraded": True,
                "fallback_mode": mode,
            },
            setup_mode=mode,
            direction_constraint=str(candidate.get("direction_constraint", "none") or "none"),
            timing_state=str(candidate.get("timing_state", "enter_now") or "enter_now"),
            best_play=str(candidate.get("best_play", "") or ""),
            entry_now=True,
            trigger=str(candidate.get("trigger", "") or ""),
            invalidation=str(candidate.get("invalidation", "") or ""),
            hold_style=str(candidate.get("hold_style", "intraday") or "intraday"),
            size_posture="reduced",
        )

    async def _capture_bar_context(self, symbol: str) -> Dict:
        if not symbol or not self.polygon_client:
            return {}
        loop = asyncio.get_event_loop()
        try:
            one_minute = await loop.run_in_executor(
                None,
                partial(self.polygon_client.get_bars, symbol, timespan="minute", multiplier=1, limit=5),
            )
        except Exception:
            one_minute = []
        try:
            five_minute = await loop.run_in_executor(
                None,
                partial(self.polygon_client.get_bars, symbol, timespan="minute", multiplier=5, limit=3),
            )
        except Exception:
            five_minute = []
        return {
            "bars_1m": one_minute or [],
            "bars_5m": five_minute or [],
        }

    def _record_setup_snapshot(
        self,
        candidate: Dict,
        symbol_state: str,
        verdict=None,
        extra: Optional[Dict] = None,
    ) -> Optional[str]:
        if not self._mode_classifier_enabled():
            return None
        trigger_live = None
        trigger_spec_payload = dict(candidate.get("trigger_spec", {}) or {})
        try:
            feature_payload = dict(candidate.get("mode_features", {}) or {})
            snapshot_features = mode_features_from_dict(feature_payload)
            if snapshot_features is None and candidate.get("symbol"):
                snapshot_features = build_mode_features(candidate)
            if snapshot_features is not None and trigger_spec_payload:
                trigger_live = evaluate_trigger(
                    snapshot_features,
                    TriggerSpec(
                        trigger_type=str(trigger_spec_payload.get("trigger_type", "") or ""),
                        params=dict(trigger_spec_payload.get("params", {}) or {}),
                        description=str(trigger_spec_payload.get("description", "") or ""),
                    ),
                )
        except Exception:
            trigger_live = None
        payload = {
            "setup_id": candidate.get("setup_id"),
            "material_change_signature": candidate.get("material_change_signature"),
            "symbol": candidate.get("symbol"),
            "side": candidate.get("side"),
            "source": candidate.get("source"),
            "strategy_tag": candidate.get("strategy_tag"),
            "signal_sources": list(candidate.get("signal_sources", []) or []),
            "signal_tier": candidate.get("signal_tier"),
            "created_at": candidate.get("created_at", candidate.get("signal_timestamp")),
            "last_refreshed_at": candidate.get("last_refreshed_at", time.time()),
            "data_age_seconds": candidate.get("data_age_seconds"),
            "symbol_state": normalize_symbol_state(symbol_state),
            "setup_mode": candidate.get("setup_mode"),
            "direction_constraint": candidate.get("direction_constraint"),
            "classifier_confidence": candidate.get("classifier_confidence"),
            "classifier_reason_codes": list(candidate.get("classifier_reason_codes", []) or []),
            "resolver_confidence": candidate.get("resolver_confidence"),
            "timing_state": candidate.get("timing_state"),
            "best_play": candidate.get("best_play"),
            "trigger": candidate.get("trigger"),
            "trigger_spec": trigger_spec_payload,
            "trigger_live": trigger_live,
            "invalidation": candidate.get("invalidation"),
            "hold_style": candidate.get("hold_style"),
            "size_posture": candidate.get("size_posture"),
            "no_trade_reason": candidate.get("no_trade_reason"),
            "feature_quality_score": candidate.get("feature_quality_score"),
            "feature_quality": candidate.get("feature_quality"),
            "missing_fields": list(candidate.get("missing_fields", []) or []),
            "mode_features": dict(candidate.get("mode_features", {}) or {}),
            "bar_context": dict(candidate.get("bar_context", {}) or {}),
            "market_regime": candidate.get("market_regime"),
            "provider_used": candidate.get("provider_used"),
            "entry_path": candidate.get("entry_path"),
            "entry_reason_code": candidate.get("entry_reason_code"),
            "entry_quality": candidate.get("entry_quality"),
            "holding_horizon": candidate.get("holding_horizon"),
            "reason": candidate.get("reason"),
            "jury_response": None,
        }
        if verdict is not None:
            payload["jury_response"] = {
                "decision": getattr(verdict, "decision", None),
                "confidence": getattr(verdict, "confidence", None),
                "provider_used": getattr(verdict, "provider_used", None),
                "reasoning": getattr(verdict, "reasoning", None),
                "agreement": (getattr(verdict, "consensus_detail", {}) or {}).get("agreement"),
            }
        if extra:
            payload.update(dict(extra))
        snapshot_id = record_setup_snapshot(payload)
        return snapshot_id

    _SHADOW_BLOCK_STATES = frozenset({
        "broker_blocked", "execution_unfavorable", "capital_blocked",
        "shadow_only", "mode_conflict", "data_insufficient",
    })

    def _record_candidate_block(
        self,
        candidate: Dict,
        symbol_state: str,
        reason: str,
        verdict=None,
        extra: Optional[Dict] = None,
    ) -> Optional[str]:
        if not self._mode_classifier_enabled():
            return None
        payload = dict(candidate or {})
        symbol = str(payload.get("symbol", "") or "").upper().strip()
        if not symbol:
            return None
        payload["symbol"] = symbol
        payload.setdefault("holding_horizon", payload.get("hold_style", "intraday"))
        payload.setdefault("last_refreshed_at", time.time())
        payload.setdefault("setup_mode", payload.get("setup_mode", "invalid"))
        payload.setdefault("direction_constraint", payload.get("direction_constraint", "none"))
        payload.setdefault("timing_state", str(symbol_state or "classified"))
        payload["no_trade_reason"] = str(reason or "").strip() or payload.get("no_trade_reason")
        if symbol_state in {"broker_blocked", "capital_blocked", "data_insufficient", "mode_conflict", "shadow_only"}:
            payload["timing_state"] = symbol_state

        if symbol_state in self._SHADOW_BLOCK_STATES:
            strategy_tag = str(payload.get("strategy_tag", "unknown") or "unknown")
            shadow_record = {
                "symbol": symbol,
                "strategy_tag": strategy_tag,
                "direction": self._shadow_direction_for_strategy(strategy_tag),
                "signal_tier": payload.get("signal_tier", "tier_2"),
                "entry_quality": payload.get("entry_quality", "neutral"),
                "signal_price": round(float(payload.get("price", 0) or 0), 4),
                "spread_pct": float(payload.get("spread_pct", 0) or 0),
                "range_pct": float(payload.get("range_pct", 0) or 0),
                "timestamp": time.time(),
                "block_reason": str(reason or ""),
                "block_state": symbol_state,
                "price_1h": None,
                "price_4h": None,
                "price_eod": None,
                "mfe": None,
                "mae": None,
            }
            self._persist_shadow_record(shadow_record)

        return self._record_setup_snapshot(payload, symbol_state, verdict=verdict, extra=extra)

    def _pending_setup_candidate_snapshot(self, candidate: Dict) -> Dict:
        keep_fields = {
            "symbol",
            "price",
            "change_pct",
            "volume",
            "volume_spike",
            "vol_accel",
            "spread_pct",
            "range_pct",
            "rolling_vwap_pct",
            "rsi_14",
            "source",
            "side",
            "score",
            "signal_timestamp",
            "strategy_tag",
            "signal_tier",
            "holding_horizon",
            "entry_quality",
            "market_regime",
            "uw_flow_summary",
            "uw_total_premium",
            "uw_net_premium_bias",
            "uw_chain_bias",
            "uw_news_bias",
            "st_bullish",
            "st_bearish",
            "sentiment_score",
            "minute_vol",
            "session_type",
            "extended_hours",
            "bar_context",
            "overnight_context",
            "human_intel",
            "copy_trader_context",
            "watchlist_reason",
            "congress_trades",
            "insider_activity",
            "pharma_signal",
            "pharma_catalyst_type",
            "earnings",
            "earnings_date",
            "catalyst_date",
            "anomaly_flags",
        }
        return {
            key: value
            for key, value in dict(candidate or {}).items()
            if key in keep_fields
        }

    def _persist_waiting_setup(self, candidate: Dict, verdict=None, shadow_mode: bool = False) -> None:
        if not self._mode_classifier_enabled():
            return
        setup_record = {
            "setup_id": candidate.get("setup_id"),
            "symbol": candidate.get("symbol"),
            "mode": candidate.get("setup_mode"),
            "direction_constraint": candidate.get("direction_constraint"),
            "timing_state": candidate.get("timing_state"),
            "best_play": candidate.get("best_play"),
            "trigger": candidate.get("trigger"),
            "trigger_spec": dict(candidate.get("trigger_spec", {}) or {}),
            "invalidation": candidate.get("invalidation"),
            "hold_style": candidate.get("hold_style"),
            "size_posture": candidate.get("size_posture"),
            "expires_at": candidate.get("expires_at"),
            "created_at": candidate.get("created_at", candidate.get("signal_timestamp", time.time())),
            "last_refreshed_at": candidate.get("last_refreshed_at", time.time()),
            "data_age_seconds": candidate.get("data_age_seconds"),
            "feature_quality_score": candidate.get("feature_quality_score"),
            "feature_quality": candidate.get("feature_quality"),
            "classifier_confidence": candidate.get("classifier_confidence"),
            "resolver_confidence": candidate.get("resolver_confidence"),
            "classifier_reason_codes": list(candidate.get("classifier_reason_codes", []) or []),
            "no_trade_reason": candidate.get("no_trade_reason"),
            "feature_snapshot_id": candidate.get("feature_snapshot_id"),
            "material_change_signature": candidate.get("material_change_signature"),
            "symbol_state": "pending_trigger",
            "shadow_mode": bool(shadow_mode),
            "jury_confidence": getattr(verdict, "confidence", None) if verdict is not None else None,
            "candidate_snapshot": self._pending_setup_candidate_snapshot(candidate),
            "mode_features": dict(candidate.get("mode_features", {}) or {}),
            "bar_context": dict(candidate.get("bar_context", {}) or {}),
        }
        upsert_pending_setup(setup_record)
        snapshot_id = self._record_setup_snapshot(candidate, "pending_trigger", verdict=verdict, extra={"shadow_mode": bool(shadow_mode)})
        if snapshot_id and not candidate.get("feature_snapshot_id"):
            candidate["feature_snapshot_id"] = snapshot_id
        self.ai_layers["pending_setup_count"] = len(list_pending_setups())

    def _remove_waiting_setup(self, symbol: str, mode: Optional[str] = None) -> None:
        remove_pending_setup(symbol, mode=mode)
        self.ai_layers["pending_setup_count"] = len(list_pending_setups())

    async def _enrich_candidate_setup_context(self, candidate: Dict) -> Dict:
        if not self._mode_classifier_enabled():
            return dict(candidate or {})

        enriched = dict(candidate or {})
        symbol = str(enriched.get("symbol", "") or "").upper()
        now_ts = time.time()
        snapshot = None
        if symbol and self.scanner and hasattr(self.scanner, "_get_alpaca_snapshot"):
            try:
                snapshot = await asyncio.get_event_loop().run_in_executor(None, self.scanner._get_alpaca_snapshot, symbol)
            except Exception:
                snapshot = None
        if isinstance(snapshot, dict):
            for key, value in snapshot.items():
                if value in (None, "", [], {}):
                    continue
                if key not in enriched or enriched.get(key) in (None, "", 0, 0.0):
                    enriched[key] = value

        avg_volume = self._to_float_safe(enriched.get("avg_volume", enriched.get("average_volume", 0.0)), 0.0)
        current_volume = self._to_float_safe(enriched.get("volume", enriched.get("minute_vol", 0.0)), 0.0)
        volume_spike = self._to_float_safe(enriched.get("volume_spike", 0.0), 0.0)
        if symbol and (avg_volume <= 0.0 or volume_spike <= 0.0):
            if isinstance(snapshot, dict):
                avg_volume = max(avg_volume, self._to_float_safe(snapshot.get("prev_volume", 0.0), 0.0))
                current_volume = max(current_volume, self._to_float_safe(snapshot.get("volume", 0.0), 0.0))
            if avg_volume <= 0.0 and self.polygon_client and hasattr(self.polygon_client, "get_avg_volume"):
                try:
                    avg_volume = self._to_float_safe(
                        await asyncio.get_event_loop().run_in_executor(
                            None, self.polygon_client.get_avg_volume, symbol, 20
                        ),
                        0.0,
                    )
                except Exception:
                    avg_volume = 0.0
            if avg_volume > 0.0:
                enriched["avg_volume"] = avg_volume
                enriched["average_volume"] = avg_volume
                if current_volume > 0.0:
                    enriched["volume_spike"] = round(current_volume / avg_volume, 3)

        price = float(enriched.get("price", 0) or 0)
        tech_missing = any(enriched.get(key) is None for key in ("range_pct", "rolling_vwap_pct", "rsi_14", "vol_accel"))
        if symbol and price > 0 and tech_missing and self.polygon_client:
            try:
                technicals = await compute_technicals(symbol, price, self.polygon_client, snapshot=snapshot) or {}
            except Exception:
                technicals = {}
            if technicals:
                enriched.update(technicals)

        if symbol and (
            not enriched.get("bar_context")
            or bar_context_is_stale(enriched.get("bar_context", {}), now_ts=now_ts)
        ):
            enriched["bar_context"] = await self._capture_bar_context(symbol)

        features = build_mode_features(enriched, now_ts=now_ts)
        classification = classify_mode(features)
        disabled_mode = None
        if classification.mode in self._disabled_setup_modes():
            disabled_mode = classification.mode
            classification.reason_codes = ["mode_disabled", f"mode_{disabled_mode}", *list(classification.reason_codes or [])]
            classification.classifier_confidence = 0.0
        resolution = resolve_play(features, classification, now_ts=now_ts)

        existing_setup = get_pending_setup(features.symbol, classification.mode)
        material_signature = build_material_change_signature(
            mode=classification.mode,
            timing_state=resolution.timing_state,
            direction_constraint=classification.direction_constraint,
            sentiment_pct=features.sentiment_pct,
            halt_count=features.halt_count,
            reclaiming_vwap=features.reclaiming_vwap,
            losing_vwap=features.losing_vwap,
            volume_accel=features.volume_accel,
        )
        created_at = float(
            existing_setup.get("created_at", features.created_at)
            if existing_setup and existing_setup.get("material_change_signature") == material_signature
            else features.created_at
        )
        if existing_setup and existing_setup.get("material_change_signature") == material_signature:
            setup_id = str(existing_setup.get("setup_id", "") or "")
        else:
            setup_id = build_setup_id(features.symbol, classification.mode, created_at, material_signature)

        enriched["created_at"] = created_at
        enriched["last_refreshed_at"] = features.last_refreshed_at
        enriched["data_age_seconds"] = features.data_age_seconds
        enriched["feature_quality_score"] = features.feature_quality_score
        enriched["feature_quality"] = features.feature_quality
        enriched["missing_fields"] = list(features.missing_fields or [])
        enriched["mode_features"] = features.to_dict()
        enriched["setup_mode"] = normalize_mode(classification.mode)
        enriched["direction_constraint"] = normalize_direction_constraint(classification.direction_constraint)
        enriched["classifier_confidence"] = classification.classifier_confidence
        enriched["classifier_reason_codes"] = list(classification.reason_codes or [])
        enriched["timing_state"] = resolution.timing_state
        enriched["best_play"] = resolution.best_play
        enriched["trigger"] = resolution.trigger
        enriched["trigger_spec"] = resolution.trigger_spec.to_dict() if resolution.trigger_spec else {}
        enriched["invalidation"] = resolution.invalidation
        enriched["hold_style"] = resolution.hold_style
        enriched["resolver_confidence"] = resolution.resolver_confidence
        enriched["size_posture"] = resolution.size_posture
        enriched["no_trade_reason"] = resolution.no_trade_reason
        enriched["expires_at"] = resolution.expires_at
        enriched["setup_id"] = setup_id
        enriched["material_change_signature"] = material_signature
        enriched["materially_new_setup"] = not bool(
            existing_setup and existing_setup.get("material_change_signature") == material_signature
        )
        if disabled_mode:
            enriched["timing_state"] = "shadow_only"
            enriched["size_posture"] = "zero"
            enriched["no_trade_reason"] = f"mode_disabled:{disabled_mode}"
            enriched["symbol_state"] = "shadow_only"
        elif resolution.timing_state == "wait_for_trigger":
            enriched["symbol_state"] = "pending_trigger"
        elif resolution.timing_state in {"data_insufficient", "mode_conflict"}:
            enriched["symbol_state"] = resolution.timing_state
        else:
            enriched["symbol_state"] = "classified"

        snapshot_id = self._record_setup_snapshot(enriched, enriched["symbol_state"])
        if snapshot_id:
            enriched["feature_snapshot_id"] = snapshot_id

        self.ai_layers["last_mode_classification"] = {
            "symbol": symbol,
            "setup_id": setup_id,
            "mode": enriched.get("setup_mode"),
            "timing_state": enriched.get("timing_state"),
            "best_play": enriched.get("best_play"),
            "trigger": enriched.get("trigger"),
            "classifier_confidence": enriched.get("classifier_confidence"),
            "resolver_confidence": enriched.get("resolver_confidence"),
        }
        return enriched

    def _setup_execution_gate(self, candidate: Dict, direction: str) -> Dict:
        setup_mode = str(candidate.get("setup_mode", "") or "").strip().lower()
        timing_state = str(candidate.get("timing_state", "") or "").strip().lower()
        direction_constraint = str(candidate.get("direction_constraint", "none") or "none").strip().lower()
        feature_quality_score = float(candidate.get("feature_quality_score", 0.0) or 0.0)
        data_age_seconds = float(candidate.get("data_age_seconds", 0.0) or 0.0)
        spread_pct = float(candidate.get("spread_pct", 0.0) or 0.0)
        hold_style = str(candidate.get("hold_style", candidate.get("holding_horizon", "intraday")) or "intraday").lower()
        desired_direction = str(direction or "BUY").upper()

        penalties = 0.0
        reasons = []
        if feature_quality_score < 0.8:
            penalties += (0.8 - feature_quality_score) * 0.5
            reasons.append("feature_quality_haircut")
        if hold_style == "intraday" and data_age_seconds > 600:
            penalties += min(0.2, (data_age_seconds - 600.0) / 600.0)
            reasons.append("stale_signal")
        if spread_pct > 0.6:
            penalties += min(0.3, max(0.0, spread_pct - 0.6) / 2.0)
            reasons.append("wide_spread")
        execution_confidence = max(0.0, min(1.0, 1.0 - penalties))

        if not self._mode_classifier_enforced():
            return {
                "allowed": True,
                "reason": "shadow_mode",
                "execution_confidence": round(execution_confidence, 2),
            }

        if timing_state == "shadow_only":
            return {"allowed": False, "reason": "shadow_only", "execution_confidence": round(execution_confidence, 2)}
        if timing_state in {"data_insufficient", "mode_conflict"}:
            return {"allowed": False, "reason": timing_state, "execution_confidence": round(execution_confidence, 2)}
        if setup_mode == "invalid" and timing_state not in {"enter_now", "wait_for_trigger"}:
            return {"allowed": False, "reason": "data_insufficient", "execution_confidence": round(execution_confidence, 2)}
        if timing_state != "enter_now":
            return {"allowed": False, "reason": timing_state or "trigger_not_live", "execution_confidence": round(execution_confidence, 2)}
        if direction_constraint == "short_only" and desired_direction != "SHORT":
            return {"allowed": False, "reason": "direction_constraint_short_only", "execution_confidence": round(execution_confidence, 2)}
        if direction_constraint == "long_only" and desired_direction != "BUY":
            return {"allowed": False, "reason": "direction_constraint_long_only", "execution_confidence": round(execution_confidence, 2)}
        trigger_spec = candidate.get("trigger_spec", {}) or {}
        if trigger_spec:
            trigger = TriggerSpec(
                trigger_type=str(trigger_spec.get("trigger_type", "") or ""),
                params=dict(trigger_spec.get("params", {}) or {}),
                description=str(trigger_spec.get("description", "") or ""),
            )
            if not evaluate_trigger(build_mode_features(candidate), trigger):
                return {"allowed": False, "reason": "trigger_not_live", "execution_confidence": round(execution_confidence, 2)}
        if hold_style == "intraday" and data_age_seconds > 900:
            return {"allowed": False, "reason": "stale_signal", "execution_confidence": round(execution_confidence, 2)}
        if spread_pct > 1.0:
            return {"allowed": False, "reason": "spread_too_wide", "execution_confidence": round(execution_confidence, 2)}
        if not self.alpaca_client:
            return {"allowed": False, "reason": "broker_unavailable", "execution_confidence": round(execution_confidence, 2)}
        if desired_direction == "SHORT" and not self._shorting_ready():
            return {"allowed": False, "reason": "shorting_not_ready", "execution_confidence": round(execution_confidence, 2)}
        return {"allowed": True, "reason": "ok", "execution_confidence": round(execution_confidence, 2)}

    def _prepare_candidate_metadata(self, candidate: Dict) -> Dict:
        prepared = dict(candidate or {})
        direction = "SHORT" if str(prepared.get("side", "")).lower() == "short" else "BUY"
        prepared["strategy_tag"] = normalize_strategy_tag(
            prepared.get("strategy_tag")
            or self._derive_strategy_tag(prepared, direction)
            or "unknown"
        )
        prepared["signal_tier"] = str(prepared.get("signal_tier") or self._derive_signal_tier(prepared))
        prepared["holding_horizon"] = str(prepared.get("holding_horizon") or self._derive_holding_horizon(prepared))
        _regime = str(
            prepared.get("market_regime")
            or getattr(self, "scan_regime", "")
            or getattr(self, "scan_regime_raw", "")
            or "mixed"
        ).lower()
        if _regime in ("mixed", ""):
            try:
                _bias = self.get_overnight_bias_context(refresh=False)
                _avg_chg = float(_bias.get("avg_change_pct", 0) or 0)
                if _avg_chg <= -1.0:
                    _regime = "risk_off"
                elif _avg_chg >= 1.0:
                    _regime = "risk_on"
            except Exception:
                pass
        prepared["market_regime"] = _regime
        prepared["uw_flow_summary"] = str(prepared.get("uw_flow_summary") or self._build_uw_flow_summary(prepared))
        session_label = self._entry_session_label()
        prepared["extended_hours"] = session_label in {"pre", "after"}
        prepared["session_type"] = self._current_session_type_label(session_label)
        prepared["entry_quality"] = str(prepared.get("entry_quality") or self._derive_entry_quality(prepared))
        prepared["overnight_context"] = str(
            prepared.get("overnight_context")
            or OvernightContext.format_summary(self.get_overnight_bias_context())
        )

        if self.sector_model:
            symbol = str(prepared.get("symbol") or "").upper()
            stock_sector = None
            for sector_name, tickers in SECTOR_STOCKS.items():
                if symbol in tickers:
                    stock_sector = sector_name
                    break
            if stock_sector:
                hot_names = {s[1] for s in self.sector_model.get_hot_sectors(3)}
                cold_names = {s[1] for s in self.sector_model.get_cold_sectors(3)}
                prepared["sector_name"] = stock_sector
                prepared["sector_hot"] = stock_sector in hot_names
                prepared["sector_cold"] = stock_sector in cold_names

        return prepared

    def _evaluate_trade_gate(self, candidate: Dict, direction: str) -> Dict:
        annotated = annotate_candidate(candidate)
        strategy_tag = normalize_strategy_tag(
            annotated.get("strategy_tag")
            or self._derive_strategy_tag(annotated, direction)
            or "unknown"
        )
        annotated["strategy_tag"] = strategy_tag
        annotated.update(annotate_candidate(annotated))
        # v2 stabilization: playbook gate is bypassed. Jury decides, risk sizes.
        return {"allowed": True, "reason": "v2_passthrough", "candidate": annotated}

    def _evaluate_options_overlay(self, candidate: Dict, direction: str, confidence: float) -> Dict:
        annotated = annotate_candidate(candidate)
        strategy_tag = normalize_strategy_tag(
            annotated.get("strategy_tag")
            or self._derive_strategy_tag(annotated, direction)
            or "unknown"
        )
        annotated["strategy_tag"] = strategy_tag
        annotated.update(annotate_candidate(annotated))

        symbol = str(annotated.get("symbol") or "").strip().upper()
        confidence = float(confidence or 0)
        summary = {
            "symbol": symbol,
            "strategy_tag": strategy_tag,
            "allocation_pct": 0.0,
            "eligible": False,
            "mode": "off",
            "reason": "options_disabled",
            "confidence": confidence,
        }

        if bool(annotated.get("extended_hours")) or not OptionsMonitor.is_regular_market_hours():
            summary["reason"] = "extended_hours"
            return summary

        options_mode = str(annotated.get("playbook_options_mode", "off") or "off").lower()
        if options_mode == "prefer":
            summary["mode"] = "playbook_prefer"
            if not self._candidate_has_uw_confirmation(annotated, direction):
                summary["reason"] = "uw_unconfirmed"
                return summary
            base_pct = float(getattr(settings, "OPTIONS_ALLOCATION_PCT", 50) or 50)
            if confidence >= 85:
                summary["allocation_pct"] = base_pct
                summary["eligible"] = True
                summary["reason"] = "uw_prefer_high_confidence"
                return summary
            if confidence >= 75:
                summary["allocation_pct"] = base_pct * 0.5
                summary["eligible"] = True
                summary["reason"] = "uw_prefer_mid_confidence"
                return summary
            summary["reason"] = "uw_prefer_low_confidence"
            return summary

        if not bool(getattr(settings, "OPTIONS_PILOT_ENABLED", False)):
            summary["reason"] = "pilot_disabled"
            return summary

        summary["mode"] = "pilot"

        allowed_symbols = {
            str(sym or "").strip().upper()
            for sym in getattr(settings, "OPTIONS_PILOT_SYMBOLS", set()) or set()
            if str(sym or "").strip()
        }
        if allowed_symbols and symbol not in allowed_symbols:
            summary["reason"] = "pilot_symbol_not_whitelisted"
            return summary

        allowed_strategy_tags = {
            str(tag or "").strip().upper()
            for tag in getattr(settings, "OPTIONS_PILOT_STRATEGY_TAGS", set()) or set()
            if str(tag or "").strip()
        }
        if allowed_strategy_tags and str(strategy_tag or "").strip().upper() not in allowed_strategy_tags:
            summary["reason"] = "pilot_strategy_not_whitelisted"
            return summary

        underlying_price = float(annotated.get("price", annotated.get("entry_price", 0)) or 0)
        if underlying_price < float(getattr(settings, "OPTIONS_PILOT_MIN_UNDERLYING_PRICE", 20.0) or 20.0):
            summary["reason"] = "pilot_underlying_too_cheap"
            return summary

        min_conf = float(getattr(settings, "OPTIONS_PILOT_MIN_CONFIDENCE", 90.0) or 90.0)
        if confidence < min_conf:
            summary["reason"] = "pilot_confidence_below_threshold"
            return summary

        base_pct = float(getattr(settings, "OPTIONS_PILOT_ALLOCATION_PCT", 35.0) or 35.0)
        if confidence >= min_conf + 5.0:
            summary["allocation_pct"] = base_pct
            summary["eligible"] = True
            summary["reason"] = "pilot_high_confidence"
            return summary
        if confidence >= min_conf + 2.0:
            summary["allocation_pct"] = base_pct * 0.85
            summary["eligible"] = True
            summary["reason"] = "pilot_mid_confidence"
            return summary
        if confidence >= min_conf:
            summary["allocation_pct"] = base_pct * 0.7
            summary["eligible"] = True
            summary["reason"] = "pilot_threshold_confidence"
            return summary
        summary["reason"] = "pilot_confidence_below_threshold"
        return summary

    def _determine_options_allocation_pct(self, candidate: Dict, direction: str, confidence: float) -> float:
        return float(self._evaluate_options_overlay(candidate, direction, confidence).get("allocation_pct", 0.0) or 0.0)

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
        guard_status = {}
        if getattr(self, "extended_guard", None):
            try:
                guard_status = self.extended_guard.get_guard_status() or {}
            except Exception:
                guard_status = {}
        extended_hours = bool(getattr(self, "extended_guard", None) and self.extended_guard.is_extended_hours())
        unprotected = []
        protection_failed = []
        for pos in positions or []:
            symbol = str(pos.get("symbol", "") or "")
            if pos.get("protection_failed"):
                protection_failed.append(symbol)
            order_status = str(pos.get("order_status", "") or "").lower()
            entry_pending = order_status in {"new", "accepted", "pending", "partially_filled"}
            has_extended_guard = bool((guard_status.get(symbol, {}) or {}).get("has_limit_order"))
            # In extended hours, the software guard order is valid protection.
            if extended_hours and has_extended_guard:
                continue
            # Do not block entry pipeline for positions still in entry/pending state.
            if entry_pending:
                continue
            has_protection = (
                pos.get("has_trailing_stop")
                or pos.get("hard_stop_order_id")
                or pos.get("swing_only")
                or (pos.get("order_state") or {}).get("hard_stop") in {"placed", "software_managed"}
            )
            if not has_protection:
                unprotected.append(symbol)
        reasons = []
        allow_new_entries = True
        if recon.get("status") == "critical_mismatch" or trust.get("broker_only_mode"):
            allow_new_entries = False
            reasons.append("critical_reconciliation")
        if trust.get("entry_pipeline_paused"):
            allow_new_entries = False
            reasons.extend(list(trust.get("degraded_mode_reasons", []) or ["degraded_mode_pause"]))
        if protection_failed:
            allow_new_entries = False
            reasons.append("protection_failed")
        if len(unprotected) > 0:
            logger.debug(f"Unprotected positions (informational only): {unprotected}")
            # v2.2: no longer blocks entries. Ratchet + hard stops handle protection.
        if risk_status.get("trading_halted"):
            allow_new_entries = False
            reasons.append("risk_halted")
        return {
            "allow_new_entries": allow_new_entries,
            "reconciliation_status": recon.get("status", "unknown"),
            "broker_only_mode": bool(trust.get("broker_only_mode")),
            "unprotected_symbols": unprotected,
            "protection_failed_symbols": protection_failed,
            "reasons": sorted(set(reasons)),
        }

    def _log_guardrail_block(self, prefix: str, reasons: list):
        # Avoid per-cycle log floods when guardrail reasons are unchanged.
        if not hasattr(self, "_guardrail_block_log_state"):
            self._guardrail_block_log_state = {}
        now = time.time()
        key = f"{prefix}:{','.join(sorted(reasons or []))}"
        state = self._guardrail_block_log_state.get(prefix, {})
        last_key = state.get("key")
        last_ts = float(state.get("ts", 0) or 0)
        if key != last_key or (now - last_ts) >= 120:
            logger.warning(f"{prefix} blocked by operating guardrails: {','.join(reasons or [])}")
            self._guardrail_block_log_state[prefix] = {"key": key, "ts": now}

    @staticmethod
    def _to_float_safe(value, default: float = 0.0) -> float:
        try:
            return float(value)
        except Exception:
            return default

    def _reentry_cooldown_store(self) -> Dict[str, float]:
        cooldowns = getattr(self, "_symbol_reentry_cooldown_until", None)
        if cooldowns is None:
            cooldowns = getattr(self, "_symbol_loss_cooldown_until", {}) or {}
            self._symbol_reentry_cooldown_until = cooldowns
        return cooldowns

    def _set_symbol_reentry_cooldown(self, symbol: str, seconds: float):
        key = str(symbol or "").upper()
        if not key:
            return
        seconds = float(seconds or 0.0)
        cooldowns = self._reentry_cooldown_store()
        if seconds <= 0:
            cooldowns.pop(key, None)
            return
        cooldowns[key] = time.time() + seconds

    def _symbol_reentry_cooldown_remaining(self, symbol: str) -> float:
        if not symbol:
            return 0.0
        cooldowns = self._reentry_cooldown_store()
        key = str(symbol).upper()
        until = float(cooldowns.get(key, 0.0) or 0.0)
        remaining = max(0.0, until - time.time())
        if remaining <= 0 and key in cooldowns:
            cooldowns.pop(key, None)
        return remaining

    def _symbol_loss_cooldown_remaining(self, symbol: str) -> float:
        return self._symbol_reentry_cooldown_remaining(symbol)

    def _cache_broker_position_symbols(self, positions: Optional[List[Dict]]) -> set:
        symbols = {
            str((row or {}).get("symbol", "") or "").upper()
            for row in (positions or [])
            if str((row or {}).get("symbol", "") or "").strip()
        }
        self._latest_broker_position_symbols = symbols
        self._latest_broker_positions_synced_at = time.time()
        return symbols

    async def _get_latest_broker_position_symbols(self, max_age_seconds: float = 15.0) -> set:
        cached = set(getattr(self, "_latest_broker_position_symbols", set()) or set())
        last_sync = float(getattr(self, "_latest_broker_positions_synced_at", 0.0) or 0.0)
        if cached and (time.time() - last_sync) <= float(max_age_seconds or 0.0):
            return cached
        if not self.alpaca_client:
            return cached
        try:
            positions = await asyncio.get_event_loop().run_in_executor(
                None, self.alpaca_client.get_positions
            )
        except Exception as e:
            logger.debug(f"Broker position cache refresh failed: {e}")
            return cached
        return self._cache_broker_position_symbols(positions)

    @staticmethod
    def _is_partial_exit_trade(trade_record: dict) -> bool:
        reason = str((trade_record or {}).get("reason", "") or "").lower()
        exit_scope = str((trade_record or {}).get("exit_scope", "") or "").lower()
        if exit_scope == "partial":
            return True
        return reason.endswith("_1") or reason.startswith("partial_")

    def _find_recent_realized_trade(
        self,
        symbol: str,
        exit_time: float,
        window_seconds: float = 30.0,
        asset_type: str = "equity",
        reason_prefixes: Optional[List[str]] = None,
    ) -> Optional[dict]:
        symbol_key = str(symbol or "").upper()
        if not symbol_key:
            return None
        try:
            target_exit = float(exit_time or 0.0)
        except Exception:
            target_exit = 0.0
        if target_exit <= 0:
            return None
        prefixes = tuple(str(prefix or "").lower() for prefix in (reason_prefixes or []) if str(prefix or "").strip())
        try:
            history = trade_history.load_all()
        except Exception:
            return None
        for existing in reversed(history or []):
            if str(existing.get("asset_type", "equity") or "equity").lower() != str(asset_type or "equity").lower():
                continue
            if str(existing.get("symbol", "") or "").upper() != symbol_key:
                continue
            if self._is_partial_exit_trade(existing):
                continue
            existing_reason = str(existing.get("reason", "") or "").lower()
            if prefixes and not any(existing_reason.startswith(prefix) for prefix in prefixes):
                continue
            try:
                existing_exit = float(existing.get("exit_time", 0) or 0)
            except Exception:
                continue
            if existing_exit <= 0:
                continue
            if abs(existing_exit - target_exit) <= float(window_seconds or 0.0):
                return existing
        return None

    def _infer_wyckoff_bias(self, signal_data: dict) -> str:
        """
        Lightweight Wyckoff state from live technical context.
        """
        signal_data = dict(signal_data or {})
        ema_signal = str(signal_data.get("ema_signal", "neutral") or "neutral").lower()
        vwap_pct = self._to_float_safe(signal_data.get("rolling_vwap_pct", 0.0), 0.0)
        range_pct = self._to_float_safe(signal_data.get("range_pct", 50.0), 50.0)
        vol_accel = self._to_float_safe(
            signal_data.get("vol_accel", signal_data.get("volume_spike", 1.0)), 1.0
        )
        rsi = self._to_float_safe(signal_data.get("rsi_14", 50.0), 50.0)
        obv_signal = "neutral"
        for row in (signal_data.get("validated_indicator_signals", []) or []):
            if not isinstance(row, dict):
                continue
            name = str(row.get("name", "") or "").lower()
            if "obv" in name:
                obv_signal = str(row.get("signal", "NEUTRAL") or "NEUTRAL").lower()
                break

        if ema_signal == "bearish" or vwap_pct < -0.35:
            return "distribution_risk"
        if range_pct >= 94.0 and rsi >= 80.0 and vol_accel <= 1.05:
            return "upthrust_risk"
        if ema_signal == "bullish" and vwap_pct >= 0.15 and 55.0 <= range_pct <= 98.0 and vol_accel >= 1.2:
            if obv_signal in ("buy", "neutral"):
                return "accumulation_markup"
        return "neutral_transition"

    def _allow_scout_override(self, candidate: dict, verdict) -> tuple:
        """
        Controlled fallback when jury unanimously SKIPs despite strong momentum.
        Keeps capital velocity without bypassing core risk gates.
        """
        if not bool(getattr(settings, "JURY_SCOUT_OVERRIDE_ENABLED", False)):
            return False, "scout_override_disabled", ""
        detail = (getattr(verdict, "consensus_detail", {}) or {})
        agreement = str(detail.get("agreement", "") or "")
        if agreement not in {"unanimous_skip", "degraded_unanimous_skip"}:
            return False, "agreement_not_unanimous_skip", ""
        if detail.get("degraded") or detail.get("rate_limited_providers"):
            return False, "degraded_or_rate_limited_panel", ""

        risk_brief = (getattr(verdict, "briefs", {}) or {}).get("risk", {}) or {}
        if isinstance(risk_brief, dict) and risk_brief.get("approved") is False:
            return False, "risk_denied", ""

        change_pct = self._to_float_safe(candidate.get("change_pct", 0.0), 0.0)
        vol = self._to_float_safe(candidate.get("volume_spike", 0.0), 0.0)
        spread = self._to_float_safe(candidate.get("spread_pct", 0.0), 0.0)
        side_hint = str(candidate.get("side", "long") or "long").lower()
        wyckoff_bias = str(candidate.get("wyckoff_bias", "") or "")
        if spread > 1.2:
            return False, "wide_spread", ""
        if wyckoff_bias in {"distribution_risk", "upthrust_risk"}:
            return False, f"wyckoff_{wyckoff_bias}", ""

        if side_hint != "short" and change_pct >= 6.0 and vol >= 3.0:
            return True, "strong_long_momentum", "BUY"
        if side_hint == "short" and change_pct <= -5.0 and vol >= 2.5:
            return True, "strong_short_momentum", "SHORT"
        return False, "momentum_not_strong_enough", ""

    def _allow_classifier_auto_enter(self, candidate: dict, verdict) -> tuple[bool, str, str]:
        """
        THE CENTERPIECE: reduced-size deterministic entry for the cleanest setups.

        Production-trusted modes: continuation_long, continuation_short, swing_catalyst_long.
        Jury becomes refinement (size upgrade), not existential veto.
        """
        if not self._mode_classifier_enforced():
            return False, "mode_classifier_not_enforced", ""
        if not bool(getattr(settings, "MODE_CLASSIFIER_AUTO_ENTER", False)):
            return False, "classifier_auto_disabled", ""

        provider_health = getattr(self, "provider_health", None)
        if provider_health:
            try:
                provider_policy = provider_health.get_policy() or {}
                candidate["provider_health_policy"] = provider_policy
                if not provider_policy.get("auto_enter_allowed", True):
                    return False, f"provider_policy:{provider_policy.get('mode', 'unknown')}", ""
            except Exception as e:
                logger.debug(f"Provider health policy lookup failed: {e}")

        detail = getattr(verdict, "consensus_detail", {}) or {}
        agreement = str(detail.get("agreement", "") or "").lower()
        if agreement in {"adversary_veto", "risk_block"}:
            return False, agreement or "protected_skip", ""
        if detail.get("degraded") or detail.get("rate_limited_providers"):
            return False, "degraded_or_rate_limited_panel", ""

        risk_brief = (getattr(verdict, "briefs", {}) or {}).get("risk", {}) or {}
        if isinstance(risk_brief, dict) and risk_brief.get("approved") is False:
            return False, "risk_denied", ""

        auto_decision = "SHORT" if str(candidate.get("direction_constraint", "none") or "").lower() == "short_only" else "BUY"
        setup_mode = str(candidate.get("setup_mode", "") or "").lower()

        PRODUCTION_TRUSTED = {"continuation_long", "continuation_short", "swing_catalyst_long"}
        if setup_mode not in PRODUCTION_TRUSTED:
            return False, "setup_mode_not_trusted", auto_decision

        if str(candidate.get("timing_state", "") or "").lower() != "enter_now":
            return False, "timing_not_live", auto_decision
        if str(candidate.get("entry_quality", "") or "").lower() not in {"pullback", "at_highs"}:
            return False, "entry_quality_not_clean", auto_decision

        min_confidence = 0.70 if setup_mode == "swing_catalyst_long" else 0.65
        if float(candidate.get("classifier_confidence", 0) or 0) < min_confidence:
            return False, "classifier_confidence_too_low", auto_decision
        if float(candidate.get("resolver_confidence", 0) or 0) < 0.55:
            return False, "resolver_confidence_too_low", auto_decision

        if setup_mode == "swing_catalyst_long":
            max_spread = 0.5
            catalyst_tag = str(candidate.get("catalyst_tag", "") or "").lower()
            if catalyst_tag not in {"congress", "insider", "fda", "earnings"}:
                return False, "catalyst_not_tier1", auto_decision
        else:
            max_spread = 0.6

        if float(candidate.get("spread_pct", 0) or 0) > max_spread:
            return False, "spread_too_wide", auto_decision

        if not self.entry_manager.is_market_open():
            return False, "market_closed", auto_decision
        if auto_decision == "SHORT" and not self._shorting_ready():
            return False, "shorting_not_ready", auto_decision

        if hasattr(self, "session_context") and self.session_context.snapshot:
            if auto_decision == "BUY":
                block_longs, reason = self.session_context.should_block_longs()
                if block_longs:
                    return False, f"session_context:{reason}", auto_decision
            if self.session_context.snapshot.broad_risk_tone == "risk_off" and auto_decision == "BUY":
                return False, "risk_off_blocks_auto_longs", auto_decision

        if hasattr(self, "pre_trade_cost"):
            report = self.pre_trade_cost.evaluate(candidate, self.session_context.snapshot if hasattr(self, "session_context") else None)
            candidate["executability_report"] = report.to_dict()
            if report.execution_verdict in ("broker_blocked", "execution_unfavorable"):
                return False, f"executability:{report.dominant_blocker}", auto_decision
            if not report.edge_survives_cost:
                return False, "edge_does_not_survive_cost", auto_decision

        if hasattr(self, "concentration_guard"):
            positions = self.entry_manager.get_positions() if self.entry_manager else {}
            conc = self.concentration_guard.evaluate(candidate, positions)
            candidate["concentration_report"] = conc.to_dict()
            logger.info(
                f"🧲 CONCENTRATION {candidate.get('symbol', '')}: allowed={conc.new_entry_allowed} "
                f"blocker={conc.dominant_blocker or conc.new_entry_reason or 'none'} "
                f"beta={conc.portfolio_beta:.2f} size_adj={conc.size_adjustment:.2f}"
            )
            if not conc.new_entry_allowed:
                return False, f"concentration:{conc.dominant_blocker}", auto_decision

        return True, "ok", auto_decision

    def _compute_auto_entry_size_pct(self, candidate: dict) -> float:
        """Tiered sizing for auto-entry: 35% / 50% / 75% based on gate quality."""
        base = 0.50
        classifier_conf = float(candidate.get("classifier_confidence", 0) or 0)
        exec_report = candidate.get("executability_report", {})
        exec_quality = float(exec_report.get("execution_quality_score", 0.5) or 0.5)

        if classifier_conf >= 0.80 and exec_quality >= 0.85:
            base = 0.75
        elif classifier_conf < 0.70 or exec_quality < 0.6:
            base = 0.35

        if hasattr(self, "session_context"):
            ctx_mod = self.session_context.get_sizing_modifier()
            base *= ctx_mod

        if hasattr(self, "pre_trade_cost") and exec_report:
            cost_mod = self.pre_trade_cost.get_size_adjustment(
                type("R", (), exec_report)()
                if not hasattr(exec_report, "execution_verdict")
                else exec_report
            )
            base *= cost_mod

        return round(max(0.1, min(0.75, base)), 3)

    def _effective_entry_confidence_floor(self, candidate: dict, verdict) -> tuple[float, list[str]]:
        """
        Keep math-approved auto entries nimble, but require more conviction for
        discretionary entries that historically leak win rate.
        """
        floor = float(getattr(settings, "MIN_JURY_CONFIDENCE", 40) or 40)
        reasons = [f"base={floor:.0f}"]

        provider_used = str(getattr(verdict, "provider_used", "") or "").lower()
        if provider_used.startswith("classifier_auto"):
            return floor, reasons

        discretionary_floor = float(getattr(settings, "DISCRETIONARY_MIN_JURY_CONFIDENCE", 50) or 50)
        if discretionary_floor > floor:
            floor = discretionary_floor
            reasons.append(f"discretionary={discretionary_floor:.0f}")

        direction = str(getattr(verdict, "decision", "") or "").upper()
        if direction == "SHORT":
            short_floor = float(getattr(settings, "SHORT_MIN_JURY_CONFIDENCE", 55) or 55)
            if short_floor > floor:
                floor = short_floor
                reasons.append(f"short={short_floor:.0f}")

        signal_tier = str(candidate.get("signal_tier", "tier_2") or "tier_2").lower()
        entry_quality = str(candidate.get("entry_quality", "neutral") or "neutral").lower()
        if signal_tier != "tier_1" and entry_quality == "neutral":
            neutral_floor = float(
                getattr(settings, "NEUTRAL_ENTRY_MIN_JURY_CONFIDENCE", 50) or 50
            )
            if neutral_floor > floor:
                floor = neutral_floor
                reasons.append(f"neutral={neutral_floor:.0f}")

        return floor, reasons

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
        guardrails = self._get_operating_guardrails()
        if not guardrails.get("allow_new_entries", True):
            self._log_guardrail_block("⚡ FAST-PATH", guardrails.get("reasons", []))
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
            try:
                await self._process_candidates_serial([candidate])
            finally:
                self._fast_path_pending.discard(symbol)
            processed += 1

    def _queue_pending_live_refresh(self, symbol: str, price: float = 0.0):
        symbol_key = str(symbol or "").upper().strip()
        if not symbol_key:
            return
        if not get_pending_setup(symbol_key):
            return
        now = time.time()
        last_refresh = float(self._pending_live_refresh_at.get(symbol_key, 0.0) or 0.0)
        cooldown = max(1.0, float(getattr(settings, "PENDING_LIVE_REFRESH_COOLDOWN_SECONDS", 5.0) or 5.0))
        if (now - last_refresh) < cooldown:
            return
        self._pending_live_refresh_at[symbol_key] = now
        payload = {
            "symbol": symbol_key,
            "price": float(price or 0.0),
            "source": "market_stream_live_refresh",
            "signal_timestamp": now,
            "score": 0.0,
        }
        try:
            self._pending_live_refresh_queue.put_nowait(payload)
        except asyncio.QueueFull:
            logger.debug(f"Pending live refresh queue full for {symbol_key}")

    async def _process_pending_live_refresh_queue(self):
        if not hasattr(self, "_pending_live_refresh_queue") or self._pending_live_refresh_queue.empty():
            return
        if not getattr(self, "_broker_ready", False):
            return
        processed = 0
        seen_symbols = set()
        while not self._pending_live_refresh_queue.empty() and processed < 3:
            try:
                candidate = self._pending_live_refresh_queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            symbol = str(candidate.get("symbol", "") or "").upper().strip()
            if not symbol or symbol in seen_symbols:
                continue
            seen_symbols.add(symbol)
            logger.debug(f"⚡ LIVE REFRESH pending setup: {symbol}")
            await self._process_candidates_serial([candidate])
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

    async def _process_candidates_serial(self, candidates):
        lock = getattr(self, "_candidate_processing_lock", None)
        if lock is None:
            self._candidate_processing_lock = asyncio.Lock()
            lock = self._candidate_processing_lock
        async with lock:
            await self._process_candidates(candidates)

    def _ensure_uw_signal_drain(self):
        task = getattr(self, "_uw_signal_drain_task", None)
        if task and not task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._uw_signal_drain_task = loop.create_task(self._drain_uw_signal_queue())

    async def _drain_uw_signal_queue(self):
        while True:
            queue = getattr(self, "_uw_signal_queue", None)
            if not queue or queue.empty():
                return
            await self._process_unusual_whales_signal_queue()
            await asyncio.sleep(0.05)

    def _build_uw_candidate(self, event: Dict) -> Dict:
        symbol = normalize_trade_symbol(event.get("ticker", "") or event.get("symbol", "") or "")
        if not is_supported_trade_symbol(symbol):
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
        self._ensure_uw_signal_drain()
        if queue.qsize() <= 2:
            try:
                await self._process_unusual_whales_signal_queue()
            except Exception as e:
                logger.error(f"UW realtime immediate handling error: {e}")

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
            await self._process_candidates_serial([candidate])
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
        return trading_session_day()

    def _roll_daily_state_if_needed(self):
        """Reset per-day P&L and risk stats once when trading day changes."""
        if not isinstance(getattr(self, "pnl_state", None), dict):
            return

        today = self._current_trading_day()
        if self._last_daily_reset_date == today and self.pnl_state.get("today_date") == today:
            return

        stored_today = str(self.pnl_state.get("today_date") or "")
        if stored_today and stored_today > today:
            self.pnl_state["today_date"] = today
            try:
                persistence.save_pnl_state(self.pnl_state)
            except Exception:
                pass
            logger.info(f"🗓️ Adjusted pnl_state day key backward to active session {today} without reset")
        elif stored_today != today:
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
        cooldown_remaining = self._symbol_reentry_cooldown_remaining(symbol)
        if cooldown_remaining > 0:
            return False, f"reentry_cooldown_{int(cooldown_remaining)}s"
        self._prune_jury_vetoes()
        jury_vetoes = getattr(self, "_jury_vetoed_symbols", {})
        vetoed_at = jury_vetoes.get(symbol)
        if vetoed_at and (time.time() - float(vetoed_at)) < 3600:
            return False, "jury_vetoed"
        if price <= 0 or price < 5 or price > 500:
            return False, "price_out_of_range"
        min_change = float(getattr(settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0))
        if abs(float(pct_change or 0.0)) < min_change:
            return False, "insufficient_change"
        min_vol_spike = float(getattr(settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0))
        if volume_spike < min_vol_spike:
            return False, "insufficient_volume"

        positions = self.entry_manager.get_positions() if self.entry_manager else []
        held_symbols = {
            str(p.get("symbol", "") or "").upper()
            for p in positions
            if str(p.get("symbol", "") or "").strip()
        }
        held_symbols |= set(getattr(self, "_latest_broker_position_symbols", set()) or set())
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
        if "can_trade" in brief:
            return (
                f"can_trade={brief.get('can_trade')} "
                f"size={brief.get('size_cap_pct', 0)} "
                f"flags={brief.get('constraint_flags', [])}"
            )
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
            "signal_tier": position.get("signal_tier", "tier_2"),
            "holding_horizon": position.get("holding_horizon", "intraday"),
            "market_regime": position.get("market_regime", "mixed"),
            "entry_reason_code": position.get("entry_reason_code", "unknown"),
            "entry_model_votes": dict(position.get("entry_model_votes", {}) or {}),
            "risk_constraints_applied": list(position.get("risk_constraints_applied", []) or []),
            "setup_id": position.get("setup_id", ""),
            "setup_mode": position.get("setup_mode", "invalid"),
            "direction_constraint": position.get("direction_constraint", "none"),
            "timing_state": position.get("timing_state", "enter_now"),
            "best_play": position.get("best_play", ""),
            "trigger": position.get("trigger", ""),
            "trigger_spec": dict(position.get("trigger_spec", {}) or {}),
            "invalidation": position.get("invalidation", ""),
            "hold_style": position.get("hold_style", position.get("holding_horizon", "intraday")),
            "size_posture": position.get("size_posture", "normal"),
            "no_trade_reason": position.get("no_trade_reason"),
            "classifier_confidence": position.get("classifier_confidence", 0.0),
            "resolver_confidence": position.get("resolver_confidence", 0.0),
            "execution_confidence": position.get("execution_confidence", 0.0),
            "feature_snapshot_id": position.get("feature_snapshot_id", ""),
            "feature_quality_score": position.get("feature_quality_score", 0.0),
            "feature_quality": position.get("feature_quality", ""),
            "missing_fields": list(position.get("missing_fields", []) or []),
            "material_change_signature": position.get("material_change_signature", ""),
            "symbol_state": position.get("symbol_state", "live_position"),
            "jury_entry_now": bool(position.get("jury_entry_now", False)),
            "jury_trigger": position.get("jury_trigger", ""),
            "jury_invalidation": position.get("jury_invalidation", ""),
            "jury_hold_style": position.get("jury_hold_style", ""),
            "jury_size_posture": position.get("jury_size_posture", ""),
            "jury_no_trade_reason": position.get("jury_no_trade_reason"),
            "ratchet_peak_pnl_pct": position.get("ratchet_peak_pnl_pct", 0.0),
            "ratchet_floor_pct": position.get("ratchet_floor_pct"),
            "ratchet_limit_order_id": position.get("ratchet_limit_order_id"),
            "hard_stop_order_id": position.get("hard_stop_order_id"),
            "order_state": dict(position.get("order_state", {}) or {}),
            "signal_sources": position.get("signal_sources", ["unknown"]),
            "decision_confidence": position.get("decision_confidence", 0),
            "provider_used": position.get("provider_used", ""),
            "signal_price": position.get("signal_price", entry_price),
            "decision_price": position.get("decision_price", entry_price),
            "fill_price": float(fill_price or 0),
            "fill_timestamp": confirmed_exit_time,
            "fill_timestamp_source": fill_source,
            "exit_order_id": position.get("exit_order_id"),
            "triggered": True,
            "entered": True,
            "profitable": pnl > 0,
            "ratchet_activated": bool(position.get("ratchet_floor_pct") is not None),
            "hard_stopped": str(reason or "").lower().startswith("hard_stop"),
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
        """Synchronous callback-safe breakout handler that routes breakouts into the jury queue."""
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
            "side": "short" if float(pct_change or 0.0) < 0 else "long",
            "score": abs(pct_change) / 100 + volume_spike / 10,
            "signal_timestamp": signal_timestamp,
            "strategy_tag": "breakout_fast_path",
            "signal_tier": "tier_2",
            "holding_horizon": "intraday",
        }

        self._fast_path_pending.add(symbol)
        log_activity(
            "scan",
            f"⚡ Fast-path candidate: {symbol} {pct_change:+.1f}% vol={volume_spike:.1f}x",
            {"signal_timestamp": signal_timestamp},
        )
        try:
            self._breakout_queue.put_nowait(candidate)
        except asyncio.QueueFull:
            logger.warning(f"⚡ Breakout queue full — dropping fast-path candidate for {symbol}")
            self._fast_path_pending.discard(symbol)
        except Exception:
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
            tech = {}
            if self.polygon_client:
                try:
                    tech = await compute_technicals(symbol, price, self.polygon_client) or {}
                except Exception:
                    tech = {}
            if tech:
                candidate.update(tech)
            wyckoff_bias = self._infer_wyckoff_bias(candidate)
            candidate["wyckoff_bias"] = wyckoff_bias
            if bool(getattr(settings, "FAST_PATH_BLOCK_BEARISH_WYCKOFF", True)) and wyckoff_bias in {
                "distribution_risk",
                "upthrust_risk",
            }:
                logger.info(f"⚡ FAST-PATH blocked by Wyckoff gate for {symbol}: {wyckoff_bias}")
                return

            if not self.entry_manager.is_market_open():
                logger.info(f"⚡ FAST-PATH blocked outside market hours: {symbol}")
                return

            if (
                self.risk_manager
                and getattr(settings, "SWING_MODE_DISABLE_FAST_PATH", True)
                and self.risk_manager.is_swing_mode()
            ):
                logger.info(f"⚡ FAST-PATH skipped in swing mode: {symbol}")
                return
            spread_pct = self._to_float_safe(candidate.get("spread_pct", 0.0), 0.0)
            max_spread = float(getattr(settings, "FAST_PATH_MAX_SPREAD_PCT", 0.80) or 0.80)
            if spread_pct > max_spread:
                logger.info(f"⚡ FAST-PATH blocked by spread for {symbol}: {spread_pct:.2f}% > {max_spread:.2f}%")
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
                "wyckoff_bias": wyckoff_bias,
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
        """Evaluate scanner candidates for entry with v2 jury semantics."""
        if not getattr(self, "_broker_ready", False):
            return
        if not self.risk_manager.can_trade():
            return

        guardrails = self._get_operating_guardrails()
        if not guardrails.get("allow_new_entries", True):
            self._log_guardrail_block("⛔ Entry pipeline", guardrails.get("reasons", []))
            return

        self._prune_jury_vetoes()
        await self._refresh_shadow_records()
        positions = self.entry_manager.get_positions()
        congress_scanner = getattr(self, "congress_scanner", None)
        human_intel_store = getattr(self, "human_intel_store", None)
        edgar_scanner = getattr(self, "edgar_scanner", None)

        def _tier_rank(candidate_row: Dict) -> int:
            return {"tier_1": 0, "tier_2": 1, "tier_3": 2}.get(str(candidate_row.get("signal_tier", "tier_2")), 1)

        pending_candidates = self._build_pending_setup_candidates(list(candidates or []))
        candidate_pool_limit = max(8, int(getattr(settings, "ENTRY_CANDIDATE_POOL_LIMIT", 40) or 40))
        combined_candidates = pending_candidates + list(candidates or [])[:candidate_pool_limit]
        prepared_candidates = [self._prepare_candidate_metadata(candidate) for candidate in combined_candidates]
        prepared_candidates.sort(
            key=lambda row: (
                0 if bool(row.get("_pending_setup")) else 1,
                _tier_rank(row),
                -float(row.get("priority", 0) or 0),
                -float(row.get("uw_total_premium", 0) or 0),
                -float(row.get("score", 0) or 0),
            )
        )

        evaluated = 0
        held_symbols = {
            str(p.get("symbol", "") or "").upper()
            for p in positions
            if str(p.get("symbol", "") or "").strip()
        }
        held_symbols |= await self._get_latest_broker_position_symbols()
        evaluated_symbols = set()

        for candidate in prepared_candidates:
            eval_limit = max(4, int(getattr(settings, "ENTRY_EVAL_LIMIT", 16) or 16))
            if evaluated >= eval_limit:
                break

            symbol = str(candidate.get("symbol", "") or "").upper()
            if not symbol or symbol in held_symbols:
                continue
            if symbol in evaluated_symbols:
                logger.debug(f"Skipping duplicate candidate in same cycle: {symbol}")
                continue
            evaluated_symbols.add(symbol)

            # ── V3: Record scan in funnel + state machine ──
            _setup_id = str(candidate.get("setup_id", "") or "")
            _mode = str(candidate.get("setup_mode", "") or "")
            _book = str(candidate.get("strategy_tag", "") or "")
            if hasattr(self, "state_store"):
                try:
                    self.state_store.record_funnel_event(symbol, _setup_id, "scanned", _mode, _book)
                except Exception:
                    pass
            if hasattr(self, "symbol_state_tracker"):
                try:
                    if not self.symbol_state_tracker.is_occupied(symbol):
                        self.symbol_state_tracker.transition(symbol, "classified", "scan_candidate", setup_id=_setup_id, setup_mode=_mode)
                except Exception:
                    pass

            cooldown_remaining = self._symbol_reentry_cooldown_remaining(symbol)
            if cooldown_remaining > 0:
                logger.info(f"🧊 ENTRY COOLDOWN {symbol}: {int(cooldown_remaining)}s after recent exit")
                continue
            blocked, block_reason = entry_controls.is_entry_blocked(symbol)
            if blocked:
                logger.info(f"⛔ PERSISTENT BLOCK {symbol}: {block_reason}")
                continue

            strategy_tag = normalize_strategy_tag(candidate.get("strategy_tag", "unknown"), fallback="unknown")
            candidate["strategy_tag"] = strategy_tag
            positions_by_strategy = self._position_strategy_counts(positions)
            max_for_strategy = int(STRATEGY_MAX_POSITIONS.get(strategy_tag, 20) or 20)
            current_count = int(positions_by_strategy.get(strategy_tag, 0) or 0)
            if current_count >= max_for_strategy:
                logger.info(f"📊 BOOK CAP {symbol}: {strategy_tag} at {current_count}/{max_for_strategy}")
                self._record_candidate_block(candidate, "capital_blocked", f"book_cap:{strategy_tag}")
                continue
            if strategy_tag == "momentum_long":
                candidate_sector = self._sector_bucket(candidate)
                sector_count = sum(
                    1
                    for pos in positions
                    if normalize_strategy_tag(pos.get("strategy_tag", "unknown"), fallback="unknown") == "momentum_long"
                    and self._sector_bucket(pos) == candidate_sector
                )
                if sector_count >= 3:
                    logger.info(
                        f"📊 SECTOR CAP {symbol}: {candidate_sector} already has "
                        f"{sector_count} momentum_long positions"
                    )
                    self._record_candidate_block(candidate, "capital_blocked", f"sector_cap:{candidate_sector}")
                    continue

            candidate["wyckoff_bias"] = self._infer_wyckoff_bias(candidate)

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

            candidate = await self._enrich_candidate_setup_context(candidate)
            pending_mode = str(candidate.get("_pending_setup_mode", "") or "").strip().lower() or None
            current_mode = str(candidate.get("setup_mode", "invalid") or "invalid").strip().lower()
            timing_state = str(candidate.get("timing_state", "mode_conflict") or "mode_conflict").strip().lower()
            candidate["mode_constraint_active"] = self._mode_classifier_enforced()
            _setup_id = str(candidate.get("setup_id", _setup_id) or _setup_id)
            _mode = current_mode

            # ── V3: Record classification in funnel ──
            if hasattr(self, "state_store"):
                try:
                    self.state_store.record_funnel_event(symbol, _setup_id, "classified", _mode, _book)
                except Exception:
                    pass

            # ── V3: Pre-trade executability check on every candidate ──
            if hasattr(self, "pre_trade_cost") and current_mode != "invalid":
                try:
                    _exec_report = self.pre_trade_cost.evaluate(
                        candidate,
                        self.session_context.snapshot if hasattr(self, "session_context") else None,
                    )
                    candidate["executability_report"] = _exec_report.to_dict()
                    candidate["execution_quality_score"] = _exec_report.execution_quality_score
                    candidate["edge_survives_cost"] = _exec_report.edge_survives_cost
                    logger.info(
                        f"💰 PRE-TRADE {symbol}: verdict={_exec_report.execution_verdict} "
                        f"edge={_exec_report.expected_edge_bps:.0f}bps "
                        f"cost={_exec_report.estimated_implementation_shortfall_bps:.0f}bps "
                        f"quality={_exec_report.execution_quality_score:.2f}"
                    )
                except Exception as e:
                    logger.debug(f"PreTradeCost eval failed for {symbol}: {e}")

            if pending_mode and pending_mode != current_mode:
                self._remove_waiting_setup(symbol, mode=pending_mode)

            if timing_state == "wait_for_trigger":
                self._persist_waiting_setup(candidate, shadow_mode=not self._mode_classifier_enforced())
                # V3: persist to trigger engine + SQLite + funnel
                if hasattr(self, "trigger_engine"):
                    try:
                        self.trigger_engine.add_pending(candidate)
                    except Exception:
                        pass
                if hasattr(self, "state_store"):
                    try:
                        self.state_store.record_funnel_event(symbol, _setup_id, "pending_trigger", _mode, _book)
                    except Exception:
                        pass
                if hasattr(self, "symbol_state_tracker"):
                    try:
                        self.symbol_state_tracker.transition(symbol, "pending_trigger", "wait_for_trigger", setup_id=_setup_id, setup_mode=_mode)
                    except Exception:
                        pass
                logger.info(
                    f"⏳ SETUP WAIT {symbol}: mode={current_mode} trigger={candidate.get('trigger', 'n/a')} "
                    f"expires_at={candidate.get('expires_at')}"
                )
                if self._mode_classifier_enforced():
                    continue
            elif timing_state == "shadow_only":
                self._remove_waiting_setup(symbol, mode=pending_mode or current_mode)
                self._record_candidate_block(candidate, "shadow_only", candidate.get("no_trade_reason", "mode_disabled"))
                logger.info(
                    f"👻 SETUP SHADOW {symbol}: mode={current_mode} "
                    f"reason={candidate.get('no_trade_reason') or 'mode_disabled'}"
                )
                continue
            elif timing_state in {"data_insufficient", "mode_conflict"} or current_mode == "invalid":
                self._remove_waiting_setup(symbol, mode=pending_mode or current_mode)
                if self._mode_classifier_enforced():
                    block_state = timing_state if timing_state in {"data_insufficient", "mode_conflict"} else "data_insufficient"
                    self._record_candidate_block(
                        candidate,
                        block_state,
                        candidate.get("no_trade_reason") or ",".join(candidate.get("classifier_reason_codes", [])[:2]) or "no_clear_setup",
                    )
                    logger.info(
                        f"🚫 SETUP BLOCK {symbol}: mode={current_mode} timing={timing_state} "
                        f"reason={candidate.get('no_trade_reason') or ','.join(candidate.get('classifier_reason_codes', [])[:2]) or 'no_clear_setup'}"
                    )
                    continue
            elif current_mode != "invalid":
                self._remove_waiting_setup(symbol, mode=pending_mode or current_mode)

            if self.position_manager and not self.position_manager.can_enter(symbol, positions, self.risk_manager):
                logger.info(
                    f"🧭 ENTRY TRACE {symbol}: blocked before jury by position_manager "
                    f"strategy={candidate.get('strategy_tag', 'unknown')} side={candidate.get('side', 'long')}"
                )
                self._record_candidate_block(candidate, "capital_blocked", "position_manager_block")
                continue

            sentiment_score = float(candidate.get("sentiment_score", 0) or 0)
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
            sentiment_data["signal_tier"] = candidate.get("signal_tier", "tier_2")
            sentiment_data["holding_horizon"] = candidate.get("holding_horizon", "intraday")
            sentiment_data["market_regime"] = candidate.get("market_regime", "mixed")
            sentiment_data["uw_flow_summary"] = candidate.get("uw_flow_summary", "")
            sentiment_data["strategy_tag"] = candidate.get("strategy_tag", "unknown")
            sentiment_data["share_notional_multiplier"] = 1.0

            if self._mode_classifier_enforced():
                constraint = str(candidate.get("direction_constraint", "none") or "none").lower()
                if constraint == "short_only":
                    candidate_direction = "SHORT"
                elif constraint == "long_only":
                    candidate_direction = "BUY"
                else:
                    candidate_direction = "SHORT" if str(candidate.get("side", "")).lower() == "short" else "BUY"
            else:
                candidate_direction = "SHORT" if str(candidate.get("side", "")).lower() == "short" else "BUY"

            # Shortability pre-check: don't waste jury evals on unshortable stocks
            if candidate_direction == "SHORT" and self.alpaca_client and hasattr(self.alpaca_client, "is_shortable"):
                if not self.alpaca_client.is_shortable(symbol):
                    candidate["timing_state"] = "broker_blocked"
                    candidate["no_trade_reason"] = "not_shortable"
                    self._record_candidate_block(candidate, "broker_blocked", "not_shortable")
                    self._record_short_verdict_block(symbol, "not_shortable", "pre_check")
                    continue
            pre_risk_brief = await book_risk_agent.analyze(
                symbol=symbol,
                price=signal_price,
                signals=candidate,
                risk_manager=self.risk_manager,
                positions=positions,
                direction=candidate_direction,
            )
            disabled_strategy = self._extract_disabled_strategy(pre_risk_brief)
            if disabled_strategy:
                candidate["strategy_tag"] = disabled_strategy
                self._record_candidate_block(candidate, "shadow_only", f"strategy_disabled:{disabled_strategy}")
                logger.info(
                    f"👻 SHADOW {symbol}: {disabled_strategy} disabled — hypothetical entry @ ${signal_price:.2f} "
                    f"entry_quality={candidate.get('entry_quality')} spread={candidate.get('spread_pct')} "
                    f"range_pct={candidate.get('range_pct')}"
                )
                log_activity("shadow", f"👻 {symbol} {disabled_strategy} hypothetical @ ${signal_price:.2f}")
                continue

            if not self.orchestrator:
                continue

            try:
                if bool(getattr(settings, "COUNCIL_MODE_ENABLED", True)):
                    verdict = await self.orchestrator.evaluate_council(
                        symbol=symbol,
                        price=signal_price,
                        signals_data=candidate,
                    )
                else:
                    verdict = await self.orchestrator.evaluate(
                        symbol=symbol,
                        price=signal_price,
                        signals_data=candidate,
                    )
            except Exception as e:
                logger.error(f"Orchestrator error for {symbol}: {e}")
                continue

            fallback_verdict = self._build_provider_fallback_verdict(candidate, verdict)
            if fallback_verdict is not None:
                verdict = fallback_verdict

            if "cooldown" not in verdict.reasoning.lower():
                evaluated += 1
                if evaluated > 1:
                    await asyncio.sleep(max(0.0, float(getattr(settings, "ENTRY_EVAL_SLEEP_SECONDS", 0.35) or 0.35)))

            self.ai_layers["last_consensus"] = verdict.to_dict()
            consensus_detail = getattr(verdict, "consensus_detail", {}) or {}
            votes = dict(consensus_detail.get("votes", {}) or {})
            briefs = getattr(verdict, "briefs", {}) or {}
            risk_brief = briefs.get("risk", {}) or {}
            agreement = str(consensus_detail.get("agreement", "unknown") or "unknown")

            logger.info(
                f"🧭 JURY TRACE {symbol}: tier={candidate.get('signal_tier')} decision={verdict.decision} "
                f"conf={verdict.confidence:.1f}% agreement={agreement} votes={votes}"
            )
            logger.info(
                f"🧭 BRIEFS {symbol}: tech={self._summarize_brief_for_trace(briefs.get('technical', {}))} "
                f"sent={self._summarize_brief_for_trace(briefs.get('sentiment', {}))} "
                f"cat={self._summarize_brief_for_trace(briefs.get('catalyst', {}))} "
                f"risk={self._summarize_brief_for_trace(risk_brief)} "
                f"macro={self._summarize_brief_for_trace(briefs.get('macro', {}))}"
            )

            if verdict.decision not in {"BUY", "SHORT"}:
                # Auto-enter path: classifier + resolver override jury SKIP for clean continuation_long pullbacks
                auto_enter = False
                auto_allowed, auto_reason, auto_decision = self._allow_classifier_auto_enter(candidate, verdict)
                if auto_allowed:
                    logger.warning(
                        f"🔥 AUTO-ENTER OVERRIDE {symbol}: classifier {candidate.get('setup_mode')} + enter_now "
                        f"(conf={candidate.get('classifier_confidence')}, quality={candidate.get('entry_quality')}, direction={auto_decision}) "
                        f"— jury skipped but math says go at 50% size"
                    )
                    log_activity("trade", f"🔥 AUTO-ENTER: {symbol} {candidate.get('setup_mode')} (jury overridden by classifier)")
                    verdict = JuryVerdict(
                        symbol=symbol,
                        decision=auto_decision,
                        size_pct=max(0.5, float(verdict.size_pct or 1.0) * 0.5),
                        trail_pct=float(verdict.trail_pct or 2.0),
                        reasoning=f"Classifier auto-enter: {candidate.get('setup_mode')} {candidate.get('entry_quality')} conf={candidate.get('classifier_confidence')}",
                        confidence=float(candidate.get("classifier_confidence", 0.7) or 0.7) * 100,
                        provider_used="classifier_auto",
                        briefs=briefs,
                        consensus_detail={"agreement": "classifier_auto_enter", "votes": votes},
                    )
                    auto_enter = True
                else:
                    logger.info(f"🧮 CLASSIFIER AUTO SKIP {symbol}: {auto_reason}")

                if not auto_enter:
                    if str(candidate.get("timing_state", "") or "").lower() == "wait_for_trigger":
                        self._persist_waiting_setup(candidate, verdict=verdict, shadow_mode=not self._mode_classifier_enforced())
                    if "cooldown" not in verdict.reasoning.lower():
                        self._record_jury_veto(symbol)
                        from src.data.entry_controls import record_jury_veto as _persist_veto

                        _persist_veto(symbol)
                        self._record_candidate_block(candidate, "mode_conflict", verdict.reasoning, verdict=verdict)
                        logger.info(f"Jury SKIP for {symbol}: {verdict.reasoning}")
                        log_activity("ai", f"{symbol}: SKIP — {verdict.reasoning}")
                    continue

            min_conf, floor_reasons = self._effective_entry_confidence_floor(candidate, verdict)
            if verdict.confidence < min_conf:
                logger.warning(
                    f"Jury {verdict.decision} for {symbol} below effective confidence floor "
                    f"({verdict.confidence:.0f}% < {min_conf:.0f}%) "
                    f"[{', '.join(floor_reasons)}] — forcing SKIP"
                )
                log_activity(
                    "ai",
                    f"{symbol}: {verdict.decision} blocked — confidence {verdict.confidence:.0f}% < {min_conf:.0f}% "
                    f"({'/'.join(floor_reasons)})",
                )
                self._record_jury_veto(symbol)
                self._record_candidate_block(
                    candidate,
                    "mode_conflict",
                    f"jury_confidence_below_floor:{verdict.confidence:.0f}:required_{min_conf:.0f}",
                    verdict=verdict,
                )
                continue

            direction = verdict.decision

            # Regime-aware entry restriction: in risk_off, long entries need higher conviction
            market_regime = str(candidate.get("market_regime", "mixed") or "mixed").lower()
            if direction == "BUY" and market_regime == "risk_off":
                regime_min_conf = float(getattr(settings, "RISK_OFF_LONG_MIN_CONFIDENCE", 70) or 70)
                if verdict.confidence < regime_min_conf:
                    logger.warning(
                        f"🛡️ REGIME GATE {symbol}: BUY blocked in risk_off regime "
                        f"(confidence {verdict.confidence:.0f}% < {regime_min_conf:.0f}% threshold)"
                    )
                    log_activity(
                        "trade",
                        f"🛡️ REGIME GATE: {symbol} BUY blocked — risk_off needs {regime_min_conf:.0f}%+ confidence",
                    )
                    continue

            candidate = self._prepare_candidate_metadata(candidate)

            if candidate.get("strategy_tag") == "uw_flow_short" and agreement == "tier1_probe":
                logger.info(f"📉 PROBE BLOCK {symbol}: uw_flow_short requires 2-of-3 jury agreement")
                log_activity("trade", f"📉 PROBE BLOCK: {symbol} uw_flow_short requires 2-of-3 jury agreement")
                continue

            tier = self.risk_manager.get_risk_tier() if self.risk_manager else {}
            tier_size = float(tier.get("size_pct", 2.0) or 2.0)
            risk_cap_pct = float(risk_brief.get("size_cap_pct", risk_brief.get("max_size_pct", tier_size)) or tier_size)
            effective_size_pct = self._compose_effective_size_pct(
                candidate=candidate,
                verdict=verdict,
                tier_size_pct=tier_size,
                risk_cap_pct=risk_cap_pct,
            )
            allocator_plan = self._allocate_entry_size(
                candidate=candidate,
                direction=direction,
                verdict=verdict,
                requested_size_pct=effective_size_pct,
                positions=self.entry_manager.get_positions() if self.entry_manager else positions,
            )
            candidate["allocator_plan"] = dict(allocator_plan or {})
            alloc_size = float(allocator_plan.get("size_pct", effective_size_pct) or effective_size_pct)
            if alloc_size < effective_size_pct:
                logger.info(
                    f"📊 ALLOCATOR LOG {symbol}: allocator recommends {alloc_size:.2f}% vs tier {effective_size_pct:.2f}% "
                    f"book={candidate.get('strategy_tag')} — using tier size (paper account mode)"
                )
            size_modifier = max(0.0, effective_size_pct / tier_size) if tier_size > 0 else 1.0

            sentiment_data["consensus_size_modifier"] = size_modifier
            sentiment_data["consensus_confidence"] = verdict.confidence
            sentiment_data["consensus_direction"] = direction
            sentiment_data["jury_trail_pct"] = verdict.trail_pct
            sentiment_data["provider_used"] = getattr(verdict, "provider_used", "")
            sentiment_data["consensus_agreement"] = agreement
            sentiment_data["strategy_tag"] = candidate.get("strategy_tag", "unknown")
            sentiment_data["signal_tier"] = candidate.get("signal_tier", "tier_2")
            sentiment_data["holding_horizon"] = candidate.get("holding_horizon", "intraday")
            sentiment_data["market_regime"] = candidate.get("market_regime", "mixed")
            sentiment_data["entry_model_votes"] = votes
            sentiment_data["risk_constraints_applied"] = list(risk_brief.get("constraint_flags", []) or [])
            sentiment_data["entry_reason_code"] = f"jury_{agreement}"
            sentiment_data["uw_flow_summary"] = candidate.get("uw_flow_summary", "")
            sentiment_data["extended_hours"] = bool(candidate.get("extended_hours"))
            sentiment_data["entry_quality"] = candidate.get("entry_quality", "neutral")
            sentiment_data["overnight_context"] = candidate.get("overnight_context", "")
            sentiment_data["allocator_state"] = allocator_plan.get("state", "neutral")
            sentiment_data["allocator_alignment"] = allocator_plan.get("alignment", "neutral")
            sentiment_data["allocator_budget_pct"] = allocator_plan.get("budget_pct", 0.0)
            sentiment_data["allocator_exposure_pct"] = allocator_plan.get("current_exposure_pct", 0.0)
            sentiment_data["allocator_remaining_budget_pct"] = allocator_plan.get("remaining_budget_pct", 0.0)
            sentiment_data["allocator_size_multiplier"] = allocator_plan.get("size_multiplier", 1.0)
            sentiment_data["allocator_reason"] = allocator_plan.get("reason", "allocator_ok")
            sentiment_data["allocator_reason_codes"] = list(allocator_plan.get("reason_codes", []) or [])
            sentiment_data["allocator_status"] = allocator_plan.get("status", "hold")
            sentiment_data["allocator_recommended_action"] = allocator_plan.get("recommended_action", "hold")
            sentiment_data["allocator_control_state"] = allocator_plan.get("control_state", "active")
            sentiment_data = self._apply_setup_fields_to_sentiment_data(sentiment_data, candidate, verdict=verdict)
            is_classifier_auto = str(getattr(verdict, "provider_used", "") or "").startswith("classifier_auto")

            log_activity(
                "trade",
                f"🗳️ {symbol}: {direction} verdict tier={candidate.get('signal_tier')} "
                f"conf={verdict.confidence:.0f}% size={effective_size_pct:.2f}%",
            )

            self._clear_jury_veto(symbol)
            from src.data.entry_controls import clear_jury_veto as _clear_persist_veto

            _clear_persist_veto(symbol)

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
                logger.info(
                    f"⛔ PLAYBOOK GATE {symbol}: {reason} "
                    f"strategy={sentiment_data['strategy_tag']} direction={direction}"
                )
                log_activity("trade", f"⛔ PLAYBOOK GATE: {symbol} {direction} blocked ({reason})")
                self._record_candidate_block(candidate, "capital_blocked", f"playbook_gate:{reason}", verdict=verdict)
                if direction == "SHORT":
                    self._record_short_verdict_block(symbol, reason, "playbook")
                continue

            execution_gate = self._setup_execution_gate(candidate, direction)
            sentiment_data = self._apply_setup_fields_to_sentiment_data(
                sentiment_data,
                candidate,
                verdict=verdict,
                execution_gate=execution_gate,
            )
            if not execution_gate.get("allowed", False):
                reason = execution_gate.get("reason", "setup_gate_block")
                logger.info(
                    f"⛔ SETUP GATE {symbol}: {reason} mode={candidate.get('setup_mode')} "
                    f"timing={candidate.get('timing_state')} direction={direction}"
                )
                log_activity("trade", f"⛔ SETUP GATE: {symbol} {direction} blocked ({reason})")
                if reason == "trigger_not_live":
                    candidate["timing_state"] = "wait_for_trigger"
                    candidate["no_trade_reason"] = "trigger_not_live"
                    self._persist_waiting_setup(candidate, verdict=verdict, shadow_mode=not self._mode_classifier_enforced())
                else:
                    block_state = "broker_blocked" if reason in {"shorting_not_ready", "broker_unavailable"} else (
                        "data_insufficient" if reason in {"data_insufficient", "stale_signal", "spread_too_wide", "shadow_only"} else "capital_blocked"
                    )
                    self._record_candidate_block(candidate, block_state, reason, verdict=verdict)
                if direction == "SHORT":
                    self._record_short_verdict_block(symbol, reason, "setup_gate")
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
            # Council and classifier auto-enter bypass sentiment gate -- the council/math already approved this entry
            is_council = str(getattr(verdict, "provider_used", "") or "").startswith("council")
            if is_classifier_auto or is_council:
                effective_sentiment_score = max(effective_sentiment_score, 1.0)
                raw_sentiment_score = max(raw_sentiment_score, 1.0)
            if direction == "SHORT":
                effective_sentiment_score = -abs(raw_sentiment_score) if raw_sentiment_score != 0 else -0.1
                sentiment_data["raw_sentiment_score"] = raw_sentiment_score
                sentiment_data["score"] = effective_sentiment_score
            else:
                sentiment_data["score"] = raw_sentiment_score

            logger.info(f"🔑 {symbol} pre-entry: direction={direction}, sentiment={sentiment_score:.2f}{' [classifier_auto]' if is_classifier_auto else ''}")
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
            if not can:
                self._record_candidate_block(candidate, "capital_blocked", f"entry_gate:{gate_reason}", verdict=verdict)
                continue

            logger.info(
                f"{'📈' if direction == 'BUY' else '📉'} Entry signal: {symbol} {direction} "
                f"(score={float(candidate.get('score', 0) or 0):.3f}, sent={sentiment_score:.2f})"
            )

            options_budget = 0.0
            options_pct = 0.0
            options_engine = getattr(self, "options_engine", None)
            if options_engine:
                confidence = sentiment_data.get("consensus_confidence", 0)
                options_overlay = self._evaluate_options_overlay(candidate, direction, confidence)
                options_pct = float(options_overlay.get("allocation_pct", 0.0) or 0.0)
                sentiment_data["options_overlay_mode"] = options_overlay.get("mode", "off")
                sentiment_data["options_overlay_reason"] = options_overlay.get("reason", "options_disabled")
                trace_reason = str(options_overlay.get("reason", "") or "")
                if trace_reason not in {"pilot_disabled", "pilot_symbol_not_whitelisted", "extended_hours", "options_disabled"}:
                    logger.info(
                        f"🧮 OPTIONS TRACE {symbol}: mode={options_overlay.get('mode', 'off')} "
                        f"eligible={bool(options_overlay.get('eligible'))} reason={trace_reason} "
                        f"alloc={options_pct:.1f}% conf={float(options_overlay.get('confidence', 0.0) or 0.0):.1f}% "
                        f"strategy={options_overlay.get('strategy_tag', sentiment_data.get('strategy_tag', 'unknown'))}"
                    )
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
                            skip_reason = str(getattr(options_engine, "last_trade_skip_reason", "") or "options_trade_skipped")
                            skip_detail = str(getattr(options_engine, "last_trade_skip_detail", "") or "")
                            sentiment_data["options_overlay_skip_reason"] = skip_reason
                            logger.info(
                                f"⚪ OPTIONS SKIP {symbol}: reason={skip_reason}"
                                f"{f' detail={skip_detail}' if skip_detail else ''}"
                            )
                            log_activity("options", f"⚪ OPTIONS SKIP: {symbol} {skip_reason}")
                    else:
                        sentiment_data["options_overlay_skip_reason"] = "portfolio_premium_cap"
                        logger.info(
                            f"⛔ OPTIONS BLOCKED {symbol}: portfolio premium cap "
                            f"(budget=${options_budget:.2f}, alloc={options_pct:.1f}%)"
                        )
                        log_activity("options", f"⛔ OPTIONS BLOCKED: {symbol} would exceed portfolio premium cap")

            if direction == "SHORT":
                pos = await self.entry_manager.enter_short(symbol, sentiment_data)
            else:
                pos = await self.entry_manager.enter_position(symbol, sentiment_data)
            if not pos:
                order_reason = getattr(self.entry_manager, "last_order_error", "") or "entry_execution_failed"
                broker_block_reasons = {
                    "broker_or_polygon_unavailable",
                    "broker_unavailable",
                    "entry_order_failed",
                    "halted",
                }
                capital_block_reasons = {
                    "duplicate_position",
                    "below_min_notional",
                    "position_size_zero",
                }
                execution_block_reasons = {
                    "price_unavailable",
                    "stale_signal_price_drift",
                    "chase_prevention",
                }
                if order_reason in broker_block_reasons or order_reason.startswith("alpaca_"):
                    block_state = "broker_blocked"
                elif order_reason in capital_block_reasons:
                    block_state = "capital_blocked"
                elif order_reason in execution_block_reasons:
                    block_state = "execution_unfavorable"
                else:
                    block_state = "execution_unfavorable"

                logger.warning(f"⛔ ENTRY EXECUTION {symbol}: {direction} failed ({order_reason})")
                log_activity("trade", f"⛔ ENTRY EXECUTION: {symbol} {direction} failed ({order_reason})")
                self._record_candidate_block(
                    candidate,
                    block_state,
                    f"entry_execution:{order_reason}",
                    verdict=verdict,
                    extra={
                        "entry_execution_failed": True,
                        "entry_execution_reason": order_reason,
                        "entry_direction": direction,
                    },
                )
                if direction == "SHORT":
                    self._record_short_verdict_block(symbol, order_reason, "execution")
                continue
            if pos:
                pos["setup_id"] = candidate.get("setup_id", pos.get("setup_id"))
                pos["setup_mode"] = candidate.get("setup_mode", pos.get("setup_mode", "invalid"))
                pos["direction_constraint"] = candidate.get("direction_constraint", pos.get("direction_constraint", "none"))
                pos["timing_state"] = candidate.get("timing_state", pos.get("timing_state", "enter_now"))
                pos["best_play"] = candidate.get("best_play", pos.get("best_play", ""))
                pos["trigger"] = getattr(verdict, "trigger", "") or candidate.get("trigger", pos.get("trigger", ""))
                pos["trigger_spec"] = dict(candidate.get("trigger_spec", pos.get("trigger_spec", {})) or {})
                pos["invalidation"] = getattr(verdict, "invalidation", "") or candidate.get(
                    "invalidation", pos.get("invalidation", "")
                )
                pos["hold_style"] = getattr(verdict, "hold_style", "") or candidate.get(
                    "hold_style", pos.get("hold_style", "")
                )
                pos["size_posture"] = getattr(verdict, "size_posture", "") or candidate.get(
                    "size_posture", pos.get("size_posture", "normal")
                )
                pos["no_trade_reason"] = getattr(verdict, "no_trade_reason", "") or pos.get("no_trade_reason")
                pos["classifier_confidence"] = float(candidate.get("classifier_confidence", 0.0) or 0.0)
                pos["resolver_confidence"] = float(candidate.get("resolver_confidence", 0.0) or 0.0)
                pos["execution_confidence"] = float(
                    sentiment_data.get("execution_confidence", pos.get("execution_confidence", 0.0)) or 0.0
                )
                pos["feature_snapshot_id"] = candidate.get("feature_snapshot_id", pos.get("feature_snapshot_id"))
                pos["feature_quality_score"] = float(candidate.get("feature_quality_score", 0.0) or 0.0)
                pos["feature_quality"] = candidate.get("feature_quality", pos.get("feature_quality", ""))
                pos["missing_fields"] = list(candidate.get("missing_fields", pos.get("missing_fields", [])) or [])
                pos["material_change_signature"] = candidate.get(
                    "material_change_signature", pos.get("material_change_signature")
                )
                pos["symbol_state"] = "live_position"
                pos["jury_entry_now"] = bool(getattr(verdict, "entry_now", False))
                pos["jury_trigger"] = getattr(verdict, "trigger", "") or ""
                pos["jury_invalidation"] = getattr(verdict, "invalidation", "") or ""
                pos["jury_hold_style"] = getattr(verdict, "hold_style", "") or ""
                pos["jury_size_posture"] = getattr(verdict, "size_posture", "") or ""
                pos["jury_no_trade_reason"] = getattr(verdict, "no_trade_reason", "") or ""
                self._remove_waiting_setup(symbol, mode=pending_mode or current_mode)
                self._record_setup_snapshot(
                    {**candidate, "symbol_state": "live_position"},
                    "live_position",
                    verdict=verdict,
                    extra={
                        "execution_confidence": sentiment_data.get("execution_confidence"),
                        "order_state": dict(pos.get("order_state", {}) or {}),
                    },
                )
                log_activity(
                    "trade",
                    f"🎯 ENTERED {symbol}: mode={pos.get('setup_mode')} play={pos.get('best_play')} "
                    f"trigger={pos.get('trigger') or 'live'}",
                )
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
        trade_record["strategy_tag"] = normalize_strategy_tag(trade_record.get("strategy_tag", "unknown"))
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

        if not self._is_partial_exit_trade(trade_record):
            recent_trade = self._find_recent_realized_trade(
                symbol=symbol,
                exit_time=float(trade_record.get("exit_time", time.time()) or time.time()),
                window_seconds=30.0,
                asset_type=asset_type,
            )
            if recent_trade:
                recorded_keys.add(trade_key)
                if position:
                    position["exit_recorded"] = True
                    position["exit_finalized_at"] = float(
                        recent_trade.get("exit_time", trade_record.get("exit_time", time.time())) or time.time()
                    )
                logger.info(
                    f"🧾 Skipping duplicate realized exit for {symbol}: "
                    f"matched existing {recent_trade.get('reason', 'trade')} within 30s"
                )
                return

        pnl = float(trade_record.get("pnl", 0))
        reason = str(trade_record.get("reason", "") or "").lower()
        if symbol and reason.startswith("ratchet"):
            self._set_symbol_reentry_cooldown(symbol, 1800)
        elif symbol and pnl < 0:
            cooldown_seconds = int(getattr(settings, "SYMBOL_LOSS_COOLDOWN_SECONDS", 900) or 900)
            if cooldown_seconds > 0:
                self._set_symbol_reentry_cooldown(symbol, cooldown_seconds)
        elif symbol and pnl >= 0:
            self._set_symbol_reentry_cooldown(symbol, 1800)
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
                    "signal_tier",
                    "holding_horizon",
                    "market_regime",
                    "session_type",
                    "entry_reason_code",
                    "entry_model_votes",
                    "risk_constraints_applied",
                    "setup_id",
                    "setup_mode",
                    "direction_constraint",
                    "timing_state",
                    "best_play",
                    "trigger",
                    "trigger_spec",
                    "invalidation",
                    "hold_style",
                    "size_posture",
                    "no_trade_reason",
                    "classifier_confidence",
                    "resolver_confidence",
                    "execution_confidence",
                    "feature_snapshot_id",
                    "feature_quality_score",
                    "feature_quality",
                    "missing_fields",
                    "material_change_signature",
                    "symbol_state",
                    "jury_entry_now",
                    "jury_trigger",
                    "jury_invalidation",
                    "jury_hold_style",
                    "jury_size_posture",
                    "jury_no_trade_reason",
                    "ratchet_peak_pnl_pct",
                    "ratchet_floor_pct",
                    "ratchet_limit_order_id",
                    "hard_stop_pct",
                    "hard_stop_flags",
                    "hard_stop_order_id",
                    "allocator_status",
                    "allocator_recommended_action",
                    "allocator_control_state",
                    "order_state",
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
                peak_pct = float(
                    position.get(
                        "ratchet_peak_pnl_pct",
                        position.get("mfe_pct", trade_record.get("pnl_pct", 0)),
                    )
                    or 0
                )
                realized_pct = float(trade_record.get("pnl_pct", 0) or 0)
                trade_record["giveback_pct"] = ProfitRatchet.compute_giveback_pct(peak_pct, realized_pct)
                trade_record["dead_money_tightened"] = bool(position.get("dead_money_tightened"))
                trade_record["dead_money"] = bool(position.get("dead_money"))
                trade_record["hard_stop_pct"] = position.get("hard_stop_pct", trade_record.get("hard_stop_pct"))
                trade_record["hard_stop_flags"] = list(
                    position.get("hard_stop_flags", trade_record.get("hard_stop_flags", [])) or []
                )
                trade_record["allocator_status"] = position.get(
                    "allocator_status",
                    trade_record.get("allocator_status", "hold"),
                )
                trade_record["allocator_recommended_action"] = position.get(
                    "allocator_recommended_action",
                    trade_record.get("allocator_recommended_action", "hold"),
                )
                trade_record["allocator_control_state"] = position.get(
                    "allocator_control_state",
                    trade_record.get("allocator_control_state", "active"),
                )
                trade_record["entry_quality"] = position.get("entry_quality", trade_record.get("entry_quality", "neutral"))
                trade_record["extended_hours_entry"] = bool(
                    position.get("extended_hours_entry", trade_record.get("extended_hours_entry", False))
                )
                trade_record["overnight_context"] = str(
                    position.get("overnight_context", trade_record.get("overnight_context", "")) or ""
                )
                if realized_pct < 0 and not trade_record.get("loss_category"):
                    hard_stop_flags = set(trade_record.get("hard_stop_flags", []) or [])
                    if "stalled_loser" in hard_stop_flags:
                        trade_record["loss_category"] = "stalled_loser"
                    elif {"disabled_book", "probation_book"} & hard_stop_flags:
                        trade_record["loss_category"] = "book_probation"
                    elif trade_record.get("entry_quality") == "at_highs" or "at_highs_entry" in hard_stop_flags:
                        trade_record["loss_category"] = "bad_timing"
                    elif (
                        trade_record.get("extended_hours_entry")
                        and float(trade_record.get("hold_seconds", 0) or 0) < 300
                    ) or "extended_hours_entry" in hard_stop_flags:
                        trade_record["loss_category"] = "extended_hours_fakeout"
                    elif bool(position.get("dead_money_tightened")):
                        trade_record["loss_category"] = "dead_money"
                    elif peak_pct < 0.3:
                        trade_record["loss_category"] = "dead_money"
                    elif peak_pct > 0:
                        trade_record["loss_category"] = "partial_favorable"
                    else:
                        trade_record["loss_category"] = "wrong_signal"
                trade_record["profitable"] = realized_pct > 0
                trade_record["ratchet_activated"] = bool(
                    position.get("ratchet_floor_pct") is not None or trade_record.get("ratchet_floor_pct") is not None
                )
                trade_record["hard_stopped"] = str(trade_record.get("reason", "") or "").lower().startswith("hard_stop")
                trade_record["symbol_state"] = "cooldown"

        recorded_keys.add(trade_key)
        trade_history.record_trade(trade_record)
        if getattr(self, "latency_tracker", None):
            try:
                self.latency_tracker.record_from_trade(trade_record)
            except Exception as e:
                logger.debug(f"Latency tracker record failed for {symbol}: {e}")
        self._record_setup_snapshot(
            trade_record,
            "cooldown",
            extra={
                "pnl": trade_record.get("pnl"),
                "pnl_pct": trade_record.get("pnl_pct"),
                "reason": trade_record.get("reason"),
                "triggered": trade_record.get("triggered", True),
                "entered": trade_record.get("entered", True),
                "profitable": trade_record.get("profitable"),
                "hard_stopped": trade_record.get("hard_stopped"),
                "ratchet_activated": trade_record.get("ratchet_activated"),
            },
        )
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
                exit_time = float(trade_record.get("exit_time", time.time()) or time.time())
                entry_controls.set_cooldown(symbol, exit_confirmed_at=exit_time)
                anomaly_flags = trade_record.get("anomaly_flags", []) or []
                reason_str = str(trade_record.get("reason", "") or "").lower()
                if not self._is_partial_exit_trade(trade_record):
                    entry_controls.record_symbol_trade_result(
                        symbol,
                        pnl,
                        exit_confirmed_at=exit_time,
                        reason=reason_str,
                        setup_id=str(trade_record.get("setup_id", "") or ""),
                        anomaly_flags=anomaly_flags,
                        loss_limit=int(getattr(settings, "SYMBOL_CONSECUTIVE_LOSS_LIMIT", 2) or 2),
                        lock_seconds=float(
                            getattr(settings, "SYMBOL_CONSECUTIVE_LOSS_LOCK_SECONDS", 86400) or 86400
                        ),
                    )
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
        persistence.save_recently_removed_positions(
            getattr(self.entry_manager, "_recently_removed_positions", {}) if self.entry_manager else {}
        )
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
        previous_tighten = pos.get("ratchet_tighten_suggestion_pct")
        try:
            previous_tighten_val = float(previous_tighten) if previous_tighten is not None else None
        except Exception:
            previous_tighten_val = None
        pos["trail_pct"] = new_trail_pct
        pos["ratchet_tighten_suggestion_pct"] = (
            min(previous_tighten_val, new_trail_pct) if previous_tighten_val is not None else new_trail_pct
        )
        if not symbol or qty < 1 or pos.get("swing_only"):
            return True
        broker = getattr(self, "alpaca_client", None)
        entry_manager = getattr(self, "entry_manager", None)
        if not broker or not entry_manager:
            return True

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
        return True

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

            current_trail = max(0.5, float(pos.get("trail_pct", ProfitRatchet.RATCHET_TRAIL_PCT) or ProfitRatchet.RATCHET_TRAIL_PCT))
            tightened_trail = max(min_trail, min(current_trail, current_trail * tighten_mult))
            pos["copy_trader_exit_action"] = signal.get("copy_trader_exit_action", "trim")
            pos["copy_trader_exit_count"] = int(signal.get("copy_trader_exit_count", 0) or 0)
            pos["copy_trader_exit_handles"] = list(signal.get("copy_trader_exit_handles", []) or [])
            pos["copy_trader_exit_context"] = signal.get("copy_trader_exit_context", "")
            pos["copy_trader_exit_at"] = time.time()
            pos["ratchet_tighten_suggestion_pct"] = tightened_trail
            self.ai_layers["last_copy_trader_exit_signal"] = f"{symbol} {pos['copy_trader_exit_context']}"
            log_activity(
                "trade",
                f"📣 {symbol}: copy-trader {pos['copy_trader_exit_action']} signal"
                f" ({pos['copy_trader_exit_count']} handles) -> ratchet tighten suggestion {tightened_trail:.1f}%",
                {
                    "symbol": symbol,
                    "handles": pos["copy_trader_exit_handles"],
                    "suggested_trail_pct": tightened_trail,
                },
            )

            processed_ids.update(new_ids)

    @staticmethod
    def _position_exit_side(position: dict) -> str:
        return "buy" if position.get("side", "long") == "short" else "sell"

    @staticmethod
    def _order_is_hard_stop(order: dict) -> bool:
        client_order_id = str(order.get("client_order_id", "") or "").lower()
        order_type = str(order.get("type", "") or "").lower()
        if "ratchet" in client_order_id:
            return False
        return order_type == "stop" or "hardstop" in client_order_id or "hard-stop" in client_order_id

    @staticmethod
    def _order_is_ratchet(order: dict) -> bool:
        client_order_id = str(order.get("client_order_id", "") or "").lower()
        return "ratchet" in client_order_id

    @staticmethod
    def _order_stop_or_limit_price(order: dict) -> float:
        try:
            return float(order.get("stop_price") or order.get("limit_price") or 0)
        except Exception:
            return 0.0

    @classmethod
    def _order_is_probable_ratchet_for_position(cls, order: dict, position: dict) -> bool:
        if cls._order_is_ratchet(order):
            return True
        side = str(position.get("side", "long") or "long").lower()
        order_price = cls._order_stop_or_limit_price(order)
        if order_price <= 0:
            return False
        hard_stop_price = float(
            position.get("hard_stop_price")
            or ProfitRatchet.price_for_pnl(
                float(position.get("entry_price", 0) or 0),
                ProfitRatchet.HARD_STOP_PCT,
                side,
            )
            or 0
        )
        if hard_stop_price <= 0:
            return False
        if side == "short":
            return order_price < (hard_stop_price - 0.01)
        return order_price > (hard_stop_price + 0.01)

    def _infer_exit_reason_from_order(self, position: dict, order: dict) -> str:
        order_id = str(order.get("id", "") or "")
        if order_id and order_id == str(position.get("hard_stop_order_id", "") or ""):
            return "hard_stop"
        if order_id and order_id == str(position.get("ratchet_limit_order_id", "") or ""):
            return "ratchet_exit"
        if self._order_is_hard_stop(order):
            return "hard_stop"
        if self._order_is_ratchet(order):
            return "ratchet_exit"
        return str(position.get("last_exit_reason", "") or "broker_exit_fill")

    async def _cancel_order_and_confirm(self, order_id: str) -> bool:
        if not order_id or not self.alpaca_client:
            return True
        cancelled = await asyncio.get_event_loop().run_in_executor(
            None, self.alpaca_client.cancel_order, order_id
        )
        if not cancelled:
            return False
        timeout_seconds = float(getattr(settings, "PROFIT_RATCHET_ORDER_CONFIRM_SECONDS", 3.0) or 3.0)
        return await asyncio.get_event_loop().run_in_executor(
            None, self.alpaca_client.wait_for_order_terminal, order_id, timeout_seconds
        )

    async def _cleanup_orphaned_protection_orders(self, open_orders: List[Dict]):
        if not self.entry_manager or not self.alpaca_client:
            return
        held_symbols = {str(symbol).upper() for symbol in self.entry_manager.positions.keys()}
        for order in open_orders:
            symbol = str(order.get("symbol", "") or "").upper()
            if not symbol:
                continue
            if not (self._order_is_hard_stop(order) or self._order_is_ratchet(order)):
                continue
            if symbol in held_symbols:
                continue
            order_id = str(order.get("id", "") or "")
            if not order_id:
                continue
            logger.warning(f"🧹 Canceling orphaned protection order for {symbol}: {order_id}")
            await self._cancel_order_and_confirm(order_id)

    async def _ensure_hard_stop(self, position: dict, open_orders_by_symbol: Dict[str, List[Dict]], current_price: float):
        symbol = position.get("symbol", "?")
        if not self.alpaca_client:
            position.setdefault("order_state", {})["hard_stop"] = "software_managed"
            return

        is_regular = self._entry_session_label() == "regular"
        if not is_regular:
            position.setdefault("order_state", {})["session_protection"] = "software_managed"

        qty = float(position.get("quantity", 0) or 0)
        if qty < 1:
            # Alpaca does not reliably accept broker-side stop orders for fractional share leftovers.
            # Keep protection software-managed instead of spamming futile 1-share stop attempts.
            position.setdefault("order_state", {})["hard_stop"] = "software_managed"
            return
        qty = int(qty)

        exit_side = self._position_exit_side(position)
        hard_stop_id = str(position.get("hard_stop_order_id", "") or "")
        ratchet_id = str(position.get("ratchet_limit_order_id", "") or "")
        existing_orders = []
        for order in (open_orders_by_symbol.get(symbol, []) or []):
            if str(order.get("side", "") or "").lower() != exit_side:
                continue
            order_id = str(order.get("id", "") or "")
            if ratchet_id and order_id == ratchet_id:
                continue
            if hard_stop_id and order_id == hard_stop_id:
                existing_orders.append(order)
                continue
            if self._order_is_hard_stop(order) and not self._order_is_probable_ratchet_for_position(order, position):
                existing_orders.append(order)
        side = position.get("side", "long")
        stop_price = float(
            position.get("hard_stop_price")
            or ProfitRatchet.price_for_pnl(
                float(position.get("entry_price", current_price) or current_price),
                ProfitRatchet.HARD_STOP_PCT,
                side,
            )
            or 0
        )
        if not stop_price:
            logger.warning(f"⚠️ {symbol}: hard stop skip — could not compute stop_price (entry={position.get('entry_price')})")
            return
        active_order = existing_orders[0] if existing_orders else None
        if active_order:
            try:
                current_stop_price = float(active_order.get("stop_price", 0) or 0)
            except Exception:
                current_stop_price = 0.0
            if abs(current_stop_price - stop_price) < 0.01:
                position["hard_stop_order_id"] = active_order.get("id", position.get("hard_stop_order_id"))
                position.setdefault("order_state", {})["hard_stop"] = "placed"
                return
            if active_order.get("id"):
                cancelled = await self._cancel_order_and_confirm(str(active_order.get("id") or ""))
                if not cancelled:
                    logger.warning(f"⚠️ Could not replace hard stop for {symbol}")
                    position.setdefault("order_state", {})["hard_stop"] = "replace_failed"
                    return
        client_order_id = ProfitRatchet.make_client_order_id(symbol, "hard-stop", stop_price)
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            partial(
                self.alpaca_client.place_stop_loss_order,
                symbol,
                qty,
                stop_price,
                side,
                client_order_id,
            ),
        )
        if order:
            position["hard_stop_price"] = stop_price
            position["hard_stop_order_id"] = order.get("id", "")
            position.setdefault("order_state", {})["hard_stop"] = "placed"
            logger.info(f"🛡️ Hard stop placed for {symbol} @ ${stop_price:.2f}")
        else:
            broker_error = {}
            if hasattr(self.alpaca_client, "pop_order_error"):
                broker_error = self.alpaca_client.pop_order_error(client_order_id) or {}
            crossed_market_price = self._ratchet_rejection_market_price(position, broker_error)
            if crossed_market_price > 0 and not position.get("exit_pending"):
                position["hard_stop_order_id"] = ""
                position.setdefault("order_state", {})["hard_stop"] = "software_exit"
                logger.warning(
                    f"⚠️ Hard stop rejected as already crossed for {symbol}: "
                    f"market=${crossed_market_price:.2f} target=${stop_price:.2f} — submitting software-managed exit"
                )
                await self._submit_software_managed_exit(position, crossed_market_price, "hard_stop")
                return
            position.setdefault("order_state", {})["hard_stop"] = "missing"
            logger.warning(f"⚠️ Failed to place hard stop for {symbol}")

    async def _place_or_replace_ratchet_order(
        self,
        position: dict,
        target_price: float,
        open_orders_by_symbol: Dict[str, List[Dict]],
    ) -> bool:
        session = self._entry_session_label()
        if not self.alpaca_client or session not in ("regular", "pre", "after"):
            return False
        is_extended = session in ("pre", "after")

        symbol = position.get("symbol", "")
        qty = int(float(position.get("quantity", 0) or 0))
        if qty < 1 or target_price <= 0:
            return False

        exit_side = self._position_exit_side(position)
        current_id = str(position.get("ratchet_limit_order_id", "") or "")
        ratchet_orders = [
            order for order in (open_orders_by_symbol.get(symbol, []) or [])
            if str(order.get("side", "") or "").lower() == exit_side
            and (
                self._order_is_probable_ratchet_for_position(order, position)
                or (current_id and str(order.get("id", "") or "") == current_id)
            )
        ]
        active_order = None
        for order in ratchet_orders:
            if current_id and str(order.get("id", "") or "") == current_id:
                active_order = order
                break
        if not active_order and ratchet_orders:
            active_order = ratchet_orders[0]

        current_stop = 0.0
        if active_order:
            try:
                current_stop = float(active_order.get("stop_price") or active_order.get("limit_price") or 0)
            except Exception:
                current_stop = 0.0
        if active_order and abs(current_stop - target_price) < 0.01:
            position["ratchet_limit_order_id"] = active_order.get("id", position.get("ratchet_limit_order_id"))
            position["ratchet_order_type"] = str(active_order.get("type", "stop") or "stop")
            position.setdefault("order_state", {})["ratchet"] = "placed"
            position["ratchet_last_target_price"] = target_price
            position["ratchet_last_place_attempt_at"] = time.time()
            return True

        cooldown_seconds = float(getattr(settings, "PROFIT_RATCHET_REPLACE_COOLDOWN_SECONDS", 20.0) or 0.0)
        last_target = float(position.get("ratchet_last_target_price", 0) or 0)
        last_attempt = float(position.get("ratchet_last_place_attempt_at", 0) or 0)
        if (
            not active_order
            and current_id
            and cooldown_seconds > 0
            and abs(last_target - target_price) < 0.01
            and (time.time() - last_attempt) < cooldown_seconds
        ):
            position.setdefault("order_state", {})["ratchet"] = "cooldown_skip"
            logger.debug(
                f"⏸️ Ratchet replace cooldown for {symbol}: "
                f"target=${target_price:.2f} age={time.time() - last_attempt:.1f}s"
            )
            return True

        if active_order and active_order.get("id"):
            cancelled = await self._cancel_order_and_confirm(str(active_order.get("id") or ""))
            if not cancelled:
                logger.warning(f"⚠️ Could not cancel prior ratchet order for {symbol}")
                return False

        client_order_id = ProfitRatchet.make_client_order_id(symbol, "ratchet", target_price)
        side = position.get("side", "long")
        exit_order_side = "buy" if side == "short" else "sell"
        order_type_label = "LIMIT" if is_extended else "STOP"
        logger.info(
            f"📈 Placing ratchet {order_type_label} {exit_order_side} for {symbol}: {qty}sh @ ${target_price:.2f} "
            f"(side={side}, floor={position.get('ratchet_floor_pct')}%, extended={is_extended})"
        )

        if is_extended:
            # Extended hours: use limit orders (Alpaca doesn't support stops in extended hours)
            if side == "short":
                order = await asyncio.get_event_loop().run_in_executor(
                    None,
                    partial(
                        self.alpaca_client.place_limit_cover,
                        symbol, qty, target_price,
                        True, client_order_id, True,
                    ),
                )
            else:
                order = await asyncio.get_event_loop().run_in_executor(
                    None,
                    partial(
                        self.alpaca_client.place_limit_sell,
                        symbol, qty, target_price,
                        True, client_order_id, True,
                    ),
                )
        else:
            order = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(
                    self.alpaca_client.place_stop_order,
                    symbol, qty, target_price,
                    exit_order_side, client_order_id, True,
                ),
            )

        if not order:
            broker_error = {}
            if hasattr(self.alpaca_client, "pop_order_error"):
                broker_error = self.alpaca_client.pop_order_error(client_order_id) or {}
            crossed_market_price = 0.0
            if not is_extended:
                crossed_market_price = self._ratchet_rejection_market_price(position, broker_error)
            if crossed_market_price > 0 and not position.get("exit_pending"):
                position["ratchet_limit_order_id"] = ""
                position["ratchet_order_type"] = "software_exit"
                position.setdefault("order_state", {})["ratchet"] = "software_exit"
                logger.warning(
                    f"⚠️ Ratchet {order_type_label} rejected as already crossed for {symbol}: "
                    f"market=${crossed_market_price:.2f} target=${target_price:.2f} — submitting software-managed exit"
                )
                await self._submit_software_managed_exit(position, crossed_market_price, "ratchet_exit")
                return True
            position.setdefault("order_state", {})["ratchet"] = "missing"
            logger.warning(f"⚠️ Ratchet {order_type_label} order failed for {symbol} @ ${target_price:.2f}")
            return False

        position["ratchet_limit_order_id"] = order.get("id", "")
        position["ratchet_order_type"] = "limit" if is_extended else str(order.get("type", "stop") or "stop")
        position["ratchet_last_target_price"] = target_price
        position["ratchet_last_place_attempt_at"] = time.time()
        position.setdefault("order_state", {})["ratchet"] = "placed"
        logger.info(f"📈 Ratchet {order_type_label} placed for {symbol} @ ${target_price:.2f} (order={order.get('id', '?')[:12]})")
        return True

    async def _cancel_existing_ratchet_orders(
        self,
        position: dict,
        open_orders_by_symbol: Dict[str, List[Dict]],
    ) -> int:
        symbol = str(position.get("symbol", "") or "")
        if not symbol:
            return 0
        exit_side = self._position_exit_side(position)
        cancelled = 0
        current_id = str(position.get("ratchet_limit_order_id", "") or "")
        for order in (open_orders_by_symbol.get(symbol, []) or []):
            if str(order.get("side", "") or "").lower() != exit_side:
                continue
            order_id = str(order.get("id", "") or "")
            if (
                not self._order_is_probable_ratchet_for_position(order, position)
                and not (current_id and order_id == current_id)
            ):
                continue
            if not order_id:
                continue
            if await self._cancel_order_and_confirm(order_id):
                cancelled += 1
        if cancelled:
            position["ratchet_limit_order_id"] = ""
            position.setdefault("order_state", {})["ratchet"] = "canceled"
        return cancelled

    async def _cancel_existing_hard_stop_orders(
        self,
        position: dict,
        open_orders_by_symbol: Dict[str, List[Dict]],
    ) -> int:
        symbol = str(position.get("symbol", "") or "")
        if not symbol:
            return 0
        exit_side = self._position_exit_side(position)
        hard_stop_id = str(position.get("hard_stop_order_id", "") or "")
        cancelled = 0
        for order in (open_orders_by_symbol.get(symbol, []) or []):
            if str(order.get("side", "") or "").lower() != exit_side:
                continue
            order_id = str(order.get("id", "") or "")
            if not order_id:
                continue
            if hard_stop_id and order_id == hard_stop_id:
                pass
            elif not self._order_is_hard_stop(order):
                continue
            elif self._order_is_probable_ratchet_for_position(order, position):
                continue
            if await self._cancel_order_and_confirm(order_id):
                cancelled += 1
        if cancelled:
            position["hard_stop_order_id"] = ""
            position.setdefault("order_state", {})["hard_stop"] = "canceled"
        return cancelled

    @staticmethod
    def _exit_target_crossed(position: dict, current_price: float, target_price: float) -> bool:
        if current_price <= 0 or target_price <= 0:
            return False
        side = str(position.get("side", "long") or "long").lower()
        if side == "short":
            return current_price >= target_price
        return current_price <= target_price

    @staticmethod
    def _ratchet_rejection_market_price(position: dict, broker_error: Dict) -> float:
        if not broker_error:
            return 0.0
        if int(broker_error.get("status_code", 0) or 0) != 422:
            return 0.0
        body = str(broker_error.get("body", "") or "")
        if not body:
            return 0.0

        side = str(position.get("side", "long") or "long").lower()
        body_lower = body.lower()
        if side == "short":
            relation_phrases = (
                "stop price must be greater than current price",
                "stop price must be greater than base price",
            )
        else:
            relation_phrases = (
                "stop price must be less than current price",
                "stop price must be less than base price",
            )
        if not any(phrase in body_lower for phrase in relation_phrases):
            return 0.0

        try:
            payload = json.loads(body)
            return float(payload.get("market_price") or 0.0)
        except Exception:
            return 0.0

    async def _submit_software_managed_exit(self, position: dict, current_price: float, reason: str) -> bool:
        if not self.alpaca_client:
            return False

        symbol = position.get("symbol", "")
        qty = int(float(position.get("quantity", 0) or 0))
        if qty < 1 or current_price <= 0:
            return False

        side = position.get("side", "long")
        extended = self._entry_session_label() in {"pre", "after"}
        buffer_bps = max(
            1.0,
            float(getattr(settings, "EXTENDED_HOURS_EXIT_LIMIT_BUFFER_BPS", 20.0) or 20.0),
        )
        buffer_pct = buffer_bps / 10000.0
        if side == "short":
            limit_price = round(current_price * (1.0 + buffer_pct), 2)
            order = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(
                    self.alpaca_client.place_limit_cover,
                    symbol,
                    qty,
                    limit_price,
                    extended,
                    ProfitRatchet.make_client_order_id(symbol, reason, limit_price),
                    True,
                ),
            )
        else:
            limit_price = round(current_price * (1.0 - buffer_pct), 2)
            order = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(
                    self.alpaca_client.place_limit_sell,
                    symbol,
                    qty,
                    limit_price,
                    extended,
                    ProfitRatchet.make_client_order_id(symbol, reason, limit_price),
                    True,
                ),
            )
        if not order:
            return False

        position["exit_pending"] = True
        position["exit_order_id"] = order.get("id", "")
        position["exit_submitted_at"] = time.time()
        position["exit_fill_qty"] = 0.0
        position["pending_exit_qty"] = qty
        position["remaining_qty"] = 0.0
        position["exit_scope"] = "full"
        position["exit_recorded"] = False
        position["last_exit_reason"] = reason
        position["last_exit_attempt_at"] = time.time()
        position.setdefault("order_state", {})["exit"] = "submitted"
        logger.warning(f"🔴 Software-managed exit submitted for {symbol}: {reason} @ ${limit_price:.2f}")
        return True

    async def _reprice_stale_extended_exit_pending(
        self,
        position: dict,
        current_price: float,
        open_orders_by_symbol: Dict[str, List[Dict]],
        now_ts: Optional[float] = None,
    ) -> bool:
        if not self.alpaca_client or not self.entry_manager:
            return False
        if not bool(position.get("exit_pending")):
            return False
        if self._entry_session_label() not in {"pre", "after"}:
            return False

        now_ts = float(now_ts or time.time())
        submitted_at = float(position.get("exit_submitted_at", 0) or 0)
        reprice_after = max(
            1.0,
            float(getattr(settings, "EXTENDED_HOURS_EXIT_REPRICE_AFTER_SECONDS", 20.0) or 20.0),
        )
        if submitted_at <= 0 or (now_ts - submitted_at) < reprice_after:
            return False

        max_attempts = max(
            0,
            int(getattr(settings, "EXTENDED_HOURS_EXIT_REPRICE_MAX_ATTEMPTS", 3) or 3),
        )
        attempt = int(position.get("extended_exit_reprice_count", 0) or 0)
        if attempt >= max_attempts:
            return False

        symbol = str(position.get("symbol", "") or "").upper()
        pending_order_id = str(position.get("exit_order_id", "") or "")
        if not symbol or not pending_order_id:
            return False

        pending_order = None
        for order in (open_orders_by_symbol.get(symbol, []) or []):
            if str(order.get("id", "") or "") == pending_order_id:
                pending_order = order
                break
        if not pending_order:
            return False

        qty = float(position.get("pending_exit_qty", position.get("quantity", 0)) or 0)
        if qty <= 0 or current_price <= 0:
            return False

        try:
            prev_limit = float(pending_order.get("limit_price", 0) or 0)
        except Exception:
            prev_limit = 0.0

        step_bps = max(
            1.0,
            float(getattr(settings, "EXTENDED_HOURS_EXIT_REPRICE_STEP_BPS", 30.0) or 30.0),
        )
        step_pct = step_bps / 10000.0
        side = str(position.get("side", "long") or "long").lower()

        try:
            await asyncio.get_event_loop().run_in_executor(None, self.alpaca_client.cancel_order, pending_order_id)
        except Exception as e:
            logger.warning(f"{symbol}: stale exit reprice cancel failed for {pending_order_id}: {e}")
            return False

        await asyncio.sleep(0.25)

        if side == "short":
            anchor = max(current_price, prev_limit if prev_limit > 0 else current_price)
            new_limit = round(anchor * (1.0 + step_pct), 2 if anchor >= 1.0 else 4)
            order = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(
                    self.alpaca_client.place_limit_cover,
                    symbol,
                    qty,
                    new_limit,
                    True,
                    ProfitRatchet.make_client_order_id(symbol, "repriced_exit", new_limit),
                    False,
                ),
            )
        else:
            anchor = min(current_price, prev_limit if prev_limit > 0 else current_price)
            new_limit = round(anchor * (1.0 - step_pct), 2 if anchor >= 1.0 else 4)
            order = await asyncio.get_event_loop().run_in_executor(
                None,
                partial(
                    self.alpaca_client.place_limit_sell,
                    symbol,
                    qty,
                    new_limit,
                    True,
                    ProfitRatchet.make_client_order_id(symbol, "repriced_exit", new_limit),
                    False,
                ),
            )

        if not order:
            logger.warning(f"{symbol}: stale extended exit reprice submit failed")
            return False

        position["exit_order_id"] = order.get("id", pending_order_id)
        position["exit_submitted_at"] = now_ts
        position["last_exit_attempt_at"] = now_ts
        position["extended_exit_reprice_count"] = attempt + 1
        logger.warning(
            f"🔁 Repriced stale extended exit for {symbol}: "
            f"${prev_limit or current_price:.2f} -> ${new_limit:.2f} "
            f"(attempt {attempt + 1}/{max_attempts})"
        )
        return True

    async def _submit_deferred_exit_on_open(self, position: dict, current_price: float) -> bool:
        if not self.entry_manager:
            return False
        if not bool(position.get("deferred_exit_on_open")):
            return False
        if bool(position.get("exit_pending")):
            return False
        if not self.entry_manager.is_market_open():
            return False

        reason = str(position.get("deferred_exit_reason", "deferred_exit_on_open") or "deferred_exit_on_open")
        submitted = await self._submit_software_managed_exit(position, current_price, reason)
        if not submitted:
            return False

        symbol = str(position.get("symbol", "") or "").upper()
        position["deferred_exit_submitted_at"] = time.time()
        position.pop("deferred_exit_on_open", None)
        logger.warning(f"🌅 Submitted deferred next-session exit for {symbol}: {reason}")
        return True

    def _sync_pending_exit_from_open_orders(
        self,
        position: dict,
        open_orders_by_symbol: Dict[str, List[Dict]],
    ) -> bool:
        symbol = str(position.get("symbol", "") or "").upper()
        if not symbol:
            return False

        exit_side = self._position_exit_side(position)
        pending_order = None
        for order in (open_orders_by_symbol.get(symbol, []) or []):
            if str(order.get("side", "") or "").lower() != exit_side:
                continue
            if self._order_is_hard_stop(order) or self._order_is_ratchet(order):
                continue
            order_type = str(order.get("type", "") or "").lower()
            if order_type not in {"limit", "market"}:
                continue
            pending_order = order
            break

        if not pending_order:
            return False

        order_id = str(pending_order.get("id", "") or "")
        position["exit_pending"] = True
        if order_id:
            position["exit_order_id"] = order_id
        try:
            submitted_ts = self._parse_iso_ts(
                pending_order.get("submitted_at") or pending_order.get("created_at") or pending_order.get("updated_at")
            )
        except Exception:
            submitted_ts = None
        if submitted_ts:
            position["exit_submitted_at"] = submitted_ts
        try:
            pending_qty = float(
                pending_order.get("qty")
                or pending_order.get("remaining_qty")
                or position.get("quantity", 0)
                or 0
            )
        except Exception:
            pending_qty = float(position.get("quantity", 0) or 0)
        if pending_qty > 0:
            position["pending_exit_qty"] = pending_qty
        position["exit_scope"] = "full"
        position.setdefault("order_state", {})["exit"] = "open"
        return True

    async def _apply_profit_ratchet_action(
        self,
        position: dict,
        current_price: float,
        action: dict,
        open_orders_by_symbol: Dict[str, List[Dict]],
    ):
        prior_hard_stop_pct = float(
            position.get("hard_stop_pct", ProfitRatchet.HARD_STOP_PCT) or ProfitRatchet.HARD_STOP_PCT
        )
        position["ratchet_peak_pnl_pct"] = max(
            float(position.get("ratchet_peak_pnl_pct", 0.0) or 0.0),
            float(action.get("peak_pnl_pct", 0.0) or 0.0),
        )
        position["ratchet_floor_pct"] = action.get("floor_pct")
        position["hard_stop_price"] = action.get("hard_stop_price") or position.get("hard_stop_price")
        position["hard_stop_pct"] = action.get("hard_stop_pct", position.get("hard_stop_pct"))
        position["hard_stop_flags"] = list(action.get("hard_stop_flags", []) or [])
        position["dead_money"] = bool(action.get("dead_money"))
        position.setdefault("order_state", {})

        session = self._entry_session_label()
        regular_session = session == "regular"
        extended_session = session in {"pre", "after"}

        sym = position.get("symbol", "?")
        if regular_session:
            ratchet_active = action.get("ratchet_active", False)
            target_exit_price = float(action.get("target_exit_price", 0) or 0)
            peak_pnl = float(action.get("peak_pnl_pct", 0) or 0)
            cur_pnl = float(action.get("current_pnl_pct", 0) or 0)
            floor_pct = action.get("floor_pct")

            if ratchet_active and target_exit_price > 0:
                cancelled_hard_stops = await self._cancel_existing_hard_stop_orders(position, open_orders_by_symbol)
                if cancelled_hard_stops:
                    logger.info(f"🧹 {sym}: canceled {cancelled_hard_stops} superseded hard stop(s); ratchet now owns protection")
                position.setdefault("order_state", {})["hard_stop"] = "superseded_by_ratchet"
            else:
                await self._ensure_hard_stop(position, open_orders_by_symbol, current_price)

            if action.get("dead_money") and not position.get("dead_money_tightened"):
                position["dead_money_tightened"] = True
                logger.warning(
                    f"💀 Dead money: {sym} tightening stop "
                    f"{prior_hard_stop_pct:.1f}% → {float(action.get('hard_stop_pct', ProfitRatchet.DEAD_MONEY_TIGHT_STOP_PCT)):.1f}%"
                )
            if action.get("action") == "hard_stop" and not position.get("exit_pending"):
                hard_stop_orders = [
                    order for order in (open_orders_by_symbol.get(position.get("symbol", ""), []) or [])
                    if str(order.get("side", "") or "").lower() == self._position_exit_side(position)
                    and self._order_is_hard_stop(order)
                    and not self._order_is_probable_ratchet_for_position(order, position)
                ]
                if not hard_stop_orders:
                    await self._submit_software_managed_exit(position, current_price, "hard_stop")
            if ratchet_active and target_exit_price > 0:
                if self._exit_target_crossed(position, current_price, target_exit_price):
                    await self._cancel_existing_ratchet_orders(position, open_orders_by_symbol)
                    logger.warning(
                        f"⚠️ Ratchet target already crossed for {sym}: "
                        f"current=${current_price:.2f} target=${target_exit_price:.2f} "
                        f"action={action.get('action')} — submitting software-managed exit"
                    )
                    if action.get("action") in {"hard_stop", "ratchet_exit"} and not position.get("exit_pending"):
                        await self._submit_software_managed_exit(
                            position,
                            current_price,
                            str(action.get("action") or "ratchet_exit"),
                        )
                else:
                    placed = await self._place_or_replace_ratchet_order(position, target_exit_price, open_orders_by_symbol)
                    if not placed and action.get("action") in {"hard_stop", "ratchet_exit"} and not position.get("exit_pending"):
                        logger.warning(
                            f"⚠️ Ratchet protection placement failed for {sym} with live exit condition "
                            f"(current=${current_price:.2f} target=${target_exit_price:.2f}) — submitting software-managed exit"
                        )
                        await self._submit_software_managed_exit(
                            position,
                            current_price,
                            str(action.get("action") or "ratchet_exit"),
                        )
            elif not ratchet_active:
                logger.debug(
                    f"🔧 {sym} ratchet inactive: peak_pnl={peak_pnl:.2f}% cur_pnl={cur_pnl:.2f}% "
                    f"activation={ProfitRatchet.RATCHET_ACTIVATION_PCT}% peak_price={position.get('peak_price')}"
                )

            return

        if extended_session:
            position["order_state"]["hard_stop"] = "software_managed"
            if action.get("dead_money"):
                position["dead_money_tightened"] = True
            # Place ratchet limit orders in extended hours (Alpaca supports limit orders with extended_hours flag)
            ratchet_active = action.get("ratchet_active", False)
            target_exit_price = float(action.get("target_exit_price", 0) or 0)
            if ratchet_active and target_exit_price > 0:
                await self._place_or_replace_ratchet_order(position, target_exit_price, open_orders_by_symbol)
            if action.get("action") in {"hard_stop", "ratchet_exit"} and not position.get("exit_pending"):
                await self._submit_software_managed_exit(position, current_price, str(action.get("action") or "exit"))

    async def _monitor_positions_loop(self):
        """Independent loop for position monitoring — decoupled from scan pipeline."""
        while True:
            try:
                await self._monitor_positions()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error(f"Monitor loop error: {e}")
            await asyncio.sleep(10)

    async def _submit_dust_cleanup_exit(self, position: dict, reason: str = "dust_cleanup") -> bool:
        """Close dust/fractional carryover without dropping local state before broker confirmation."""
        if not position or not self.alpaca_client or not self.entry_manager:
            return False

        symbol = str(position.get("symbol", "") or "").upper().strip()
        qty = float(position.get("quantity", 0) or 0)
        if not symbol or qty <= 0:
            return False

        position["_dust_close_attempted"] = True
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: self.alpaca_client.close_position(symbol, qty=qty),
            )
        except Exception as e:
            logger.debug(f"Cleanup failed for {symbol}: {e}")
            position.pop("_dust_close_attempted", None)
            return False

        if not result:
            position.pop("_dust_close_attempted", None)
            return False

        status = str(result.get("status", "") or "").lower()
        if status == "not_found":
            self.entry_manager.remove_position(symbol)
            logger.info(f"🧹 Cleanup remove {symbol}: broker already flat")
            return True

        position["exit_pending"] = True
        position["exit_order_id"] = str(result.get("id", "") or "")
        position["exit_submitted_at"] = time.time()
        position["last_exit_reason"] = reason
        logger.info(
            f"🧹 Cleanup exit submitted for {symbol}: order={position['exit_order_id'] or 'broker_close'} qty={qty:.6f}"
        )
        return True

    async def _monitor_positions(self):
        """Monitor open positions with deterministic hard-stop and profit-ratchet control."""
        positions = self.entry_manager.get_positions()
        if not positions:
            return

        # Dust cleanup: close micro positions that are dead capital (uses dust_policy rules)
        if self.entry_manager.is_market_open():
            for pos in list(positions):
                if pos.get("exit_pending") or pos.get("_dust_close_attempted"):
                    continue
                if should_auto_liquidate(pos):
                    sym = pos.get("symbol", "")
                    qty = float(pos.get("quantity", 0) or 0)
                    price = float(pos.get("current_price", 0) or pos.get("entry_price", 0) or 0)
                    notional = qty * price
                    logger.warning(f"🧹 DUST CLEANUP {sym}: {qty:.6f} shares (${notional:.2f} notional) — auto-liquidating via dust_policy")
                    await self._submit_dust_cleanup_exit(pos, "dust")

        try:
            alpaca_positions = await asyncio.get_event_loop().run_in_executor(
                None, self.alpaca_client.get_positions
            )
            logger.debug(f"🔄 Monitor: synced {len(alpaca_positions)} broker positions, {len(positions)} local")
            self._cache_broker_position_symbols(alpaca_positions)
            if self.entry_manager and hasattr(self.entry_manager, "sync_positions_from_brokerage"):
                self.entry_manager.sync_positions_from_brokerage(alpaca_positions)
                positions = self.entry_manager.get_positions()
            alpaca_symbols = {p["symbol"] for p in alpaca_positions}
            alpaca_position_map = {p["symbol"]: p for p in alpaca_positions}
        except Exception as e:
            logger.warning(f"⚠️ Alpaca position sync error (monitor skipped): {e}")
            return

        open_orders = []
        open_orders_by_symbol: Dict[str, List[Dict]] = {}
        pending_entry_order_keys = set()
        try:
            open_orders = await asyncio.get_event_loop().run_in_executor(
                None, self.alpaca_client.get_orders
            )
            pending_entry_order_keys = {
                (o.get("symbol", ""), o.get("side", ""))
                for o in open_orders
                if o.get("status") in ("new", "accepted", "pending_new", "partially_filled")
                and not self._order_is_hard_stop(o)
                and not self._order_is_ratchet(o)
            }
            for order in open_orders:
                open_orders_by_symbol.setdefault(str(order.get("symbol", "") or ""), []).append(order)
            await self._cleanup_orphaned_protection_orders(open_orders)
        except Exception:
            pending_entry_order_keys = set()

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
            symbol = pos.get("symbol", "")
            if symbol in alpaca_symbols:
                entry_state = (pos.get("order_state") or {}).get("entry", "")
                if entry_state in ("open", "pending_new", "new", "accepted", ""):
                    pos.setdefault("order_state", {})["entry"] = "filled"
                    if not pos.get("fill_timestamp"):
                        import time as _time
                        pos["fill_timestamp"] = pos.get("entry_time") or _time.time()
                        pos["fill_timestamp_source"] = "broker_sync_forced"
            if pos.get("fill_timestamp"):
                continue
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
                        logger.debug(f"{symbol}: exit already being/been recorded — skipping duplicate")
                        continue
                    pos["_exit_recording"] = True
                    try:
                        exit_price = float(latest_fill_price or pos.get("entry_price", 0) or 0)
                        reason = self._infer_exit_reason_from_order(pos, latest)
                        if self._find_recent_realized_trade(
                            symbol=symbol,
                            exit_time=float(latest_fill_ts or time.time()),
                            window_seconds=60.0,
                            asset_type="equity",
                            reason_prefixes=["ratchet", "hard_stop"],
                        ):
                            logger.info(
                                f"🧾 {symbol}: skipping duplicate broker reconciliation exit "
                                "because a ratchet/hard-stop trade was already recorded"
                            )
                            pos["_exit_recorded"] = True
                            continue
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
                        if reason.startswith("ratchet"):
                            await self._close_paired_options(symbol, reason="underlying_ratchet_exit")
                        elif reason.startswith("hard_stop"):
                            await self._close_paired_options(symbol, reason="underlying_hard_stop")
                    finally:
                        pos.pop("_exit_recording", None)
                    continue
                else:
                    pos.pop("_missing_broker_warned", None)

                current_price = float(pos.get("current_price", 0) or 0)
                if current_price <= 0 and self.polygon_client:
                    try:
                        current_price = await asyncio.get_event_loop().run_in_executor(
                            None, self.polygon_client.get_price, symbol
                        )
                    except Exception:
                        current_price = float(pos.get("entry_price", 0) or 0)
                if current_price <= 0:
                    continue

                if await self._submit_deferred_exit_on_open(pos, current_price):
                    continue
                self._sync_pending_exit_from_open_orders(pos, open_orders_by_symbol)
                if pos.get("exit_pending"):
                    if await self._reprice_stale_extended_exit_pending(
                        pos,
                        current_price,
                        open_orders_by_symbol,
                        now_ts=time.time(),
                    ):
                        continue
                    continue

                self.entry_manager.update_peak_price(symbol, current_price)
                if await self._run_close_carry_review(
                    pos,
                    current_price,
                    open_orders_by_symbol,
                    now_ts=time.time(),
                ):
                    continue
                self._apply_close_profit_lock(pos, current_price, now_ts=time.time())
                action = self.profit_ratchet.check_position(pos, current_price, now=time.time())
                ratchet_status = (
                    f"🔄 {symbol}: price=${current_price:.2f} peak=${pos.get('peak_price', '?')} "
                    f"pnl={action.get('current_pnl_pct', 0):.2f}% peak_pnl={action.get('peak_pnl_pct', 0):.2f}% "
                    f"ratchet={'ON' if action.get('ratchet_active') else 'off'} action={action.get('action')} "
                    f"floor={action.get('floor_pct')} target=${action.get('target_exit_price') or 0:.2f}"
                )
                _last_ratchet_log = getattr(self, "_last_ratchet_log_ts", {})
                if time.time() - _last_ratchet_log.get(symbol, 0) > 60:
                    logger.info(ratchet_status)
                    _last_ratchet_log[symbol] = time.time()
                    self._last_ratchet_log_ts = _last_ratchet_log
                await self._apply_profit_ratchet_action(pos, current_price, action, open_orders_by_symbol)

            except Exception as e:
                logger.error(f"Monitor error for {symbol}: {e}")

    @staticmethod
    def _parse_event_date(value) -> Optional[datetime.date]:
        if value is None:
            return None
        text = str(value).strip()
        if not text:
            return None
        for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
            try:
                return datetime.strptime(text[:10], fmt).date()
            except Exception:
                continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
        except Exception:
            return None

    async def _get_close_review_earnings_event(self, symbol: str, now_ts: float) -> Optional[Dict]:
        if not symbol or not getattr(self, "earnings_scanner", None):
            return None
        cache = getattr(self, "_close_review_earnings_cache", {})
        cache_key = f"{datetime.fromtimestamp(now_ts, EASTERN).date().isoformat()}:{symbol.upper()}"
        if cache_key in cache:
            return cache.get(cache_key)
        try:
            event = await self.earnings_scanner.check_ticker(symbol)
        except Exception as e:
            logger.debug(f"Close-review earnings lookup failed for {symbol}: {e}")
            event = None
        cache[cache_key] = event
        self._close_review_earnings_cache = cache
        return event

    def _close_review_bias_snapshot(self, position: Dict) -> Dict:
        sources = []
        overnight_bias = self.get_overnight_bias_context(refresh=False) or {}
        overnight_direction = normalize_bias_label(overnight_bias.get("direction"))
        if overnight_direction not in ("unknown", ""):
            sources.append(overnight_direction)

        regime = str(position.get("market_regime", "") or "").lower()
        if regime == "risk_on":
            sources.append("bullish")
        elif regime == "risk_off":
            sources.append("bearish")

        overnight_context = normalize_bias_label(position.get("overnight_context"))
        if overnight_context not in ("unknown", ""):
            sources.append(overnight_context)

        scores = score_directional_biases(sources)
        if scores["bullish"] > scores["bearish"]:
            dominant = "bullish"
        elif scores["bearish"] > scores["bullish"]:
            dominant = "bearish"
        else:
            dominant = "mixed"
        direction = "SHORT" if str(position.get("side", "long") or "long").lower() == "short" else "BUY"
        aligned = dominant in {"bullish", "bearish"} and bias_matches_direction(dominant, direction)
        return {
            "overnight_bias": overnight_bias,
            "dominant": dominant,
            "aligned": aligned,
            "inputs": sources,
        }

    def _close_review_catalyst_reasons(
        self,
        position: Dict,
        now_ts: float,
        earnings_event: Optional[Dict] = None,
    ) -> List[str]:
        reasons: List[str] = []
        now_dt = datetime.fromtimestamp(now_ts, EASTERN)
        today = now_dt.date()
        lookahead_days = max(0, int(getattr(settings, "CLOSE_CARRY_REVIEW_CATALYST_LOOKAHEAD_DAYS", 1) or 1))

        if bool(position.get("pharma_signal", False)):
            reasons.append("pharma_catalyst")
        pharma_catalyst_type = str(position.get("pharma_catalyst_type", "") or "").strip().lower()
        if pharma_catalyst_type:
            reasons.append(f"pharma:{pharma_catalyst_type}")

        event_candidates = []
        if isinstance(earnings_event, dict) and earnings_event:
            event_candidates.append(
                (
                    "earnings",
                    self._parse_event_date(earnings_event.get("date")),
                    str(earnings_event.get("timing", "") or "").upper(),
                )
            )
        for label, value in (
            ("earnings", position.get("earnings_date") or (today.isoformat() if position.get("earnings") else None)),
            ("catalyst", position.get("catalyst_date")),
        ):
            event_candidates.append((label, self._parse_event_date(value), ""))

        for label, event_date, timing in event_candidates:
            if not event_date:
                continue
            delta_days = (event_date - today).days
            if delta_days < 0 or delta_days > lookahead_days:
                continue
            if label == "earnings" and delta_days == 0 and timing == "AMC":
                reasons.append("earnings_today_amc")
            elif label == "earnings" and delta_days == 1 and timing == "BMO":
                reasons.append("earnings_tomorrow_bmo")
            else:
                reasons.append(f"{label}_within_{delta_days}d")

        signal_sources = {str(source or "").strip().lower() for source in (position.get("signal_sources", []) or [])}
        if {"earnings", "earnings_reaction"} & signal_sources:
            reasons.append("earnings_signal")
        if {"pharma", "pharma_catalyst"} & signal_sources:
            reasons.append("pharma_signal")
        if {"edgar", "copy_trader", "congress"} & signal_sources:
            reasons.append("structural_signal_source")

        deduped = []
        seen = set()
        for reason in reasons:
            if reason in seen:
                continue
            seen.add(reason)
            deduped.append(reason)
        return deduped

    def _close_review_decision(
        self,
        position: Dict,
        current_price: float,
        now_ts: float,
        earnings_event: Optional[Dict] = None,
    ) -> Dict:
        entry_price = float(position.get("entry_price", 0) or 0)
        if entry_price <= 0 or current_price <= 0:
            return {"decision": "carry", "reason": "invalid_price_context", "reason_codes": ["invalid_price_context"]}

        side = str(position.get("side", "long") or "long").lower()
        current_pnl_pct = ProfitRatchet.calc_pnl_pct(entry_price, current_price, side)
        peak_price = float(position.get("peak_price", current_price) or current_price)
        peak_pnl_pct = ProfitRatchet.calc_pnl_pct(entry_price, peak_price, side)
        giveback_pct = ProfitRatchet.compute_giveback_pct(peak_pnl_pct, current_pnl_pct)
        hold_seconds = max(0.0, now_ts - float(position.get("entry_time", now_ts) or now_ts))
        holding_horizon = str(position.get("holding_horizon", "intraday") or "intraday").lower()
        bias = self._close_review_bias_snapshot(position)
        bias_aligned = bool(bias.get("aligned"))
        catalyst_reasons = self._close_review_catalyst_reasons(position, now_ts, earnings_event=earnings_event)
        structural_hold = bool(catalyst_reasons) or holding_horizon == "swing"
        strong_flow = int(position.get("copy_trader_convergence", 0) or 0) >= 2
        trend_intact = current_pnl_pct >= 1.0 and (giveback_pct is None or giveback_pct <= 35.0)

        min_profit = float(getattr(settings, "CLOSE_CARRY_REVIEW_MIN_PROFIT_PCT", 0.5) or 0.5)
        loser_exit_pct = float(getattr(settings, "CLOSE_CARRY_REVIEW_LOSER_EXIT_PCT", -0.35) or -0.35)
        max_giveback_pct = float(getattr(settings, "CLOSE_CARRY_REVIEW_MAX_GIVEBACK_PCT", 55.0) or 55.0)
        now_dt = datetime.fromtimestamp(now_ts, EASTERN)

        carry_score = 0
        if structural_hold:
            carry_score += 2
        if bias_aligned:
            carry_score += 1
        if strong_flow:
            carry_score += 1
        if trend_intact:
            carry_score += 1

        reason_codes = list(catalyst_reasons)
        if bias_aligned:
            reason_codes.append(f"bias_aligned:{bias.get('dominant', 'mixed')}")
        if strong_flow:
            reason_codes.append("copy_trader_convergence")
        if trend_intact:
            reason_codes.append("trend_intact")
        if holding_horizon == "swing":
            reason_codes.append("swing_horizon")

        if current_pnl_pct <= loser_exit_pct and not structural_hold:
            return {
                "decision": "flatten",
                "reason": f"losing_into_close_no_catalyst ({current_pnl_pct:+.2f}%)",
                "reason_codes": reason_codes + ["losing_into_close_no_catalyst"],
                "current_pnl_pct": current_pnl_pct,
                "peak_pnl_pct": peak_pnl_pct,
                "giveback_pct": giveback_pct,
                "carry_score": carry_score,
            }

        if (
            now_dt.weekday() == 4
            and current_pnl_pct >= min_profit
            and not structural_hold
            and carry_score < 3
        ):
            return {
                "decision": "flatten",
                "reason": f"friday_profit_lock ({current_pnl_pct:+.2f}%)",
                "reason_codes": reason_codes + ["friday_profit_lock"],
                "current_pnl_pct": current_pnl_pct,
                "peak_pnl_pct": peak_pnl_pct,
                "giveback_pct": giveback_pct,
                "carry_score": carry_score,
            }

        if (
            current_pnl_pct >= min_profit
            and giveback_pct is not None
            and giveback_pct >= max_giveback_pct
            and not structural_hold
            and not bias_aligned
        ):
            return {
                "decision": "flatten",
                "reason": f"late_day_giveback {giveback_pct:.0f}% without overnight thesis",
                "reason_codes": reason_codes + ["late_day_giveback"],
                "current_pnl_pct": current_pnl_pct,
                "peak_pnl_pct": peak_pnl_pct,
                "giveback_pct": giveback_pct,
                "carry_score": carry_score,
            }

        if (
            0 < current_pnl_pct < min_profit
            and hold_seconds >= 1800
            and not structural_hold
            and not bias_aligned
        ):
            return {
                "decision": "flatten",
                "reason": f"thin_profit_no_overnight_thesis ({current_pnl_pct:+.2f}%)",
                "reason_codes": reason_codes + ["thin_profit_no_overnight_thesis"],
                "current_pnl_pct": current_pnl_pct,
                "peak_pnl_pct": peak_pnl_pct,
                "giveback_pct": giveback_pct,
                "carry_score": carry_score,
            }

        if (
            current_pnl_pct <= 0.10
            and hold_seconds >= 7200
            and not structural_hold
            and not bias_aligned
        ):
            return {
                "decision": "flatten",
                "reason": f"stalled_intraday_carry ({current_pnl_pct:+.2f}%)",
                "reason_codes": reason_codes + ["stalled_intraday_carry"],
                "current_pnl_pct": current_pnl_pct,
                "peak_pnl_pct": peak_pnl_pct,
                "giveback_pct": giveback_pct,
                "carry_score": carry_score,
            }

        return {
            "decision": "carry",
            "reason": ", ".join(reason_codes[:3]) if reason_codes else "ratchet_protected_carry",
            "reason_codes": reason_codes or ["ratchet_protected_carry"],
            "current_pnl_pct": current_pnl_pct,
            "peak_pnl_pct": peak_pnl_pct,
            "giveback_pct": giveback_pct,
            "carry_score": carry_score,
        }

    async def _run_close_carry_review(
        self,
        position: Dict,
        current_price: float,
        open_orders_by_symbol: Dict[str, List[Dict]],
        now_ts: Optional[float] = None,
    ) -> bool:
        if not bool(getattr(settings, "CLOSE_CARRY_REVIEW_ENABLED", True)):
            return False
        if position.get("exit_pending"):
            return False

        now_ts = float(now_ts or time.time())
        if not is_market_hours(now_ts):
            return False

        minutes_window = int(getattr(settings, "CLOSE_CARRY_REVIEW_MINUTES", 0) or 0)
        if minutes_window <= 0:
            return False

        dt = datetime.fromtimestamp(now_ts, EASTERN)
        close_dt = dt.replace(hour=16, minute=0, second=0, microsecond=0)
        minutes_to_close = (close_dt - dt).total_seconds() / 60.0
        if minutes_to_close <= 0 or minutes_to_close > minutes_window:
            return False

        cooldown = float(getattr(settings, "CLOSE_CARRY_REVIEW_COOLDOWN_SECONDS", 180) or 180)
        last_review_at = float(position.get("close_carry_reviewed_at", 0) or 0)
        if cooldown > 0 and (now_ts - last_review_at) < cooldown:
            return False

        symbol = str(position.get("symbol", "") or "")
        earnings_event = await self._get_close_review_earnings_event(symbol, now_ts)
        decision = self._close_review_decision(position, current_price, now_ts, earnings_event=earnings_event)
        position["close_carry_reviewed_at"] = now_ts
        position["close_carry_decision"] = decision.get("decision")
        position["close_carry_reason"] = decision.get("reason")
        position["close_carry_reason_codes"] = list(decision.get("reason_codes", []) or [])

        if decision.get("decision") != "flatten":
            last_logged = float(position.get("close_carry_review_logged_at", 0) or 0)
            if now_ts - last_logged >= cooldown:
                logger.info(
                    f"🌙 Close review HOLD {symbol}: {decision.get('reason')} "
                    f"(pnl={float(decision.get('current_pnl_pct', 0) or 0):+.2f}% "
                    f"peak={float(decision.get('peak_pnl_pct', 0) or 0):+.2f}% "
                    f"giveback={float(decision.get('giveback_pct', 0) or 0):.0f}% "
                    f"{minutes_to_close:.0f}m to close)"
                )
                position["close_carry_review_logged_at"] = now_ts
            return False

        exit_side = self._position_exit_side(position)
        canceled = await cancel_conflicting_exit_orders(self.alpaca_client, symbol, exit_side) if self.alpaca_client else 0
        if canceled:
            position["hard_stop_order_id"] = ""
            position["ratchet_limit_order_id"] = ""
            position.setdefault("order_state", {})["hard_stop"] = "canceled_for_close_review"
            position.setdefault("order_state", {})["ratchet"] = "canceled_for_close_review"

        holding_horizon = str(position.get("holding_horizon", "intraday") or "intraday").lower()
        eod_partial_horizons = {"swing", "multiday", "catalyst"}

        if holding_horizon in eod_partial_horizons and self.alpaca_client:
            eod_partial_pct = float(getattr(settings, "EOD_PARTIAL_EXIT_PCT", 60.0) or 60.0)
            logger.warning(
                f"🌆 Close review PARTIAL EXIT {symbol} ({eod_partial_pct:.0f}%): {decision.get('reason')} "
                f"(horizon={holding_horizon} pnl={float(decision.get('current_pnl_pct', 0) or 0):+.2f}% "
                f"peak={float(decision.get('peak_pnl_pct', 0) or 0):+.2f}% "
                f"giveback={float(decision.get('giveback_pct', 0) or 0):.0f}% "
                f"{minutes_to_close:.0f}m to close)"
            )
            try:
                result = await asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: self.alpaca_client.close_position(symbol, percentage=eod_partial_pct),
                )
            except Exception as e:
                logger.error(f"EOD partial exit failed for {symbol}: {e}")
                result = None

            if result and str(result.get("status", "") or "").lower() != "not_found":
                old_qty = float(position.get("quantity", 0) or 0)
                keep_pct = (100.0 - eod_partial_pct) / 100.0
                new_qty = round(old_qty * keep_pct, 6)
                position["quantity"] = new_qty
                position["actual_qty"] = new_qty
                entry_price = float(position.get("entry_price", 0) or 0)
                position["actual_notional"] = entry_price * new_qty
                position["partial_exit"] = True
                position["eod_partial_exit_at"] = time.time()
                position["eod_partial_exit_pct"] = eod_partial_pct
                position["last_exit_reason"] = "eod_partial_exit"
                logger.success(
                    f"🌆 EOD partial exit: {symbol} {old_qty:.4f} → {new_qty:.4f} shares "
                    f"({eod_partial_pct:.0f}% closed, {100 - eod_partial_pct:.0f}% carried overnight)"
                )
                log_activity("trade", f"🌆 EOD partial exit {symbol}: {eod_partial_pct:.0f}% closed, {100 - eod_partial_pct:.0f}% overnight — {decision.get('reason')}")
                return True
            return False

        logger.warning(
            f"🌆 Close review FLATTEN {symbol}: {decision.get('reason')} "
            f"(pnl={float(decision.get('current_pnl_pct', 0) or 0):+.2f}% "
            f"peak={float(decision.get('peak_pnl_pct', 0) or 0):+.2f}% "
            f"giveback={float(decision.get('giveback_pct', 0) or 0):.0f}% "
            f"{minutes_to_close:.0f}m to close)"
        )
        submitted = await self._submit_software_managed_exit(position, current_price, "close_review_flatten")
        if submitted:
            log_activity("trade", f"🌆 Close review flatten: {symbol} — {decision.get('reason')}")
        return bool(submitted)

    def _apply_close_profit_lock(self, position: Dict, current_price: float, now_ts: Optional[float] = None) -> None:
        now_ts = float(now_ts or time.time())
        if not is_market_hours(now_ts):
            return

        minutes_window = int(getattr(settings, "PROFIT_RATCHET_CLOSE_TIGHTEN_MINUTES", 0) or 0)
        if minutes_window <= 0:
            return

        dt = datetime.fromtimestamp(now_ts, EASTERN)
        close_dt = dt.replace(hour=16, minute=0, second=0, microsecond=0)
        minutes_to_close = (close_dt - dt).total_seconds() / 60.0
        if minutes_to_close <= 0 or minutes_to_close > minutes_window:
            return

        if str(position.get("holding_horizon", "intraday") or "intraday").lower() == "swing":
            return

        entry_price = float(position.get("entry_price", 0) or 0)
        if entry_price <= 0 or current_price <= 0:
            return

        pnl_pct = ProfitRatchet.calc_pnl_pct(entry_price, current_price, position.get("side", "long"))
        min_profit = float(getattr(settings, "PROFIT_RATCHET_CLOSE_TIGHTEN_MIN_PROFIT_PCT", 0.5) or 0.5)
        if pnl_pct < min_profit:
            return

        close_trail = max(
            0.5,
            min(
                float(position.get("trail_pct", settings.PROFIT_RATCHET_TRAIL_PCT) or settings.PROFIT_RATCHET_TRAIL_PCT),
                float(getattr(settings, "PROFIT_RATCHET_CLOSE_TRAIL_PCT", 1.5) or 1.5),
            ),
        )
        close_activation = max(
            0.1,
            min(
                float(getattr(settings, "PROFIT_RATCHET_ACTIVATION_PCT", 1.0) or 1.0),
                float(getattr(settings, "PROFIT_RATCHET_CLOSE_ACTIVATION_PCT", 0.75) or 0.75),
            ),
        )
        close_floor = max(
            float(getattr(settings, "PROFIT_RATCHET_INITIAL_FLOOR_PCT", 0.25) or 0.25),
            float(getattr(settings, "PROFIT_RATCHET_CLOSE_FLOOR_PCT", 0.35) or 0.35),
        )

        def _float_or_none(value):
            try:
                if value is None:
                    return None
                return float(value)
            except Exception:
                return None

        prior_tighten = _float_or_none(position.get("ratchet_tighten_suggestion_pct"))
        prior_activation = _float_or_none(position.get("ratchet_activation_override_pct"))
        prior_floor = _float_or_none(position.get("ratchet_initial_floor_override_pct"))

        new_tighten = close_trail if prior_tighten is None else min(prior_tighten, close_trail)
        new_activation = close_activation if prior_activation is None else min(prior_activation, close_activation)
        new_floor = close_floor if prior_floor is None else max(prior_floor, close_floor)

        changed = (
            prior_tighten is None
            or prior_activation is None
            or prior_floor is None
            or abs(new_tighten - prior_tighten) >= 0.05
            or abs(new_activation - prior_activation) >= 0.05
            or abs(new_floor - prior_floor) >= 0.01
        )
        if not changed:
            return

        position["ratchet_tighten_suggestion_pct"] = new_tighten
        position["ratchet_activation_override_pct"] = new_activation
        position["ratchet_initial_floor_override_pct"] = new_floor

        last_logged = float(position.get("close_profit_lock_logged_at", 0) or 0)
        if now_ts - last_logged >= 300:
            logger.info(
                f"⏰ Close-lock {position.get('symbol', '?')}: "
                f"trail<={new_tighten:.2f}% activation<={new_activation:.2f}% floor>={new_floor:.2f}% "
                f"({minutes_to_close:.0f}m to close, pnl={pnl_pct:+.2f}%)"
            )
            position["close_profit_lock_logged_at"] = now_ts

    def _on_breakout_detected(self, symbol: str, price: float, volume_spike: float, pct_change: float):
        """Called by market stream when a breakout is detected."""
        direction = "🚀" if pct_change > 0 else "💥"
        logger.info(f"{direction} BREAKOUT: {symbol} {pct_change:+.1f}% @ ${price:.2f} (vol {volume_spike:.1f}x)")
        log_activity("scan", f"{direction} Breakout: {symbol} {pct_change:+.1f}% vol={volume_spike:.1f}x")

        if not bool(getattr(settings, "FAST_PATH_ENABLED", False)):
            return
        self._handle_fast_path_breakout(
            symbol=symbol,
            price=price,
            pct_change=pct_change,
            volume_spike=volume_spike,
        )

    def _on_market_trade(self, symbol: str, price: float, size: float, timestamp: str):
        self._queue_pending_live_refresh(symbol=symbol, price=price)

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
        if not symbol:
            return
        if not self.entry_manager:
            return
        pos = self.entry_manager.positions.get(symbol)
        if not pos:
            return

        filled_at = self._parse_iso_ts(order.get("filled_at"))
        order_side = str(order.get("side", "") or "").lower()
        expected_exit_side = "buy" if pos.get("side", "long") == "short" else "sell"
        order_id = str(order.get("id", "") or "")
        matched_exit_order = order_side == expected_exit_side and (
            pos.get("exit_pending")
            or order_id == str(pos.get("hard_stop_order_id", "") or "")
            or order_id == str(pos.get("ratchet_limit_order_id", "") or "")
            or self._order_is_hard_stop(order)
            or self._order_is_ratchet(order)
        )
        if matched_exit_order:
            if pos.get("exit_recorded"):
                return
            pending_order_id = str(pos.get("exit_order_id", "") or "")
            tracked_exit_ids = {
                pending_order_id,
                str(pos.get("hard_stop_order_id", "") or ""),
                str(pos.get("ratchet_limit_order_id", "") or ""),
            }
            tracked_exit_ids.discard("")
            if tracked_exit_ids and order_id and order_id not in tracked_exit_ids and not (
                self._order_is_hard_stop(order) or self._order_is_ratchet(order)
            ):
                return
            fill_price = float(order.get("filled_avg_price", 0) or 0)
            filled_qty = float(order.get("filled_qty", pos.get("quantity", 0)) or pos.get("quantity", 0) or 0)
            if fill_price <= 0 or filled_qty <= 0:
                return
            pos["exit_fill_qty"] = filled_qty
            reason = self._infer_exit_reason_from_order(pos, order)
            trade_record = self._build_confirmed_exit_trade(
                pos,
                fill_price=fill_price,
                qty=filled_qty,
                reason=reason,
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
        persistence.save_recently_removed_positions(
            getattr(self.entry_manager, "_recently_removed_positions", {}) if self.entry_manager else {}
        )
        if getattr(self, "options_engine", None):
            persistence.save_options_positions(self.options_engine.positions)
        persistence.save_pnl_state(getattr(self, 'pnl_state', {}))
        persistence.save_ai_state(self.ai_layers)
        positions = self.entry_manager.get_positions() if self.entry_manager else []
        if positions:
            logger.info(f"🛡️ Preserving {len(positions)} positions through restart (broker hard stops protect them)")
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
