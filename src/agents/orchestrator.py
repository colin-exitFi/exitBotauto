"""
Orchestrator - Coordinates all specialized agents for a given symbol.
Runs agents 1-5 in parallel, feeds briefs to jury, returns final decision.
Manages Exit Agent lifecycle separately.
"""

import asyncio
import time
from typing import Dict, List, Optional
from loguru import logger

from src.agents import technical_agent, sentiment_agent, catalyst_agent, risk_agent, macro_agent
from src.agents.jury import JuryVerdict, deliberate
from src.agents.exit_agent import ExitAgent


class Orchestrator:
    """Coordinates specialized agents and the jury for trade decisions."""

    def __init__(self, broker=None, entry_manager=None, risk_manager=None):
        self.broker = broker
        self.entry_manager = entry_manager
        self.risk_manager = risk_manager

        # Exit agent (long-running)
        self.exit_agent = ExitAgent(
            broker=broker,
            entry_manager=entry_manager,
            risk_manager=risk_manager,
        )

        # Cache: symbol -> (verdict, timestamp)
        self._cache: Dict[str, tuple] = {}
        self._skip_cache: Dict[str, float] = {}  # symbol -> timestamp
        self._history: List[Dict] = []

    async def start_exit_agent(self):
        """Start the exit agent monitoring loop. Call once from main.py."""
        await self.exit_agent.start()

    async def stop_exit_agent(self):
        """Stop the exit agent loop."""
        await self.exit_agent.stop()

    async def evaluate(self, symbol: str, price: float, signals_data: Dict) -> JuryVerdict:
        """
        Full evaluation pipeline:
        1. Check caches/cooldowns
        2. Run 5 agents in parallel
        3. Feed briefs to jury
        4. Update exit agent with briefs
        5. Return verdict
        """
        # Skip cooldown
        skip_ts = self._skip_cache.get(symbol)
        if skip_ts and (time.time() - skip_ts) < 300:  # 5 min cooldown after SKIP
            return JuryVerdict(
                symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
                reasoning="Skip cooldown (2 min)",
            )

        # Check cache
        cached = self._cache.get(symbol)
        cache_ttl = 300  # 5 min
        if cached:
            verdict, ts = cached
            ttl = 300 if verdict.decision == "SKIP" else cache_ttl
            if (time.time() - ts) < ttl:
                logger.debug(f"Orchestrator cache hit for {symbol}")
                return verdict

        # Enrich signals with index data for macro agent
        await self._enrich_macro_data(signals_data)

        # Determine likely direction for risk/macro context
        change_pct = signals_data.get("change_pct", 0)
        direction = "SHORT" if signals_data.get("fade_signal") else "BUY"

        positions = self.entry_manager.get_positions() if self.entry_manager else []

        logger.info(f"🎯 Orchestrator: evaluating {symbol} @ ${price:.2f} with 5 agents in parallel")

        # Run all 5 agents in parallel
        tech_task = technical_agent.analyze(symbol, price, signals_data)
        sent_task = sentiment_agent.analyze(symbol, price, signals_data)
        cat_task = catalyst_agent.analyze(symbol, price, signals_data)
        risk_task = risk_agent.analyze(symbol, price, signals_data,
                                       risk_manager=self.risk_manager,
                                       positions=positions, direction=direction)
        macro_task = macro_agent.analyze(symbol, price, signals_data, direction=direction)

        tech_brief, sent_brief, cat_brief, risk_brief, macro_brief = await asyncio.gather(
            tech_task, sent_task, cat_task, risk_task, macro_task,
            return_exceptions=True,
        )

        # Handle exceptions from gather
        if isinstance(tech_brief, Exception):
            logger.error(f"Technical agent exception: {tech_brief}")
            tech_brief = technical_agent.DEFAULT_BRIEF
        if isinstance(sent_brief, Exception):
            logger.error(f"Sentiment agent exception: {sent_brief}")
            sent_brief = sentiment_agent.DEFAULT_BRIEF
        if isinstance(cat_brief, Exception):
            logger.error(f"Catalyst agent exception: {cat_brief}")
            cat_brief = catalyst_agent.DEFAULT_BRIEF
        if isinstance(risk_brief, Exception):
            logger.error(f"Risk agent exception: {risk_brief}")
            risk_brief = risk_agent.DEFAULT_BRIEF
        if isinstance(macro_brief, Exception):
            logger.error(f"Macro agent exception: {macro_brief}")
            macro_brief = macro_agent.DEFAULT_BRIEF

        briefs = {
            "technical": tech_brief,
            "sentiment": sent_brief,
            "catalyst": cat_brief,
            "risk": risk_brief,
            "macro": macro_brief,
        }

        # Feed briefs to jury (include raw scanner data so jury can see price/volume)
        verdict = await deliberate(symbol, price, briefs, signals_data=signals_data)

        # Update exit agent with latest briefs for this symbol
        self.exit_agent.update_briefs(symbol, briefs)

        # Cache & track
        self._cache[symbol] = (verdict, time.time())
        if verdict.decision == "SKIP":
            self._skip_cache[symbol] = time.time()

        self._history.append(verdict.to_dict())
        self._history = self._history[-50:]

        return verdict

    async def _enrich_macro_data(self, signals: Dict):
        """Fetch SPY/QQQ/VIX snapshots for macro agent if not present."""
        if signals.get("spy_info") and signals["spy_info"] != "N/A":
            return  # Already enriched

        try:
            if not self.broker:
                return
            import requests
            headers = {
                'APCA-API-KEY-ID': self.broker.api_key,
                'APCA-API-SECRET-KEY': self.broker.secret_key,
            }
            syms = "SPY,QQQ,DIA"
            resp = requests.get(
                f"https://data.alpaca.markets/v2/stocks/snapshots?symbols={syms}&feed=iex",
                headers=headers, timeout=8,
            )
            if resp.status_code == 200:
                data = resp.json()
                for sym_key, label in [("SPY", "spy_info"), ("QQQ", "qqq_info"), ("DIA", "dia_info")]:
                    snap = data.get(sym_key, {})
                    lt = snap.get("latestTrade", {})
                    pb = snap.get("prevDailyBar", {})
                    p = lt.get("p", 0)
                    prev = pb.get("c", 0)
                    chg = ((p - prev) / prev * 100) if prev > 0 else 0
                    signals[label] = f"${p:.2f} ({chg:+.2f}%)" if p else "N/A"

            # VIX — try from Polygon or set N/A
            signals.setdefault("vix_info", "N/A")

        except Exception as e:
            logger.debug(f"Macro data enrichment failed: {e}")

    def get_history(self) -> List[Dict]:
        return list(self._history)

    def get_stats(self) -> Dict:
        from src.agents.base_agent import get_api_stats
        total = len(self._history)
        if not total:
            return {"total": 0, "api_calls": get_api_stats()}
        return {
            "total": total,
            "buys": sum(1 for h in self._history if h["decision"] == "BUY"),
            "shorts": sum(1 for h in self._history if h["decision"] == "SHORT"),
            "skips": sum(1 for h in self._history if h["decision"] == "SKIP"),
            "avg_confidence": sum(h.get("confidence", 0) for h in self._history) / total,
            "api_calls": get_api_stats(),
        }
