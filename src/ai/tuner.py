"""
Tuner (Layer 3) - Runs every 30 minutes.
Sees: advisor output + current config + recent performance.
Can adjust trading parameters within HARD BOUNDS.
Persists config changes to data/config_state.json.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, Optional
from loguru import logger

import anthropic

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings
from .trade_history import get_analytics

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL = "claude-sonnet-4-5-20250929"
CONFIG_STATE_FILE = DATA_DIR / "config_state.json"

MISSION = (
    "You are an AI trading advisor for exitBotauto. Mission: grow $1,000 to $1,000,000 "
    "through capital velocity — stacking consistent 1-2% wins, taking profit aggressively, "
    "cutting losers fast. You have full autonomy within hard safety bounds. "
    "Every dollar of dead capital is a missed opportunity."
)

# Hard bounds — tuner CANNOT exceed these
TUNABLE_PARAMS = {
    "STOP_LOSS_PCT":            {"min": 0.5, "max": 3.0,  "type": float},
    "TAKE_PROFIT_1_PCT":        {"min": 0.5, "max": 5.0,  "type": float},
    "TAKE_PROFIT_2_PCT":        {"min": 1.0, "max": 10.0, "type": float},
    "TRAILING_STOP_PCT":        {"min": 0.2, "max": 2.0,  "type": float},
    "POSITION_SIZE_PCT":        {"min": 1.0, "max": 10.0, "type": float},
    "MAX_CONCURRENT_POSITIONS": {"min": 3,   "max": 15,   "type": int},
    "SCAN_INTERVAL_SECONDS":    {"min": 60,  "max": 600,  "type": int},
    "MIN_ENTRY_SENTIMENT":      {"min": -0.5,"max": 0.8,  "type": float},
    "MAX_HOLD_HOURS":           {"min": 1,   "max": 24,   "type": float},
}

SYSTEM_PROMPT = f"""{MISSION}

You are Layer 3: The Tuner. You run every 30 minutes. You can CHANGE trading parameters.

TUNABLE PARAMETERS (with hard bounds you CANNOT exceed):
- STOP_LOSS_PCT (0.5-3.0): Hard stop loss percentage
- TAKE_PROFIT_1_PCT (0.5-5.0): First take profit level (sell half)
- TAKE_PROFIT_2_PCT (1.0-10.0): Second take profit (sell rest)
- TRAILING_STOP_PCT (0.2-2.0): Trailing stop from peak
- POSITION_SIZE_PCT (1.0-10.0): Position size as % of equity
- MAX_CONCURRENT_POSITIONS (3-15): Max positions at once
- SCAN_INTERVAL_SECONDS (60-600): How often to scan for opportunities
- MIN_ENTRY_SENTIMENT (-0.5 to 0.8): Minimum sentiment to enter
- MAX_HOLD_HOURS (1-24): Maximum hold time

RULES:
1. Maximum 3 changes per run
2. Every change must cite specific performance data
3. If win rate >60% and P&L positive, be conservative with changes
4. If losing money, be more aggressive
5. Track what previous changes did — don't oscillate
6. Capital velocity is king: prefer changes that INCREASE trading activity

Output JSON:
{{
    "changes": [
        {{"param": "STOP_LOSS_PCT", "value": 1.5, "reason": "data-backed reason"}}
    ],
    "reasoning": "one sentence on overall tuning direction",
    "no_change_reason": "why no changes (if applicable)"
}}"""


class Tuner:
    """Layer 3 AI: adjusts trading parameters within hard bounds."""

    INTERVAL = 1800  # 30 minutes

    def __init__(self):
        self._client = None
        if settings.ANTHROPIC_API_KEY:
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        self._change_history: list = []
        DATA_DIR.mkdir(exist_ok=True)
        # Load persisted config state on startup
        self._load_config_state()

    async def run(self, bot, advisor_output: Optional[Dict] = None) -> Optional[Dict]:
        """Run tuning cycle. Returns changes applied or None."""
        now = time.time()
        if now - self._last_run < self.INTERVAL:
            return None
        self._last_run = now

        if not self._client:
            return None

        try:
            risk_status = bot.risk_manager.get_status() if bot.risk_manager else {}
            trade_analytics = get_analytics()
            recent_trades = bot.exit_manager.get_history(30) if bot.exit_manager else []

            current_config = {
                "STOP_LOSS_PCT": settings.STOP_LOSS_PCT,
                "TAKE_PROFIT_1_PCT": settings.TAKE_PROFIT_1_PCT,
                "TAKE_PROFIT_2_PCT": settings.TAKE_PROFIT_2_PCT,
                "TRAILING_STOP_PCT": settings.TRAILING_STOP_PCT,
                "POSITION_SIZE_PCT": settings.POSITION_SIZE_PCT,
                "MAX_CONCURRENT_POSITIONS": settings.MAX_CONCURRENT_POSITIONS,
                "SCAN_INTERVAL_SECONDS": settings.SCAN_INTERVAL_SECONDS,
                "MIN_ENTRY_SENTIMENT": settings.MIN_ENTRY_SENTIMENT,
                "MAX_HOLD_HOURS": settings.MAX_HOLD_HOURS,
            }

            prompt = f"""ADVISOR OUTPUT (latest):
{json.dumps(advisor_output or {}, indent=2, default=str)}

CURRENT CONFIG:
{json.dumps(current_config, indent=2)}

RISK STATUS:
{json.dumps(risk_status, indent=2)}

TRADE ANALYTICS:
{json.dumps(trade_analytics, indent=2, default=str)}

RECENT TRADES:
{json.dumps(recent_trades[-15:], indent=2, default=str)}

PREVIOUS TUNER CHANGES (track what worked):
{json.dumps(self._change_history[-10:], indent=2, default=str)}

What parameters should change?"""

            response = await asyncio.to_thread(
                self._client.messages.create,
                model=MODEL,
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            result = _parse_json(text)

            changes = result.get("changes", [])
            if not changes:
                logger.info(f"🔧 Tuner: No changes — {result.get('no_change_reason', 'performance OK')[:60]}")
                self._last_output = result
                return result

            # Validate and apply changes (max 3)
            applied = []
            for c in changes[:3]:
                param = c.get("param", "")
                value = c.get("value")
                reason = c.get("reason", "")

                if param not in TUNABLE_PARAMS:
                    logger.warning(f"Tuner: unknown param {param}")
                    continue

                bounds = TUNABLE_PARAMS[param]
                typed_value = bounds["type"](value)
                typed_value = max(bounds["min"], min(bounds["max"], typed_value))

                old_value = getattr(settings, param, None)
                if old_value is None:
                    continue

                # Apply to settings module
                setattr(settings, param, typed_value)
                applied.append({
                    "param": param,
                    "old": old_value,
                    "new": typed_value,
                    "reason": reason,
                    "timestamp": time.time(),
                })
                logger.info(f"🔧 Tuner: {param}: {old_value} → {typed_value} ({reason[:60]})")

            if applied:
                self._change_history.extend(applied)
                self._save_config_state()

            result["applied"] = applied
            self._last_output = result
            self._save(result)
            return result

        except Exception as e:
            logger.error(f"Tuner failed: {e}")
            return None

    def get_last_output(self) -> Optional[Dict]:
        return self._last_output

    def _save_config_state(self):
        """Persist current config to survive restarts."""
        state = {}
        for param in TUNABLE_PARAMS:
            state[param] = getattr(settings, param, None)
        state["_history"] = self._change_history[-20:]
        state["_saved_at"] = time.time()
        try:
            CONFIG_STATE_FILE.write_text(json.dumps(state, indent=2))
            logger.debug("Config state saved")
        except Exception as e:
            logger.warning(f"Failed to save config state: {e}")

    def _load_config_state(self):
        """Load persisted config state and apply to settings."""
        if not CONFIG_STATE_FILE.exists():
            return
        try:
            state = json.loads(CONFIG_STATE_FILE.read_text())
            applied = []
            for param, bounds in TUNABLE_PARAMS.items():
                if param in state and state[param] is not None:
                    typed_value = bounds["type"](state[param])
                    typed_value = max(bounds["min"], min(bounds["max"], typed_value))
                    old = getattr(settings, param, None)
                    if old != typed_value:
                        setattr(settings, param, typed_value)
                        applied.append(f"{param}: {old} → {typed_value}")
            self._change_history = state.get("_history", [])
            if applied:
                logger.info(f"Loaded tuner config: {', '.join(applied)}")
        except Exception as e:
            logger.warning(f"Failed to load config state: {e}")

    def _save(self, result: Dict):
        result["timestamp"] = time.time()
        tuner_file = DATA_DIR / "tuner.json"
        try:
            history = json.loads(tuner_file.read_text()) if tuner_file.exists() else []
        except Exception:
            history = []
        history.append(result)
        history = history[-50:]
        tuner_file.write_text(json.dumps(history, indent=2))


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
