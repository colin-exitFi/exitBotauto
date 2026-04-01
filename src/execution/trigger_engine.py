"""
Trigger Engine — continuous background monitoring of pending setups.

Subscribes to MarketStream for pending symbols, evaluates triggers on each
price update, and pushes fired triggers into a priority queue for immediate
processing by the main loop.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from loguru import logger

from src.signals.mode_classifier import ModeFeatures, build_mode_features, classify_mode
from src.signals.play_resolver import TriggerSpec, evaluate_trigger


MODE_HOLD_STYLE = {
    "continuation_long": "intraday",
    "continuation_short": "intraday",
    "exhaustion_fade_short": "intraday",
    "swing_catalyst_long": "multiday",
    "general_momentum_long": "intraday",
    "general_momentum_short": "intraday",
}

STALE_THRESHOLDS = {
    "continuation_long": 300,
    "continuation_short": 300,
    "exhaustion_fade_short": 600,
    "swing_catalyst_long": 3600,
    "general_momentum_long": 300,
    "general_momentum_short": 300,
}


@dataclass
class TriggerResult:
    symbol: str
    setup_id: str
    trigger_type: str
    fired: bool
    expired: bool
    invalidated: bool
    mode_changed: bool
    new_mode: Optional[str] = None
    reason: str = ""
    timestamp: float = 0.0
    candidate_snapshot: Optional[Dict] = None


@dataclass
class PendingSetup:
    symbol: str
    setup_id: str
    mode: str
    direction_constraint: str
    trigger_spec: Optional[Dict] = None
    invalidation_type: Optional[str] = None
    invalidation_params: Optional[Dict] = None
    created_at: float = 0.0
    expires_at: float = 0.0
    candidate_snapshot: Optional[Dict] = None
    shadow_mode: bool = False
    source_priority: str = "normal"
    feature_snapshot_id: Optional[str] = None


class TriggerEngine:
    """
    Background task that monitors pending setups and fires triggers.

    Usage:
        engine = TriggerEngine()
        engine.load_pending(pending_setups_list)
        engine.set_fire_callback(on_trigger_fired)
        await engine.start(market_stream)
    """

    def __init__(self, state_store=None):
        self._pending: Dict[str, PendingSetup] = {}
        self._fire_callback: Optional[Callable] = None
        self._running = False
        self._batch_interval = 30.0
        self._store = state_store
        self._stats = {
            "triggers_fired": 0,
            "triggers_expired": 0,
            "triggers_invalidated": 0,
            "mode_reclassifications": 0,
        }

    def set_fire_callback(self, callback: Callable):
        self._fire_callback = callback

    def load_pending(self, setups: Optional[List[Dict]] = None):
        """Load pending setups from provided list or SQLite store."""
        if setups is None and self._store:
            setups = self._store.get_active_pending_setups()
        for s in setups or []:
            symbol = str(s.get("symbol", "") or "").upper()
            setup_id = str(s.get("setup_id", s.get("_pending_setup_id", "")) or "")
            if not symbol:
                continue
            self._pending[f"{symbol}:{s.get('setup_mode', 'unknown')}"] = PendingSetup(
                symbol=symbol,
                setup_id=setup_id,
                mode=str(s.get("setup_mode", "unknown") or "unknown"),
                direction_constraint=str(s.get("direction_constraint", "none") or "none"),
                trigger_spec=s.get("trigger_spec"),
                invalidation_type=s.get("invalidation_type"),
                invalidation_params=s.get("invalidation_params"),
                created_at=float(s.get("created_at", time.time()) or time.time()),
                expires_at=float(s.get("expires_at", 0) or 0),
                candidate_snapshot=s.get("candidate_snapshot"),
                shadow_mode=bool(s.get("shadow_mode", False)),
                source_priority=str(s.get("source_priority", "normal") or "normal"),
                feature_snapshot_id=s.get("feature_snapshot_id"),
            )
        logger.info(f"TriggerEngine loaded {len(self._pending)} pending setups")

    def add_pending(self, setup: Dict):
        """Add a single pending setup and persist to SQLite."""
        symbol = str(setup.get("symbol", "") or "").upper()
        mode = str(setup.get("setup_mode", "unknown") or "unknown")
        key = f"{symbol}:{mode}"
        if key in self._pending:
            logger.debug(f"TriggerEngine: updating existing pending {key}")
        if self._store:
            try:
                self._store.upsert_pending_setup(setup)
            except Exception as e:
                logger.debug(f"TriggerEngine: SQLite persist failed: {e}")
        self._pending[key] = PendingSetup(
            symbol=symbol,
            setup_id=str(setup.get("setup_id", "") or ""),
            mode=mode,
            direction_constraint=str(setup.get("direction_constraint", "none") or "none"),
            trigger_spec=setup.get("trigger_spec"),
            invalidation_type=setup.get("invalidation_type"),
            invalidation_params=setup.get("invalidation_params"),
            created_at=float(setup.get("created_at", time.time()) or time.time()),
            expires_at=float(setup.get("expires_at", 0) or 0),
            candidate_snapshot=setup.get("candidate_snapshot"),
            shadow_mode=bool(setup.get("shadow_mode", False)),
            source_priority=str(setup.get("source_priority", "normal") or "normal"),
            feature_snapshot_id=setup.get("feature_snapshot_id"),
        )

    def remove_pending(self, symbol: str, mode: str):
        key = f"{symbol.upper()}:{mode}"
        self._pending.pop(key, None)

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pending_symbols(self) -> List[str]:
        return list({p.symbol for p in self._pending.values()})

    async def start(self, market_stream=None):
        """Start the trigger engine as a background task."""
        self._running = True
        if market_stream:
            try:
                symbols = self.pending_symbols
                if symbols:
                    market_stream.subscribe(symbols)
                    logger.info(f"TriggerEngine subscribed to {len(symbols)} pending symbols")
            except Exception as e:
                logger.debug(f"TriggerEngine stream subscribe failed: {e}")

        while self._running:
            try:
                results = self.evaluate_all_pending()
                for result in results:
                    if result.fired and self._fire_callback:
                        try:
                            if asyncio.iscoroutinefunction(self._fire_callback):
                                await self._fire_callback(result)
                            else:
                                self._fire_callback(result)
                        except Exception as e:
                            logger.error(f"TriggerEngine fire callback error: {e}")
            except Exception as e:
                logger.error(f"TriggerEngine evaluation error: {e}")

            await asyncio.sleep(self._batch_interval)

    def stop(self):
        self._running = False

    def evaluate_all_pending(self) -> List[TriggerResult]:
        """Evaluate all pending setups. Returns results for fired/expired/invalidated."""
        now = time.time()
        results: List[TriggerResult] = []
        to_remove: List[str] = []

        for key, setup in list(self._pending.items()):
            if setup.expires_at > 0 and now > setup.expires_at:
                results.append(TriggerResult(
                    symbol=setup.symbol,
                    setup_id=setup.setup_id,
                    trigger_type=setup.trigger_spec.get("trigger_type", "unknown") if setup.trigger_spec else "unknown",
                    fired=False, expired=True, invalidated=False, mode_changed=False,
                    reason="trigger_ttl_exceeded",
                    timestamp=now,
                ))
                to_remove.append(key)
                self._stats["triggers_expired"] += 1
                continue

            stale_threshold = STALE_THRESHOLDS.get(setup.mode, 300)
            data_age = now - setup.created_at
            if data_age > stale_threshold * 3:
                results.append(TriggerResult(
                    symbol=setup.symbol,
                    setup_id=setup.setup_id,
                    trigger_type="stale",
                    fired=False, expired=True, invalidated=False, mode_changed=False,
                    reason="data_too_stale",
                    timestamp=now,
                ))
                to_remove.append(key)
                self._stats["triggers_expired"] += 1
                continue

            if setup.trigger_spec and setup.candidate_snapshot:
                try:
                    features = build_mode_features(setup.candidate_snapshot)
                    ts = TriggerSpec(
                        trigger_type=setup.trigger_spec.get("trigger_type", ""),
                        params=setup.trigger_spec.get("params", {}),
                        description=setup.trigger_spec.get("description", ""),
                    )
                    if evaluate_trigger(features, ts):
                        results.append(TriggerResult(
                            symbol=setup.symbol,
                            setup_id=setup.setup_id,
                            trigger_type=ts.trigger_type,
                            fired=True, expired=False, invalidated=False, mode_changed=False,
                            reason="trigger_conditions_met",
                            timestamp=now,
                            candidate_snapshot=setup.candidate_snapshot,
                        ))
                        to_remove.append(key)
                        self._stats["triggers_fired"] += 1
                        continue
                except Exception as e:
                    logger.debug(f"TriggerEngine eval error for {setup.symbol}: {e}")

        for key in to_remove:
            self._pending.pop(key, None)

        return results

    def evaluate_new_triggers(
        self,
        features: ModeFeatures,
        trigger_spec: Optional[TriggerSpec],
    ) -> bool:
        """Evaluate trigger types including new ones added by the plan."""
        if not trigger_spec:
            return False

        if evaluate_trigger(features, trigger_spec):
            return True

        trigger_type = str(trigger_spec.trigger_type or "").strip().lower()
        params = dict(trigger_spec.params or {})

        if trigger_type == "hod_break_reaccel":
            min_volume = float(params.get("min_volume_mult", 1.5))
            return (
                features.range_pct >= 95.0
                and features.volume_accel >= min_volume
                and not features.losing_vwap
            )

        if trigger_type == "momentum_continuation":
            min_bars = int(params.get("min_bars_above_vwap", 2))
            min_volume = float(params.get("min_volume_mult", 1.5))
            return (
                (features.vwap_distance_pct or 0) > 0.1
                and features.volume_accel >= min_volume
                and not features.losing_vwap
            )

        if trigger_type == "first_bounce_failure":
            return (
                features.daily_pct < -5.0
                and features.losing_vwap
                and features.volume_accel < 0
            )

        if trigger_type == "loss_of_trend":
            return (
                features.losing_vwap
                and features.volume_accel < -0.1
                and features.range_pct < 30.0
            )

        return False

    def get_stats(self) -> Dict:
        return {
            **self._stats,
            "pending_count": len(self._pending),
            "pending_symbols": self.pending_symbols,
        }
