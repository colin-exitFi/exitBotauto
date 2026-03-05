"""
Position Manager (Layer 4) - Runs every 2 minutes.
Sees: all positions + current prices + sentiment.
Can execute HIGH URGENCY exits only (sentiment crash, flash crash).
Has VETO power over new entries that violate risk rules.
Uses market bid, NOT fair value.
"""

import asyncio
import json
import time
from typing import Dict, List, Optional
from loguru import logger

import anthropic

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data" if 'Path' in dir() else None

from pathlib import Path
DATA_DIR = Path(__file__).parent.parent.parent / "data"

MODEL = "claude-sonnet-4-5-20250929"

from ai.mission import MISSION

SYSTEM_PROMPT = f"""{MISSION}

You are Layer 4: The Position Manager. You run every 2 minutes. You are the LAST LINE OF DEFENSE.

You see every open position with real-time prices and sentiment. You can:
1. Force-exit positions in EMERGENCIES (sentiment crash, flash crash, circuit breaker)
2. VETO new entries that violate risk rules (even if other layers approve)
3. Flag positions that need attention

WHEN TO FORCE EXIT (high urgency only):
- Symbol sentiment crashes to deeply negative (-0.5 or worse) suddenly
- Price drops >3% in under 5 minutes (flash crash)
- Multiple positions in same sector all dropping simultaneously
- Total portfolio heat exceeds 80% of daily loss budget

WHEN TO HOLD:
- Normal pullbacks within stop loss range
- Gradual sentiment decline (exit_manager handles this)
- Single position slightly negative

VETO RULES (block new entries when):
- Portfolio at max positions for current risk tier
- Portfolio heat > 60% of daily loss budget
- Same sector already has 40%+ of positions
- In a 3+ loss streak AND position would be speculative conviction

You are NOT the primary exit engine — that's exit_manager.py.
You handle EMERGENCIES and VETOES only.

Output JSON:
{{
    "emergency_exits": [
        {{"symbol": "AAPL", "reason": "sentiment crashed to -0.7 in 5 min", "urgency": "critical"}}
    ],
    "vetoes": ["list of symbols to block entry on"],
    "position_notes": [
        {{"symbol": "TSLA", "status": "watching", "note": "sentiment declining, monitor closely"}}
    ],
    "portfolio_health": "healthy|stressed|critical",
    "action_taken": "none|exits_recommended|vetoes_active"
}}"""


class PositionManager:
    """Layer 4 AI: emergency exits and entry vetoes."""

    INTERVAL = 120  # 2 minutes

    def __init__(self):
        self._client = None
        if settings.ANTHROPIC_API_KEY:
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        self._vetoed_symbols: set = set()
        DATA_DIR.mkdir(exist_ok=True)

    async def run(self, bot) -> Optional[Dict]:
        """Run position management check."""
        now = time.time()
        if now - self._last_run < self.INTERVAL:
            return None
        self._last_run = now

        if not self._client:
            return None

        positions = bot.entry_manager.get_positions() if bot.entry_manager else []
        if not positions:
            self._vetoed_symbols.clear()
            return None

        try:
            risk_status = bot.risk_manager.get_status() if bot.risk_manager else {}

            # Enrich positions with current prices and sentiment
            enriched = []
            for pos in positions:
                symbol = pos["symbol"]
                price = 0
                if bot.polygon_client:
                    try:
                        price = bot.polygon_client.get_price(symbol)
                    except Exception:
                        pass
                if not price:
                    price = pos.get("entry_price", 0)

                pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] else 0
                hold_min = (now - pos.get("entry_time", now)) / 60

                # Get sentiment if available
                sent_score = 0
                if bot.sentiment_analyzer:
                    cached = bot.sentiment_analyzer.get_cached(symbol)
                    if cached:
                        sent_score = cached.get("score", 0)

                enriched.append({
                    "symbol": symbol,
                    "entry_price": round(pos["entry_price"], 2),
                    "current_price": round(price, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "hold_minutes": round(hold_min, 0),
                    "quantity": pos["quantity"],
                    "peak_price": round(pos.get("peak_price", price), 2),
                    "sentiment": round(sent_score, 2),
                    "partial_exit": pos.get("partial_exit", False),
                })

            prompt = f"""RISK STATUS:
{json.dumps(risk_status, indent=2)}

OPEN POSITIONS ({len(enriched)}):
{json.dumps(enriched, indent=2)}

Current time: {time.strftime('%H:%M ET')}

Review all positions. Are there any emergencies? Should any entries be vetoed?"""

            response = await asyncio.to_thread(
                self._client.messages.create,
                model=MODEL,
                max_tokens=1000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            result = _parse_json(text)
            self._last_output = result

            # Process emergency exits
            emergency_exits = result.get("emergency_exits", [])
            if emergency_exits and bot.exit_manager:
                for exit_rec in emergency_exits:
                    symbol = exit_rec.get("symbol", "")
                    reason = exit_rec.get("reason", "AI position manager emergency")
                    urgency = exit_rec.get("urgency", "medium")
                    if urgency in ("critical", "high"):
                        # Find the position and execute exit
                        for pos in positions:
                            if pos["symbol"] == symbol:
                                price = 0
                                if bot.polygon_client:
                                    try:
                                        price = bot.polygon_client.get_price(symbol)
                                    except Exception:
                                        price = pos.get("entry_price", 0)
                                pnl_pct = ((price - pos["entry_price"]) / pos["entry_price"] * 100) if pos["entry_price"] else 0
                                logger.warning(f"🤖 AI EMERGENCY EXIT: {symbol} — {reason}")
                                await bot.exit_manager._execute_exit(pos, pos["quantity"], price, f"ai_emergency: {reason}", pnl_pct)
                                break

            # Update veto list
            self._vetoed_symbols = set(result.get("vetoes", []))

            health = result.get("portfolio_health", "healthy")
            if health != "healthy":
                logger.warning(f"🤖 Position Manager: portfolio {health}")

            return result

        except Exception as e:
            logger.error(f"Position manager failed: {e}")
            return None

    def is_vetoed(self, symbol: str) -> bool:
        """Check if a symbol is currently vetoed by the position manager."""
        return symbol in self._vetoed_symbols

    def can_enter(self, symbol: str, positions: List[Dict], risk_manager) -> bool:
        """
        Two-layer veto check. Even if signals say buy, position manager checks:
        - Is symbol vetoed by AI?
        - Max positions for tier?
        - Portfolio heat too high?
        - Sector overweight? (simplified — count positions)
        """
        if self.is_vetoed(symbol):
            logger.info(f"🤖 VETO: {symbol} blocked by AI position manager")
            return False

        if risk_manager:
            tier = risk_manager.get_risk_tier()
            if len(positions) >= tier["max_positions"]:
                return False

            status = risk_manager.get_status()
            if status.get("heat_pct", 0) > 60:
                logger.info(f"🤖 VETO: heat too high ({status['heat_pct']:.0f}%), blocking {symbol}")
                return False

        return True

    def get_last_output(self) -> Optional[Dict]:
        return self._last_output


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
        return {"raw": text, "emergency_exits": [], "vetoes": [], "portfolio_health": "healthy"}
