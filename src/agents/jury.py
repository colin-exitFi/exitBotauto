"""
Jury 🗳️ - Synthesizes all 5 agent briefs into a final trade decision.
All three configured jury models vote in parallel. 2-of-3 agreement wins.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from loguru import logger

from config import settings
from src.agents.base_agent import call_claude, call_gpt, call_grok


@dataclass
class JuryVerdict:
    symbol: str
    decision: str  # "BUY", "SHORT", "SKIP"
    size_pct: float  # position size as % of equity
    trail_pct: float  # trailing stop %
    reasoning: str
    confidence: float = 0.0
    provider_used: str = ""
    briefs: Dict = field(default_factory=dict)
    consensus_detail: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "decision": self.decision,
            "size_pct": self.size_pct,
            "trail_pct": self.trail_pct,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "provider_used": self.provider_used,
            "consensus_detail": self.consensus_detail,
            "timestamp": self.timestamp,
        }


PROMPT_TEMPLATE = """You are the JURY — the final decision maker inside Velox.
{mission}

You receive briefs from 5 specialized agents. Synthesize them into ONE trade decision.

SYMBOL: {symbol} @ ${price:.2f}
TODAY'S MOVE: {change_pct:+.1f}%
VOLUME vs AVG: {volume_spike:.1f}x
SPREAD: {spread_pct}%
SETUP TAG: {strategy_tag}
SIDE BIAS: {side_bias}
FADE CONTEXT: {fade_context}
ECONOMIC CALENDAR: {economic_calendar}
HUMAN INTEL: {human_intel}
PRO TRADER CONTEXT: {copy_trader_context}
RETRO FEEDBACK: {retro_feedback}

AGENT BRIEFS:

📊 TECHNICAL:
{technical}

🐦 SENTIMENT:
{sentiment}

🔬 CATALYST:
{catalyst}

🛡️ RISK:
{risk}

🌍 MACRO:
{macro}

DECISION FRAMEWORK:
- BIAS TOWARD ACTION. Dead capital is the enemy. Trailing stops manage risk — your job is to find trades, not avoid them.
- BUY if: Stock is up significantly (+10%+) with volume (1.5x+) and risk approves. Technical BUY is ideal but Technical HOLD with decent confidence (50%+) is also fine if other signals support.
- SHORT if: Stock is crashing hard (-5%+ down) with volume, Technical says SELL, and macro/catalyst supports bearish case.
- SHORT if: This is a fade-the-runner setup. A stock that ran big yesterday and is stalling/fading today is a mean-reversion short, not a momentum long.
- For fade setups, prioritize exhaustion signals: yesterday's huge run, RSI stretched, day-2 volume failing to match day-1, and price trading weak versus the run close.
- Convergence from multiple Tier-1 pro traders is supportive confirmation, not crowding by itself. Only discount it if retail/FOMO evidence is also obvious.
- Use RETRO FEEDBACK as calibration, not a blind override. If the same setup has recently lost money, demand cleaner alignment or smaller size. If it has recently worked, don't overreact to weak objections.
- SKIP ONLY if: Risk explicitly denies, OR the stock has tiny volume (<1x avg), OR the move is clearly over (price reversing against the trend).
- If some agents are unavailable, MAKE THE CALL with what you have. 3 agents is enough. Don't skip just because sentiment or catalyst is offline.
- "Decelerating momentum" alone is NOT a reason to skip. Stocks don't go straight up — they consolidate and continue. If price is still up big on volume, the trend is intact.
- We have trailing stops at 3%. Maximum downside per trade is 3%. The cost of a wrong entry is small. The cost of missing a runner is infinite.

SIZING:
- size_pct: 0.5% (speculative) to 3% (high conviction) of equity
- trail_pct: 1.5% (tight, lock in gains) to 4% (wide, let it run)

Respond with ONLY valid JSON:
{{"decision": "BUY" or "SHORT" or "SKIP", "size_pct": number, "trail_pct": number, "reasoning": "brief synthesis of why", "confidence": 0-100}}"""


async def deliberate(symbol: str, price: float, briefs: Dict, signals_data: Dict = None) -> JuryVerdict:
    """Synthesize agent briefs into a final trade decision."""
    try:
        # Format briefs for the prompt
        def fmt(brief: Dict) -> str:
            if not brief or brief.get("error"):
                return "⚠️ Agent unavailable"
            lines = []
            for k, v in brief.items():
                if k in ("error", "symbol"):
                    continue
                lines.append(f"  {k}: {v}")
            return "\n".join(lines) if lines else "  No data"

        from src.ai.mission import MISSION_SHORT
        sd = signals_data or {}
        side_bias = "SHORT" if str(sd.get("side", "")).strip().lower() == "short" else "LONG"
        if sd.get("fade_signal"):
            fade_context = (
                f"Ran {float(sd.get('fade_run_pct', 0) or 0):+.1f}% on the prior session; "
                f"now {float(sd.get('price_change_from_run', 0) or 0):+.1f}% vs run close; "
                f"RSI {float(sd.get('rsi', 0) or 0):.1f}; "
                f"day-2 volume {float(sd.get('volume', 0) or 0):,.0f} vs day-1 {float(sd.get('run_volume', 0) or 0):,.0f}"
            )
        else:
            fade_context = "None"
        retro_feedback = _build_retro_feedback(symbol, sd)
        prompt = PROMPT_TEMPLATE.format(
            mission=MISSION_SHORT,
            symbol=symbol,
            price=price,
            change_pct=sd.get("change_pct", 0),
            volume_spike=sd.get("volume_spike", 0),
            spread_pct=sd.get("spread_pct", "N/A"),
            strategy_tag=sd.get("strategy_tag", "unknown"),
            side_bias=side_bias,
            fade_context=fade_context,
            economic_calendar=sd.get("economic_calendar", "None"),
            human_intel=sd.get("human_intel", "None"),
            copy_trader_context=sd.get("copy_trader_context", "None"),
            retro_feedback=retro_feedback,
            technical=fmt(briefs.get("technical", {})),
            sentiment=fmt(briefs.get("sentiment", {})),
            catalyst=fmt(briefs.get("catalyst", {})),
            risk=fmt(briefs.get("risk", {})),
            macro=fmt(briefs.get("macro", {})),
        )

        provider_names = ["claude", "gpt", "grok"]
        results = await asyncio.gather(
            _safe_call("claude", call_claude, prompt),
            _safe_call("gpt", call_gpt, prompt),
            _safe_call("grok", call_grok, prompt),
            return_exceptions=True,
        )

        votes = []
        for provider_name, result in zip(provider_names, results):
            if isinstance(result, Exception) or not isinstance(result, dict):
                continue
            normalized = _normalize_vote(provider_name, result)
            if normalized:
                votes.append(normalized)

        verdict = _apply_consensus(symbol, price, votes, briefs)

        # Check if risk agent denied — hard override
        risk_brief = briefs.get("risk", {})
        if risk_brief and not risk_brief.get("approved", True) and not risk_brief.get("error"):
            if verdict.decision in ("BUY", "SHORT"):
                logger.info(f"🛡️ Jury overridden by Risk Agent for {symbol}: {risk_brief.get('reasoning', '')}")
                return JuryVerdict(
                    symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
                    reasoning=f"Risk agent denied: {risk_brief.get('reasoning', 'portfolio risk too high')}",
                    provider_used=verdict.provider_used,
                    briefs=briefs,
                    consensus_detail={**verdict.consensus_detail, "risk_override": True},
                )

        # Cap size_pct by risk agent's max
        if risk_brief and risk_brief.get("max_size_pct"):
            verdict.size_pct = min(verdict.size_pct, float(risk_brief["max_size_pct"]))

        votes_text = verdict.consensus_detail.get("votes", {})
        logger.info(
            f"🗳️ Jury verdict for {symbol}: {verdict.decision} "
            f"size={verdict.size_pct}% trail={verdict.trail_pct}% "
            f"conf={verdict.confidence}% provider={verdict.provider_used or '?'} "
            f"votes={votes_text} — {verdict.reasoning[:100]}"
        )
        return verdict

    except Exception as e:
        logger.error(f"Jury error for {symbol}: {e}")
        return JuryVerdict(
            symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
            reasoning=f"Jury exception: {e}", briefs=briefs,
        )


async def _safe_call(provider_name: str, caller, prompt: str) -> Optional[Dict]:
    try:
        return await caller(prompt, max_tokens=400)
    except Exception as e:
        logger.warning(f"Jury {provider_name} failed: {e}")
        return None


def _normalize_vote(provider_name: str, result: Dict) -> Optional[Dict]:
    decision = str(result.get("decision", "SKIP") or "SKIP").upper()
    if decision not in ("BUY", "SHORT", "SKIP"):
        decision = "SKIP"
    try:
        size_pct = max(0.0, min(5.0, float(result.get("size_pct", 0) or 0)))
    except Exception:
        size_pct = 0.0
    try:
        trail_pct = max(1.0, min(5.0, float(result.get("trail_pct", 3.0) or 3.0)))
    except Exception:
        trail_pct = 3.0
    try:
        confidence = max(0.0, min(100.0, float(result.get("confidence", 0) or 0)))
    except Exception:
        confidence = 0.0
    return {
        "provider": provider_name,
        "decision": decision,
        "size_pct": size_pct,
        "trail_pct": trail_pct,
        "confidence": confidence,
        "reasoning": str(result.get("reasoning", "") or "")[:220],
    }


def _build_retro_feedback(symbol: str, signals_data: Dict) -> str:
    if not bool(getattr(settings, "JURY_RETRO_ENABLED", True)):
        return "None"

    try:
        from src.ai import trade_history

        recent = trade_history.get_recent(int(getattr(settings, "JURY_RETRO_LOOKBACK_TRADES", 40) or 40))
    except Exception:
        return "None"

    if not recent:
        return "None"

    min_matches = max(2, int(getattr(settings, "JURY_RETRO_MIN_MATCHES", 3) or 3))
    strategy_tag = str((signals_data or {}).get("strategy_tag", "unknown") or "unknown")
    signal_sources = (signals_data or {}).get("signal_sources", []) or []
    if isinstance(signal_sources, str):
        signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
    source_set = {str(src).strip() for src in signal_sources if str(src).strip()}

    rows: List[str] = []

    if symbol:
        symbol_trades = [
            trade for trade in recent
            if str(trade.get("symbol", "")).upper() == str(symbol).upper()
        ]
        if len(symbol_trades) >= min_matches:
            rows.append(_format_retro_row(f"Recent {symbol}", symbol_trades))

    if strategy_tag and strategy_tag != "unknown":
        strategy_trades = [
            trade for trade in recent
            if str(trade.get("strategy_tag", "unknown") or "unknown") == strategy_tag
        ]
        if len(strategy_trades) >= min_matches:
            rows.append(_format_retro_row(f"Strategy {strategy_tag}", strategy_trades))
            high_conf = [
                trade for trade in strategy_trades
                if float(trade.get("decision_confidence", 0) or 0) >= 75.0
            ]
            if len(high_conf) >= min_matches:
                rows.append(_format_confidence_calibration(high_conf))

    if source_set:
        source_trades = []
        for trade in recent:
            sources = trade.get("signal_sources", []) or []
            if isinstance(sources, str):
                sources = [s.strip() for s in sources.split(",") if s.strip()]
            if source_set.intersection({str(src).strip() for src in sources if str(src).strip()}):
                source_trades.append(trade)
        if len(source_trades) >= min_matches:
            rows.append(_format_retro_row(f"Sources {', '.join(sorted(source_set))}", source_trades))

    if not rows:
        return "None"
    return "\n".join(f"- {row}" for row in rows[:4])


def _format_retro_row(label: str, trades: List[Dict]) -> str:
    pnl = sum(float(trade.get("pnl", 0) or 0) for trade in trades)
    wins = sum(1 for trade in trades if float(trade.get("pnl", 0) or 0) > 0)
    win_rate = wins / max(1, len(trades))
    return f"{label}: {len(trades)} trades, {win_rate:.0%} WR, ${pnl:.2f} P&L."


def _format_confidence_calibration(trades: List[Dict]) -> str:
    pnl = sum(float(trade.get("pnl", 0) or 0) for trade in trades)
    wins = sum(1 for trade in trades if float(trade.get("pnl", 0) or 0) > 0)
    win_rate = wins / max(1, len(trades))
    if win_rate <= 0.40 or pnl < 0:
        return (
            f"Calibration: recent high-confidence calls underperformed "
            f"({len(trades)} trades, {win_rate:.0%} WR, ${pnl:.2f})."
        )
    return (
        f"Calibration: recent high-confidence calls worked "
        f"({len(trades)} trades, {win_rate:.0%} WR, ${pnl:.2f})."
    )


def _apply_consensus(symbol: str, price: float, votes: List[Dict], briefs: Dict) -> JuryVerdict:
    if not votes:
        return JuryVerdict(
            symbol=symbol,
            decision="SKIP",
            size_pct=0,
            trail_pct=3.0,
            reasoning="All jury models failed",
            provider_used="none",
            briefs=briefs,
            consensus_detail={
                "votes": {},
                "total_models": 0,
                "agreement": "none",
                "size_modifier": 0.0,
                "confidence": 0.0,
            },
        )

    grouped = {"BUY": [], "SHORT": [], "SKIP": []}
    for vote in votes:
        grouped[vote["decision"]].append(vote)

    providers_used = [vote["provider"] for vote in votes]
    vote_map = {vote["provider"]: vote["decision"] for vote in votes}
    total = len(votes)

    buy_votes = grouped["BUY"]
    short_votes = grouped["SHORT"]
    skip_votes = grouped["SKIP"]

    if total == 1:
        only_vote = votes[0]
        if only_vote["decision"] == "SKIP":
            return _skip_verdict(symbol, briefs, providers_used, vote_map, total, "single_skip", "Single model responded with SKIP")
        return _decision_verdict(
            symbol=symbol,
            decision=only_vote["decision"],
            agreeing_votes=[only_vote],
            opposing_votes=[],
            providers_used=providers_used,
            vote_map=vote_map,
            briefs=briefs,
            agreement="single",
            size_modifier=0.50,
            confidence_multiplier=0.60,
        )

    if total == 2:
        if len(buy_votes) == 2:
            return _decision_verdict(symbol, "BUY", buy_votes, [], providers_used, vote_map, briefs, "majority_two_model", 1.0, 0.85)
        if len(short_votes) == 2:
            return _decision_verdict(symbol, "SHORT", short_votes, [], providers_used, vote_map, briefs, "majority_two_model", 1.0, 0.85)
        if len(skip_votes) == 2:
            return _skip_verdict(symbol, briefs, providers_used, vote_map, total, "unanimous_skip", "Two-model unanimous SKIP")
        return _skip_verdict(symbol, briefs, providers_used, vote_map, total, "split", "Two responding models disagreed")

    if len(buy_votes) == 3:
        return _decision_verdict(symbol, "BUY", buy_votes, [], providers_used, vote_map, briefs, "unanimous", 1.0, 1.0)
    if len(short_votes) == 3:
        return _decision_verdict(symbol, "SHORT", short_votes, [], providers_used, vote_map, briefs, "unanimous", 1.0, 1.0)
    if len(skip_votes) == 3:
        return _skip_verdict(symbol, briefs, providers_used, vote_map, total, "unanimous_skip", "All jury models SKIPped")

    if len(buy_votes) == 2:
        conflict = len(short_votes) > 0
        return _decision_verdict(
            symbol,
            "BUY",
            buy_votes,
            short_votes,
            providers_used,
            vote_map,
            briefs,
            "majority_conflict" if conflict else "majority",
            0.75 if conflict else 1.0,
            0.85,
        )
    if len(short_votes) == 2:
        conflict = len(buy_votes) > 0
        return _decision_verdict(
            symbol,
            "SHORT",
            short_votes,
            buy_votes,
            providers_used,
            vote_map,
            briefs,
            "majority_conflict" if conflict else "majority",
            0.75 if conflict else 1.0,
            0.85,
        )

    return _skip_verdict(symbol, briefs, providers_used, vote_map, total, "none", "No consensus across jury models")


def _decision_verdict(
    symbol: str,
    decision: str,
    agreeing_votes: List[Dict],
    opposing_votes: List[Dict],
    providers_used: List[str],
    vote_map: Dict[str, str],
    briefs: Dict,
    agreement: str,
    size_modifier: float,
    confidence_multiplier: float,
) -> JuryVerdict:
    base_size = sum(vote["size_pct"] for vote in agreeing_votes) / max(1, len(agreeing_votes))
    trail_pct = sum(vote["trail_pct"] for vote in agreeing_votes) / max(1, len(agreeing_votes))
    avg_conf = sum(vote["confidence"] for vote in agreeing_votes) / max(1, len(agreeing_votes))
    consensus_conf = max(0.0, min(100.0, avg_conf * confidence_multiplier))
    size_pct = max(0.0, min(5.0, base_size * size_modifier))

    reasons = [f"{vote['provider']}={vote['decision']} ({vote['reasoning'][:70]})" for vote in agreeing_votes + opposing_votes]
    reason_text = "; ".join(reasons[:3])
    if agreement == "unanimous":
        summary = f"{decision} unanimous 3/3"
    elif agreement == "single":
        summary = f"{decision} single-model fallback"
    elif agreement == "majority_conflict":
        summary = f"{decision} 2/3 with direct opposition"
    elif agreement == "majority_two_model":
        summary = f"{decision} 2/2"
    else:
        summary = f"{decision} 2/3 majority"

    return JuryVerdict(
        symbol=symbol,
        decision=decision,
        size_pct=round(size_pct, 3),
        trail_pct=round(trail_pct, 3),
        reasoning=f"{summary}. {reason_text}".strip(),
        confidence=round(consensus_conf, 2),
        provider_used=",".join(providers_used),
        briefs=briefs,
        consensus_detail={
            "votes": vote_map,
            "total_models": len(providers_used),
            "agreement": agreement,
            "size_modifier": round(size_modifier, 3),
            "confidence": round(consensus_conf, 2),
            "base_size_pct": round(base_size, 3),
            "agreeing_models": [vote["provider"] for vote in agreeing_votes],
        },
    )


def _skip_verdict(
    symbol: str,
    briefs: Dict,
    providers_used: List[str],
    vote_map: Dict[str, str],
    total_models: int,
    agreement: str,
    reasoning: str,
) -> JuryVerdict:
    return JuryVerdict(
        symbol=symbol,
        decision="SKIP",
        size_pct=0,
        trail_pct=3.0,
        reasoning=reasoning,
        confidence=0.0,
        provider_used=",".join(providers_used),
        briefs=briefs,
        consensus_detail={
            "votes": vote_map,
            "total_models": total_models,
            "agreement": agreement,
            "size_modifier": 0.0,
            "confidence": 0.0,
        },
    )
