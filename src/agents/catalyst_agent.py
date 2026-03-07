"""
Catalyst Agent 🔬 - Earnings, FDA, EDGAR filings, congressional trades, unusual options, news.
Uses Perplexity (research-focused, has web search).
"""

from typing import Dict
from loguru import logger

from src.agents.base_agent import call_perplexity


DEFAULT_BRIEF = {
    "catalyst_type": "none",
    "magnitude": 0,
    "timing": "pre",
    "edge": "No catalyst identified",
    "error": True,
}

PROMPT_TEMPLATE = """You are a CATALYST RESEARCH specialist inside Velox, an autonomous momentum trading engine.
Your job: identify WHY this stock is moving and whether the catalyst has more legs.
Other agents handle technicals, sentiment, macro — you focus on the FUNDAMENTAL DRIVER.

SYMBOL: {symbol}
PRICE: ${price:.2f} ({change_pct:+.2f}% today)
VOLUME: {volume_spike:.1f}x average

KNOWN SIGNALS:
- Earnings: {earnings_info}
- Pharma/FDA catalyst: {pharma_info}
- News headlines: {news}
- Unusual options activity: {options_info}
- Congressional trades: {congress_info}
- SEC filings: {edgar_info}
- Insider activity: {insider_info}
- Economic calendar: {economic_calendar}
- Human intel: {human_intel}

Search for the latest news about {symbol} in the last 24 hours. What is driving this move?
Is the catalyst front-loaded (sell the news) or does it have legs (accumulation phase)?

Respond with ONLY valid JSON:
{{"catalyst_type": "earnings" or "fda" or "filing" or "insider" or "options" or "none", "magnitude": 1-10, "timing": "pre" or "post" or "imminent", "edge": "brief description of the trading edge"}}"""


async def analyze(symbol: str, price: float, signals: Dict) -> Dict:
    """Run catalyst research. Returns structured brief."""
    try:
        news_list = signals.get("news_headlines", signals.get("news", []))
        if isinstance(news_list, list):
            news_str = "\n".join(f"- {h}" for h in news_list[:5]) or "No recent news"
        else:
            news_str = str(news_list) or "No recent news"

        prompt = PROMPT_TEMPLATE.format(
            symbol=symbol,
            price=price,
            change_pct=signals.get("change_pct", 0),
            volume_spike=signals.get("volume_spike", 1.0),
            earnings_info=signals.get("earnings_date", "None") or "None",
            pharma_info=signals.get("pharma_drug", "None") if signals.get("pharma_signal") else "None",
            news=news_str,
            options_info=signals.get("unusual_options", "None") or "None",
            congress_info=signals.get("congress_trades", "None") or "None",
            edgar_info=signals.get("edgar_filings", "None") or "None",
            insider_info=signals.get("insider_activity", "None") or "None",
            economic_calendar=signals.get("economic_calendar", "None") or "None",
            human_intel=signals.get("human_intel", "None") or "None",
        )

        result = await call_perplexity(prompt, max_tokens=400)
        if not result or "catalyst_type" not in result:
            logger.warning(f"Catalyst agent failed for {symbol} — using default")
            return {**DEFAULT_BRIEF, "symbol": symbol}

        brief = {
            "catalyst_type": result.get("catalyst_type", "none"),
            "magnitude": max(0, min(10, int(result.get("magnitude", 0)))),
            "timing": result.get("timing", "pre"),
            "edge": str(result.get("edge", ""))[:200],
        }
        logger.debug(f"🔬 Catalyst {symbol}: type={brief['catalyst_type']} mag={brief['magnitude']} timing={brief['timing']}")
        return brief

    except Exception as e:
        logger.error(f"Catalyst agent error for {symbol}: {e}")
        return {**DEFAULT_BRIEF, "symbol": symbol}
