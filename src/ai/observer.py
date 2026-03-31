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

from config import settings
from src.ai.position_payload import sanitize_candidates_for_ai, sanitize_positions_for_ai
from src.signals.overnight_context import OvernightContext

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL = getattr(settings, "CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

from src.ai.mission import MISSION

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

SYSTEM_PROMPT += """

Interpretation rules for open positions:
- `position_origin="tracked_live_position"` means the bot has structured thesis/protection context for that live position even if it was reloaded from broker truth during a restart. Do not call these inherited leftovers, broker-synced baggage, random broker debris, or blind entries.
- `position_origin="broker_restored_live"` means the position exists at the broker but local thesis context is thin. Treat that as higher-risk context.
- If a position has setup/play metadata plus stop or trail protection, treat it as an intentional managed position unless the data explicitly says otherwise.
"""


class Observer:
    """Layer 1 AI: observes everything, flags issues, logs findings."""

    INTERVAL = 600  # 10 minutes during market hours
    INTERVAL_AFTER_HOURS = 1800  # 30 minutes after hours

    def __init__(self):
        self._client = None
        if settings.ANTHROPIC_API_KEY:
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        DATA_DIR.mkdir(exist_ok=True)

    @staticmethod
    def _runtime_ready(bot, now: float) -> bool:
        scanner = getattr(bot, "scanner", None)
        if not scanner:
            return True
        scan_stats = getattr(scanner, "_last_scan_stats", {}) or {}
        if float(scan_stats.get("last_completed_at") or 0):
            return True
        bot_start = float(getattr(bot, "start_time", 0.0) or 0.0)
        uptime_seconds = max(0.0, now - bot_start) if bot_start else 0.0
        return uptime_seconds >= 180.0

    async def run(self, bot) -> Optional[Dict]:
        """Run observation cycle. Returns findings dict or None."""
        now = time.time()
        # Throttle: 10 min during market hours, 30 min after hours
        from datetime import datetime
        try:
            import zoneinfo
            et_hour = datetime.now(zoneinfo.ZoneInfo("US/Eastern")).hour
        except Exception:
            et_hour = 12
        interval = self.INTERVAL if 4 <= et_hour < 20 else self.INTERVAL_AFTER_HOURS
        if now - self._last_run < interval:
            return None

        if not self._client:
            logger.warning("Observer: no Anthropic API key")
            return None
        if not self._runtime_ready(bot, now):
            logger.info("Observer warmup: waiting for first completed scan after startup")
            return None
        self._last_run = now

        try:
            # Gather state
            positions = sanitize_positions_for_ai(
                bot.entry_manager.get_positions() if bot.entry_manager else []
            )
            risk_status = bot.risk_manager.get_status() if bot.risk_manager else {}
            recent_trades = bot.exit_manager.get_history(20) if bot.exit_manager else []
            candidates = sanitize_candidates_for_ai(
                bot.scanner.get_cached_candidates() if bot.scanner else [],
                limit=5,
            )

            account = {}
            if bot.alpaca_client:
                account = bot.alpaca_client.get_account()

            from datetime import datetime as _dt
            try:
                import zoneinfo
                _et_now = _dt.now(zoneinfo.ZoneInfo("US/Eastern"))
                _now_et = _et_now.strftime("%Y-%m-%d %H:%M ET (%A)")
                _et_h, _et_m = _et_now.hour, _et_now.minute
            except Exception:
                _now_et = time.strftime('%Y-%m-%d %H:%M ET')
                _et_h, _et_m = 12, 0

            if (_et_h == 9 and _et_m >= 30) or (10 <= _et_h < 16):
                _session_label = "REGULAR HOURS — full liquidity, all order types"
            elif 4 <= _et_h < 9 or (_et_h == 9 and _et_m < 30):
                _session_label = "PRE-MARKET — extended hours trading active, limit orders only"
            elif 16 <= _et_h < 20:
                _session_label = "AFTER-HOURS — extended hours trading active, limit orders only"
            else:
                _session_label = "OVERNIGHT — market closed"
            overnight_bias = {}
            if hasattr(bot, "get_overnight_bias_context"):
                try:
                    overnight_bias = bot.get_overnight_bias_context()
                except Exception:
                    overnight_bias = {}
            overnight_summary = OvernightContext.format_summary(overnight_bias)

            prompt = f"""Current date/time: {_now_et}
SESSION: {_session_label}
OVERNIGHT INDEX CONTEXT: {overnight_summary}
Current state:

ACCOUNT:
{json.dumps(account, indent=2)}

RISK STATUS:
{json.dumps(risk_status, indent=2)}

OPEN POSITIONS ({len(positions)}):
{json.dumps(positions[:20], indent=2, default=str)}

RECENT TRADES ({len(recent_trades)}):
{json.dumps(recent_trades[-10:], indent=2, default=str)}

TOP SCANNER CANDIDATES:
{json.dumps(candidates, indent=2, default=str)}

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
            raw = json.loads(obs_file.read_text()) if obs_file.exists() else []
            history = raw if isinstance(raw, list) else []
        except Exception:
            history = []
        history.append(result)
        history = history[-100:]  # keep last 100
        obs_file.write_text(json.dumps(history, indent=2))


def _parse_json(text: str) -> dict:
    def _default(raw_text: str) -> dict:
        return {
            "raw": raw_text,
            "market_assessment": "",
            "position_health": [],
            "risk_flags": [],
            "what_working": [],
            "what_not_working": [],
            "overall_sentiment": "neutral",
            "urgency": "none",
        }

    text = str(text or "").strip()
    if not text:
        return _default("")
    if "```" in text:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        else:
            text = text.split("```")[1].split("```")[0]
    text = text.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end])
        else:
            return _default(text)
    if isinstance(parsed, list):
        parsed = next((row for row in reversed(parsed) if isinstance(row, dict)), None)
    if not isinstance(parsed, dict):
        return _default(text)
    parsed.setdefault("market_assessment", "")
    parsed.setdefault("position_health", [])
    parsed.setdefault("risk_flags", [])
    parsed.setdefault("what_working", [])
    parsed.setdefault("what_not_working", [])
    parsed.setdefault("overall_sentiment", "neutral")
    parsed.setdefault("urgency", "none")
    return parsed
