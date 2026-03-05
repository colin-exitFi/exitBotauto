"""
Technical Agent 📊 - Price action, volume, RSI, VWAP, ATR, support/resistance.
Uses Claude Sonnet for focused technical analysis.
"""

import asyncio
from typing import Dict, Optional
from loguru import logger

from agents.base_agent import call_claude


# ── Default brief on failure ──────────────────────────────────────
DEFAULT_BRIEF = {
    "signal": "HOLD",
    "confidence": 0,
    "key_levels": {"support": 0, "resistance": 0},
    "momentum": "decelerating",
    "timeframe": "hours",
    "error": True,
}

PROMPT_TEMPLATE = """You are a TECHNICAL ANALYSIS specialist inside Velox, an aggressive momentum trading engine.
Analyze ONLY the technicals. Ignore fundamentals, news, sentiment — other agents handle those.

SYMBOL: {symbol}
PRICE: ${price:.2f}
TODAY'S CHANGE: {change_pct:+.2f}%
VOLUME: {volume_spike:.1f}x average

TECHNICALS:
- RSI (14): {rsi}
- vs VWAP: {vwap_relation}
- ATR (14): {atr}
- Bid/Ask Spread: {spread_pct}%

RECENT PRICE ACTION:
{price_action}

CRITICAL CONTEXT — THIS IS A MOMENTUM BOT:
- We CHASE momentum. High RSI is GOOD (means strong buying pressure), not a sell signal.
- Volume spikes >3x = institutional interest = BUY signal.
- Change >5% with volume = momentum entry. We use trailing stops to manage risk, not entry timing.
- HOLD is for when there's genuinely no direction. If it's moving with volume, pick a side.
- Bias toward BUY on breakouts. Bias toward SELL on breakdowns. HOLD = no momentum either way.
- Dead capital in cash is worse than a stopped-out trade. Favor action.

Respond with ONLY valid JSON:
{{"signal": "BUY" or "SELL" or "HOLD", "confidence": 0-100, "key_levels": {{"support": number, "resistance": number}}, "momentum": "accelerating" or "decelerating", "timeframe": "minutes" or "hours" or "days"}}"""


async def analyze(symbol: str, price: float, signals: Dict) -> Dict:
    """Run technical analysis. Returns structured brief."""
    try:
        # Build price action summary from available data
        price_action_lines = []
        if signals.get("change_pct"):
            price_action_lines.append(f"Today: {signals['change_pct']:+.2f}%")
        if signals.get("prev_close"):
            price_action_lines.append(f"Prev close: ${signals['prev_close']:.2f}")
        if signals.get("high"):
            price_action_lines.append(f"Day high: ${signals['high']:.2f}")
        if signals.get("low"):
            price_action_lines.append(f"Day low: ${signals['low']:.2f}")
        price_action = "\n".join(price_action_lines) or "Limited price action data available"

        prompt = PROMPT_TEMPLATE.format(
            symbol=symbol,
            price=price,
            change_pct=signals.get("change_pct", 0),
            volume_spike=signals.get("volume_spike", 1.0),
            rsi=signals.get("rsi", "N/A"),
            vwap_relation=signals.get("vwap_relation", "N/A"),
            atr=signals.get("atr", "N/A"),
            spread_pct=signals.get("spread_pct", "N/A"),
            price_action=price_action,
        )

        result = await call_claude(prompt, max_tokens=400)
        if not result or "signal" not in result:
            logger.warning(f"Technical agent failed for {symbol} — using default")
            return {**DEFAULT_BRIEF, "symbol": symbol}

        # Validate and normalize
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
