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
- BUY if: Technical says BUY AND risk approves. That's it. Other agents add conviction for sizing.
- If Technical says BUY with >60% confidence — DEFAULT IS BUY unless Risk explicitly denies.
- "Overbought" is NOT a reason to skip. RSI 90+ means strong buying, which is what we want.
- "Post-move" is NOT a reason to skip. We trade momentum — the move IS the signal.
- "Resistance" is NOT a reason to skip. Breakouts through resistance = strongest momentum.
- SHORT if: Technical says SELL with >60% confidence AND catalyst or macro supports bearish
- SKIP ONLY if: Risk explicitly denies, OR Technical says HOLD/SELL on an upside play
- NEVER mention "overbought", "extended", "chasing", or "post-move" as reasons to skip a BUY signal
- Dead capital = missed opportunity. A 3% trailing stop loss is NOTHING vs a 20-50% missed runner.
- When in doubt, BUY at minimum size (0.5%) rather than SKIP.

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
