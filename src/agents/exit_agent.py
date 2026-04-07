"""
Exit Agent 🚪 - Continuously monitors open positions, adjusts trailing stops.
Runs on its own loop (every 2-3 minutes), receives briefs from other agents.
Uses Claude Sonnet.
"""

import asyncio
import time
from typing import Dict, List, Optional, Tuple
from loguru import logger

from config import settings
from src.agents.base_agent import call_claude
from src.exit.order_conflicts import cancel_conflicting_exit_orders


DEFAULT_ACTION = {
    "action": "HOLD",
    "new_trail_pct": None,
    "reasoning": "Default hold — no adjustment needed",
}

PROMPT_TEMPLATE = """You are an EXIT MANAGEMENT specialist inside Velox, an autonomous momentum trading engine.
Your ONLY job: monitor open positions and recommend mechanical exit changes. You do NOT have live execution authority.

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
- HOLD: keep current protection, position is fine
- TIGHTEN: recommend a tighter ratchet / stop posture to lock in more profit
- WIDEN: recommend more room if momentum is strong but volatile
- EXIT_NOW: recommend immediate mechanical exit — recommendation only, never execute directly

Trail range: 0.5% (very tight) to 5.0% (very wide). Current default is 3%.
If we're up >5%, consider tightening. If momentum is accelerating, consider widening to ride it.

Respond with ONLY valid JSON:
{{"action": "HOLD" or "TIGHTEN" or "WIDEN" or "EXIT_NOW", "new_trail_pct": number or null, "reasoning": "brief explanation"}}"""


class ExitAgent:
    """Manages the exit agent loop — monitors positions and adjusts trailing stops."""

    def __init__(self, broker=None, entry_manager=None, risk_manager=None, exit_manager=None):
        self.broker = broker
        self.entry_manager = entry_manager
        self.risk_manager = risk_manager
        self._exit_manager = exit_manager
        self._running = False
        self._last_briefs: Dict[str, Dict] = {}  # symbol -> latest agent briefs
        self._last_check: Dict[str, float] = {}  # symbol -> last check timestamp
        self._processed_advisor_actions = set()
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

    def _is_market_active(self) -> bool:
        """Check if market is open for trading (extended hours included)."""
        if self.entry_manager and hasattr(self.entry_manager, "is_market_open"):
            return self.entry_manager.is_market_open()
        return True

    async def _loop(self):
        """Main monitoring loop — runs every 2 minutes during market hours, 10 min otherwise."""
        while self._running:
            try:
                market_open = self._is_market_active()
                positions = self.entry_manager.get_positions() if self.entry_manager else []

                if not market_open:
                    await asyncio.sleep(600)
                    continue

                for pos in positions:
                    symbol = pos.get("symbol", "")
                    if not symbol:
                        continue
                    if pos.get("halted"):
                        continue
                    last = self._last_check.get(symbol, 0)
                    if time.time() - last < 120:
                        continue
                    self._last_check[symbol] = time.time()

                    action = await self._evaluate_position(pos)
                    if action and action.get("action") != "HOLD":
                        await self._execute_action(symbol, pos, action)

                held_symbols = {p.get("symbol") for p in positions}
                stale = [s for s in self._last_briefs if s not in held_symbols]
                for s in stale:
                    del self._last_briefs[s]
                    self._last_check.pop(s, None)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Exit agent loop error: {e}")

            await asyncio.sleep(120)

    @staticmethod
    def _within_hard_stop_range(pos: Dict, pnl_pct: float, hold_seconds: float) -> bool:
        """Position is losing but hasn't breached the hard stop — let the stop do its job.

        Returns False (not guarded) when:
        - Position is profitable (pnl_pct >= 0)
        - Position has been held > 24h (dead money — exit agent can act)
        - P&L has already breached past the hard stop
        """
        if pnl_pct >= 0:
            return False
        if hold_seconds >= 24 * 3600:
            return False
        try:
            from src.exit.profit_ratchet import ProfitRatchet
            hard_stop_pct = float(pos.get("hard_stop_pct") or ProfitRatchet.HARD_STOP_PCT)
        except Exception:
            hard_stop_pct = -3.0
        return pnl_pct > hard_stop_pct

    async def _evaluate_position(self, pos: Dict) -> Optional[Dict]:
        """Evaluate a single position using AI."""
        symbol = pos.get("symbol", "")
        entry_price = pos.get("entry_price", 0)
        if entry_price <= 0:
            return None

        if pos.get("luld_at_risk") and not pos.get("halted"):
            tightened = max(1.0, min(float(pos.get("trail_pct", 3.0) or 3.0), 1.5))
            return {
                "action": "TIGHTEN",
                "new_trail_pct": tightened,
                "reasoning": "LULD band is tightening near entry; reducing room proactively",
            }

        current_price = entry_price  # fallback
        fetched_broker_positions, broker_position = self._lookup_broker_position(symbol)
        if fetched_broker_positions and broker_position is None:
            logger.info(f"Exit agent dropping stale tracked position for {symbol}: no Alpaca counterpart")
            if self.entry_manager and hasattr(self.entry_manager, "remove_position"):
                self.entry_manager.remove_position(symbol)
            self._last_briefs.pop(symbol, None)
            self._last_check.pop(symbol, None)
            return None
        if broker_position:
            try:
                current_price = float(broker_position.get("current_price", entry_price) or entry_price)
            except Exception:
                current_price = entry_price

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
                action = self._rule_based_fallback_action(pos, current_price, pnl_pct, hold_seconds)
                action["current_price"] = current_price
                action["pnl_pct"] = pnl_pct
                action["hold_seconds"] = hold_seconds
                return action

            action = {
                "action": result.get("action", "HOLD").upper(),
                "new_trail_pct": result.get("new_trail_pct"),
                "reasoning": str(result.get("reasoning", ""))[:200],
            }

            # Validate trail_pct range
            if action["new_trail_pct"] is not None:
                action["new_trail_pct"] = max(0.5, min(5.0, float(action["new_trail_pct"])))

            if action["action"] in ("EXIT_NOW", "TIGHTEN") and self._within_hard_stop_range(pos, pnl_pct, hold_seconds):
                logger.info(
                    f"🚪 Exit Agent {symbol}: overriding {action['action']} → HOLD "
                    f"(pnl {pnl_pct:+.1f}% within hard stop range, let stop do its job)"
                )
                action["action"] = "HOLD"
                action["reasoning"] = f"Overridden: pnl {pnl_pct:+.1f}% within hard stop range"

            if action["action"] != "HOLD":
                logger.info(f"🚪 Exit Agent {symbol}: {action['action']} — {action['reasoning']}")

            action["current_price"] = current_price
            action["pnl_pct"] = pnl_pct
            action["hold_seconds"] = hold_seconds
            return action
        except Exception as e:
            logger.error(f"Exit agent evaluation error for {symbol}: {e}")
            action = self._rule_based_fallback_action(pos, current_price, pnl_pct, hold_seconds)
            action["current_price"] = current_price
            action["pnl_pct"] = pnl_pct
            action["hold_seconds"] = hold_seconds
            return action

    @staticmethod
    def _rule_based_fallback_action(pos: Dict, current_price: float, pnl_pct: float, hold_seconds: float) -> Dict:
        """Safety fallback when Claude is unavailable or returns invalid output."""
        current_trail = max(0.5, min(5.0, float(pos.get("trail_pct", 3.0) or 3.0)))
        in_hard_stop_range = ExitAgent._within_hard_stop_range(pos, pnl_pct, hold_seconds)

        if pos.get("protection_failed") or pnl_pct <= -3.0:
            if in_hard_stop_range:
                return DEFAULT_ACTION
            return {
                "action": "EXIT_NOW",
                "new_trail_pct": None,
                "reasoning": "Rule-based safety exit while AI unavailable",
            }
        if pnl_pct >= 5.0:
            return {
                "action": "TIGHTEN",
                "new_trail_pct": min(current_trail, 1.5),
                "reasoning": "Rule-based profit lock while AI unavailable",
            }
        if hold_seconds >= 4 * 3600 and pnl_pct < 0:
            if in_hard_stop_range:
                return DEFAULT_ACTION
            return {
                "action": "TIGHTEN",
                "new_trail_pct": min(current_trail, 1.5),
                "reasoning": "Rule-based loss control while AI unavailable",
            }
        return DEFAULT_ACTION

    def _lookup_broker_position(self, symbol: str) -> Tuple[bool, Optional[Dict]]:
        if not self.broker:
            return False, None
        try:
            for broker_position in self.broker.get_positions():
                if broker_position.get("symbol") == symbol:
                    return True, broker_position
            return True, None
        except Exception as e:
            logger.debug(f"Exit agent broker lookup failed for {symbol}: {e}")
            return False, None

    async def _execute_action(self, symbol: str, pos: Dict, action: Dict):
        """Apply exit-agent actions conservatively."""
        act = action.get("action", "HOLD")
        reason = str(action.get("reasoning", "") or "").strip()
        new_trail = action.get("new_trail_pct")
        previous_recommendation = dict(pos.get("exit_agent_recommendation") or {})
        if act != "EXIT_NOW":
            pos["exit_agent_exit_now_count"] = 0
        pos["exit_agent_recommendation"] = {
            "action": act,
            "reasoning": reason,
            "new_trail_pct": new_trail,
            "timestamp": time.time(),
        }
        if act == "HOLD":
            return
        logger.info(
            f"🚪 Exit Agent recommendation: {symbol} {act}"
            f"{f' -> {float(new_trail):.2f}%' if new_trail is not None else ''} "
            f"({reason})"
        )
        try:
            from src.dashboard.dashboard import log_activity

            message = f"Exit agent recommendation: {symbol} {act}"
            if new_trail is not None:
                message += f" -> {float(new_trail):.2f}%"
            if reason:
                message += f" ({reason})"
            log_activity("ai", message)
        except Exception:
            pass

        if act == "EXIT_NOW":
            count = self._record_exit_now_confirmation(pos, previous_recommendation)
            hold_minutes = max(0.0, float(action.get("hold_seconds", 0.0) or 0.0) / 60.0)
            pnl_pct = float(action.get("pnl_pct", 0.0) or 0.0)
            logger.info(
                f"🚪 Exit Agent EXIT_NOW state {symbol}: count={count} "
                f"hold={hold_minutes:.1f}m pnl={pnl_pct:+.2f}%"
            )
            if await self._should_execute_exit_now(pos, action, count):
                order = await self._execute_exit_now(symbol, pos, action, reason)
                if order:
                    logger.warning(f"🚪 Exit Agent executed EXIT_NOW for {symbol} — {reason}")
                    try:
                        from src.dashboard.dashboard import log_activity

                        log_activity("trade", f"🚪 Exit Agent EXIT_NOW executed: {symbol} — {reason}")
                    except Exception:
                        pass
            return

        if act == "TIGHTEN" and new_trail is not None:
            await self._apply_trail_adjustment(symbol, pos, float(new_trail), reason)

    async def _cancel_conflicting_exit_orders(self, symbol: str, side: str = "long") -> int:
        exit_side = "buy" if side == "short" else "sell"
        return await cancel_conflicting_exit_orders(self.broker, symbol, exit_side)

    def _record_exit_now_confirmation(self, pos: Dict, previous_recommendation: Dict) -> int:
        prev_action = str(previous_recommendation.get("action", "") or "").upper()
        prev_ts = float(previous_recommendation.get("timestamp", 0.0) or 0.0)
        window_seconds = float(getattr(settings, "EXIT_AGENT_EXIT_NOW_CONFIRM_WINDOW_SECONDS", 900.0) or 900.0)
        count = int(pos.get("exit_agent_exit_now_count", 0) or 0)
        if prev_action == "EXIT_NOW" and prev_ts > 0 and (time.time() - prev_ts) <= window_seconds:
            count += 1
        else:
            count = 1
        pos["exit_agent_exit_now_count"] = count
        return count

    async def _should_execute_exit_now(self, pos: Dict, action: Dict, count: int) -> bool:
        if not bool(getattr(settings, "EXIT_AGENT_EXECUTE_EXIT_NOW_ENABLED", True)):
            return False
        if pos.get("exit_pending"):
            return False

        pnl_pct = float(action.get("pnl_pct", 0.0) or 0.0)
        hold_seconds = max(0.0, float(action.get("hold_seconds", 0.0) or 0.0))

        if self._within_hard_stop_range(pos, pnl_pct, hold_seconds):
            logger.info(
                f"🚪 Exit Agent blocking EXIT_NOW execution for {pos.get('symbol', '?')}: "
                f"pnl {pnl_pct:+.1f}% within hard stop range"
            )
            return False

        min_confirms = max(1, int(getattr(settings, "EXIT_AGENT_EXIT_NOW_CONFIRMATIONS", 2) or 2))
        if count < min_confirms:
            return False

        hold_minutes = hold_seconds / 60.0
        min_hold_minutes = float(getattr(settings, "EXIT_AGENT_EXIT_NOW_MIN_HOLD_MINUTES", 3.0) or 3.0)
        if hold_minutes < min_hold_minutes:
            return False

        max_pnl_pct = float(getattr(settings, "EXIT_AGENT_EXIT_NOW_MAX_PNL_PCT", 0.5) or 0.5)
        return pnl_pct <= max_pnl_pct

    async def _execute_exit_now(self, symbol: str, pos: Dict, action: Dict, reason: str):
        qty = float(pos.get("quantity", 0) or 0)
        if qty <= 0:
            return None

        current_price = float(
            action.get("current_price", pos.get("current_price", pos.get("entry_price", 0.0))) or 0.0
        )
        pnl_pct = float(action.get("pnl_pct", 0.0) or 0.0)
        exit_reason = f"exit_agent: {reason or 'EXIT_NOW'}"

        if self._exit_manager and hasattr(self._exit_manager, "_execute_exit"):
            return await self._exit_manager._execute_exit(
                pos,
                qty,
                current_price,
                exit_reason,
                pnl_pct,
            )

        if not self.broker:
            return None

        side = str(pos.get("side", "long") or "long").lower()
        canceled = await self._cancel_conflicting_exit_orders(symbol, side=side)
        if canceled:
            logger.info(f"🚪 Exit Agent canceled {canceled} conflicting exit orders for {symbol}")
        broker_call = self.broker.place_market_buy if side == "short" else self.broker.place_market_sell
        order = await asyncio.get_event_loop().run_in_executor(None, broker_call, symbol, qty)
        if order:
            pos["exit_pending"] = True
            pos["exit_order_id"] = order.get("id")
            pos["exit_submitted_at"] = time.time()
            pos["last_exit_reason"] = exit_reason
        return order

    async def _apply_trail_adjustment(self, symbol: str, pos: Dict, requested_trail_pct: float, reason: str) -> bool:
        current_trail = float(pos.get("trail_pct", 3.0) or 3.0)
        new_trail = max(0.5, min(current_trail, float(requested_trail_pct)))
        if new_trail >= current_trail - 0.05:
            return False

        prior_tighten = pos.get("ratchet_tighten_suggestion_pct")
        try:
            prior_tighten_val = float(prior_tighten) if prior_tighten is not None else None
        except Exception:
            prior_tighten_val = None

        pos["trail_pct"] = new_trail
        pos["ratchet_tighten_suggestion_pct"] = (
            min(prior_tighten_val, new_trail) if prior_tighten_val is not None else new_trail
        )

        if not self.entry_manager or not hasattr(self.entry_manager, "_place_entry_protection_order"):
            logger.info(f"🚪 Exit Agent tightened {symbol} trail to {new_trail:.2f}% ({reason})")
            return True

        side = str(pos.get("side", "long") or "long").lower()
        qty = int(float(pos.get("quantity", 0) or 0))
        if qty < 1:
            return True

        try:
            if self.broker:
                cancel_fn = self.broker.cancel_open_buys_for_symbol if side == "short" else self.broker.cancel_open_sells_for_symbol
                if cancel_fn:
                    await asyncio.get_event_loop().run_in_executor(None, cancel_fn, symbol)
            trail_order, protection_failed = await self.entry_manager._place_entry_protection_order(
                symbol,
                qty,
                new_trail,
                side,
            )
            pos["trail_pct"] = new_trail
            pos["protection_failed"] = bool(protection_failed)
            if trail_order:
                pos["has_trailing_stop"] = True
                pos["trailing_stop_order_id"] = trail_order.get("id", pos.get("trailing_stop_order_id"))
                logger.info(f"🚪 Exit Agent tightened {symbol} trail to {new_trail:.2f}% ({reason})")
                return True
        except Exception as e:
            logger.warning(f"Exit Agent trail refresh failed for {symbol}: {e}")
        logger.info(f"🚪 Exit Agent tightened {symbol} trail to {new_trail:.2f}% ({reason})")
        return True

    async def _check_advisor_recommendations(self, positions: List[Dict], advisor) -> List[Dict]:
        """Apply advisor-issued trim/exit suggestions conservatively."""
        if not advisor:
            return []

        actions = advisor.get_position_actions()
        if not actions:
            return []

        held = {str(pos.get("symbol", "")).upper(): pos for pos in positions or []}
        applied = []
        for rec in actions:
            symbol = str(rec.get("symbol", "")).upper()
            pos = held.get(symbol)
            if not pos or pos.get("halted"):
                continue

            rec_key = (symbol, str(rec.get("action", "")), float(rec.get("timestamp", 0) or 0))
            if rec_key in self._processed_advisor_actions:
                continue

            if self.risk_manager and hasattr(self.risk_manager, "can_exit_position"):
                if not self.risk_manager.can_exit_position(pos, reason="advisor_recommendation", log_block=False):
                    continue

            urgency = str(rec.get("urgency", "medium") or "medium").lower()
            action = str(rec.get("action", "trim") or "trim").lower()
            old_trail = float(pos.get("trail_pct", 3.0) or 3.0)
            if action == "exit" and urgency == "high":
                new_trail = min(old_trail, 1.0)
            elif action == "exit":
                new_trail = min(old_trail, 1.5)
            else:
                new_trail = min(old_trail, 1.5)
            new_trail = max(0.5, min(5.0, new_trail))

            if new_trail >= old_trail:
                self._processed_advisor_actions.add(rec_key)
                continue

            await self._execute_action(
                symbol,
                pos,
                {
                    "action": "TIGHTEN",
                    "new_trail_pct": new_trail,
                    "reasoning": f"Advisor {action}/{urgency}: {rec.get('reason', '')[:120]}",
                },
            )
            pos["advisor_action"] = action
            pos["advisor_action_reason"] = rec.get("reason", "")
            pos["advisor_action_at"] = float(rec.get("timestamp", time.time()) or time.time())
            self._processed_advisor_actions.add(rec_key)
            applied.append({"symbol": symbol, "action": action, "new_trail_pct": new_trail})
            try:
                from src.dashboard.dashboard import log_activity

                log_activity("ai", f"🎯 Advisor tightened {symbol} trail to {new_trail:.1f}% ({action}/{urgency})")
            except Exception:
                pass

        return applied


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
