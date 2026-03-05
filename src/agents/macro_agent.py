"""
Macro Agent 🌍 - VIX, SPY/QQQ/DIA, sector rotation, market regime, pre-market futures.
Uses GPT-5.2.
"""

from typing import Dict
from loguru import logger

from agents.base_agent import call_gpt


DEFAULT_BRIEF = {
    "regime": "choppy",
    "sector_flow": "mixed",
    "bias": "neutral",
    "headwinds": ["macro_agent_unavailable"],
    "error": True,
}

PROMPT_TEMPLATE = """You are a MACRO/MARKET REGIME specialist inside Velox, an autonomous momentum trading engine.
Analyze the broad market environment. Other agents handle individual stock technicals, sentiment, catalysts.

MARKET INDICES:
- SPY (S&P 500): {spy_info}
- QQQ (Nasdaq): {qqq_info}
- DIA (Dow): {dia_info}
- VIX (Volatility): {vix_info}

SECTOR ROTATION:
{sector_rotation}

PROPOSED TRADE: {symbol} ({direction})

What is the current market regime? Is the macro environment supportive for this trade direction?
Consider: risk-on/risk-off sentiment, sector rotation trends, volatility regime.

Respond with ONLY valid JSON:
{{"regime": "risk-on" or "risk-off" or "choppy", "sector_flow": "brief description of where money is flowing", "bias": "long" or "short" or "neutral", "headwinds": ["list", "of", "macro", "concerns"]}}"""


async def analyze(symbol: str, price: float, signals: Dict,
                  direction: str = "BUY") -> Dict:
    """Run macro analysis. Returns structured brief."""
    try:
        # Pull index data from signals if available
        spy_info = signals.get("spy_info", "N/A")
        qqq_info = signals.get("qqq_info", "N/A")
        dia_info = signals.get("dia_info", "N/A")
        vix_info = signals.get("vix_info", "N/A")
        sector_rotation = signals.get("sector_rotation", "No sector rotation data available")

        prompt = PROMPT_TEMPLATE.format(
            spy_info=spy_info,
            qqq_info=qqq_info,
            dia_info=dia_info,
            vix_info=vix_info,
            sector_rotation=sector_rotation,
            symbol=symbol,
            direction=direction,
        )

        result = await call_gpt(prompt, max_tokens=400)
        if not result or "regime" not in result:
            logger.warning(f"Macro agent failed for {symbol} — using default")
            return {**DEFAULT_BRIEF, "symbol": symbol}

        brief = {
            "regime": result.get("regime", "choppy"),
            "sector_flow": str(result.get("sector_flow", "mixed"))[:200],
            "bias": result.get("bias", "neutral"),
            "headwinds": result.get("headwinds", []),
        }
        logger.debug(f"🌍 Macro {symbol}: regime={brief['regime']} bias={brief['bias']}")
        return brief

    except Exception as e:
        logger.error(f"Macro agent error for {symbol}: {e}")
        return {**DEFAULT_BRIEF, "symbol": symbol}
