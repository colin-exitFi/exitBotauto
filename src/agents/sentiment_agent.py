"""
Sentiment Agent 🐦 - Social sentiment, StockTwits, X/Twitter trending, retail flow.
Uses Grok-4 (native X/Twitter access).
"""

from typing import Dict
from loguru import logger

from agents.base_agent import call_grok


DEFAULT_BRIEF = {
    "signal": "NEUTRAL",
    "crowd_direction": "with",
    "social_velocity": "flat",
    "contrarian_flag": False,
    "error": True,
}

PROMPT_TEMPLATE = """You are a SOCIAL SENTIMENT specialist inside Velox, an autonomous momentum trading engine.
Analyze ONLY social/retail sentiment. Other agents handle technicals, catalysts, macro.

SYMBOL: {symbol}
PRICE: ${price:.2f} ({change_pct:+.2f}% today)

STOCKTWITS:
- Sentiment score: {sentiment_score:.2f} (-1 bearish to +1 bullish)
- Trending: {trending}

X/TWITTER:
- Trending reason: {grok_x_reason}
- Sentiment: {grok_x_sentiment}

VOLUME: {volume_spike:.1f}x average (social buzz often correlates with volume)

Assess: Is the crowd driving this move? Is it contrarian to fade? Is social velocity accelerating?
Consider: Retail FOMO can fuel momentum, but extreme bullishness can signal a top.

Respond with ONLY valid JSON:
{{"signal": "BULLISH" or "BEARISH" or "NEUTRAL", "crowd_direction": "with" or "against", "social_velocity": "rising" or "falling" or "flat", "contrarian_flag": true/false}}"""


async def analyze(symbol: str, price: float, signals: Dict) -> Dict:
    """Run sentiment analysis. Returns structured brief."""
    try:
        prompt = PROMPT_TEMPLATE.format(
            symbol=symbol,
            price=price,
            change_pct=signals.get("change_pct", 0),
            sentiment_score=signals.get("sentiment_score", 0),
            trending=signals.get("trending", "unknown"),
            grok_x_reason=signals.get("grok_x_reason", "Not trending on X") or "Not trending on X",
            grok_x_sentiment=signals.get("grok_x_sentiment", "N/A") or "N/A",
            volume_spike=signals.get("volume_spike", 1.0),
        )

        result = await call_grok(prompt, max_tokens=300)
        if not result or "signal" not in result:
            logger.warning(f"Sentiment agent failed for {symbol} — using default")
            return {**DEFAULT_BRIEF, "symbol": symbol}

        brief = {
            "signal": result.get("signal", "NEUTRAL").upper(),
            "crowd_direction": result.get("crowd_direction", "with"),
            "social_velocity": result.get("social_velocity", "flat"),
            "contrarian_flag": bool(result.get("contrarian_flag", False)),
        }
        logger.debug(f"🐦 Sentiment {symbol}: {brief['signal']} crowd={brief['crowd_direction']} vel={brief['social_velocity']}")
        return brief

    except Exception as e:
        logger.error(f"Sentiment agent error for {symbol}: {e}")
        return {**DEFAULT_BRIEF, "symbol": symbol}
