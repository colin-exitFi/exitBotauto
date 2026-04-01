"""
Entry Manager - Validate conditions, size positions, execute via Alpaca.
"""

import asyncio
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from loguru import logger

from config import settings
from src import persistence
from src.data import strategy_controls
from src.data.setup_identity import normalize_symbol_state
from src.data.trade_schema import normalize_position_context
from src.data.strategy_tags import is_artifact_strategy_tag, normalize_strategy_tag
from src.execution.dust_policy import residual_blocks_entry
from src.exit.profit_ratchet import ProfitRatchet


class EntryManager:
    """
    Entry flow:
      1. Validate: sentiment > threshold, risk approves, market open
      2. Size: % of buying power
      3. Execute: Alpaca limit order, 30s timeout, up to 3 retries
    """

    def __init__(self, alpaca_client=None, polygon_client=None, risk_manager=None):
        self.broker = alpaca_client
        self.polygon = polygon_client
        self.risk = risk_manager

        self.min_sentiment = settings.MIN_ENTRY_SENTIMENT
        self.max_retries = 3
        self.order_timeout = 30  # seconds

        self.max_chase_pct = settings.MAX_PRICE_CHASE_PCT

        # Track active positions (symbol -> position dict)
        self.positions: Dict[str, Dict] = {}
        self.last_gate: Dict[str, str] = {}
        self.last_order_error: str = ""
        self._recently_removed_positions: Dict[str, Dict] = {}
        self._halted_symbols = set()
        # Load existing brokerage positions on init
        self._load_brokerage_positions()
        logger.info("Entry manager initialized")

    def is_market_open(self) -> bool:
        """Check if US stock market is open including extended hours (4:00 AM - 8:00 PM ET)."""
        from pytz import timezone as tz
        try:
            et = tz("US/Eastern")
        except Exception:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")

        now = datetime.now(et)

        # Weekday check (0=Mon, 6=Sun)
        if now.weekday() >= 5:
            return False
        extended_open = now.replace(hour=4, minute=0, second=0, microsecond=0)
        extended_close = now.replace(hour=20, minute=0, second=0, microsecond=0)
        return extended_open <= now <= extended_close

    def is_extended_hours(self) -> bool:
        """Check if market is in extended hours (before 9:30 AM or after 4:00 PM ET)."""
        from pytz import timezone as tz
        try:
            et = tz("US/Eastern")
        except Exception:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")

        now = datetime.now(et)
        market_open = now.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now.replace(hour=16, minute=0, second=0, microsecond=0)
        return now < market_open or now >= market_close

    @staticmethod
    def _parse_iso_ts(value) -> Optional[float]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except Exception:
            return None

    @staticmethod
    def _carryover_fallback_entry_time() -> float:
        try:
            import zoneinfo

            et = zoneinfo.ZoneInfo("US/Eastern")
        except Exception:
            from pytz import timezone as tz

            et = tz("US/Eastern")

        now_et = datetime.now(et)
        if now_et.weekday() == 0:
            fallback_day = now_et - timedelta(days=3)
        else:
            fallback_day = now_et - timedelta(days=1)
        fallback_midnight = fallback_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return fallback_midnight.timestamp()

    def _estimate_carryover_entry_time(
        self,
        symbol: str,
        side: str,
        closed_orders: Optional[List[Dict]],
    ) -> Tuple[float, str]:
        fallback = self._carryover_fallback_entry_time()
        if not closed_orders:
            return fallback, "broker_fallback"

        deltas = []
        for order in closed_orders:
            if str(order.get("symbol", "")).upper() != str(symbol).upper():
                continue
            order_side = str(order.get("side", "") or "").lower()
            if order_side not in ("buy", "sell"):
                continue
            try:
                filled_qty = float(order.get("filled_qty", order.get("qty", 0)) or 0)
            except Exception:
                filled_qty = 0.0
            if filled_qty <= 0:
                continue
            ts = self._parse_iso_ts(
                order.get("filled_at")
                or order.get("submitted_at")
                or order.get("created_at")
            )
            if ts is None:
                continue
            signed_delta = filled_qty if order_side == "buy" else -filled_qty
            deltas.append((ts, signed_delta))

        if not deltas:
            return fallback, "broker_fallback"

        deltas.sort(key=lambda item: item[0])
        net_qty = 0.0
        entry_time = None
        for ts, delta in deltas:
            prev_qty = net_qty
            net_qty += delta
            if abs(net_qty) < 1e-6:
                net_qty = 0.0

            prev_sign = 1 if prev_qty > 0 else (-1 if prev_qty < 0 else 0)
            new_sign = 1 if net_qty > 0 else (-1 if net_qty < 0 else 0)
            if new_sign == 0:
                entry_time = None
                continue
            if prev_sign == 0 or prev_sign != new_sign:
                entry_time = ts

        target_sign = -1 if side == "short" else 1
        final_sign = 1 if net_qty > 0 else (-1 if net_qty < 0 else 0)
        if final_sign == target_sign and entry_time is not None:
            return entry_time, "broker_orders"
        return fallback, "broker_fallback"

    @staticmethod
    def _restored_snapshot_has_meaningful_context(snapshot: Optional[Dict]) -> bool:
        if not isinstance(snapshot, dict) or not snapshot:
            return False

        strategy_tag = normalize_strategy_tag(
            snapshot.get("strategy_tag", "unknown"),
            fallback="unknown",
            allow_artifacts=True,
        )
        if strategy_tag not in {"unknown", "carryover"} and not is_artifact_strategy_tag(strategy_tag):
            return True

        if str(snapshot.get("setup_id", "") or "").strip():
            return True

        entry_path = str(snapshot.get("entry_path", "") or "").strip().lower()
        if entry_path and not entry_path.startswith("broker_sync") and entry_path != "unknown":
            return True

        entry_reason_code = str(snapshot.get("entry_reason_code", "") or "").strip().lower()
        if entry_reason_code and entry_reason_code not in {"unknown", "broker_sync"}:
            return True

        signal_sources = snapshot.get("signal_sources", []) or []
        if isinstance(signal_sources, str):
            signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
        normalized_sources = {
            str(source or "").strip().lower()
            for source in signal_sources
            if str(source or "").strip()
        }
        if normalized_sources - {"broker_sync", "broker_reconciliation"}:
            return True

        if snapshot.get("entry_model_votes"):
            return True

        return False

    @staticmethod
    def _prune_recently_removed_positions(
        positions: Optional[Dict[str, Dict]],
        max_age_seconds: float = 172800.0,
        max_entries: int = 500,
    ) -> Dict[str, Dict]:
        now_ts = time.time()
        trimmed: List[tuple] = []
        for symbol, payload in (positions or {}).items():
            if not isinstance(payload, dict):
                continue
            removed_at = float(payload.get("removed_at", 0) or 0)
            if removed_at > 0 and (now_ts - removed_at) > float(max_age_seconds or 0):
                continue
            trimmed.append((str(symbol or "").upper(), payload))
        trimmed.sort(key=lambda item: float((item[1] or {}).get("removed_at", 0) or 0), reverse=True)
        return {symbol: payload for symbol, payload in trimmed[: max(1, int(max_entries or 1))]}

    def _apply_whole_share_floor_notional(
        self,
        symbol: str,
        price: float,
        notional: float,
        sentiment_data: Dict,
        side: str,
    ) -> float:
        """
        Prevent strong whole-share-only setups from dying at zero size when a
        single share still fits inside a small, capped slice of equity.
        """
        if str(side or "").lower() != "short":
            return notional
        if not bool(getattr(settings, "WHOLE_SHARE_FLOOR_ENABLED", True)):
            return notional
        if price <= 0 or notional >= price:
            return notional
        if self._options_overlay_active(sentiment_data):
            return notional

        confidence = float(sentiment_data.get("consensus_confidence", 0.0) or 0.0)
        min_confidence = float(getattr(settings, "WHOLE_SHARE_FLOOR_MIN_CONFIDENCE", 75.0) or 75.0)
        if confidence < min_confidence:
            return notional

        provider_used = str(sentiment_data.get("provider_used", "") or "").lower()
        if not (provider_used.startswith("classifier_auto") or provider_used.startswith("council")):
            return notional

        entry_quality = str(sentiment_data.get("entry_quality", "neutral") or "neutral").lower()
        if entry_quality == "at_highs":
            return notional

        equity = getattr(self.risk, "equity", None) if self.risk else None
        if equity is None:
            equity = getattr(self.risk, "_equity", 25000) if self.risk else 25000
        max_pct = float(getattr(settings, "WHOLE_SHARE_FLOOR_MAX_NOTIONAL_PCT", 3.0) or 3.0)
        max_one_share_notional = float(equity or 0) * (max_pct / 100.0)
        if price > max_one_share_notional:
            return notional

        lifted_notional = price
        logger.info(
            f"🔼 WHOLE-SHARE FLOOR {symbol}: lifting short notional from ${notional:.2f} "
            f"to ${lifted_notional:.2f} (conf={confidence:.0f}% provider={provider_used})"
        )
        return lifted_notional

    @staticmethod
    def _options_overlay_active(sentiment_data: Dict) -> bool:
        used = float(sentiment_data.get("options_budget_used", 0.0) or 0.0)
        share_mult = float(sentiment_data.get("share_notional_multiplier", 1.0) or 1.0)
        return used > 0.0 or share_mult < 0.999

    def _apply_high_confidence_min_notional(
        self,
        symbol: str,
        notional: float,
        sentiment_data: Dict,
        side: str,
    ) -> float:
        """
        Lift tiny but valid high-confidence entries to a slightly higher
        baseline notional without changing the broader sizing regime.
        """
        if not bool(getattr(settings, "HIGH_CONFIDENCE_MIN_NOTIONAL_ENABLED", True)):
            return notional
        if notional <= 0:
            return notional
        if self._options_overlay_active(sentiment_data):
            return notional

        confidence = float(sentiment_data.get("consensus_confidence", 0.0) or 0.0)
        min_confidence = float(
            getattr(settings, "HIGH_CONFIDENCE_MIN_NOTIONAL_MIN_CONFIDENCE", 75.0) or 75.0
        )
        if confidence < min_confidence:
            return notional

        provider_used = str(sentiment_data.get("provider_used", "") or "").lower()
        if not (provider_used.startswith("classifier_auto") or provider_used.startswith("council")):
            return notional

        entry_quality = str(sentiment_data.get("entry_quality", "neutral") or "neutral").lower()
        if entry_quality == "at_highs":
            return notional

        equity = getattr(self.risk, "equity", None) if self.risk else None
        if equity is None:
            equity = getattr(self.risk, "_equity", 25000) if self.risk else 25000

        floor_abs = float(getattr(settings, "HIGH_CONFIDENCE_MIN_NOTIONAL", 325.0) or 325.0)
        max_pct = float(getattr(settings, "HIGH_CONFIDENCE_MIN_NOTIONAL_MAX_PCT", 1.5) or 1.5)
        capped_floor = min(floor_abs, float(equity or 0.0) * (max_pct / 100.0))
        if capped_floor <= 0 or notional >= capped_floor:
            return notional

        logger.info(
            f"🔼 HIGH-CONF FLOOR {symbol}: lifting {side} notional from ${notional:.2f} "
            f"to ${capped_floor:.2f} (conf={confidence:.0f}% provider={provider_used})"
        )
        return capped_floor

    async def _cancel_conflicting_protection_orders(self, symbol: str, side: str) -> int:
        if not self.broker:
            return 0
        cancel_fn = None
        if side == "short" and hasattr(self.broker, "cancel_open_buys_for_symbol"):
            cancel_fn = self.broker.cancel_open_buys_for_symbol
        elif side != "short" and hasattr(self.broker, "cancel_open_sells_for_symbol"):
            cancel_fn = self.broker.cancel_open_sells_for_symbol
        if not cancel_fn:
            return 0
        try:
            cancelled = await asyncio.get_event_loop().run_in_executor(None, cancel_fn, symbol)
            return int(cancelled or 0)
        except Exception as e:
            logger.warning(f"Could not cancel conflicting protection orders for {symbol}: {e}")
            return 0

    @staticmethod
    def _initial_hard_stop_profile(sentiment_data: Dict, extended: bool) -> Tuple[float, List[str]]:
        position_context = {
            "entry_quality": sentiment_data.get("entry_quality", "neutral"),
            "extended_hours_entry": bool(extended),
            "holding_horizon": sentiment_data.get("holding_horizon", "intraday"),
            "allocator_status": sentiment_data.get("allocator_status", "hold"),
            "allocator_recommended_action": sentiment_data.get("allocator_recommended_action", "hold"),
            "allocator_control_state": sentiment_data.get("allocator_control_state", "active"),
            "allocator_reason_codes": list(sentiment_data.get("allocator_reason_codes", []) or []),
        }
        return ProfitRatchet.initial_hard_stop_profile(position_context)

    async def _place_hard_stop_order(
        self,
        symbol: str,
        qty: int,
        entry_price: float,
        side: str,
        hard_stop_pct: Optional[float] = None,
    ):
        if qty < 1:
            return None, False
        # Only place protection when a matching broker position already exists.
        # This avoids Alpaca rejecting protective orders while entry orders are still pending.
        try:
            broker_pos = self.broker.get_position(symbol) if hasattr(self.broker, "get_position") else None
        except Exception:
            broker_pos = None
        broker_side = str((broker_pos or {}).get("side", "") or "").lower()
        if side == "short":
            if broker_side != "short":
                return None, False
        elif broker_side != "long":
            return None, False
        if not hasattr(self.broker, "place_stop_loss_order"):
            return None, False

        stop_price = ProfitRatchet.price_for_pnl(
            float(entry_price or 0),
            hard_stop_pct if hard_stop_pct is not None else ProfitRatchet.HARD_STOP_PCT,
            side,
        )
        client_order_id = ProfitRatchet.make_client_order_id(symbol, "hardstop", stop_price)
        order = await asyncio.get_event_loop().run_in_executor(
            None,
            self.broker.place_stop_loss_order,
            symbol,
            qty,
            stop_price,
            side,
            client_order_id,
        )
        if order:
            return order, False

        for attempt in range(1, 4):
            cancelled = await self._cancel_conflicting_protection_orders(symbol, side)
            if cancelled:
                logger.info(
                    f"Cancelled {cancelled} conflicting protection orders for {symbol} before retry {attempt}/3"
                )
            await asyncio.sleep(1)
            client_order_id = ProfitRatchet.make_client_order_id(symbol, "hardstop", stop_price)
            order = await asyncio.get_event_loop().run_in_executor(
                None,
                self.broker.place_stop_loss_order,
                symbol,
                qty,
                stop_price,
                side,
                client_order_id,
            )
            if order:
                return order, False

        logger.critical(f"Protection placement failed for {symbol} after 3 retries")
        return None, True

    def _set_gate(self, symbol: str, allowed: bool, reason: str):
        self.last_gate = {"symbol": symbol, "allowed": allowed, "reason": reason}
        return allowed

    @staticmethod
    def _copy_trader_size_multiplier(sentiment_data: Dict, swing_only: bool) -> float:
        if swing_only:
            return 1.0
        try:
            multiplier = float(sentiment_data.get("copy_trader_size_multiplier", 1.0) or 1.0)
        except Exception:
            multiplier = 1.0
        return max(0.75, min(1.25, multiplier))

    @staticmethod
    def _extract_setup_metadata(sentiment_data: Dict) -> Dict:
        data = dict(sentiment_data or {})
        missing_fields = data.get("missing_fields", []) or []
        if isinstance(missing_fields, str):
            missing_fields = [field.strip() for field in missing_fields.split(",") if field.strip()]
        if not isinstance(missing_fields, list):
            missing_fields = []
        normalized = normalize_position_context(
            {
                "symbol": data.get("symbol"),
                "side": data.get("side"),
                "source": data.get("source"),
                "strategy_tag": data.get("strategy_tag"),
                "provider_used": data.get("provider_used"),
                "entry_path": data.get("entry_path"),
                "entry_reason_code": data.get("entry_reason_code"),
                "signal_sources": data.get("signal_sources"),
                "setup_id": data.get("setup_id"),
                "setup_mode": data.get("setup_mode"),
                "direction_constraint": data.get("direction_constraint"),
                "timing_state": data.get("timing_state"),
                "best_play": data.get("best_play"),
                "trigger": data.get("trigger"),
                "trigger_spec": dict(data.get("trigger_spec", {}) or {}),
                "invalidation": data.get("invalidation"),
                "hold_style": data.get("hold_style"),
                "holding_horizon": data.get("holding_horizon"),
                "entry_quality": data.get("entry_quality"),
                "size_posture": data.get("size_posture"),
                "no_trade_reason": data.get("no_trade_reason"),
                "classifier_confidence": data.get("classifier_confidence"),
                "resolver_confidence": data.get("resolver_confidence"),
                "execution_confidence": data.get("execution_confidence"),
                "feature_snapshot_id": data.get("feature_snapshot_id"),
                "feature_quality_score": data.get("feature_quality_score"),
                "feature_quality": data.get("feature_quality"),
                "missing_fields": list(missing_fields),
                "material_change_signature": data.get("material_change_signature"),
                "symbol_state": data.get("symbol_state", "live_position"),
            }
        )
        return {
            "setup_id": normalized.get("setup_id"),
            "setup_mode": str(normalized.get("setup_mode", "general_momentum_long") or "general_momentum_long"),
            "direction_constraint": str(normalized.get("direction_constraint", "long_only") or "long_only"),
            "timing_state": str(normalized.get("timing_state", "enter_now") or "enter_now"),
            "best_play": normalized.get("best_play"),
            "trigger": normalized.get("trigger"),
            "trigger_spec": dict(normalized.get("trigger_spec", {}) or {}),
            "invalidation": normalized.get("invalidation"),
            "hold_style": normalized.get("hold_style"),
            "size_posture": normalized.get("size_posture", "normal"),
            "no_trade_reason": normalized.get("no_trade_reason"),
            "classifier_confidence": float(normalized.get("classifier_confidence", 0.0) or 0.0),
            "resolver_confidence": float(normalized.get("resolver_confidence", 0.0) or 0.0),
            "execution_confidence": float(normalized.get("execution_confidence", 0.0) or 0.0),
            "feature_snapshot_id": normalized.get("feature_snapshot_id"),
            "feature_quality_score": float(normalized.get("feature_quality_score", 0.0) or 0.0),
            "feature_quality": str(normalized.get("feature_quality", "") or ""),
            "missing_fields": list(missing_fields),
            "material_change_signature": normalized.get("material_change_signature"),
            "symbol_state": normalize_symbol_state(normalized.get("symbol_state", "live_position")),
            "mode_features": dict(data.get("mode_features", {}) or {}),
            "bar_context": dict(data.get("bar_context", {}) or {}),
            "setup_created_at": data.get("created_at"),
            "setup_last_refreshed_at": data.get("last_refreshed_at"),
            "data_age_seconds": float(data.get("data_age_seconds", 0.0) or 0.0),
            "jury_entry_now": bool(data.get("jury_entry_now", False)),
            "jury_trigger": data.get("jury_trigger"),
            "jury_invalidation": data.get("jury_invalidation"),
            "jury_hold_style": data.get("jury_hold_style"),
            "jury_size_posture": data.get("jury_size_posture"),
            "jury_no_trade_reason": data.get("jury_no_trade_reason"),
            "pharma_signal": bool(data.get("pharma_signal", False)),
            "pharma_catalyst_type": str(data.get("pharma_catalyst_type", "") or ""),
            "earnings": bool(data.get("earnings", False)),
            "earnings_date": data.get("earnings_date"),
            "catalyst_date": data.get("catalyst_date"),
        }

    def _apply_strategy_controls(self, symbol: str, sentiment_data: Dict, notional: float) -> Optional[float]:
        controls = strategy_controls.load_controls()
        strategy_tag = normalize_strategy_tag(sentiment_data.get("strategy_tag", "unknown"))
        disabled = strategy_controls.get_effective_disabled(controls)
        if strategy_tag in disabled:
            logger.warning(f"⛔ Strategy '{strategy_tag}' is disabled — blocking entry for {symbol}")
            self.last_order_error = "strategy_disabled"
            return None

        size_mult = strategy_controls.get_size_multiplier(strategy_tag, controls)
        if size_mult < 1.0:
            logger.info(f"📉 Strategy '{strategy_tag}' size reduced to {size_mult:.0%} by control plane")
            notional *= size_mult
        return notional

    async def can_enter(self, symbol: str, sentiment_score: float, current_positions: List[Dict]) -> bool:
        """Check all entry conditions including persistent controls."""
        self.last_order_error = ""
        if not self.is_market_open():
            logger.debug("Market closed, cannot enter")
            return self._set_gate(symbol, False, "market_closed")

        if symbol in getattr(self, "_halted_symbols", set()):
            logger.info(f"⛔ {symbol} is halted — blocking entry")
            return self._set_gate(symbol, False, "halted")

        from src.data.entry_controls import is_entry_blocked
        blocked, reason = is_entry_blocked(symbol)
        if blocked:
            logger.info(f"⛔ {symbol} blocked by persistent controls: {reason}")
            return self._set_gate(symbol, False, f"persistent_{reason}")

        if symbol in self.positions:
            logger.info(f"⛔ Already in position: {symbol} — duplicate entry blocked")
            return self._set_gate(symbol, False, "already_held")

        if residual_blocks_entry(symbol, self.positions):
            logger.info(f"⛔ {symbol} blocked by dust residual — existing dust position must close first")
            return self._set_gate(symbol, False, "dust_residual_blocks_entry")

        if sentiment_score < self.min_sentiment:
            logger.info(f"⛔ {symbol} sentiment {sentiment_score:.2f} < threshold {self.min_sentiment}")
            return self._set_gate(symbol, False, "sentiment_below_threshold")

        if self.risk and not self.risk.can_open_position(current_positions, symbol=symbol):
            return self._set_gate(symbol, False, "risk_open_position_block")

        if self.risk and not self.risk.can_enter_sector(symbol, current_positions):
            return self._set_gate(symbol, False, "sector_block")

        return self._set_gate(symbol, True, "ok")

    async def enter_position(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Execute entry: get price → size → limit order → wait for fill.
        Returns position dict on success, None on failure.
        """
        if not self.broker or not self.polygon:
            logger.error("Broker or Polygon client not available")
            self.last_order_error = "broker_or_polygon_unavailable"
            return None
        self.last_order_error = ""
        if symbol in getattr(self, "_halted_symbols", set()):
            logger.warning(f"Entry blocked for halted symbol {symbol}")
            self.last_order_error = "halted"
            return None
        if symbol in self.positions:
            logger.warning(f"Duplicate long entry blocked for {symbol}")
            self.last_order_error = "duplicate_position"
            return None

        # Get current price
        price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if price <= 0:
            logger.warning(f"Could not get price for {symbol}")
            self.last_order_error = "price_unavailable"
            return None
        signal_timestamp = float(sentiment_data.get("signal_timestamp", time.time()) or time.time())

        # Consensus already ran in main loop — use the modifier passed in sentiment_data
        consensus_size_modifier = sentiment_data.get("consensus_size_modifier", 1.0)

        # Get buying power (cash account aware)
        balances = await asyncio.get_event_loop().run_in_executor(
            None, self.broker.get_balances
        )
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)
        swing_only = bool(self.risk and getattr(self.risk, "is_swing_mode", None) and self.risk.is_swing_mode())

        # Extended hours adjustments
        extended = self.is_extended_hours()
        if extended:
            logger.info(f"Extended hours detected — forcing limit orders, reducing size")

        # Signal price for chase detection
        signal_price = price

        # Dynamic position sizing from risk tier
        # Determine conviction from sentiment strength
        sent_score = sentiment_data.get("score", 0)
        if sent_score > 0.6:
            conviction = "high"
        elif sent_score < 0.1:
            conviction = "speculative"
        else:
            conviction = "normal"

        notional = self.risk.get_position_size(price, buying_power, conviction) if self.risk else 0
        # Apply consensus size modifier
        notional *= consensus_size_modifier
        notional *= self._copy_trader_size_multiplier(sentiment_data, swing_only)
        # If options were placed, reduce share notional to keep total risk inside tier budget.
        share_mult = float(sentiment_data.get("share_notional_multiplier", 1.0) or 1.0)
        notional *= max(0.0, min(1.0, share_mult))
        adjusted_notional = self._apply_strategy_controls(symbol, sentiment_data, notional)
        if adjusted_notional is None:
            return None
        notional = adjusted_notional
        if extended:
            notional *= settings.EXTENDED_HOURS_SIZE_MULT
        equity = getattr(self.risk, 'equity', None) if self.risk else None
        if equity is None:
            equity = getattr(self.risk, '_equity', 25000) if self.risk else 25000
        notional = min(notional, equity * 0.25)
        notional = self._apply_high_confidence_min_notional(symbol, notional, sentiment_data, "long")
        min_notional = float(getattr(settings, "MIN_NOTIONAL", 25.0) or 25.0)
        if notional < min_notional:
            logger.warning(f"Notional ${notional:.2f} below MIN_NOTIONAL ${min_notional:.2f} for {symbol} — rejecting dust entry")
            self.last_order_error = "below_min_notional"
            return None
        shares = self.risk.get_shares(price, notional) if self.risk else 0
        if shares <= 0 or notional <= 0:
            tier = self.risk.get_risk_tier() if self.risk else {}
            logger.warning(
                f"Position size is 0 for {symbol} @ ${price:.2f} "
                f"(buying_power=${buying_power:.2f}, tier={tier.get('name', '?')}, conviction={conviction})"
            )
            self.last_order_error = "position_size_zero"
            return None

        # Use smart order execution for larger positions
        shares_int = int(shares) if shares >= 1 else shares
        logger.info(
            f"Entering {symbol}: ${notional:.2f} notional, {shares:.4f} shares @ ${price:.2f} "
            f"(conviction={conviction}, tier={self.risk.get_risk_tier().get('name', '?') if self.risk else '?'}"
            f"{', EXTENDED' if extended else ''})"
        )

        # ── Chase Prevention: re-check price before executing ──────
        recheck_price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if recheck_price > 0:
            chase_pct = abs((recheck_price - signal_price) / signal_price) * 100
            if chase_pct > self.max_chase_pct:
                logger.warning(f"CHASE PREVENTION: {symbol} moved {chase_pct:.2f}% since signal → SKIPPING")
                self.last_order_error = "chase_prevention"
                return None
            price = recheck_price  # use freshest price

        # Calculate ATR for dynamic stops (store in position)
        atr_value = None
        if hasattr(self, '_exit_manager') and self._exit_manager:
            atr_value = self._exit_manager.calculate_atr(symbol)
        else:
            # Try importing exit manager's ATR calc via polygon directly
            try:
                from src.exit.exit_manager import ExitManager
                _tmp = ExitManager.__new__(ExitManager)
                _tmp.polygon = self.polygon
                atr_value = _tmp.calculate_atr(symbol)
            except Exception:
                pass

        # ── STEP 1: BUY the stock ─────────────────────────────────
        order = None
        entry_order_timestamp = None
        for attempt in range(1, self.max_retries + 1):
            limit_price = round(price * 1.002, 2)  # 0.2% slippage buffer
            attempt_order_ts = time.time()

            if extended:
                if hasattr(self.broker, 'place_limit_buy_extended'):
                    order = await asyncio.get_event_loop().run_in_executor(
                        None, self.broker.place_limit_buy_extended, symbol, int(shares) if shares >= 1 else shares, limit_price
                    )
                else:
                    order = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self.broker.place_limit_buy,
                        symbol,
                        int(shares) if shares >= 1 else shares,
                        limit_price,
                        True,
                    )
            elif hasattr(self.broker, 'smart_buy'):
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.smart_buy, symbol, notional
                )
            else:
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_limit_buy, symbol, int(shares), limit_price
                )
            if order:
                entry_order_timestamp = attempt_order_ts
                break

            price = await asyncio.get_event_loop().run_in_executor(
                None, self.polygon.get_price, symbol
            )
            if price <= 0:
                break
            await asyncio.sleep(2)

        # ── STEP 2: Immediately place hard stop (regular hours) ──
        hard_stop_order = None
        protection_failed = False
        if swing_only:
            logger.info(f"🌙 Swing-only entry for {symbol}: broker hard stop deferred until same-day exit budget returns")
        elif not extended and order and hasattr(self.broker, "place_stop_loss_order"):
            order_status = str(order.get("status", "") or "").lower()
            try:
                filled_qty = int(float(order.get("filled_qty", 0) or 0))
            except Exception:
                filled_qty = 0
            if filled_qty >= 1 and order_status in {"filled", "partially_filled"}:
                await asyncio.sleep(1)
                stop_entry_price = float(order.get("filled_avg_price", price) or price)
                hard_stop_order, protection_failed = await self._place_hard_stop_order(
                    symbol,
                    filled_qty,
                    entry_price=stop_entry_price,
                    side="long",
                    hard_stop_pct=self._initial_hard_stop_profile(sentiment_data, extended)[0],
                )
                if hard_stop_order:
                    logger.success(
                        f"🛡️ Hard stop set: {symbol} {filled_qty}sh @ ${float(hard_stop_order.get('stop_price', 0) or 0):.2f}"
                    )
                else:
                    logger.warning(f"⚠️ Hard stop FAILED for {symbol}")
            else:
                logger.info(
                    f"⏳ Deferring hard stop for {symbol}: entry status={order_status or 'unknown'} "
                    f"filled_qty={filled_qty}"
                )

        if not order:
            # Check if we accidentally got filled on a limit before smart_buy cancelled it
            try:
                alpaca_positions = self.broker.get_positions()
                for p in alpaca_positions:
                    if p.get("symbol") == symbol:
                        actual_qty = float(p.get("qty", p.get("quantity", 0)) or 0)
                        actual_price = float(p.get("avg_entry_price", p.get("average_price", price)) or price)
                        if actual_qty > 0:
                            logger.warning(f"⚠️ {symbol}: order failed but found {actual_qty} shares on Alpaca — recording position")
                            shares = actual_qty
                            price = actual_price
                            order = {"id": "recovered", "filled_qty": str(actual_qty)}
                            entry_order_timestamp = time.time()
                            break
            except Exception:
                pass

        if not order:
            logger.error(f"Failed to enter {symbol} after {self.max_retries} attempts")
            self.last_order_error = "entry_order_failed"
            return None

        fill_timestamp = self._parse_iso_ts(order.get("filled_at")) if isinstance(order, dict) else None
        fill_timestamp_source = "order_response" if fill_timestamp is not None else "unknown"
        try:
            fill_price = float(order.get("filled_avg_price", price) or price)
        except Exception:
            fill_price = price
        entry_price = fill_price if fill_price > 0 else price
        try:
            requested_qty = float(shares)
        except Exception:
            requested_qty = 0.0
        try:
            order_qty = float(order.get("qty", requested_qty) or requested_qty)
        except Exception:
            order_qty = requested_qty
        try:
            filled_qty = float(order.get("filled_qty", order_qty) or order_qty)
        except Exception:
            filled_qty = 0.0
        actual_qty = filled_qty if filled_qty > 0 else order_qty
        if actual_qty <= 0:
            actual_qty = requested_qty
        if actual_qty <= 0:
            logger.error(f"Failed to determine filled quantity for {symbol}")
            return None
        order_status = str(order.get("status", "") or "").lower()
        if not order_status:
            order_status = "pending" if extended else "filled"
        actual_notional = entry_price * actual_qty

        # Record position
        signal_sources = sentiment_data.get("signal_sources", ["unknown"])
        if isinstance(signal_sources, str):
            signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
        if not isinstance(signal_sources, list):
            signal_sources = ["unknown"]
        if not signal_sources:
            signal_sources = ["unknown"]
        entry_votes = dict(sentiment_data.get("entry_model_votes", {}) or {})
        risk_constraints = list(sentiment_data.get("risk_constraints_applied", []) or [])
        signal_tier = str(sentiment_data.get("signal_tier", "tier_2") or "tier_2")
        holding_horizon = str(sentiment_data.get("holding_horizon", "intraday") or "intraday")
        market_regime = str(sentiment_data.get("market_regime", "mixed") or "mixed")
        hard_stop_pct, hard_stop_flags = self._initial_hard_stop_profile(sentiment_data, extended)
        hard_stop_price = ProfitRatchet.price_for_pnl(entry_price, hard_stop_pct, "long")
        position = {
            "symbol": symbol,
            "entry_price": entry_price,
            "fill_price": fill_price,
            "quantity": actual_qty,
            "entry_time": time.time(),
            "signal_timestamp": signal_timestamp,
            "entry_order_timestamp": entry_order_timestamp,
            "fill_timestamp": fill_timestamp,
            "fill_timestamp_source": fill_timestamp_source,
            "sentiment_at_entry": sentiment_data.get("score", 0),
            "peak_price": entry_price,
            "side": "long",
            "order_id": order.get("id", order.get("brokerage_order_id", "")),
            "entry_order_id": order.get("id", order.get("brokerage_order_id", "")),
            "partial_exit": False,
            "atr_at_entry": atr_value,
            "extended_hours_entry": extended,
            "conviction_level": conviction,
            "risk_tier": self.risk.get_risk_tier().get("name", "?") if self.risk else "?",
            "notional": actual_notional,
            "trail_pct": ProfitRatchet.RATCHET_TRAIL_PCT,
            "trailing_stop_order_id": None,
            "has_trailing_stop": False,
            "hard_stop_price": hard_stop_price,
            "hard_stop_pct": hard_stop_pct,
            "hard_stop_flags": list(hard_stop_flags),
            "hard_stop_order_id": hard_stop_order.get("id") if hard_stop_order else None,
            "ratchet_limit_order_id": None,
            "ratchet_peak_pnl_pct": 0.0,
            "ratchet_floor_pct": None,
            "ratchet_order_type": None,
            "protection_failed": protection_failed,
            "order_status": order_status,
            "order_state": {
                "entry": order_status,
                "hard_stop": "placed" if hard_stop_order else ("deferred" if extended or swing_only else "missing"),
                "ratchet": "inactive",
            },
            "strategy_tag": normalize_strategy_tag(sentiment_data.get("strategy_tag", "unknown")),
            "signal_tier": signal_tier,
            "holding_horizon": holding_horizon,
            "market_regime": market_regime,
            "session_type": str(sentiment_data.get("session_type", "") or ""),
            "entry_reason_code": sentiment_data.get("entry_reason_code", "jury_consensus"),
            "entry_model_votes": entry_votes,
            "risk_constraints_applied": risk_constraints,
            "entry_path": sentiment_data.get("entry_path", "jury"),
            "signal_sources": signal_sources,
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "signal_price": sentiment_data.get("signal_price", price),
            "decision_price": sentiment_data.get("decision_price", price),
            "intended_notional": float(notional or 0),
            "actual_notional": actual_notional,
            "intended_qty": float(requested_qty or 0),
            "actual_qty": actual_qty,
            "entry_quality": sentiment_data.get("entry_quality", "neutral"),
            "overnight_context": sentiment_data.get("overnight_context", ""),
            "allocator_state": sentiment_data.get("allocator_state", "neutral"),
            "allocator_alignment": sentiment_data.get("allocator_alignment", "neutral"),
            "allocator_budget_pct": float(sentiment_data.get("allocator_budget_pct", 0.0) or 0.0),
            "allocator_exposure_pct": float(sentiment_data.get("allocator_exposure_pct", 0.0) or 0.0),
            "allocator_remaining_budget_pct": float(sentiment_data.get("allocator_remaining_budget_pct", 0.0) or 0.0),
            "allocator_size_multiplier": float(sentiment_data.get("allocator_size_multiplier", 1.0) or 1.0),
            "allocator_reason": sentiment_data.get("allocator_reason", "allocator_ok"),
            "allocator_reason_codes": list(sentiment_data.get("allocator_reason_codes", []) or []),
            "allocator_status": sentiment_data.get("allocator_status", "hold"),
            "allocator_recommended_action": sentiment_data.get("allocator_recommended_action", "hold"),
            "allocator_control_state": sentiment_data.get("allocator_control_state", "active"),
            "dead_money_tightened": False,
            "dead_money": False,
            "anomaly_flags": list(sentiment_data.get("anomaly_flags", []) or []),
            "scout_escalated": bool(sentiment_data.get("scout_escalated", False)),
            "copy_trader_context": sentiment_data.get("copy_trader_context", ""),
            "copy_trader_handles": list(sentiment_data.get("copy_trader_handles", []) or []),
            "copy_trader_signal_count": int(sentiment_data.get("copy_trader_signal_count", 0) or 0),
            "copy_trader_convergence": int(sentiment_data.get("copy_trader_convergence", 0) or 0),
            "copy_trader_weight": float(sentiment_data.get("copy_trader_weight", 1.0) or 1.0),
            "swing_only": swing_only,
            "_exit_recorded": False,
        }
        position.update(self._extract_setup_metadata(sentiment_data))
        position["symbol_state"] = "live_position"
        self.positions[symbol] = position
        if extended:
            logger.success(
                f"📋 LIMIT ORDER PLACED: {actual_qty:.4f} {symbol} @ ${price:.2f} "
                f"(${actual_notional:.2f} est) — awaiting fill"
            )
        else:
            stop_info = (
                f" 🛡️ stop=${hard_stop_price:.2f}"
                if position["hard_stop_order_id"]
                else " ⚠️ NO HARD STOP"
            )
            logger.success(
                f"✅ ENTERED: {actual_qty:.4f} {symbol} @ ${entry_price:.2f} "
                f"(${actual_notional:.2f} total){stop_info}"
            )
        return position

    async def enter_short(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Execute SHORT entry: get price → size → sell short → trailing stop (buy to cover).
        Returns position dict on success, None on failure.
        """
        if not self.broker or not self.polygon:
            logger.error("Broker or Polygon client not available")
            self.last_order_error = "broker_or_polygon_unavailable"
            return None
        self.last_order_error = ""
        if symbol in getattr(self, "_halted_symbols", set()):
            logger.warning(f"Short entry blocked for halted symbol {symbol}")
            self.last_order_error = "halted"
            return None
        if symbol in self.positions:
            logger.warning(f"Duplicate short entry blocked for {symbol}")
            self.last_order_error = "duplicate_position"
            return None

        price = await asyncio.get_event_loop().run_in_executor(
            None, self.polygon.get_price, symbol
        )
        if price <= 0:
            logger.warning(f"Could not get price for {symbol}")
            self.last_order_error = "price_unavailable"
            return None
        signal_timestamp = float(sentiment_data.get("signal_timestamp", time.time()) or time.time())

        # Get buying power
        balances = await asyncio.get_event_loop().run_in_executor(
            None, self.broker.get_balances
        )
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)
        swing_only = bool(self.risk and getattr(self.risk, "is_swing_mode", None) and self.risk.is_swing_mode())

        extended = self.is_extended_hours()

        # Conviction from sentiment (inverted for shorts — bearish = high conviction)
        sent_score = sentiment_data.get("score", 0)
        if sent_score < -0.3:
            conviction = "high"
        elif sent_score < 0:
            conviction = "normal"
        else:
            conviction = "speculative"

        consensus_size_modifier = sentiment_data.get("consensus_size_modifier", 1.0)

        notional = self.risk.get_position_size(price, buying_power, conviction) if self.risk else 0
        notional *= consensus_size_modifier
        notional *= self._copy_trader_size_multiplier(sentiment_data, swing_only)
        share_mult = float(sentiment_data.get("share_notional_multiplier", 1.0) or 1.0)
        notional *= max(0.0, min(1.0, share_mult))
        adjusted_notional = self._apply_strategy_controls(symbol, sentiment_data, notional)
        if adjusted_notional is None:
            return None
        notional = adjusted_notional
        if extended:
            notional *= settings.EXTENDED_HOURS_SIZE_MULT
        equity = getattr(self.risk, 'equity', None) if self.risk else None
        if equity is None:
            equity = getattr(self.risk, '_equity', 25000) if self.risk else 25000
        notional = min(notional, equity * 0.25)
        notional = self._apply_high_confidence_min_notional(symbol, notional, sentiment_data, "short")
        min_notional = float(getattr(settings, "MIN_NOTIONAL", 25.0) or 25.0)
        if notional < min_notional:
            logger.warning(f"SHORT notional ${notional:.2f} below MIN_NOTIONAL ${min_notional:.2f} for {symbol} — rejecting")
            self.last_order_error = "below_min_notional"
            return None
        notional = self._apply_whole_share_floor_notional(symbol, price, notional, sentiment_data, "short")
        shares = int(notional / price) if price > 0 else 0
        if shares < 1:
            logger.warning(f"SHORT position size too small for {symbol} @ ${price:.2f}")
            self.last_order_error = "position_size_zero"
            return None

        atr_value = None
        try:
            from src.exit.exit_manager import ExitManager
            _tmp = ExitManager.__new__(ExitManager)
            _tmp.polygon = self.polygon
            atr_value = _tmp.calculate_atr(symbol)
        except Exception:
            atr_value = None

        logger.info(f"🩳 Shorting {symbol}: {shares}sh @ ${price:.2f} (${shares * price:.2f} total, conviction={conviction})")

        # Place short sell order. Extended hours requires a limit order.
        order = None
        entry_order_timestamp = time.time()
        try:
            if extended and bool(getattr(settings, "EXTENDED_HOURS_LIMIT_ONLY", True)):
                limit_price = round(price * 0.998, 2) if price >= 1.0 else round(price * 0.998, 4)
                if hasattr(self.broker, "place_limit_short"):
                    order = await asyncio.get_event_loop().run_in_executor(
                        None,
                        self.broker.place_limit_short,
                        symbol,
                        int(shares),
                        limit_price,
                        True,
                    )
                else:
                    import requests as req_lib

                    order_data = {
                        "symbol": symbol,
                        "qty": str(shares),
                        "side": "sell",
                        "type": "limit",
                        "limit_price": str(limit_price),
                        "time_in_force": "day",
                        "extended_hours": True,
                    }
                    resp = req_lib.post(
                        f"{self.broker._base_url}/v2/orders",
                        headers=self.broker._rest_headers(),
                        json=order_data,
                        timeout=10,
                    )
                    if resp.status_code in (200, 201):
                        order = resp.json()
                    else:
                        logger.error(f"Extended short limit failed: {resp.status_code} {resp.text[:200]}")
                        self.last_order_error = f"alpaca_short_rejected_{resp.status_code}"
                        if "cannot be sold short" in str(resp.text or "").lower():
                            if self.broker and hasattr(self.broker, "mark_unshortable"):
                                self.broker.mark_unshortable(symbol)
                                logger.warning(f"🩳 Marked {symbol} as unshortable for the session")
                        return None
            else:
                import requests as req_lib

                order_data = {
                    'symbol': symbol,
                    'qty': str(shares),
                    'side': 'sell',
                    'type': 'market',
                    'time_in_force': 'day',
                }
                resp = req_lib.post(
                    f'{self.broker._base_url}/v2/orders',
                    headers=self.broker._rest_headers(),
                    json=order_data,
                    timeout=10,
                )
                if resp.status_code in (200, 201):
                    order = resp.json()
                else:
                    logger.error(f"Short sell failed: {resp.status_code} {resp.text[:200]}")
                    self.last_order_error = f"alpaca_short_rejected_{resp.status_code}"
                    if "cannot be sold short" in str(resp.text or "").lower():
                        if self.broker and hasattr(self.broker, "mark_unshortable"):
                            self.broker.mark_unshortable(symbol)
                            logger.warning(f"🩳 Marked {symbol} as unshortable for the session")
                    return None
        except Exception as e:
            logger.error(f"Short sell error for {symbol}: {e}")
            self.last_order_error = "alpaca_short_exception"
            return None

        # Place hard stop (buy to cover) for the short during regular hours.
        hard_stop_order = None
        protection_failed = False
        if swing_only:
            logger.info(f"🌙 Swing-only short entry for {symbol}: broker hard stop deferred until same-day exit budget returns")
        elif not extended and order and hasattr(self.broker, "place_stop_loss_order"):
            await asyncio.sleep(1)
            try:
                order_status = str(order.get("status", "") or "").lower()
                try:
                    stop_qty = int(float(order.get("filled_qty", 0) or 0))
                except Exception:
                    stop_qty = 0
                if stop_qty >= 1 and order_status in {"filled", "partially_filled"}:
                    stop_entry_price = float(order.get("filled_avg_price", price) or price)
                    hard_stop_order, protection_failed = await self._place_hard_stop_order(
                        symbol,
                        stop_qty,
                        stop_entry_price,
                        "short",
                        hard_stop_pct=self._initial_hard_stop_profile(sentiment_data, extended)[0],
                    )
                else:
                    logger.info(
                        f"⏳ Deferring short hard stop for {symbol}: entry status={order_status or 'unknown'} "
                        f"filled_qty={stop_qty}"
                    )

                if hard_stop_order:
                    logger.success(
                        f"🛡️ SHORT hard stop set: {symbol} {stop_qty}sh @ ${float(hard_stop_order.get('stop_price', 0) or 0):.2f}"
                    )
                else:
                    logger.warning(f"⚠️ SHORT hard stop FAILED for {symbol}")
            except Exception as e:
                logger.warning(f"⚠️ SHORT hard stop error for {symbol}: {e}")

        signal_sources = sentiment_data.get("signal_sources", ["unknown"])
        if isinstance(signal_sources, str):
            signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
        if not isinstance(signal_sources, list):
            signal_sources = ["unknown"]
        if not signal_sources:
            signal_sources = ["unknown"]

        fill_timestamp = self._parse_iso_ts(order.get("filled_at")) if isinstance(order, dict) else None
        fill_timestamp_source = "order_response" if fill_timestamp is not None else "unknown"
        try:
            fill_price = float(order.get("filled_avg_price", price) or price)
        except Exception:
            fill_price = price
        entry_price = fill_price if fill_price > 0 else price
        try:
            requested_qty = float(shares)
        except Exception:
            requested_qty = 0.0
        try:
            order_qty = float(order.get("qty", requested_qty) or requested_qty)
        except Exception:
            order_qty = requested_qty
        try:
            filled_qty = float(order.get("filled_qty", order_qty) or order_qty)
        except Exception:
            filled_qty = 0.0
        actual_qty = filled_qty if filled_qty > 0 else order_qty
        if actual_qty <= 0:
            actual_qty = requested_qty
        if actual_qty <= 0:
            logger.error(f"Failed to determine short filled quantity for {symbol}")
            return None
        order_status = str(order.get("status", "") or "").lower()
        if not order_status:
            order_status = "filled"
        actual_notional = entry_price * actual_qty

        entry_votes = dict(sentiment_data.get("entry_model_votes", {}) or {})
        risk_constraints = list(sentiment_data.get("risk_constraints_applied", []) or [])
        signal_tier = str(sentiment_data.get("signal_tier", "tier_2") or "tier_2")
        holding_horizon = str(sentiment_data.get("holding_horizon", "intraday") or "intraday")
        market_regime = str(sentiment_data.get("market_regime", "mixed") or "mixed")
        hard_stop_pct, hard_stop_flags = self._initial_hard_stop_profile(sentiment_data, extended)
        hard_stop_price = ProfitRatchet.price_for_pnl(entry_price, hard_stop_pct, "short")
        position = {
            "symbol": symbol,
            "side": "short",
            "entry_price": entry_price,
            "fill_price": fill_price,
            "quantity": actual_qty,
            "entry_time": time.time(),
            "signal_timestamp": signal_timestamp,
            "entry_order_timestamp": entry_order_timestamp,
            "fill_timestamp": fill_timestamp,
            "fill_timestamp_source": fill_timestamp_source,
            "sentiment_at_entry": sentiment_data.get("score", 0),
            "peak_price": entry_price,
            "order_id": order.get("id", ""),
            "entry_order_id": order.get("id", ""),
            "partial_exit": False,
            "extended_hours_entry": extended,
            "conviction_level": conviction,
            "risk_tier": self.risk.get_risk_tier().get("name", "?") if self.risk else "?",
            "notional": actual_notional,
            "trail_pct": ProfitRatchet.RATCHET_TRAIL_PCT,
            "trailing_stop_order_id": None,
            "has_trailing_stop": False,
            "hard_stop_price": hard_stop_price,
            "hard_stop_pct": hard_stop_pct,
            "hard_stop_flags": list(hard_stop_flags),
            "hard_stop_order_id": hard_stop_order.get("id") if hard_stop_order else None,
            "ratchet_limit_order_id": None,
            "ratchet_peak_pnl_pct": 0.0,
            "ratchet_floor_pct": None,
            "ratchet_order_type": None,
            "protection_failed": protection_failed,
            "order_status": order_status,
            "order_state": {
                "entry": order_status,
                "hard_stop": "placed" if hard_stop_order else ("deferred" if extended or swing_only else "missing"),
                "ratchet": "inactive",
            },
            "strategy_tag": normalize_strategy_tag(sentiment_data.get("strategy_tag", "unknown")),
            "signal_tier": signal_tier,
            "holding_horizon": holding_horizon,
            "market_regime": market_regime,
            "session_type": str(sentiment_data.get("session_type", "") or ""),
            "entry_reason_code": sentiment_data.get("entry_reason_code", "jury_consensus"),
            "entry_model_votes": entry_votes,
            "risk_constraints_applied": risk_constraints,
            "entry_path": sentiment_data.get("entry_path", "jury"),
            "signal_sources": signal_sources,
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "signal_price": sentiment_data.get("signal_price", price),
            "decision_price": sentiment_data.get("decision_price", price),
            "intended_notional": float(notional or 0),
            "actual_notional": actual_notional,
            "intended_qty": float(requested_qty or 0),
            "actual_qty": actual_qty,
            "entry_quality": sentiment_data.get("entry_quality", "neutral"),
            "overnight_context": sentiment_data.get("overnight_context", ""),
            "allocator_state": sentiment_data.get("allocator_state", "neutral"),
            "allocator_alignment": sentiment_data.get("allocator_alignment", "neutral"),
            "allocator_budget_pct": float(sentiment_data.get("allocator_budget_pct", 0.0) or 0.0),
            "allocator_exposure_pct": float(sentiment_data.get("allocator_exposure_pct", 0.0) or 0.0),
            "allocator_remaining_budget_pct": float(sentiment_data.get("allocator_remaining_budget_pct", 0.0) or 0.0),
            "allocator_size_multiplier": float(sentiment_data.get("allocator_size_multiplier", 1.0) or 1.0),
            "allocator_reason": sentiment_data.get("allocator_reason", "allocator_ok"),
            "allocator_reason_codes": list(sentiment_data.get("allocator_reason_codes", []) or []),
            "allocator_status": sentiment_data.get("allocator_status", "hold"),
            "allocator_recommended_action": sentiment_data.get("allocator_recommended_action", "hold"),
            "allocator_control_state": sentiment_data.get("allocator_control_state", "active"),
            "dead_money_tightened": False,
            "dead_money": False,
            "anomaly_flags": list(sentiment_data.get("anomaly_flags", []) or []),
            "scout_escalated": bool(sentiment_data.get("scout_escalated", False)),
            "copy_trader_context": sentiment_data.get("copy_trader_context", ""),
            "copy_trader_handles": list(sentiment_data.get("copy_trader_handles", []) or []),
            "copy_trader_signal_count": int(sentiment_data.get("copy_trader_signal_count", 0) or 0),
            "copy_trader_convergence": int(sentiment_data.get("copy_trader_convergence", 0) or 0),
            "copy_trader_weight": float(sentiment_data.get("copy_trader_weight", 1.0) or 1.0),
            "swing_only": swing_only,
            "_exit_recorded": False,
        }
        position.update(self._extract_setup_metadata(sentiment_data))
        position["symbol_state"] = "live_position"
        self.positions[symbol] = position
        stop_info = (
            f" 🛡️ stop=${hard_stop_price:.2f}"
            if position["hard_stop_order_id"]
            else " ⚠️ NO HARD STOP"
        )
        logger.success(f"🩳 SHORTED: {actual_qty:.4f} {symbol} @ ${entry_price:.2f} (${actual_notional:.2f}){stop_info}")
        return position

    async def add_to_scout(self, symbol: str, sentiment_data: Dict) -> Optional[Dict]:
        """
        Escalate a fast-path scout position to full size.
        Only valid for breakout_fast_path positions and only once.
        Must pass persistent entry controls.
        """
        pos = self.positions.get(symbol)
        if not pos:
            return None
        if pos.get("strategy_tag") != "breakout_fast_path":
            return None
        if pos.get("scout_escalated"):
            return None
        if pos.get("side", "long") != "long":
            return None
        if not self.broker or not self.polygon:
            return None

        from src.data.entry_controls import is_entry_blocked
        blocked, reason = is_entry_blocked(symbol)
        if blocked:
            logger.info(f"Scout escalation blocked for {symbol}: {reason}")
            return None

        current_positions = self.get_positions()
        if self.risk and hasattr(self.risk, "can_trade") and not self.risk.can_trade():
            return None
        if self.risk and not self.risk.can_enter_sector(symbol, current_positions):
            return None

        price = await asyncio.get_event_loop().run_in_executor(None, self.polygon.get_price, symbol)
        if price <= 0:
            return None

        balances = await asyncio.get_event_loop().run_in_executor(None, self.broker.get_balances)
        buying_power = self.risk.get_buying_power_field(balances) if self.risk else balances.get("buying_power", 0)

        sent_score = float(sentiment_data.get("score", pos.get("sentiment_at_entry", 0)) or 0)
        if sent_score > 0.6:
            conviction = "high"
        elif sent_score < 0.1:
            conviction = "speculative"
        else:
            conviction = "normal"

        consensus_size_modifier = float(sentiment_data.get("consensus_size_modifier", 1.0) or 1.0)
        if self.risk:
            target_notional = self.risk.get_position_size(price, buying_power, conviction) * consensus_size_modifier
        else:
            target_notional = float(pos.get("notional", 0) or 0)

        current_qty = float(pos.get("quantity", 0) or 0)
        current_notional = float(pos.get("entry_price", price) or price) * current_qty
        add_notional = max(0.0, target_notional - current_notional)
        if add_notional <= 0:
            return None

        if self.risk:
            add_shares = self.risk.get_shares(price, add_notional)
        else:
            add_shares = add_notional / price
        add_qty = int(add_shares)
        if add_qty < 1:
            return None

        extended = self.is_extended_hours()
        entry_order_timestamp = time.time()
        order = None
        if extended:
            limit_price = round(price * 1.002, 2)
            if hasattr(self.broker, "place_limit_buy_extended"):
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_limit_buy_extended, symbol, add_qty, limit_price
                )
            else:
                order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_limit_buy, symbol, add_qty, limit_price, True
                )
        elif hasattr(self.broker, "smart_buy"):
            order = await asyncio.get_event_loop().run_in_executor(None, self.broker.smart_buy, symbol, add_notional)
        else:
            limit_price = round(price * 1.002, 2)
            order = await asyncio.get_event_loop().run_in_executor(
                None, self.broker.place_limit_buy, symbol, add_qty, limit_price
            )
        if not order:
            return None

        try:
            filled_qty = float(order.get("filled_qty", order.get("qty", add_qty)) or add_qty)
        except Exception:
            filled_qty = float(add_qty)
        if filled_qty <= 0:
            filled_qty = float(add_qty)
        try:
            add_fill_price = float(order.get("filled_avg_price", price) or price)
        except Exception:
            add_fill_price = price

        new_qty = current_qty + filled_qty
        if new_qty <= 0:
            return None
        old_cost = float(pos.get("entry_price", price) or price) * current_qty
        add_cost = add_fill_price * filled_qty
        new_entry_price = (old_cost + add_cost) / new_qty

        pos["quantity"] = new_qty
        pos["entry_price"] = new_entry_price
        pos["fill_price"] = add_fill_price
        pos["notional"] = new_entry_price * new_qty
        pos["actual_notional"] = pos["notional"]
        pos["actual_qty"] = new_qty
        pos["intended_notional"] = max(float(pos.get("intended_notional", 0) or 0), float(target_notional or 0))
        pos["intended_qty"] = max(float(pos.get("intended_qty", 0) or 0), float(new_qty))
        pos["order_id"] = order.get("id", pos.get("order_id", ""))
        pos["entry_order_timestamp"] = entry_order_timestamp
        pos["signal_timestamp"] = pos.get("signal_timestamp", sentiment_data.get("signal_timestamp"))

        fill_timestamp = self._parse_iso_ts(order.get("filled_at"))
        if fill_timestamp is not None:
            pos["fill_timestamp"] = fill_timestamp
            pos["fill_timestamp_source"] = "order_response"
        else:
            pos.setdefault("fill_timestamp", None)
            pos.setdefault("fill_timestamp_source", "unknown")

        pos["decision_confidence"] = sentiment_data.get("consensus_confidence", pos.get("decision_confidence", 0))
        pos["provider_used"] = sentiment_data.get("provider_used", pos.get("provider_used", ""))
        pos["scout_escalated"] = True

        trail_pct = sentiment_data.get("jury_trail_pct", pos.get("trail_pct", 3.0))
        trail_pct = max(1.0, min(5.0, float(trail_pct)))
        pos["trail_pct"] = trail_pct
        if hasattr(self.broker, "place_trailing_stop"):
            try:
                trail_order = await asyncio.get_event_loop().run_in_executor(
                    None, self.broker.place_trailing_stop, symbol, int(new_qty), trail_pct
                )
                if trail_order:
                    pos["has_trailing_stop"] = True
                    pos["trailing_stop_order_id"] = trail_order.get(
                        "id", pos.get("trailing_stop_order_id")
                    )
            except Exception as e:
                logger.warning(f"Could not refresh trailing stop after scout add for {symbol}: {e}")

        logger.success(
            f"⚡ Scout escalated: {symbol} +{filled_qty:.2f} -> {new_qty:.2f} shares @ avg ${new_entry_price:.2f}"
        )
        return pos

    def _load_brokerage_positions(self):
        """Load existing positions from brokerage into tracking."""
        if not self.broker:
            return
        try:
            brokerage_positions = self.broker.get_positions()
            self.sync_positions_from_brokerage(brokerage_positions)
            # Recover existing hard-stop / ratchet orders into local state.
            try:
                open_orders = self.broker.get_orders(status="open")
                for order in open_orders:
                    sym = order.get("symbol", "")
                    pos = self.positions.get(sym)
                    if not pos:
                        continue
                    otype = str(order.get("type", "") or "").lower()
                    side = str(order.get("side", "") or "").lower()
                    client_order_id = str(order.get("client_order_id", "") or "")
                    expected_exit_side = "buy" if pos.get("side", "long") == "short" else "sell"
                    if side != expected_exit_side:
                        continue
                    if otype == "stop":
                        pos["hard_stop_order_id"] = order.get("id", "")
                    elif "ratchet" in client_order_id or otype in {"limit", "stop_limit"}:
                        pos["ratchet_limit_order_id"] = order.get("id", "")
                        pos["ratchet_order_type"] = otype
            except Exception as e:
                logger.warning(f"Could not recover existing protection orders: {e}")

            logger.success(f"Loaded {len(self.positions)} existing positions from Alpaca")
        except Exception as e:
            logger.error(f"Failed to load brokerage positions: {e}")

    def sync_positions_from_brokerage(self, brokerage_positions: Optional[List[Dict]] = None) -> int:
        """Upsert Alpaca positions into local tracking and keep quantities in sync."""
        if brokerage_positions is None:
            if not self.broker:
                return 0
            brokerage_positions = self.broker.get_positions()
        self._recently_removed_positions = self._prune_recently_removed_positions(
            getattr(self, "_recently_removed_positions", {}) or {}
        )

        missing_symbols = [
            str(p.get("symbol", "")).upper()
            for p in (brokerage_positions or [])
            if p.get("symbol") and p.get("symbol") not in self.positions
        ]
        closed_orders = None
        open_orders = None
        if missing_symbols and self.broker and hasattr(self.broker, "get_orders"):
            try:
                closed_orders = self.broker.get_orders(status="closed")
            except Exception as e:
                logger.warning(f"Could not fetch closed orders for carryover entry times: {e}")
            try:
                open_orders = self.broker.get_orders(status="open")
            except Exception as e:
                logger.warning(f"Could not fetch open orders for broker resync diagnostics: {e}")

        updates = 0
        for p in brokerage_positions or []:
            sym = p.get("symbol", "")
            if not sym:
                continue
            raw_qty = float(p.get("quantity", 0) or 0)
            side = p.get("side")
            if side not in ("long", "short"):
                side = "short" if raw_qty < 0 else "long"
            qty = abs(raw_qty)
            if qty <= 0:
                continue
            avg_price = float(p.get("average_price", 0) or 0)
            cur_price = float(p.get("current_price", avg_price) or avg_price)

            existing = self.positions.get(sym)
            if existing:
                old_qty = float(existing.get("quantity", 0) or 0)
                existing_hard_stop_pct, existing_hard_stop_flags = ProfitRatchet.initial_hard_stop_profile(existing)
                existing.setdefault("hard_stop_pct", existing_hard_stop_pct)
                existing.setdefault("hard_stop_flags", list(existing_hard_stop_flags))
                existing.setdefault(
                    "hard_stop_price",
                    ProfitRatchet.price_for_pnl(avg_price, float(existing.get("hard_stop_pct") or existing_hard_stop_pct), side),
                )
                existing.setdefault("hard_stop_order_id", "")
                existing.setdefault("ratchet_limit_order_id", "")
                existing.setdefault("ratchet_peak_pnl_pct", 0.0)
                existing.setdefault("ratchet_floor_pct", None)
                existing.setdefault("ratchet_order_type", None)
                existing.setdefault("signal_tier", "tier_2")
                existing.setdefault("holding_horizon", "intraday")
                existing.setdefault("market_regime", "mixed")
                existing.setdefault("entry_quality", "neutral")
                existing.setdefault("overnight_context", "")
                existing.setdefault("allocator_status", "hold")
                existing.setdefault("allocator_recommended_action", "hold")
                existing.setdefault("allocator_control_state", "active")
                existing.setdefault("dead_money_tightened", False)
                existing.setdefault("dead_money", False)
                existing.setdefault("entry_reason_code", "unknown")
                existing.setdefault("entry_model_votes", {})
                existing.setdefault("risk_constraints_applied", [])
                existing.setdefault("order_state", {"entry": str(existing.get("order_status", "open") or "open")})
                existing["current_price"] = cur_price
                existing["broker_synced_at"] = time.time()
                if abs(old_qty - qty) > 1e-6:
                    existing["quantity"] = qty
                    existing["side"] = side
                    existing["actual_qty"] = qty
                    entry_price = float(existing.get("entry_price", avg_price) or avg_price)
                    existing["actual_notional"] = entry_price * qty
                    if qty < old_qty and qty < 1.0:
                        existing["_dust_remainder"] = True
                    elif qty >= 1.0:
                        existing.pop("_dust_remainder", None)
                    logger.warning(f"🔄 Synced {sym} quantity {old_qty:.4f} → {qty:.4f} from Alpaca")
                    updates += 1
                self.positions[sym] = normalize_position_context(existing)
                continue

            entry_time, entry_time_source = self._estimate_carryover_entry_time(sym, side, closed_orders)
            recent_removed = (getattr(self, "_recently_removed_positions", {}) or {}).get(sym)
            recent_snapshot = dict((recent_removed or {}).get("position", {}) or {})
            meaningful_local_context = self._restored_snapshot_has_meaningful_context(recent_snapshot)
            reload_reason = "broker_sync_missing_local"
            open_exit_order = None
            if recent_removed:
                removed_at = float(recent_removed.get("removed_at", 0) or 0)
                age_seconds = max(0.0, time.time() - removed_at)
                if age_seconds <= 900:
                    expected_exit_side = "buy" if str(side).lower() == "short" else "sell"
                    for order in open_orders or []:
                        if str(order.get("symbol", "")).upper() != sym:
                            continue
                        if str(order.get("side", "")).lower() != expected_exit_side:
                            continue
                        open_exit_order = order
                        break
                    reload_reason = (
                        "broker_still_open_after_local_removal_pending_exit"
                        if open_exit_order
                        else "broker_still_open_after_local_removal"
                    )
            _raw_tag = recent_snapshot.get("strategy_tag", "unknown")
            if _raw_tag in ("unknown", "", None) and recent_snapshot:
                try:
                    from src.data.signal_attribution import derive_strategy_tag
                    _raw_tag = derive_strategy_tag(recent_snapshot, direction=side)
                except Exception:
                    pass
            if _raw_tag in ("unknown", "", None):
                _raw_tag = "carryover"
            restored_strategy_tag = normalize_strategy_tag(_raw_tag, fallback="unknown", allow_artifacts=True)
            if is_artifact_strategy_tag(restored_strategy_tag):
                restored_strategy_tag = "unknown"
            anomaly_flags = list(recent_snapshot.get("anomaly_flags", []) or [])
            if recent_removed:
                anomaly_flags = [
                    flag
                    for flag in anomaly_flags
                    if str(flag or "").strip().lower()
                    not in {"carryover_sync", "broker_reloaded_after_local_removal"}
                ]
            normalized_anomaly_flags = {
                str(flag or "").strip().lower()
                for flag in anomaly_flags
                if str(flag or "").strip()
            }
            if (
                not meaningful_local_context
                and not recent_removed
                and "carryover_sync" not in normalized_anomaly_flags
            ):
                anomaly_flags.append("carryover_sync")
            restored_position = normalize_position_context({
                "symbol": sym,
                "side": side,
                "entry_price": avg_price,
                "quantity": qty,
                "entry_time": entry_time,
                "entry_time_source": entry_time_source,
                "signal_timestamp": None,
                "entry_order_timestamp": None,
                "fill_timestamp": None,
                "fill_timestamp_source": "unknown",
                "sentiment_at_entry": 0,
                "peak_price": max(avg_price, cur_price) if side == "long" else min(avg_price, cur_price),
                "order_id": "",
                "partial_exit": False,
                "from_brokerage": True,
                "strategy_tag": restored_strategy_tag,
                "entry_path": recent_snapshot.get("entry_path", "broker_sync"),
                "signal_sources": list(recent_snapshot.get("signal_sources", ["broker_sync"]) or ["broker_sync"]),
                "decision_confidence": recent_snapshot.get("decision_confidence", 0),
                "provider_used": recent_snapshot.get("provider_used", ""),
                "signal_price": avg_price,
                "decision_price": avg_price,
                "intended_notional": avg_price * qty,
                "actual_notional": avg_price * qty,
                "intended_qty": qty,
                "actual_qty": qty,
                "hard_stop_price": recent_snapshot.get("hard_stop_price"),
                "hard_stop_pct": recent_snapshot.get("hard_stop_pct"),
                "hard_stop_flags": list(recent_snapshot.get("hard_stop_flags", []) or []),
                "hard_stop_order_id": "",
                "ratchet_limit_order_id": "",
                "ratchet_peak_pnl_pct": max(
                    0.0,
                    ProfitRatchet.calc_pnl_pct(
                        avg_price,
                        max(avg_price, cur_price) if side == "long" else min(avg_price, cur_price),
                        side,
                    ),
                ),
                "ratchet_floor_pct": None,
                "ratchet_order_type": None,
                "signal_tier": recent_snapshot.get("signal_tier", "tier_2"),
                "holding_horizon": recent_snapshot.get("holding_horizon", "intraday"),
                "market_regime": recent_snapshot.get("market_regime", "mixed"),
                "entry_quality": recent_snapshot.get("entry_quality", "neutral"),
                "overnight_context": recent_snapshot.get("overnight_context", ""),
                "entry_reason_code": recent_snapshot.get("entry_reason_code", "broker_sync"),
                "entry_model_votes": dict(recent_snapshot.get("entry_model_votes", {}) or {}),
                "risk_constraints_applied": list(recent_snapshot.get("risk_constraints_applied", []) or []),
                "allocator_status": recent_snapshot.get("allocator_status", "hold"),
                "allocator_recommended_action": recent_snapshot.get("allocator_recommended_action", "hold"),
                "allocator_control_state": recent_snapshot.get("allocator_control_state", "active"),
                "allocator_reason_codes": list(recent_snapshot.get("allocator_reason_codes", []) or []),
                "order_state": {
                    "entry": "open",
                    "hard_stop": "unknown",
                    "ratchet": "inactive",
                },
                "anomaly_flags": anomaly_flags,
                "scout_escalated": False,
                "swing_only": False,
                "_exit_recorded": False,
                "current_price": cur_price,
                "broker_synced_at": time.time(),
                "reload_reason": reload_reason,
                "reloaded_from_broker": bool(recent_removed),
            })
            restored_hard_stop_pct, restored_hard_stop_flags = ProfitRatchet.initial_hard_stop_profile(restored_position)
            restored_position["hard_stop_pct"] = float(
                restored_position.get("hard_stop_pct", restored_hard_stop_pct) or restored_hard_stop_pct
            )
            restored_position["hard_stop_flags"] = list(
                restored_position.get("hard_stop_flags", restored_hard_stop_flags) or restored_hard_stop_flags
            )
            restored_position["hard_stop_price"] = ProfitRatchet.price_for_pnl(
                avg_price,
                restored_position["hard_stop_pct"],
                side,
            )
            self.positions[sym] = restored_position
            if recent_removed and not meaningful_local_context and not open_exit_order:
                self.positions[sym]["anomaly_flags"] = list({
                    *self.positions[sym].get("anomaly_flags", []),
                    "broker_reloaded_after_local_removal",
                })
            if recent_removed:
                self.positions[sym]["last_exit_reason"] = recent_removed.get("last_exit_reason", "")
                self.positions[sym]["last_exit_attempt_at"] = recent_removed.get("removed_at", time.time())
                if open_exit_order:
                    self.positions[sym]["exit_pending"] = True
                    self.positions[sym]["exit_order_id"] = open_exit_order.get("id", "")
                    self.positions[sym]["exit_submitted_at"] = recent_removed.get("removed_at", time.time())
                logger.warning(
                    f"🔁 Reloaded {sym} from broker after local removal "
                    f"(reason={reload_reason}, qty={qty:.4f}, open_exit_order={open_exit_order.get('id', '') if open_exit_order else 'none'})"
                )
            side_tag = "SHORT" if side == "short" else "LONG"
            logger.info(
                f"📦 Loaded {side_tag} position: {qty:.4f} {sym} @ ${avg_price:.2f} "
                f"(current ${cur_price:.2f}, P&L ${p.get('open_pnl', 0):.2f}, entry={entry_time_source}, reload={reload_reason})"
            )
            updates += 1
        return updates

    def get_positions(self) -> List[Dict]:
        """Return list of tracked positions."""
        return list(self.positions.values())

    def get_recently_removed_position(self, symbol: str) -> Optional[Dict]:
        recent_removed = getattr(self, "_recently_removed_positions", {}) or {}
        entry = recent_removed.get(str(symbol or "").upper()) or {}
        snapshot = entry.get("position", {}) if isinstance(entry, dict) else {}
        return dict(snapshot or {}) if snapshot else None

    def remove_position(self, symbol: str):
        """Remove a position after full exit."""
        pos = self.positions.pop(symbol, None)
        if pos:
            if not hasattr(self, "_recently_removed_positions") or self._recently_removed_positions is None:
                self._recently_removed_positions = {}
            self._recently_removed_positions[str(symbol).upper()] = {
                "removed_at": time.time(),
                "last_exit_reason": pos.get("last_exit_reason", ""),
                "exit_order_id": pos.get("exit_order_id", ""),
                "quantity": pos.get("quantity", 0),
                "side": pos.get("side", "long"),
                "position": dict(pos),
            }
            self._recently_removed_positions = self._prune_recently_removed_positions(
                self._recently_removed_positions
            )
            try:
                persistence.save_recently_removed_positions(self._recently_removed_positions)
            except Exception:
                logger.debug(f"Could not persist removed position snapshot for {symbol}")

    def update_peak_price(self, symbol: str, current_price: float):
        """Update favorable excursion tracking for ratchet logic."""
        if symbol in self.positions:
            pos = self.positions[symbol]
            side = str(pos.get("side", "long") or "long").lower()
            peak_price = float(pos.get("peak_price", current_price) or current_price)
            if side == "short":
                if current_price < peak_price:
                    pos["peak_price"] = current_price
            elif current_price > peak_price:
                pos["peak_price"] = current_price
