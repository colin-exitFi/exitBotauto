"""
Position Manager (Layer 4) - Runs every 2 minutes.
Sees: all positions + current prices + sentiment.
Can execute HIGH URGENCY exits, strategic exits, and profit-protection actions.
Has VETO power over new entries that violate risk rules.
Uses market bid, NOT fair value.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

import anthropic

from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data"
MODEL = getattr(settings, "CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

from src.ai.mission import MISSION

SYSTEM_PROMPT = f"""{MISSION}

You are Layer 4: The Position Manager. You run every 2 minutes. You are the LAST LINE OF DEFENSE and the PROFIT PROTECTOR.

CORE MANDATE:
- Take profit, and protect profit at all costs.
- Never let a winner turn into a loser.
- Don't kill a run too early.
- But once profits exist, protect them aggressively.
- Capital velocity matters: harvesting gains and redeploying is better than watching winners round-trip.

You see every open position with real-time prices, peak P&L context, sentiment, risk state, the latest Observer view, and the latest Advisor recommendations.
You can:
1. Force-exit positions in EMERGENCIES (sentiment crash, flash crash, circuit breaker)
2. Execute STRATEGIC EXITS when the Advisor recommends exit and your own assessment agrees
3. Tighten trailing stops to protect profits when a winner starts retracing
4. VETO new entries that violate risk rules (even if other layers approve)
5. Flag positions that need attention

PRIORITIES:
1. Protect existing profits first
2. Let true runners keep running while momentum is healthy
3. When momentum fades or reversal signals appear on a profitable position, act immediately
4. Use all available intelligence, not just one signal in isolation

WHEN TO FORCE EXIT (high urgency):
- Symbol sentiment crashes to deeply negative (-0.5 or worse) suddenly
- Price drops >3% in under 5 minutes (flash crash)
- Multiple positions in same sector all dropping simultaneously
- Total portfolio heat exceeds 80% of daily loss budget

WHEN TO EXECUTE A STRATEGIC EXIT:
- The Advisor recommends EXIT with high urgency
- AND your own assessment agrees there is no strong reason to keep holding
- Especially when a profitable position is retracing materially from peak, momentum is fading, sentiment/catalyst is deteriorating, or the thesis looks tired
- Goal: secure profits, period

WHEN TO TIGHTEN TRAILS:
- Advisor recommends TRIM
- A position is profitable and retracing from peak P&L
- You want to protect gains without killing a strong run outright
- Use 1.0% to 1.5% trail when tightening for profit protection

WHEN TO HOLD:
- The position just entered and momentum is still building
- The winner is still extending cleanly with no meaningful reversal evidence
- You have a clear strong reason to override the Advisor recommendation

VETO RULES (block new entries when):
- Portfolio at max positions for current risk tier
- Portfolio heat > 60% of daily loss budget
- Same sector already has 40%+ of positions
- In a 3+ loss streak AND position would be speculative conviction

You are NOT the primary exit engine — that's exit_manager.py.
But you DO have authority to protect profits and execute strategic exits when conditions warrant.

Output JSON:
{{
    "emergency_exits": [
        {{"symbol": "AAPL", "reason": "sentiment crashed to -0.7 in 5 min", "urgency": "critical"}}
    ],
    "strategic_exits": [
        {{"symbol": "NVDA", "reason": "Advisor high-urgency exit confirmed by fading momentum and profit retrace", "urgency": "high"}}
    ],
    "trail_adjustments": [
        {{"symbol": "TSLA", "trail_pct": 1.0, "reason": "profitable retrace from peak; lock gains", "action": "tighten"}}
    ],
    "vetoes": ["list of symbols to block entry on"],
    "position_notes": [
        {{"symbol": "META", "status": "hold|watching|trim|exit", "note": "brief explanation"}}
    ],
    "portfolio_health": "healthy|stressed|critical",
    "action_taken": "none|exits_recommended|profit_protection|vetoes_active"
}}"""


class PositionManager:
    """Layer 4 AI: emergency exits, strategic exits, profit protection, and entry vetoes."""

    INTERVAL = 120  # 2 minutes

    def __init__(self):
        self._client = None
        if settings.ANTHROPIC_API_KEY:
            self._client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        self._vetoed_symbols: set = set()
        DATA_DIR.mkdir(exist_ok=True)

    async def run(self, bot, advisor_output: Optional[Dict] = None) -> Optional[Dict]:
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
            observer_output = bot.observer.get_last_output() if getattr(bot, "observer", None) else {}
            advisor_output = advisor_output or self._load_latest_advisor_output()
            advisor_actions = self._extract_advisor_actions(advisor_output)
            exit_agent = getattr(getattr(bot, "orchestrator", None), "exit_agent", None)
            latest_briefs = getattr(exit_agent, "_last_briefs", {}) if exit_agent else {}

            # Enrich positions with current prices, sentiment, peak-P&L context, and latest agent briefs
            enriched = []
            positions_by_symbol = {}
            for pos in positions:
                symbol = str(pos.get("symbol", "") or "").upper()
                if not symbol:
                    continue
                positions_by_symbol[symbol] = pos

                price = 0.0
                if bot.polygon_client:
                    try:
                        price = float(bot.polygon_client.get_price(symbol) or 0)
                    except Exception:
                        price = 0.0
                if not price:
                    price = float(pos.get("entry_price", 0) or 0)

                entry_price = float(pos.get("entry_price", 0) or 0)
                side = str(pos.get("side", "long") or "long").lower()
                pnl_pct = self._calc_pnl_pct(entry_price, price, side)
                hold_min = (now - float(pos.get("entry_time", now) or now)) / 60
                peak_price = float(pos.get("peak_price", price) or price)
                peak_pnl_pct = self._calc_pnl_pct(entry_price, peak_price, side)
                drawdown_from_peak_pct = max(0.0, round(peak_pnl_pct - pnl_pct, 2))

                sent_score = 0.0
                if bot.sentiment_analyzer:
                    cached = bot.sentiment_analyzer.get_cached(symbol)
                    if cached:
                        sent_score = float(cached.get("score", 0) or 0)

                advisor_rec = advisor_actions.get(symbol)
                briefs = latest_briefs.get(symbol, {}) if isinstance(latest_briefs, dict) else {}
                brief_summary = {
                    "technical": _brief_summary(briefs.get("technical", {})),
                    "sentiment": _brief_summary(briefs.get("sentiment", {})),
                    "catalyst": _brief_summary(briefs.get("catalyst", {})),
                    "risk": _brief_summary(briefs.get("risk", {})),
                    "macro": _brief_summary(briefs.get("macro", {})),
                }

                enriched.append(
                    {
                        "symbol": symbol,
                        "side": side,
                        "entry_price": round(entry_price, 2),
                        "current_price": round(price, 2),
                        "pnl_pct": round(pnl_pct, 2),
                        "peak_price": round(peak_price, 2),
                        "peak_pnl_pct": round(peak_pnl_pct, 2),
                        "drawdown_from_peak_pct": drawdown_from_peak_pct,
                        "hold_minutes": round(hold_min, 0),
                        "quantity": pos.get("quantity", 0),
                        "sentiment": round(sent_score, 2),
                        "partial_exit": pos.get("partial_exit", False),
                        "advisor_recommendation": advisor_rec,
                        "agent_briefs": brief_summary,
                    }
                )

            specialist_briefs = []
            for row in enriched:
                specialist_briefs.append(
                    f"""{row['symbol']} SPECIALIST BRIEFS:
- Technical: {row['agent_briefs']['technical']}
- Sentiment: {row['agent_briefs']['sentiment']}
- Catalyst: {row['agent_briefs']['catalyst']}
- Risk: {row['agent_briefs']['risk']}
- Macro: {row['agent_briefs']['macro']}
- Advisor recommendation: {json.dumps(row.get('advisor_recommendation') or {}, default=str)}"""
                )

            prompt = f"""RISK STATUS:
{json.dumps(risk_status, indent=2, default=str)}

OBSERVER OUTPUT:
{json.dumps(observer_output or {}, indent=2, default=str)}

ADVISOR STRATEGY:
{json.dumps({
    'strategy': (advisor_output or {}).get('strategy', ''),
    'position_advice': (advisor_output or {}).get('position_advice', []),
    'key_insight': (advisor_output or {}).get('key_insight', ''),
    'aggression_level': (advisor_output or {}).get('aggression_level', ''),
}, indent=2, default=str)}

OPEN POSITIONS ({len(enriched)}):
{json.dumps(enriched, indent=2, default=str)}

SPECIALIST AGENT BRIEFS:
{chr(10).join(specialist_briefs) if specialist_briefs else 'No specialist briefs available.'}

Current time: {time.strftime('%H:%M ET')}

Protect profits first. Review all positions with full context.
Weigh the Advisor against the current thesis from specialist briefs and live position data.
If the Advisor says exit but catalyst/technical/macro evidence says the run is still accelerating and the thesis is intact, hold.
If profits are fading and the thesis is weakening, act immediately.
Which positions need emergency exits, strategic exits, or tighter trails right now? Which new entries should be vetoed?"""

            response = await asyncio.to_thread(
                self._client.messages.create,
                model=MODEL,
                max_tokens=1400,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            result = _parse_json(text)
            self._last_output = result

            # Process emergency exits first
            emergency_exits = result.get("emergency_exits", []) or []
            for exit_rec in emergency_exits:
                symbol = str(exit_rec.get("symbol", "") or "").upper()
                pos = positions_by_symbol.get(symbol)
                if not pos:
                    continue
                reason = str(exit_rec.get("reason", "AI position manager emergency") or "AI position manager emergency")
                urgency = str(exit_rec.get("urgency", "medium") or "medium").lower()
                if urgency not in {"critical", "high"}:
                    continue
                await self._execute_market_exit(
                    bot,
                    pos,
                    reason=reason,
                    source="ai_emergency",
                )

            # Process advisor-aligned strategic exits
            strategic_exits = result.get("strategic_exits", []) or []
            for exit_rec in strategic_exits:
                symbol = str(exit_rec.get("symbol", "") or "").upper()
                pos = positions_by_symbol.get(symbol)
                if not pos:
                    continue
                reason = str(exit_rec.get("reason", "Advisor-aligned strategic exit") or "Advisor-aligned strategic exit")
                urgency = str(exit_rec.get("urgency", "medium") or "medium").lower()
                if urgency not in {"critical", "high"}:
                    continue
                await self._execute_market_exit(
                    bot,
                    pos,
                    reason=reason,
                    source="advisor_strategic_exit",
                )

            # Process trail tightening / profit protection
            trail_adjustments = result.get("trail_adjustments", []) or []
            for rec in trail_adjustments:
                symbol = str(rec.get("symbol", "") or "").upper()
                pos = positions_by_symbol.get(symbol)
                if not pos:
                    continue
                trail_pct = rec.get("trail_pct")
                if trail_pct is None:
                    continue
                reason = str(rec.get("reason", "profit protection") or "profit protection")
                await self._tighten_trailing_stop(bot, pos, float(trail_pct), reason)

            # Update veto list
            self._vetoed_symbols = {str(s).upper() for s in (result.get("vetoes", []) or []) if str(s).strip()}

            health = result.get("portfolio_health", "healthy")
            if health != "healthy":
                logger.warning(f"🤖 Position Manager: portfolio {health}")

            return result

        except Exception as e:
            logger.error(f"Position manager failed: {e}")
            return None

    def is_vetoed(self, symbol: str) -> bool:
        """Check if a symbol is currently vetoed by the position manager."""
        return str(symbol or "").upper() in self._vetoed_symbols

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

    @staticmethod
    def _calc_pnl_pct(entry_price: float, current_price: float, side: str) -> float:
        if not entry_price:
            return 0.0
        if side == "short":
            return ((entry_price - current_price) / entry_price) * 100
        return ((current_price - entry_price) / entry_price) * 100

    @staticmethod
    def _extract_advisor_actions(advisor_output: Optional[Dict]) -> Dict[str, Dict]:
        if not isinstance(advisor_output, dict):
            return {}
        actions = {}
        for row in advisor_output.get("position_advice", []) or []:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol", "") or "").upper().strip()
            action = str(row.get("action", "") or "").lower().strip()
            if not symbol or action not in {"exit", "trim", "hold", "add"}:
                continue
            urgency = str(row.get("urgency", "") or "").lower().strip()
            if urgency not in {"high", "medium", "low"}:
                urgency = _infer_urgency(action, str(row.get("reason", "") or ""))
            actions[symbol] = {
                "action": action,
                "reason": str(row.get("reason", "") or ""),
                "urgency": urgency,
            }
        return actions

    @staticmethod
    def _load_latest_advisor_output() -> Optional[Dict]:
        adv_file = DATA_DIR / "advisor.json"
        if not adv_file.exists():
            return None
        try:
            raw = json.loads(adv_file.read_text())
        except Exception:
            return None
        if isinstance(raw, list) and raw:
            last = raw[-1]
            return last if isinstance(last, dict) else None
        if isinstance(raw, dict):
            return raw
        return None

    async def _execute_market_exit(self, bot, position: Dict, reason: str, source: str) -> Optional[Dict]:
        symbol = str(position.get("symbol", "") or "").upper()
        side = str(position.get("side", "long") or "long").lower()
        price = 0.0
        if getattr(bot, "polygon_client", None):
            try:
                price = float(bot.polygon_client.get_price(symbol) or 0)
            except Exception:
                price = 0.0
        if not price:
            price = float(position.get("entry_price", 0) or 0)

        pnl_pct = self._calc_pnl_pct(float(position.get("entry_price", 0) or 0), price, side)
        logger.warning(f"🤖 PM {source.upper()}: {symbol} — {reason}")

        if getattr(bot, "exit_manager", None):
            return await bot.exit_manager._execute_exit(
                position,
                position.get("quantity", 0),
                price,
                f"{source}: {reason}",
                pnl_pct,
            )

        broker = getattr(bot, "alpaca_client", None)
        if not broker:
            return None

        qty = float(position.get("quantity", 0) or 0)
        if qty <= 0:
            return None

        try:
            broker_call = broker.place_market_buy if side == "short" else broker.place_market_sell
            order = await asyncio.get_event_loop().run_in_executor(None, broker_call, symbol, qty)
            if order and getattr(bot, "entry_manager", None) and hasattr(bot.entry_manager, "remove_position"):
                bot.entry_manager.remove_position(symbol)
            return order
        except Exception as e:
            logger.error(f"PM strategic exit failed for {symbol}: {e}")
            return None

    async def _tighten_trailing_stop(self, bot, position: Dict, requested_trail_pct: float, reason: str) -> bool:
        broker = getattr(bot, "alpaca_client", None)
        if not broker:
            return False

        symbol = str(position.get("symbol", "") or "").upper()
        side = str(position.get("side", "long") or "long").lower()
        current_trail = float(position.get("trail_pct", 3.0) or 3.0)
        target_trail = max(1.0, min(1.5, float(requested_trail_pct)))
        target_trail = min(current_trail, target_trail)
        if target_trail >= current_trail - 0.05:
            return False

        try:
            stop_id = position.get("trailing_stop_order_id")
            if stop_id:
                await asyncio.get_event_loop().run_in_executor(None, broker.cancel_order, stop_id)
                await asyncio.sleep(0.3)

            qty = float(position.get("quantity", 0) or 0)
            if qty <= 0:
                return False

            trail_fn = broker.place_trailing_stop_short if side == "short" and hasattr(broker, "place_trailing_stop_short") else broker.place_trailing_stop
            new_stop = await asyncio.get_event_loop().run_in_executor(None, trail_fn, symbol, qty, target_trail)
            if not new_stop:
                return False

            position["trail_pct"] = target_trail
            position["trailing_stop_order_id"] = new_stop.get("id")
            logger.info(f"🤖 PM PROFIT PROTECTION: {symbol} trail {current_trail:.1f}% → {target_trail:.1f}% ({reason})")
            return True
        except Exception as e:
            logger.error(f"PM trail tighten failed for {symbol}: {e}")
            return False


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
        return {
            "raw": text,
            "emergency_exits": [],
            "strategic_exits": [],
            "trail_adjustments": [],
            "vetoes": [],
            "portfolio_health": "healthy",
        }


def _brief_summary(brief: Dict) -> str:
    if not brief:
        return "No data available"
    parts = []
    for k, v in brief.items():
        if k in ("error", "symbol"):
            continue
        parts.append(f"{k}={v}")
    return ", ".join(parts) if parts else "No data available"


def _infer_urgency(action: str, reason: str) -> str:
    text = f"{action} {reason}".lower()
    if any(term in text for term in ("urgent", "immediate", "broken", "thesis broken", "exit now", "high conviction")):
        return "high"
    if action == "trim":
        return "medium"
    if any(term in text for term in ("trim", "reduce", "tighten", "cautious")):
        return "medium"
    return "low"
