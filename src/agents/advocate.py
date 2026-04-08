"""
Advocate Agent -- Structural bias: BULLISH ON ACTION.

The Advocate's job is to find the highest-conviction play on every stock.
Dead capital earns nothing. "No play" is not acceptable for a moving stock.
Uses GPT (historically most willing to trade).
"""

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

from loguru import logger

from src.agents.base_agent import call_gpt


ADVOCATE_PROMPT = """You are the ADVOCATE inside Velox, an autonomous momentum trading engine.

YOUR STRUCTURAL BIAS: BULLISH ON ACTION. Capital sitting idle is losing money every second.
Your job is to find the HIGHEST-CONVICTION play on this stock RIGHT NOW.

CLASSIFIED SETUP: {mode} ({direction_constraint})
SYMBOL: {symbol} @ ${price:.2f}
TODAY'S MOVE: {change_pct:+.1f}%
VOLUME: {volume_context}
ENTRY QUALITY: {entry_quality}
RANGE POSITION: {range_pct:.0f}% of day range
MARKET REGIME: {market_regime}
SESSION: {session}
OVERNIGHT CONTEXT: {overnight_context}

CLASSIFIER REASONS: {classifier_reasons}
PLAY TRIGGER: {trigger}
PLAY INVALIDATION: {invalidation}

AGENT BRIEFS:
TECHNICAL: {technical}
SENTIMENT: {sentiment}
MACRO: {macro}

RULES:
- You MUST output a play. Every moving stock has a winning trade.
- Direction is constrained by the classifier: {direction_constraint}. Work within it.
- Size your conviction 0-100. Higher = more capital deployed.
- Think about: Is this accelerating or decelerating? Where are we in the move? What's the risk/reward?
- Conviction > 70 means you'd bet big. Conviction 40-70 means smaller but still worth taking. Below 40 means the thesis is weak but still a play.

Respond with ONLY valid JSON:
{{"direction": "BUY" or "SHORT", "conviction": 0-100, "reasoning": "2-3 sentence thesis", "entry_trigger": "what confirms entry", "hold_style": "intraday" or "swing", "size_posture": "reduced" or "normal" or "aggressive"}}"""


@dataclass
class AdvocateVerdict:
    symbol: str
    direction: str
    conviction: float
    reasoning: str
    entry_trigger: str
    hold_style: str
    size_posture: str
    provider: str = "gpt"
    timestamp: float = field(default_factory=time.time)
    error: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)


def _format_brief(brief: Dict) -> str:
    if not brief or brief.get("error"):
        return "Unavailable"
    parts = []
    for k, v in brief.items():
        if k in ("error", "symbol"):
            continue
        parts.append(f"{k}={v}")
    return ", ".join(parts[:8]) if parts else "No data"


def _format_volume_context(candidate: Dict) -> str:
    try:
        volume_spike = float(candidate.get("volume_spike", 0) or 0)
    except Exception:
        volume_spike = 0.0
    if volume_spike > 0:
        return f"{volume_spike:.1f}x average"

    try:
        avg_volume = float(candidate.get("avg_volume", candidate.get("average_volume", 0)) or 0)
    except Exception:
        avg_volume = 0.0
    try:
        live_volume = float(candidate.get("volume", candidate.get("minute_vol", 0)) or 0)
    except Exception:
        live_volume = 0.0

    if avg_volume > 0 and live_volume <= 0:
        return "average available, live volume pending"
    if avg_volume > 0:
        return "average available, live volume unclear"
    return "unknown"


async def evaluate(
    symbol: str,
    price: float,
    candidate: Dict,
    briefs: Dict,
) -> AdvocateVerdict:
    mode = str(candidate.get("setup_mode", "general_momentum_long") or "unknown")
    direction_constraint = str(candidate.get("direction_constraint", "long_only") or "none")

    prompt = ADVOCATE_PROMPT.format(
        symbol=symbol,
        price=price,
        mode=mode,
        direction_constraint=direction_constraint,
        change_pct=float(candidate.get("change_pct", 0) or 0),
        volume_context=_format_volume_context(candidate),
        entry_quality=candidate.get("entry_quality", "neutral"),
        range_pct=float(candidate.get("range_pct", 50) or 50),
        market_regime=candidate.get("market_regime", "mixed"),
        session=candidate.get("session_type", "regular"),
        overnight_context=str(candidate.get("overnight_context", "None") or "None")[:200],
        classifier_reasons=", ".join(candidate.get("classifier_reason_codes", [])[:5]),
        trigger=str(candidate.get("trigger", "not provided") or "not provided")[:150],
        invalidation=str(candidate.get("invalidation", "not provided") or "not provided")[:150],
        technical=_format_brief(briefs.get("technical", {})),
        sentiment=_format_brief(briefs.get("sentiment", {})),
        macro=_format_brief(briefs.get("macro", {})),
    )

    try:
        result = await call_gpt(prompt, max_tokens=400)
        if not result or not isinstance(result, dict):
            return _fallback_verdict(symbol, candidate)

        direction = str(result.get("direction", "BUY") or "BUY").upper()
        if direction not in {"BUY", "SHORT"}:
            direction = "BUY" if "long" in direction_constraint else "SHORT"

        if direction_constraint == "short_only" and direction != "SHORT":
            direction = "SHORT"
        elif direction_constraint == "long_only" and direction != "BUY":
            direction = "BUY"

        conviction = max(0.0, min(100.0, float(result.get("conviction", 50) or 50)))

        return AdvocateVerdict(
            symbol=symbol,
            direction=direction,
            conviction=conviction,
            reasoning=str(result.get("reasoning", "") or "")[:300],
            entry_trigger=str(result.get("entry_trigger", "") or "")[:200],
            hold_style=str(result.get("hold_style", "intraday") or "intraday"),
            size_posture=str(result.get("size_posture", "normal") or "normal"),
        )
    except Exception as e:
        logger.error(f"Advocate error for {symbol}: {e}")
        return _fallback_verdict(symbol, candidate)


def _fallback_verdict(symbol: str, candidate: Dict) -> AdvocateVerdict:
    constraint = str(candidate.get("direction_constraint", "long_only") or "long_only")
    direction = "SHORT" if constraint == "short_only" else "BUY"
    return AdvocateVerdict(
        symbol=symbol,
        direction=direction,
        conviction=45.0,
        reasoning="Advocate fallback: GPT unavailable, using classifier direction at reduced conviction",
        entry_trigger="classifier approved",
        hold_style="intraday",
        size_posture="reduced",
        error=True,
    )
