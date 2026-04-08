"""
Adversary Agent -- Structural bias: SKEPTICAL.

The Adversary reviews the Advocate's thesis and searches for the fatal flaw.
It can VETO a trade, but only with a specific, data-backed reason.
"Uncertainty" or "I'm not sure" is NOT a valid veto.
Uses Claude (historically most conservative, best as a canary).
"""

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, Optional

from loguru import logger

from src.agents.base_agent import call_claude


ADVERSARY_PROMPT = """You are the ADVERSARY inside Velox, an autonomous momentum trading engine.

YOUR STRUCTURAL BIAS: SKEPTICAL. Your job is to protect capital from bad trades.
But you are NOT allowed to be generically cautious. You must find a SPECIFIC flaw or ALLOW the trade.

THE ADVOCATE'S THESIS:
The Advocate wants to {direction} {symbol} @ ${price:.2f} with {conviction}% conviction.
Their reasoning: {advocate_reasoning}
Their entry trigger: {advocate_trigger}

MARKET CONTEXT:
- Setup mode: {mode}
- Direction constraint: {direction_constraint}
- Today's move: {change_pct:+.1f}%
- Volume: {volume_context}
- Market regime: {market_regime}
- Entry quality: {entry_quality}
- Range position: {range_pct:.0f}% of day range

AGENT BRIEFS:
TECHNICAL: {technical}
RISK: {risk}
MACRO: {macro}

YOUR RULES:
- VETO only if you find a SPECIFIC, DATA-BACKED fatal flaw. Examples of valid vetoes:
  * "Stock is at 99% of day range with decelerating volume -- reversal imminent"
  * "This stock has been halted 5 times today -- too volatile for controlled entry"
  * "Macro regime is strongly against this direction"
- These are NOT valid vetoes:
  * "I'm not sure about this setup"
  * "Recent trades have been losing"
  * "The data is incomplete"
  * "I'd want to see more confirmation"
- Missing or stale volume context alone is NOT a valid fatal flaw.
- If you cannot find a specific kill reason, you MUST set veto=false.
- risk_score 0-100: how risky is this trade? Higher = more risk = smaller position size.
  50 is normal risk. Above 70 means significant concern. Below 30 means low risk.

Respond with ONLY valid JSON:
{{"veto": true or false, "kill_reason": "specific reason or empty string", "risk_score": 0-100, "reasoning": "brief analysis"}}"""


@dataclass
class AdversaryVerdict:
    symbol: str
    veto: bool
    kill_reason: str
    risk_score: float
    reasoning: str
    provider: str = "claude"
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
    advocate_verdict: Dict,
) -> AdversaryVerdict:
    mode = str(candidate.get("setup_mode", "unknown") or "unknown")
    direction_constraint = str(candidate.get("direction_constraint", "none") or "none")

    prompt = ADVERSARY_PROMPT.format(
        symbol=symbol,
        price=price,
        direction=str(advocate_verdict.get("direction", "BUY")),
        conviction=float(advocate_verdict.get("conviction", 50)),
        advocate_reasoning=str(advocate_verdict.get("reasoning", "No thesis provided"))[:300],
        advocate_trigger=str(advocate_verdict.get("entry_trigger", "Not specified"))[:200],
        mode=mode,
        direction_constraint=direction_constraint,
        change_pct=float(candidate.get("change_pct", 0) or 0),
        volume_context=_format_volume_context(candidate),
        market_regime=candidate.get("market_regime", "mixed"),
        entry_quality=candidate.get("entry_quality", "neutral"),
        range_pct=float(candidate.get("range_pct", 50) or 50),
        technical=_format_brief(briefs.get("technical", {})),
        risk=_format_brief(briefs.get("risk", {})),
        macro=_format_brief(briefs.get("macro", {})),
    )

    try:
        result = await call_claude(prompt, max_tokens=400)
        if not result or not isinstance(result, dict):
            return _fallback_verdict(symbol)

        veto = bool(result.get("veto", False))
        kill_reason = str(result.get("kill_reason", "") or "").strip()

        if veto and not kill_reason:
            veto = False
            kill_reason = ""

        risk_score = max(0.0, min(100.0, float(result.get("risk_score", 50) or 50)))

        return AdversaryVerdict(
            symbol=symbol,
            veto=veto,
            kill_reason=kill_reason[:300],
            risk_score=risk_score,
            reasoning=str(result.get("reasoning", "") or "")[:300],
        )
    except Exception as e:
        logger.error(f"Adversary error for {symbol}: {e}")
        return _fallback_verdict(symbol)


def _fallback_verdict(symbol: str) -> AdversaryVerdict:
    return AdversaryVerdict(
        symbol=symbol,
        veto=False,
        kill_reason="",
        risk_score=50.0,
        reasoning="Adversary fallback: Claude unavailable, defaulting to ALLOW at medium risk",
        error=True,
    )
