"""
Jury 🗳️ - Synthesizes all 5 agent briefs into a final trade decision.
ONE AI call (Claude) to make the final call.
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional
from loguru import logger

from agents.base_agent import call_claude


@dataclass
class JuryVerdict:
    symbol: str
    decision: str  # "BUY", "SHORT", "SKIP"
    size_pct: float  # position size as % of equity
    trail_pct: float  # trailing stop %
    reasoning: str
    confidence: float = 0.0
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
            "timestamp": self.timestamp,
        }


PROMPT_TEMPLATE = """You are the JURY — the final decision maker inside Velox.
{mission}

You receive briefs from 5 specialized agents. Synthesize them into ONE trade decision.

SYMBOL: {symbol} @ ${price:.2f}

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
- SKIP ONLY if: Risk explicitly denies, OR the stock has tiny volume (<1x avg), OR the move is clearly over (price reversing against the trend).
- If some agents are unavailable, MAKE THE CALL with what you have. 3 agents is enough. Don't skip just because sentiment or catalyst is offline.
- "Decelerating momentum" alone is NOT a reason to skip. Stocks don't go straight up — they consolidate and continue. If price is still up big on volume, the trend is intact.
- We have trailing stops at 3%. Maximum downside per trade is 3%. The cost of a wrong entry is small. The cost of missing a runner is infinite.

SIZING:
- size_pct: 0.5% (speculative) to 3% (high conviction) of equity
- trail_pct: 1.5% (tight, lock in gains) to 4% (wide, let it run)

Respond with ONLY valid JSON:
{{"decision": "BUY" or "SHORT" or "SKIP", "size_pct": number, "trail_pct": number, "reasoning": "brief synthesis of why", "confidence": 0-100}}"""


async def deliberate(symbol: str, price: float, briefs: Dict) -> JuryVerdict:
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

        from ai.mission import MISSION_SHORT
        prompt = PROMPT_TEMPLATE.format(
            mission=MISSION_SHORT,
            symbol=symbol,
            price=price,
            technical=fmt(briefs.get("technical", {})),
            sentiment=fmt(briefs.get("sentiment", {})),
            catalyst=fmt(briefs.get("catalyst", {})),
            risk=fmt(briefs.get("risk", {})),
            macro=fmt(briefs.get("macro", {})),
        )

        result = await call_claude(prompt, max_tokens=400)

        if not result or "decision" not in result:
            logger.warning(f"Jury failed for {symbol} — defaulting to SKIP")
            return JuryVerdict(
                symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
                reasoning="Jury AI call failed", briefs=briefs,
            )

        # Check if risk agent denied — hard override
        risk_brief = briefs.get("risk", {})
        if risk_brief and not risk_brief.get("approved", True) and not risk_brief.get("error"):
            if result.get("decision") in ("BUY", "SHORT"):
                logger.info(f"🛡️ Jury overridden by Risk Agent for {symbol}: {risk_brief.get('reasoning', '')}")
                return JuryVerdict(
                    symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
                    reasoning=f"Risk agent denied: {risk_brief.get('reasoning', 'portfolio risk too high')}",
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
            briefs=briefs,
        )

        logger.info(
            f"🗳️ Jury verdict for {symbol}: {verdict.decision} "
            f"size={verdict.size_pct}% trail={verdict.trail_pct}% "
            f"conf={verdict.confidence}% — {verdict.reasoning[:100]}"
        )
        return verdict

    except Exception as e:
        logger.error(f"Jury error for {symbol}: {e}")
        return JuryVerdict(
            symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
            reasoning=f"Jury exception: {e}", briefs=briefs,
        )
