"""
Advisor (Layer 2) - Runs every 30 minutes.
Sees: observer output + full account state + game film.
Outputs: strategic recommendations (hold/sell/buy more, avoid sectors).
Advisory only — logs to data/advisor.json.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, Optional
from loguru import logger

import anthropic

from config import settings
from .trade_history import get_analytics

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL = getattr(settings, "CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

from src.ai.mission import MISSION

SYSTEM_PROMPT = f"""{MISSION}

You are Layer 2: The Advisor. You run every 30 minutes.

You see the Observer's findings, the full account state, and game film analytics.
Your job is to give STRATEGIC advice:
- Which positions to hold, trim, or exit
- Which sectors/symbols to avoid or favor
- Whether the bot should be more aggressive or conservative right now
- Specific parameter suggestions for the Tuner

RULES:
1. Every recommendation must cite specific data
2. Don't be wishy-washy — commit to a direction
3. Capital velocity > safety theater. Idle cash is lost opportunity.
4. If things are working, say "stay the course" — don't change for the sake of changing

Output JSON:
{{
    "strategy": "one sentence strategic direction",
    "position_advice": [
        {{"symbol": "AAPL", "action": "hold|trim|exit|add", "reason": "data-backed reason"}}
    ],
    "sector_bias": {{"favor": ["tech", "energy"], "avoid": ["retail"]}},
    "aggression_level": "increase|maintain|decrease",
    "parameter_suggestions": [
        {{"param": "TAKE_PROFIT_1_PCT", "current": 1.5, "suggested": 2.0, "reason": "winners are running past TP1"}}
    ],
    "key_insight": "most important thing the bot should know right now"
}}"""


class Advisor:
    """Layer 2 AI: strategic recommendations based on accumulated intelligence."""

    INTERVAL = 1800  # 30 minutes during market hours
    INTERVAL_AFTER_HOURS = 3600  # 60 minutes after hours

    def __init__(self):
        self._client = None
        if settings.ANTHROPIC_API_KEY:
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        DATA_DIR.mkdir(exist_ok=True)

    async def run(self, bot, observer_output: Optional[Dict] = None) -> Optional[Dict]:
        """Run advisory cycle."""
        now = time.time()
        from datetime import datetime
        try:
            import zoneinfo
            et_hour = datetime.now(zoneinfo.ZoneInfo("US/Eastern")).hour
        except Exception:
            et_hour = 12
        interval = self.INTERVAL if 4 <= et_hour < 20 else self.INTERVAL_AFTER_HOURS
        if now - self._last_run < interval:
            return None
        self._last_run = now

        if not self._client:
            return None

        try:
            positions = bot.entry_manager.get_positions() if bot.entry_manager else []
            risk_status = bot.risk_manager.get_status() if bot.risk_manager else {}
            trade_analytics = get_analytics()
            recent_trades = bot.exit_manager.get_history(30) if bot.exit_manager else []

            # Load game film if available
            game_film = {}
            gf_file = DATA_DIR / "game_film.json"
            if gf_file.exists():
                try:
                    game_film = json.loads(gf_file.read_text())
                except Exception:
                    pass

            prompt = f"""OBSERVER OUTPUT (latest):
{json.dumps(observer_output or {}, indent=2, default=str)}

RISK STATUS:
{json.dumps(risk_status, indent=2)}

OPEN POSITIONS ({len(positions)}):
{json.dumps(positions[:20], indent=2, default=str)}

TRADE ANALYTICS (all history):
{json.dumps(trade_analytics, indent=2, default=str)}

GAME FILM INSIGHTS:
{json.dumps(game_film, indent=2, default=str) if game_film else "No game film yet."}

RECENT TRADES:
{json.dumps(recent_trades[-15:], indent=2, default=str)}

CURRENT CONFIG:
- TAKE_PROFIT_1_PCT: {settings.TAKE_PROFIT_1_PCT}
- TAKE_PROFIT_2_PCT: {settings.TAKE_PROFIT_2_PCT}
- STOP_LOSS_PCT: {settings.STOP_LOSS_PCT}
- POSITION_SIZE_PCT: {settings.POSITION_SIZE_PCT}
- SCAN_INTERVAL_SECONDS: {settings.SCAN_INTERVAL_SECONDS}
- MIN_ENTRY_SENTIMENT: {settings.MIN_ENTRY_SENTIMENT}

What's your strategic advice?"""

            response = await asyncio.to_thread(
                self._client.messages.create,
                model=MODEL,
                max_tokens=1500,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            result = _parse_json(text)
            self._last_output = result

            self._save(result)
            logger.info(f"🎯 Advisor: {result.get('strategy', 'no strategy')[:80]}")
            return result

        except Exception as e:
            logger.error(f"Advisor failed: {e}")
            return None

    def get_last_output(self) -> Optional[Dict]:
        return self._last_output

    def _save(self, result: Dict):
        result["timestamp"] = time.time()
        adv_file = DATA_DIR / "advisor.json"
        try:
            history = json.loads(adv_file.read_text()) if adv_file.exists() else []
        except Exception:
            history = []
        history.append(result)
        history = history[-50:]
        adv_file.write_text(json.dumps(history, indent=2))


def _parse_json(text: str) -> dict:
    text = text.strip()
    if "```" in text:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        else:
            text = text.split("```")[1].split("```")[0]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        return {"raw": text}
