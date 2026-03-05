"""
Observer (Layer 1) - Runs every 10 minutes.
Sees: all positions, account balance, recent trades, market conditions.
Outputs: market assessment, position health, risk flags.
Logs to data/observations.json.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import anthropic

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL = "claude-sonnet-4-5-20250929"

from ai.mission import MISSION

SYSTEM_PROMPT = f"""{MISSION}

You are Layer 1: The Observer. You run every 10 minutes and see EVERYTHING.

Your job:
1. Assess overall market conditions (trending up, down, choppy, volatile)
2. Evaluate health of each open position (healthy, at risk, dying)
3. Flag risk concerns (concentration, correlation, overexposure)
4. Note what's working and what isn't
5. Provide actionable observations for the Advisor layer

Output JSON:
{{
    "market_assessment": "one sentence on current conditions",
    "position_health": [
        {{"symbol": "AAPL", "status": "healthy|at_risk|dying", "note": "reason"}}
    ],
    "risk_flags": ["list of concerns"],
    "what_working": ["patterns that are profitable"],
    "what_not_working": ["patterns that are losing"],
    "overall_sentiment": "bullish|bearish|neutral",
    "urgency": "none|low|medium|high"
}}"""


class Observer:
    """Layer 1 AI: observes everything, flags issues, logs findings."""

    INTERVAL = 600  # 10 minutes

    def __init__(self):
        self._client = None
        if settings.ANTHROPIC_API_KEY:
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        DATA_DIR.mkdir(exist_ok=True)

    async def run(self, bot) -> Optional[Dict]:
        """Run observation cycle. Returns findings dict or None."""
        now = time.time()
        if now - self._last_run < self.INTERVAL:
            return None
        self._last_run = now

        if not self._client:
            logger.warning("Observer: no Anthropic API key")
            return None

        try:
            # Gather state
            positions = bot.entry_manager.get_positions() if bot.entry_manager else []
            risk_status = bot.risk_manager.get_status() if bot.risk_manager else {}
            recent_trades = bot.exit_manager.get_history(20) if bot.exit_manager else []
            candidates = bot.scanner.get_cached_candidates() if bot.scanner else []

            account = {}
            if bot.alpaca_client:
                account = bot.alpaca_client.get_account()

            prompt = f"""Current state at {time.strftime('%H:%M ET')}:

ACCOUNT:
{json.dumps(account, indent=2)}

RISK STATUS:
{json.dumps(risk_status, indent=2)}

OPEN POSITIONS ({len(positions)}):
{json.dumps(positions[:20], indent=2, default=str)}

RECENT TRADES ({len(recent_trades)}):
{json.dumps(recent_trades[-10:], indent=2, default=str)}

TOP SCANNER CANDIDATES:
{json.dumps(candidates[:5], indent=2, default=str)}

Analyze this state. What do you see?"""

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

            # Save to disk
            self._save(result)
            logger.info(f"🔭 Observer: {result.get('market_assessment', 'no assessment')[:80]}")
            return result

        except Exception as e:
            logger.error(f"Observer failed: {e}")
            return None

    def get_last_output(self) -> Optional[Dict]:
        return self._last_output

    def _save(self, result: Dict):
        result["timestamp"] = time.time()
        obs_file = DATA_DIR / "observations.json"
        try:
            history = json.loads(obs_file.read_text()) if obs_file.exists() else []
        except Exception:
            history = []
        history.append(result)
        history = history[-100:]  # keep last 100
        obs_file.write_text(json.dumps(history, indent=2))


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
