"""
Exit Agent 🚪 - Continuously monitors open positions, adjusts trailing stops.
Runs on its own loop (every 2-3 minutes), receives briefs from other agents.
Uses Claude Sonnet.
"""

import asyncio
import time
from typing import Dict, List, Optional
from loguru import logger

from agents.base_agent import call_claude

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


DEFAULT_ACTION = {
    "action": "HOLD",
    "new_trail_pct": None,
    "reasoning": "Default hold — no adjustment needed",
}

PROMPT_TEMPLATE = """You are an EXIT MANAGEMENT specialist inside Velox, an autonomous momentum trading engine.
Your ONLY job: manage trailing stops on open positions. You can tighten, widen, or trigger immediate exit.

POSITION:
- Symbol: {symbol}
- Side: {side}
- Entry: ${entry_price:.2f}
- Current: ${current_price:.2f}
- P&L: {pnl_pct:+.1f}% (${pnl:.2f})
- Current trailing stop: {trail_pct}%
- Hold time: {hold_time}
- Peak price: ${peak_price:.2f}

AGENT BRIEFS (from other specialists):
- Technical: {technical_brief}
- Sentiment: {sentiment_brief}
- Catalyst: {catalyst_brief}
- Risk: {risk_brief}
- Macro: {macro_brief}

RULES:
- HOLD: keep current trail, position is fine
- TIGHTEN: reduce trail % to lock in more profit (e.g. 3% → 1.5% when up big)
- WIDEN: increase trail % to give it more room if momentum is strong but volatile
- EXIT_NOW: immediate market sell — only for extreme circumstances (crash, halt, thesis broken)

Trail range: 0.5% (very tight) to 5.0% (very wide). Current default is 3%.
If we're up >5%, consider tightening. If momentum is accelerating, consider widening to ride it.

Respond with ONLY valid JSON:
{{"action": "HOLD" or "TIGHTEN" or "WIDEN" or "EXIT_NOW", "new_trail_pct": number or null, "reasoning": "brief explanation"}}"""


class ExitAgent:
    """Manages the exit agent loop — monitors positions and adjusts trailing stops."""

    def __init__(self, broker=None, entry_manager=None, risk_manager=None):
        self.broker = broker
        self.entry_manager = entry_manager
        self.risk_manager = risk_manager
        self._running = False
        self._last_briefs: Dict[str, Dict] = {}  # symbol -> latest agent briefs
        self._last_check: Dict[str, float] = {}  # symbol -> last check timestamp
        self._task: Optional[asyncio.Task] = None

    def update_briefs(self, symbol: str, briefs: Dict):
        """Update the latest agent briefs for a position. Called by orchestrator after entry."""
        self._last_briefs[symbol] = {
            "technical": briefs.get("technical", {}),
            "sentiment": briefs.get("sentiment", {}),
            "catalyst": briefs.get("catalyst", {}),
            "risk": briefs.get("risk", {}),
            "macro": briefs.get("macro", {}),
            "updated_at": time.time(),
        }

    async def start(self):
        """Start the exit agent monitoring loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("🚪 Exit Agent started")

    async def stop(self):
        """Stop the exit agent loop."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🚪 Exit Agent stopped")

    async def _loop(self):
        """Main monitoring loop — runs every 2 minutes."""
        while self._running:
            try:
                positions = self.entry_manager.get_positions() if self.entry_manager else []
                for pos in positions:
                    symbol = pos.get("symbol", "")
                    if not symbol:
                        continue
                    # Don't check more than once per 2 minutes per position
                    last = self._last_check.get(symbol, 0)
                    if time.time() - last < 120:
                        continue
                    self._last_check[symbol] = time.time()

                    action = await self._evaluate_position(pos)
                    if action and action.get("action") != "HOLD":
                        await self._execute_action(symbol, pos, action)

                # Clean up briefs for positions we no longer hold
                held_symbols = {p.get("symbol") for p in positions}
                stale = [s for s in self._last_briefs if s not in held_symbols]
                for s in stale:
                    del self._last_briefs[s]
                    self._last_check.pop(s, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Exit agent loop error: {e}")

            await asyncio.sleep(120)  # 2 minutes

    async def _evaluate_position(self, pos: Dict) -> Optional[Dict]:
        """Evaluate a single position using AI."""
        symbol = pos.get("symbol", "")
        entry_price = pos.get("entry_price", 0)
        if entry_price <= 0:
            return None

        # Get current price
        current_price = entry_price  # fallback
        try:
            if self.broker:
                alpaca_positions = self.broker.get_positions()
                for ap in alpaca_positions:
                    if ap.get("symbol") == symbol:
                        current_price = float(ap.get("current_price", entry_price))
                        break
        except Exception:
            pass

        side = pos.get("side", "long")
        if side == "short":
            pnl = (entry_price - current_price) * pos.get("quantity", 0)
            pnl_pct = ((entry_price - current_price) / entry_price) * 100
        else:
            pnl = (current_price - entry_price) * pos.get("quantity", 0)
            pnl_pct = ((current_price - entry_price) / entry_price) * 100

        hold_seconds = time.time() - pos.get("entry_time", time.time())
        if hold_seconds < 3600:
            hold_time = f"{hold_seconds / 60:.0f} minutes"
        else:
            hold_time = f"{hold_seconds / 3600:.1f} hours"

        briefs = self._last_briefs.get(symbol, {})

        prompt = PROMPT_TEMPLATE.format(
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            current_price=current_price,
            pnl_pct=pnl_pct,
            pnl=pnl,
            trail_pct=pos.get("trail_pct", 3.0),
            hold_time=hold_time,
            peak_price=pos.get("peak_price", entry_price),
            technical_brief=_brief_summary(briefs.get("technical", {})),
            sentiment_brief=_brief_summary(briefs.get("sentiment", {})),
            catalyst_brief=_brief_summary(briefs.get("catalyst", {})),
            risk_brief=_brief_summary(briefs.get("risk", {})),
            macro_brief=_brief_summary(briefs.get("macro", {})),
        )

        try:
            result = await call_claude(prompt, max_tokens=300)
            if not result or "action" not in result:
                return DEFAULT_ACTION

            action = {
                "action": result.get("action", "HOLD").upper(),
                "new_trail_pct": result.get("new_trail_pct"),
                "reasoning": str(result.get("reasoning", ""))[:200],
            }

            # Validate trail_pct range
            if action["new_trail_pct"] is not None:
                action["new_trail_pct"] = max(0.5, min(5.0, float(action["new_trail_pct"])))

            if action["action"] != "HOLD":
                logger.info(f"🚪 Exit Agent {symbol}: {action['action']} — {action['reasoning']}")

            return action
        except Exception as e:
            logger.error(f"Exit agent evaluation error for {symbol}: {e}")
            return DEFAULT_ACTION

    async def _execute_action(self, symbol: str, pos: Dict, action: Dict):
        """Execute the exit agent's decision — adjust trailing stop or exit."""
        if not self.broker:
            logger.warning(f"Exit agent: no broker to execute {action['action']} for {symbol}")
            return

        act = action.get("action", "HOLD")

        if act == "EXIT_NOW":
            # Immediate market sell
            qty = int(float(pos.get("quantity", 0)))
            if qty > 0:
                logger.warning(f"🚨 EXIT_NOW: {symbol} — {action.get('reasoning', '')}")
                try:
                    # Cancel existing trailing stop first
                    stop_id = pos.get("trailing_stop_order_id")
                    if stop_id:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self.broker.cancel_order, stop_id
                        )
                    side = pos.get("side", "long")
                    if side == "short":
                        # Buy to cover
                        await asyncio.get_event_loop().run_in_executor(
                            None, self.broker.place_market_buy, symbol, qty
                        )
                    else:
                        await asyncio.get_event_loop().run_in_executor(
                            None, self.broker.place_market_sell, symbol, qty
                        )
                except Exception as e:
                    logger.error(f"Exit agent EXIT_NOW failed for {symbol}: {e}")

        elif act in ("TIGHTEN", "WIDEN"):
            new_trail = action.get("new_trail_pct")
            if new_trail is None:
                return
            old_trail = pos.get("trail_pct", 3.0)
            if abs(new_trail - old_trail) < 0.1:
                return  # Not enough change to bother

            logger.info(f"🔧 Exit Agent adjusting {symbol} trail: {old_trail}% → {new_trail}%")
            try:
                # Mark position as adjusting so monitor doesn't panic
                pos["_trail_adjusting"] = True
                # Cancel old trailing stop and place new one
                stop_id = pos.get("trailing_stop_order_id")
                if stop_id:
                    await asyncio.get_event_loop().run_in_executor(
                        None, self.broker.cancel_order, stop_id
                    )
                    await asyncio.sleep(0.3)

                qty = int(float(pos.get("quantity", 0)))
                if qty >= 1 and hasattr(self.broker, 'place_trailing_stop'):
                    new_stop = await asyncio.get_event_loop().run_in_executor(
                        None, self.broker.place_trailing_stop, symbol, qty, new_trail
                    )
                    if new_stop:
                        pos["trail_pct"] = new_trail
                        pos["trailing_stop_order_id"] = new_stop.get("id")
                        logger.success(f"📈 Trail adjusted: {symbol} {old_trail}% → {new_trail}%")
                    else:
                        # Failed — restore old stop
                        logger.warning(f"Trail adjust failed for {symbol} — restoring {old_trail}%")
                        restore = await asyncio.get_event_loop().run_in_executor(
                            None, self.broker.place_trailing_stop, symbol, qty, old_trail
                        )
                        if restore:
                            pos["trailing_stop_order_id"] = restore.get("id")
            except Exception as e:
                logger.error(f"Exit agent trail adjust failed for {symbol}: {e}")
            finally:
                pos.pop("_trail_adjusting", None)  # Always clear the flag


def _brief_summary(brief: Dict) -> str:
    """Convert agent brief to a concise string for the prompt."""
    if not brief:
        return "No data available"
    # Just stringify the dict compactly
    parts = []
    for k, v in brief.items():
        if k in ("error", "symbol"):
            continue
        parts.append(f"{k}={v}")
    return ", ".join(parts) if parts else "No data available"
