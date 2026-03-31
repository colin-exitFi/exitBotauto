"""
Tuner (Layer 3) - Runs every 30 minutes.
Sees: advisor output + current config + recent performance.
Can adjust trading parameters within HARD BOUNDS.
Persists config changes to data/config_state.json.
"""

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

import anthropic

from config import settings
from .trade_history import get_analytics

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL = getattr(settings, "CLAUDE_MODEL", "claude-sonnet-4-5-20250929")
CONFIG_STATE_FILE = DATA_DIR / "config_state.json"
IMPACT_STATE_FILE = DATA_DIR / "tuner_impact.json"

from src.ai.mission import MISSION

# Hard bounds — tuner CANNOT exceed these
TUNABLE_PARAMS = {
    "STOP_LOSS_PCT":            {"min": 1.0, "max": 2.0,  "type": float},
    "TAKE_PROFIT_1_PCT":        {"min": 1.0, "max": 3.0,  "type": float},
    "TAKE_PROFIT_2_PCT":        {"min": 2.0, "max": 6.0,  "type": float},
    "TRAILING_STOP_PCT":        {"min": 0.5, "max": 1.5,  "type": float},
    "POSITION_SIZE_PCT":        {"min": 2.0, "max": 5.0,  "type": float},
    "MAX_CONCURRENT_POSITIONS": {"min": 5,   "max": 10,   "type": int},
    "SCAN_INTERVAL_SECONDS":    {"min": 60,  "max": 300,  "type": int},
    "MIN_ENTRY_SENTIMENT":      {"min": -0.5,"max": 0.3,  "type": float},
    "MAX_HOLD_HOURS":           {"min": 2,   "max": 8,    "type": float},
}

# Per-parameter cooldown: once changed, cannot change again for this many seconds
PARAM_COOLDOWN_SECONDS = 7200  # 2 hours

# Max total changes per trading day
MAX_DAILY_CHANGES = 6

# Max step: parameter can move at most 25% of its range per change
MAX_STEP_FRACTION = 0.25

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
- MIN_ENTRY_SENTIMENT (-0.5 to 0.45): Minimum sentiment to enter
- MAX_HOLD_HOURS (1-24): Maximum hold time

RULES:
1. Maximum 2 changes per run, maximum 6 changes per day
2. Every change must cite specific performance data from at least 10 trades
3. If win rate >60% and P&L positive, make ZERO changes — the system is working
4. Each parameter has a 2-hour cooldown after being changed — you cannot change the same parameter twice in 2 hours
5. Changes are clamped to 25% of the parameter's range per step — no dramatic swings
6. DO NOT OSCILLATE. If you changed a parameter in one direction last time, do NOT change it back unless you have 20+ trades of evidence that the change hurt
7. Capital velocity matters but STABILITY matters more. A bot that thrashes between settings every 30 minutes will never learn what works
8. When in doubt, make ZERO changes. The default settings are designed to work.

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

    INTERVAL = 1800  # 30 minutes during market hours
    INTERVAL_AFTER_HOURS = 3600  # 60 minutes after hours

    def __init__(self):
        self._client = None
        self._enabled = bool(getattr(settings, "TUNER_ENABLED", False))
        if settings.ANTHROPIC_API_KEY:
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        self._change_history: list = []
        self._impact_history: list = []
        self._param_last_changed: Dict[str, float] = {}
        self._daily_change_count: int = 0
        self._daily_change_date: str = ""
        DATA_DIR.mkdir(exist_ok=True)
        # Tuner is opt-in during live trading; do not let stale mutations silently
        # override runtime defaults unless explicitly enabled.
        if self._enabled and bool(getattr(settings, "TUNER_LOAD_PERSISTED_CONFIG", False)):
            self._load_config_state()
        elif CONFIG_STATE_FILE.exists():
            logger.info("🔧 Tuner: persisted config ignored (disabled or load flag off)")
        self._load_impact_state()

    async def run(self, bot, advisor_output: Optional[Dict] = None) -> Optional[Dict]:
        """Run tuning cycle. Returns changes applied or None."""
        if not self._enabled:
            return None

        min_trades_to_tune = max(10, int(getattr(settings, "TUNER_MIN_TRADES_TO_TUNE", 40) or 40))
        # HARD LOCK: Don't tune until we have enough REAL trades with game film data
        # Fail-closed: if we can't verify trade count, DO NOT TUNE
        try:
            analytics = get_analytics()
            total = int(analytics.get("clean_total_trades", analytics.get("total_trades", 0)) or 0)
        except Exception as e:
            logger.warning(f"🔧 Tuner: LOCKED — cannot verify trade count ({e}), refusing to tune")
            return None
        if total < min_trades_to_tune:
            logger.info(
                f"🔧 Tuner: LOCKED — need {min_trades_to_tune - total} more trades before tuning (have {total})"
            )
            return None

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
            impact_updates = await self.measure_impact()
            risk_status = bot.risk_manager.get_status() if bot.risk_manager else {}
            trade_analytics = analytics
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

IMPACT HISTORY (what worked and what didn't):
{json.dumps(self._impact_history[-10:], indent=2, default=str)}

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

            # Reset daily counter if new day
            from datetime import datetime
            try:
                import zoneinfo
                today_str = datetime.now(zoneinfo.ZoneInfo("US/Eastern")).strftime("%Y-%m-%d")
            except Exception:
                today_str = datetime.now().strftime("%Y-%m-%d")
            if self._daily_change_date != today_str:
                self._daily_change_count = 0
                self._daily_change_date = today_str

            # Validate and apply changes (max 2 per run, max 6 per day)
            applied = []
            for c in changes[:2]:
                param = c.get("param", "")
                value = c.get("value")
                reason = c.get("reason", "")

                if param not in TUNABLE_PARAMS:
                    logger.warning(f"Tuner: unknown param {param}")
                    continue

                # Daily cap
                if self._daily_change_count >= MAX_DAILY_CHANGES:
                    logger.info(f"🔧 Tuner: DAILY CAP reached ({MAX_DAILY_CHANGES} changes today) — no more changes")
                    break

                # Per-param cooldown
                last_changed = self._param_last_changed.get(param, 0)
                if time.time() - last_changed < PARAM_COOLDOWN_SECONDS:
                    remaining = int(PARAM_COOLDOWN_SECONDS - (time.time() - last_changed))
                    logger.info(f"🔧 Tuner: {param} on cooldown ({remaining}s remaining) — skipping")
                    continue

                bounds = TUNABLE_PARAMS[param]
                typed_value = bounds["type"](value)
                typed_value = max(bounds["min"], min(bounds["max"], typed_value))

                old_value = getattr(settings, param, None)
                if old_value is None:
                    continue

                # Max step size: can't move more than 25% of the range in one change
                param_range = bounds["max"] - bounds["min"]
                max_step = param_range * MAX_STEP_FRACTION
                if abs(typed_value - old_value) > max_step:
                    direction = 1 if typed_value > old_value else -1
                    typed_value = bounds["type"](old_value + direction * max_step)
                    typed_value = max(bounds["min"], min(bounds["max"], typed_value))
                    logger.info(f"🔧 Tuner: {param} step clamped to {typed_value} (max step {max_step:.2f})")

                if typed_value == old_value:
                    continue

                if self._was_hurtful_change(param, typed_value):
                    logger.info(f"🔧 Tuner: skipping {param} → {typed_value}; same change recently hurt")
                    continue

                snapshot = self._snapshot_performance()
                setattr(settings, param, typed_value)
                self._param_last_changed[param] = time.time()
                self._daily_change_count += 1
                change = ParameterChange(
                    param=param,
                    old_value=old_value,
                    new_value=typed_value,
                    reason=reason,
                    timestamp=time.time(),
                    snapshot_win_rate=float(snapshot.get("win_rate", 0) or 0),
                    snapshot_pnl=float(snapshot.get("total_pnl", 0) or 0),
                    snapshot_sharpe=float(snapshot.get("sharpe", 0) or 0),
                    snapshot_trade_count=int(snapshot.get("trade_count", 0) or 0),
                )
                change_row = asdict(change)
                applied.append(
                    {
                        "param": param,
                        "old": old_value,
                        "new": typed_value,
                        "reason": reason,
                        "timestamp": change.timestamp,
                    }
                )
                self._impact_history.append(change_row)
                logger.info(f"🔧 Tuner: {param}: {old_value} → {typed_value} ({reason[:60]})")

            if applied:
                self._change_history.extend(applied)
                self._save_config_state()
                self._save_impact_state()

            result["applied"] = applied
            result["impact_updates"] = impact_updates
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
        if not self._enabled or not bool(getattr(settings, "TUNER_LOAD_PERSISTED_CONFIG", False)):
            return
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

    def _load_impact_state(self):
        if not IMPACT_STATE_FILE.exists():
            return
        try:
            raw = json.loads(IMPACT_STATE_FILE.read_text())
            self._impact_history = raw if isinstance(raw, list) else []
        except Exception as e:
            logger.warning(f"Failed to load tuner impact state: {e}")

    def _save_impact_state(self):
        try:
            IMPACT_STATE_FILE.write_text(json.dumps(self._impact_history[-100:], indent=2, default=str))
        except Exception as e:
            logger.warning(f"Failed to save tuner impact state: {e}")

    def _snapshot_performance(self) -> Dict:
        analytics = get_analytics()
        recent = analytics.get("recent_20", {}) or {}
        overall = analytics.get("overall", {}) or {}
        return {
            "win_rate": float(analytics.get("clean_win_rate", analytics.get("win_rate", overall.get("win_rate_pct", 0) / 100.0)) or 0),
            "total_pnl": float(analytics.get("clean_pnl", analytics.get("total_pnl", overall.get("total_pnl", 0))) or 0),
            "sharpe": float(analytics.get("sharpe_ratio", overall.get("sharpe_ratio", 0)) or 0),
            "trade_count": int(analytics.get("clean_total_trades", analytics.get("total_trades", 0)) or 0),
            "recent_20_win_rate": float(recent.get("clean_win_rate_pct", recent.get("win_rate_pct", 0)) or 0),
            "recent_20_pnl": float(recent.get("clean_pnl", recent.get("pnl", 0)) or 0),
        }

    async def measure_impact(self) -> List[Dict]:
        snapshot = self._snapshot_performance()
        measured = []
        changed = False
        for row in self._impact_history:
            if not isinstance(row, dict):
                continue
            if row.get("impact_measured_at"):
                continue
            trades_since = int(snapshot.get("trade_count", 0) or 0) - int(row.get("snapshot_trade_count", 0) or 0)
            if trades_since < 15:
                continue

            post_win_rate = float(snapshot.get("win_rate", 0) or 0)
            post_pnl = float(snapshot.get("total_pnl", 0) or 0)
            post_sharpe = float(snapshot.get("sharpe", 0) or 0)
            pre_win_rate = float(row.get("snapshot_win_rate", 0) or 0)
            pre_pnl = float(row.get("snapshot_pnl", 0) or 0)
            pre_sharpe = float(row.get("snapshot_sharpe", 0) or 0)

            verdict = "neutral"
            if post_win_rate > pre_win_rate and post_pnl > pre_pnl and post_sharpe >= pre_sharpe:
                verdict = "helped"
            elif post_win_rate < (pre_win_rate - 0.05) or (post_pnl < pre_pnl and post_sharpe < pre_sharpe):
                verdict = "hurt"

            row["post_win_rate"] = post_win_rate
            row["post_pnl"] = post_pnl
            row["post_sharpe"] = post_sharpe
            row["post_trade_count"] = int(snapshot.get("trade_count", 0) or 0)
            row["impact_measured_at"] = time.time()
            row["verdict"] = verdict
            row["trades_since_change"] = trades_since
            measured.append(row)
            changed = True

        if changed:
            self._save_impact_state()
        return measured

    def _was_hurtful_change(self, param: str, new_value: Any) -> bool:
        for row in reversed(self._impact_history):
            if not isinstance(row, dict):
                continue
            if str(row.get("param", "")) != str(param):
                continue
            if row.get("verdict") != "hurt":
                continue
            if row.get("new_value") == new_value:
                return True
        return False

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


@dataclass
class ParameterChange:
    param: str
    old_value: Any
    new_value: Any
    reason: str
    timestamp: float
    snapshot_win_rate: float
    snapshot_pnl: float
    snapshot_sharpe: float
    snapshot_trade_count: int
    post_win_rate: Optional[float] = None
    post_pnl: Optional[float] = None
    post_sharpe: Optional[float] = None
    post_trade_count: Optional[int] = None
    impact_measured_at: Optional[float] = None
    verdict: Optional[str] = None


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
