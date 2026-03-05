"""
Technical Agent 📊 - AI-powered momentum analysis.
Uses Claude Sonnet for nuanced technical reads.
"""

from typing import Dict
from loguru import logger

from src.agents.base_agent import call_claude


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

You are a MOMENTUM analyst inside an AGGRESSIVE trading bot. Key principles:
- A stock up 10%+ with volume above average is a BUY. Period. The trailing stop handles risk.
- A stock up 30% is STILL a BUY if volume is strong. Momentum runners go to 50%, 100%, 200%.
- "Overbought", "extended", "overheated" are NOT reasons to avoid. RSI 90 = strong buying pressure = momentum.
- The ONLY reason to say SELL is if price is actively FALLING (negative change today) with volume.
- The ONLY reason to say HOLD is if volume is dead (<1x average) — no conviction either way.
- If price is UP and volume is UP, say BUY. Don't overthink it.
- We have 3% trailing stops. Max loss per trade is 3%. Missing a 50% runner costs way more.

BUY = price up with volume. DEFAULT for any stock up 5%+ with 1x+ volume.
SELL = price down significantly with volume (for short opportunities)
HOLD = volume dead, no directional conviction

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
