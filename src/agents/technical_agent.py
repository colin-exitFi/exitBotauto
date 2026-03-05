"""
Technical Agent 📊 - AI-powered momentum analysis.
Uses Claude Sonnet for nuanced technical reads.
"""

from typing import Dict
from loguru import logger

from agents.base_agent import call_claude


DEFAULT_BRIEF = {
    "signal": "HOLD",
    "confidence": 0,
    "key_levels": {"support": 0, "resistance": 0},
    "momentum": "decelerating",
    "timeframe": "hours",
}

PROMPT_TEMPLATE = """You are a TECHNICAL specialist inside Velox, a momentum trading engine ($25K → $5M goal).

SYMBOL: {symbol} @ ${price:.2f}
TODAY'S CHANGE: {change_pct:+.1f}%
VOLUME vs AVG: {volume_spike:.1f}x
SPREAD: {spread_pct}%

{price_action}

You are a MOMENTUM analyst, not a value analyst. Key principles:
- A stock up 30% with volume is a momentum BUY if the move is FRESH and ACCELERATING
- A stock up 30% that's been fading for an hour is a HOLD — the move already happened
- High RSI is fine for momentum. "Overbought" is not a reason to skip.
- Volume confirms conviction. Low volume moves are suspect.
- The question is: "Is this move still happening, or did we miss it?"

BUY = momentum is fresh/accelerating, still room to run
SELL = breaking down, fading hard, no bid support  
HOLD = move already happened, momentum fading, or no clear direction

Respond with ONLY valid JSON:
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0-100, "key_levels": {{"support": number, "resistance": number}}, "momentum": "accelerating" or "decelerating", "timeframe": "minutes" or "hours" or "days"}}"""


async def analyze(symbol: str, price: float, signals: Dict) -> Dict:
    """AI-powered technical momentum analysis."""
    try:
        price_action_lines = []
        if signals.get("change_pct"):
            price_action_lines.append(f"Today: {signals['change_pct']:+.1f}%")
        if signals.get("prev_close"):
            price_action_lines.append(f"Prev close: ${signals['prev_close']:.2f}")
        if signals.get("high"):
            price_action_lines.append(f"Day high: ${signals['high']:.2f}")
        if signals.get("low"):
            price_action_lines.append(f"Day low: ${signals['low']:.2f}")
        if signals.get("rsi"):
            price_action_lines.append(f"RSI: {signals['rsi']}")
        if signals.get("vwap_relation"):
            price_action_lines.append(f"vs VWAP: {signals['vwap_relation']}")
        price_action = "\n".join(price_action_lines) or "Limited data"

        prompt = PROMPT_TEMPLATE.format(
            symbol=symbol,
            price=price,
            change_pct=signals.get("change_pct", 0),
            volume_spike=signals.get("volume_spike", 1.0),
            spread_pct=signals.get("spread_pct", "N/A"),
            price_action=price_action,
        )

        result = await call_claude(prompt, max_tokens=300)
        if not result or "signal" not in result:
            logger.warning(f"Technical agent failed for {symbol} — using default")
            return {**DEFAULT_BRIEF, "symbol": symbol}

        brief = {
            "signal": result.get("signal", "HOLD").upper(),
            "confidence": max(0, min(100, int(result.get("confidence", 0)))),
            "key_levels": {
                "support": float(result.get("key_levels", {}).get("support", 0)),
                "resistance": float(result.get("key_levels", {}).get("resistance", 0)),
            },
            "momentum": result.get("momentum", "decelerating"),
            "timeframe": result.get("timeframe", "hours"),
        }
        logger.debug(f"📊 Technical {symbol}: {brief['signal']} conf={brief['confidence']}% mom={brief['momentum']}")
        return brief

    except Exception as e:
        logger.error(f"Technical agent error for {symbol}: {e}")
        return {**DEFAULT_BRIEF, "symbol": symbol}
