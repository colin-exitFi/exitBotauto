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

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from loguru import logger
from dotenv import load_dotenv

from config import settings
from broker.alpaca_client import AlpacaClient
from data.polygon_client import PolygonClient
from scanner.scanner import Scanner
from sentiment.sentiment_analyzer import SentimentAnalyzer
from signals.stocktwits import StockTwitsClient
from signals.twitter import TwitterSentimentClient
from signals.pharma_catalyst import PharmaCatalystScanner
from signals.fade_runner import FadeRunnerScanner
from signals.watchlist import DynamicWatchlist
from signals.edgar import EdgarScanner
from dashboard.dashboard import log_activity
import persistence
from entry.entry_manager import EntryManager
from exit.exit_manager import ExitManager
from risk.risk_manager import RiskManager
from ai.observer import Observer
from ai.advisor import Advisor
from ai.tuner import Tuner
from ai.game_film import GameFilm
from ai.position_manager import PositionManager
from ai import trade_history
from ai.consensus import ConsensusEngine
from dashboard.dashboard import start_dashboard


class TradingBot:
    """Main trading bot orchestrator."""

    def __init__(self):
        self.running = False
        self.paused = False
        self.start_time = time.time()

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

        # Dynamic watchlist (built overnight, used during trading)
        self.watchlist = DynamicWatchlist()

        # Scanner (with StockTwits + Pharma + Fade)
        self.scanner = Scanner(
            polygon_client=self.polygon_client,
            sentiment_analyzer=self.sentiment_analyzer,
            stocktwits_client=self.stocktwits_client,
            alpaca_client=self.alpaca_client,
            pharma_scanner=self.pharma_scanner,
            fade_scanner=self.fade_scanner,
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

        # Consensus engine
        self.consensus_engine = ConsensusEngine()

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

        logger.success("✅ All components initialized")

    async def run(self):
        """Main trading loop with AI layers running as concurrent tasks."""
        self.running = True
        self.start_time = time.time()
        logger.info("🚀 Velox LIVE")

        # Launch AI layers as background tasks
        ai_task = asyncio.create_task(self._ai_loop())

        scan_interval = settings.SCAN_INTERVAL_SECONDS
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
                        candidates = await self.scanner.scan()
                        await self._process_candidates(candidates)
                    except Exception as e:
                        logger.error(f"Scan error: {e}")

                # ── MONITOR positions ──────────────────────────────
                try:
                    await self._monitor_positions()
                except Exception as e:
                    logger.error(f"Monitor error: {e}")

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
                    await asyncio.get_event_loop().run_in_executor(None, self.game_film.analyze)
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

    async def _process_candidates(self, candidates):
        """Evaluate scanner candidates for entry with position manager veto."""
        if not self.risk_manager.can_trade():
            return

        positions = self.entry_manager.get_positions()

        for candidate in candidates[:5]:
            symbol = candidate["symbol"]
            sentiment_score = candidate.get("sentiment_score", 0)
            sentiment_data = self.sentiment_analyzer.get_cached(symbol) or {"score": sentiment_score}

            # Position manager veto check
            if self.position_manager and not self.position_manager.can_enter(symbol, positions, self.risk_manager):
                continue

            # Consensus engine — Claude + GPT jury must agree on direction
            if self.consensus_engine:
                try:
                    consensus = await self.consensus_engine.evaluate(
                        symbol=symbol,
                        price=candidate.get("price", 0),
                        signals_data=candidate,
                    )
                    self.ai_layers["last_consensus"] = consensus.to_dict()
                    if consensus.final_decision not in ("BUY", "SHORT"):
                        logger.info(f"🗳️ Consensus SKIP for {symbol}: {consensus.reasoning}")
                        log_activity("ai", f"🗳️ {symbol}: SKIP — {consensus.reasoning[:100]}")
                        continue
                    direction = consensus.final_decision
                    log_activity("trade", f"🗳️ {symbol}: {direction} consensus! conf={consensus.avg_confidence}% size={consensus.size_modifier}%")
                    sentiment_data["consensus_size_modifier"] = consensus.size_modifier
                    sentiment_data["consensus_confidence"] = consensus.avg_confidence
                    sentiment_data["consensus_direction"] = direction
                except Exception as e:
                    logger.error(f"Consensus error for {symbol}: {e}")
                    continue  # Never trade without consensus

            can = await self.entry_manager.can_enter(symbol, sentiment_score, positions)
            if can:
                direction = sentiment_data.get("consensus_direction", "BUY")
                logger.info(f"{'📈' if direction == 'BUY' else '📉'} Entry signal: {symbol} {direction} (score={candidate['score']:.3f}, sent={sentiment_score:.2f})")
                if direction == "SHORT":
                    pos = await self.entry_manager.enter_short(symbol, sentiment_data)
                else:
                    pos = await self.entry_manager.enter_position(symbol, sentiment_data)
                if pos:
                    positions = self.entry_manager.get_positions()

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

        for pos in list(positions):
            symbol = pos["symbol"]
            try:
                # ── DETECT TRAILING STOP FILLS ──
                # If we're tracking it but Alpaca no longer has it → trailing stop fired
                if symbol not in alpaca_symbols:
                    entry_price = pos.get("entry_price", 0)
                    # Get the last fill price from closed orders
                    exit_price = entry_price  # default
                    try:
                        orders = await asyncio.get_event_loop().run_in_executor(
                            None, self.alpaca_client.get_orders, "closed"
                        )
                        for o in orders:
                            if o.get("symbol") == symbol and o.get("type") == "trailing_stop":
                                exit_price = float(o.get("filled_avg_price", entry_price))
                                break
                    except Exception:
                        pass

                    qty = pos.get("quantity", 0)
                    side = pos.get("side", "long")
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
                    }
                    trade_history.record_trade(trade_record)
                    self.entry_manager.remove_position(symbol)
                    self.risk_manager.record_trade_pnl(pnl)

                    # Update P&L tracking
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
                    persistence.save_positions(self.entry_manager.positions)
                    persistence.save_trades([trade_record])

                    emoji = "✅" if pnl >= 0 else "❌"
                    logger.info(f"{emoji} TRAILING STOP EXIT: {symbol} P&L: ${pnl:.2f} ({pnl_pct:+.1f}%)")
                    log_activity("trade", f"{emoji} {symbol} stopped out: ${pnl:.2f} ({pnl_pct:+.1f}%)")
                    continue

                # ── VERIFY TRAILING STOP EXISTS ──
                if not pos.get("has_trailing_stop"):
                    logger.warning(f"⚠️ {symbol} has NO trailing stop — retrying placement")
                    qty = int(float(pos.get("quantity", 0)))
                    trail_pct = pos.get("trail_pct", 3.0)
                    if qty >= 1:
                        stop_order = await asyncio.get_event_loop().run_in_executor(
                            None, self.alpaca_client.place_trailing_stop, symbol, qty, trail_pct
                        )
                        if stop_order:
                            pos["has_trailing_stop"] = True
                            pos["trailing_stop_order_id"] = stop_order.get("id")
                            logger.success(f"📈 Trailing stop recovered: {symbol} trail={trail_pct}%")
                        else:
                            # Last resort: market sell to protect capital
                            logger.error(f"🚨 TRAILING STOP FAILED 3x for {symbol} — MARKET SELLING for protection")
                            await asyncio.get_event_loop().run_in_executor(
                                None, self.alpaca_client.place_market_sell, symbol, qty
                            )
                            self.entry_manager.remove_position(symbol)
                            log_activity("alert", f"🚨 Emergency market sell: {symbol} — trailing stop could not be placed")

            except Exception as e:
                logger.error(f"Monitor error for {symbol}: {e}")

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
