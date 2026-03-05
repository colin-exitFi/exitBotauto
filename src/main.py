#!/usr/bin/env python3
"""
exitBotauto - Autonomous Stock Trading Bot
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
        logger.info("🤖 Initializing exitBotauto...")

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

        # Scanner (with StockTwits + Pharma)
        self.scanner = Scanner(
            polygon_client=self.polygon_client,
            sentiment_analyzer=self.sentiment_analyzer,
            stocktwits_client=self.stocktwits_client,
            alpaca_client=self.alpaca_client,
            pharma_scanner=self.pharma_scanner,
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

        # Dashboard
        start_dashboard(bot=self)

        logger.success("✅ All components initialized")

    async def run(self):
        """Main trading loop with AI layers running as concurrent tasks."""
        self.running = True
        self.start_time = time.time()
        logger.info("🚀 exitBotauto LIVE")

        # Launch AI layers as background tasks
        ai_task = asyncio.create_task(self._ai_loop())

        scan_interval = settings.SCAN_INTERVAL_SECONDS
        monitor_interval = 5
        last_scan = 0
        last_equity_sync = 0

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
                        logger.debug("⏰ Market closed (dead hours). Sleeping 60s...")
                        await asyncio.sleep(60)
                        continue
                    # Extended hours: scan but don't trade (unless pharma catalyst)
                    logger.debug(f"📡 Extended hours scanning ({et.strftime('%H:%M')} ET)")

                if self.paused:
                    await asyncio.sleep(5)
                    continue

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

    async def _ai_loop(self):
        """Run AI layers on their own intervals, concurrently with trading."""
        while self.running:
            try:
                # Observer (every 10 min)
                obs = await self.observer.run(self)
                if obs:
                    self.ai_layers["last_observation"] = obs.get("market_assessment", str(obs)[:200])

                # Advisor (every 30 min)
                adv = await self.advisor.run(self, self.observer.get_last_output())
                if adv:
                    self.ai_layers["last_advice"] = adv.get("strategy", str(adv)[:200])

                # Tuner (every 30 min)
                tun = await self.tuner.run(self, self.advisor.get_last_output())
                if tun and tun.get("applied"):
                    changes_str = ", ".join(f"{c['param']}:{c['old']}→{c['new']}" for c in tun["applied"])
                    self.ai_layers["last_tuner_changes"] = changes_str

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

            # Consensus engine — Claude + GPT jury must agree
            if self.consensus_engine:
                try:
                    consensus = await self.consensus_engine.evaluate(
                        symbol=symbol,
                        price=candidate.get("price", 0),
                        signals_data=candidate,
                    )
                    self.ai_layers["last_consensus"] = consensus.to_dict()
                    if consensus.final_decision != "BUY":
                        logger.info(f"🗳️ Consensus SKIP for {symbol}: {consensus.reasoning}")
                        continue
                    # Apply size modifier to sentiment_data for entry manager
                    sentiment_data["consensus_size_modifier"] = consensus.size_modifier
                    sentiment_data["consensus_confidence"] = consensus.avg_confidence
                except Exception as e:
                    logger.error(f"Consensus error for {symbol}: {e}")

            can = await self.entry_manager.can_enter(symbol, sentiment_score, positions)
            if can:
                logger.info(f"📈 Entry signal: {symbol} (score={candidate['score']:.3f}, sent={sentiment_score:.2f})")
                pos = await self.entry_manager.enter_position(symbol, sentiment_data)
                if pos:
                    positions = self.entry_manager.get_positions()

    async def _monitor_positions(self):
        """Check exit conditions for all open positions."""
        positions = self.entry_manager.get_positions()
        if not positions:
            return

        for pos in list(positions):
            symbol = pos["symbol"]
            try:
                price = await asyncio.get_event_loop().run_in_executor(
                    None, self.polygon_client.get_price, symbol
                )
                if price <= 0:
                    continue

                self.entry_manager.update_peak_price(symbol, price)

                sent = self.sentiment_analyzer.get_cached(symbol)
                sent_score = sent["score"] if sent else 0

                # Exit manager uses dynamic stop from risk tier
                trade = await self.exit_manager.check_and_exit(pos, price, sent_score)
                if trade:
                    logger.info(f"📊 Exit: {symbol} → {trade['reason']} P&L: ${trade['pnl']:.2f}")
                    # Persist to trade history
                    trade_record = {
                        "symbol": symbol,
                        "side": "sell",
                        "entry_price": trade.get("entry_price", 0),
                        "exit_price": trade.get("exit_price", 0),
                        "quantity": trade.get("quantity", 0),
                        "pnl": trade.get("pnl", 0),
                        "pnl_pct": trade.get("pnl_pct", 0),
                        "reason": trade.get("reason", ""),
                        "hold_seconds": trade.get("hold_seconds", 0),
                        "entry_time": pos.get("entry_time", 0),
                        "exit_time": time.time(),
                        "sentiment_at_entry": pos.get("sentiment_at_entry", 0),
                        "sentiment_at_exit": sent_score,
                        "conviction_level": pos.get("conviction_level", "normal"),
                        "risk_tier": self.risk_manager.get_risk_tier().get("name", "?"),
                    }
                    trade_history.record_trade(trade_record)

            except Exception as e:
                logger.error(f"Monitor error for {symbol}: {e}")

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
