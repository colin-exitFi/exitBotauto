"""
Jury 🗳️ - Synthesizes all 5 agent briefs into a final trade decision.
ONE AI call (Claude) to make the final call.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from loguru import logger

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
            technical=fmt(briefs.get("technical", {})),
            sentiment=fmt(briefs.get("sentiment", {})),
            catalyst=fmt(briefs.get("catalyst", {})),
            risk=fmt(briefs.get("risk", {})),
            macro=fmt(briefs.get("macro", {})),
        )

        result = None
        provider_used = None
        provider_chain = [
            ("claude", call_claude),
            ("gpt", call_gpt),
            ("grok", call_grok),
        ]
        for provider_name, caller in provider_chain:
            result = await caller(prompt, max_tokens=400)
            if result and "decision" in result:
                provider_used = provider_name
                break

        if not result or "decision" not in result:
            logger.warning(f"Jury failed for {symbol} — defaulting to SKIP")
            return JuryVerdict(
                symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
                reasoning="Jury AI chain failed (claude->gpt->grok)",
                provider_used=provider_used or "",
                briefs=briefs,
            )

        # Check if risk agent denied — hard override
        risk_brief = briefs.get("risk", {})
        if risk_brief and not risk_brief.get("approved", True) and not risk_brief.get("error"):
            if result.get("decision") in ("BUY", "SHORT"):
                logger.info(f"🛡️ Jury overridden by Risk Agent for {symbol}: {risk_brief.get('reasoning', '')}")
                return JuryVerdict(
                    symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
                    reasoning=f"Risk agent denied: {risk_brief.get('reasoning', 'portfolio risk too high')}",
                    provider_used=provider_used or "",
                    briefs=briefs,
                )

        decision = result.get("decision", "SKIP").upper()
        if decision not in ("BUY", "SHORT", "SKIP"):
            decision = "SKIP"

        size_pct = max(0, min(5.0, float(result.get("size_pct", 0))))
        trail_pct = max(1.0, min(5.0, float(result.get("trail_pct", 3.0))))

        # Cap size_pct by risk agent's max
        if risk_brief and risk_brief.get("max_size_pct"):
            size_pct = min(size_pct, float(risk_brief["max_size_pct"]))

        verdict = JuryVerdict(
            symbol=symbol,
            decision=decision,
            size_pct=size_pct,
            trail_pct=trail_pct,
            reasoning=str(result.get("reasoning", ""))[:300],
            confidence=max(0, min(100, float(result.get("confidence", 0)))),
            provider_used=provider_used or "",
            briefs=briefs,
        )

        logger.info(
            f"🗳️ Jury verdict for {symbol}: {verdict.decision} "
            f"size={verdict.size_pct}% trail={verdict.trail_pct}% "
            f"conf={verdict.confidence}% provider={provider_used or '?'} — {verdict.reasoning[:100]}"
        )
        return verdict

    except Exception as e:
        logger.error(f"Jury error for {symbol}: {e}")
        return JuryVerdict(
            symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
            reasoning=f"Jury exception: {e}", briefs=briefs,
        )
