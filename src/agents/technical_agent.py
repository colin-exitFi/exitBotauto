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

PROMPT_TEMPLATE = """You are a TECHNICAL specialist inside Velox.
Your role is to interpret evidence, not to force directional bias.

SYMBOL: {symbol} @ ${price:.2f}
TODAY'S CHANGE: {change_pct:+.1f}%
VOLUME vs AVG: {volume_spike:.1f}x
SPREAD: {spread_pct}%

EVIDENCE:
{price_action}

Interpretation framework:
- Identify whether momentum is intact, exhausting, or reversing.
- Weigh RSI, EMA structure, VWAP relation, day-range location, and volume acceleration together.
- Overbought/oversold can be continuation OR exhaustion depending on context; use nuance.
- If evidence conflicts, prefer HOLD over forced conviction.
- If levels are missing, acknowledge uncertainty via lower confidence.

Output mapping:
- BUY: evidence supports continued upside momentum.
- SELL: evidence supports downside/reversal pressure.
- HOLD: mixed or insufficient evidence.

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
        if signals.get("rsi_14") is not None:
            price_action_lines.append(f"RSI-14: {signals['rsi_14']}")
        if signals.get("rolling_vwap") is not None:
            price_action_lines.append(
                f"Rolling VWAP: ${signals['rolling_vwap']:.2f} ({signals.get('rolling_vwap_pct', 0):+.2f}% vs price)"
            )
        if signals.get("ema_9") is not None and signals.get("ema_20") is not None:
            price_action_lines.append(
                f"EMA 9/20: {signals['ema_9']:.2f} / {signals['ema_20']:.2f} ({signals.get('ema_signal', 'neutral')})"
            )
        if signals.get("ema_cross_bars_ago") is not None:
            price_action_lines.append(f"EMA crossover recency: {signals['ema_cross_bars_ago']} bars ago")
        if signals.get("range_pct") is not None:
            price_action_lines.append(f"Day range location: {signals['range_pct']:.1f}%")
        if signals.get("day_high") is not None and signals.get("day_low") is not None:
            price_action_lines.append(f"Derived day levels: high ${signals['day_high']:.2f}, low ${signals['day_low']:.2f}")
        if signals.get("vol_accel") is not None:
            price_action_lines.append(f"Volume acceleration: {signals['vol_accel']:.2f}x")
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
