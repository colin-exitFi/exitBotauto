"""
Technical Agent 📊 - Rule-based momentum scoring.
No AI call — deterministic, fast, and consistent.
A momentum bot should BUY momentum, not fight it.
"""

from typing import Dict
from loguru import logger


DEFAULT_BRIEF = {
    "signal": "HOLD",
    "confidence": 0,
    "key_levels": {"support": 0, "resistance": 0},
    "momentum": "decelerating",
    "timeframe": "hours",
}


async def analyze(symbol: str, price: float, signals: Dict) -> Dict:
    """Rule-based technical momentum scoring. No AI call needed."""
    try:
        change_pct = signals.get("change_pct", 0)
        volume_spike = signals.get("volume_spike", 0)
        spread_pct = signals.get("spread_pct", 0)
        prev_close = signals.get("prev_close", 0)

        # ── MOMENTUM SCORE (0-100) ──
        # Change component: +5% = 25pts, +10% = 50pts, +20% = 75pts, +30%+ = 90pts
        abs_change = abs(change_pct)
        if abs_change >= 30:
            change_score = 90
        elif abs_change >= 20:
            change_score = 75
        elif abs_change >= 10:
            change_score = 50
        elif abs_change >= 5:
            change_score = 25
        else:
            change_score = abs_change * 5  # 0-25 for 0-5%

        # Volume component: 1x = 0pts, 3x = 30pts, 5x+ = 50pts
        vol_score = min(volume_spike * 10, 50) if volume_spike > 0 else 0

        # Spread penalty: >2% spread = illiquid, reduce confidence
        spread_penalty = max(0, (spread_pct - 1.0) * 10) if spread_pct > 1.0 else 0

        confidence = min(100, max(0, int(change_score + vol_score - spread_penalty)))

        # ── SIGNAL DIRECTION ──
        if change_pct > 5 and confidence >= 40:
            signal = "BUY"
            momentum = "accelerating" if change_pct > 15 else "decelerating"
        elif change_pct < -5 and confidence >= 40:
            signal = "SELL"
            momentum = "accelerating" if change_pct < -15 else "decelerating"
        else:
            signal = "HOLD"
            momentum = "decelerating"

        # Key levels (simple)
        support = round(prev_close, 2) if prev_close > 0 else round(price * 0.95, 2)
        resistance = round(price * 1.05, 2)

        # Timeframe based on magnitude
        timeframe = "days" if abs_change > 20 else "hours" if abs_change > 10 else "minutes"

        brief = {
            "signal": signal,
            "confidence": confidence,
            "key_levels": {"support": support, "resistance": resistance},
            "momentum": momentum,
            "timeframe": timeframe,
        }
        logger.debug(f"📊 Technical {symbol}: {signal} conf={confidence}% chg={change_pct:+.1f}% vol={volume_spike:.1f}x")
        return brief

    except Exception as e:
        logger.error(f"Technical agent error for {symbol}: {e}")
        return {**DEFAULT_BRIEF, "symbol": symbol}
