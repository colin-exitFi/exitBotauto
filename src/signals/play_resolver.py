"""
Deterministic play resolver and trigger engine.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from src.signals.mode_classifier import ModeClassification, ModeFeatures, normalize_direction_constraint, normalize_mode


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _ttl_seconds_for_mode(mode: str) -> int:
    if mode.startswith("continuation"):
        return 30 * 60
    if mode.startswith("exhaustion"):
        return 60 * 60
    if mode.startswith("swing"):
        return 8 * 60 * 60
    return 15 * 60


@dataclass
class TriggerSpec:
    trigger_type: str
    params: Dict = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> Dict:
        payload = asdict(self)
        payload["params"] = dict(self.params or {})
        return payload


@dataclass
class PlayResolution:
    symbol: str
    mode: str
    direction_constraint: str
    timing_state: str
    best_play: str
    trigger: Optional[str]
    invalidation: Optional[str]
    hold_style: str
    classifier_confidence: float
    resolver_confidence: float
    reason_codes: List[str] = field(default_factory=list)
    expires_at: Optional[float] = None
    size_posture: str = "normal"
    entry_now: bool = False
    no_trade_reason: Optional[str] = None
    trigger_spec: Optional[TriggerSpec] = None

    def to_dict(self) -> Dict:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes or [])
        payload["trigger_spec"] = self.trigger_spec.to_dict() if self.trigger_spec else None
        return payload


def evaluate_trigger(features: ModeFeatures, trigger_spec: Optional[TriggerSpec]) -> bool:
    if not trigger_spec:
        return False

    params = dict(trigger_spec.params or {})
    trigger_type = str(trigger_spec.trigger_type or "").strip().lower()

    if trigger_type == "already_live":
        return True

    if trigger_type == "vwap_reclaim_hold":
        min_vwap = _to_float(params.get("min_vwap_distance_pct"), 0.0)
        min_volume = _to_float(params.get("min_volume_accel"), 0.15)
        return (
            (features.vwap_distance_pct or 0.0) >= min_vwap
            and features.volume_accel >= min_volume
            and not features.losing_vwap
        )

    if trigger_type == "vwap_reject_breakdown":
        max_vwap = _to_float(params.get("max_vwap_distance_pct"), -0.05)
        min_volume = _to_float(params.get("min_volume_accel"), 0.1)
        return (
            (features.vwap_distance_pct or 0.0) <= max_vwap
            and features.volume_accel >= min_volume
            and not features.reclaiming_vwap
        )

    if trigger_type == "fade_failure_reject":
        min_daily_pct = _to_float(params.get("min_daily_pct"), 20.0)
        max_volume_accel = _to_float(params.get("max_volume_accel"), 0.0)
        return (
            features.daily_pct >= min_daily_pct
            and features.losing_vwap
            and features.volume_accel <= max_volume_accel
        )

    if trigger_type == "pullback_hold_reclaim":
        min_vwap = _to_float(params.get("min_vwap_distance_pct"), 0.0)
        min_volume = _to_float(params.get("min_volume_accel"), 0.05)
        max_range = _to_float(params.get("max_range_pct"), 90.0)
        return (
            (features.vwap_distance_pct or 0.0) >= min_vwap
            and features.volume_accel >= min_volume
            and features.range_pct <= max_range
        )

    return False


def resolve_play(
    features: ModeFeatures,
    classification: ModeClassification,
    now_ts: Optional[float] = None,
) -> PlayResolution:
    now_ts = float(now_ts or time.time())
    mode = normalize_mode(classification.mode)
    direction_constraint = normalize_direction_constraint(classification.direction_constraint)
    base_reasons = list(classification.reason_codes or [])
    ttl = _ttl_seconds_for_mode(mode)
    expires_at = now_ts + ttl

    if mode == "invalid":
        return PlayResolution(
            symbol=features.symbol,
            mode=mode,
            direction_constraint="none",
            timing_state="no_edge",
            best_play="no_valid_setup",
            trigger=None,
            invalidation=None,
            hold_style=features.holding_horizon or "intraday",
            classifier_confidence=classification.classifier_confidence,
            resolver_confidence=0.0,
            reason_codes=base_reasons or ["no_clear_setup"],
            expires_at=expires_at,
            size_posture="zero",
            entry_now=False,
            no_trade_reason=(base_reasons[0] if base_reasons else "no_clear_setup"),
            trigger_spec=None,
        )

    if mode == "continuation_long":
        invalidation = "lose VWAP or lose pullback low"
        if False:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="no_edge",
                best_play="continuation_long",
                trigger=None,
                invalidation=invalidation,
                hold_style="intraday",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.2,
                reason_codes=base_reasons + ["timing_not_live"],
                expires_at=expires_at,
                size_posture="zero",
                entry_now=False,
                no_trade_reason="too_extended" if features.range_pct >= 98.0 else "volume_not_confirming",
            )

        # Classifier already approved this setup. Enter unless volume is completely dead.
        if features.volume_accel >= -0.8 and features.entry_quality in {"pullback", "neutral", "at_highs"}:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="enter_now",
                best_play="continuation_long",
                trigger="volume confirming with acceptable entry quality",
                invalidation=invalidation,
                hold_style="intraday",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.78,
                reason_codes=base_reasons + ["timing_live"],
                expires_at=expires_at,
                size_posture="normal",
                entry_now=True,
                trigger_spec=TriggerSpec("already_live", {}, "entry conditions are already live"),
            )

        return PlayResolution(
            symbol=features.symbol,
            mode=mode,
            direction_constraint=direction_constraint,
            timing_state="wait_for_trigger",
            best_play="continuation_long",
            trigger="volume re-acceleration needed",
            invalidation=invalidation,
            hold_style="intraday",
            classifier_confidence=classification.classifier_confidence,
            resolver_confidence=0.55,
            reason_codes=base_reasons + ["waiting_for_volume"],
            expires_at=expires_at,
            size_posture="normal",
            entry_now=False,
            trigger_spec=TriggerSpec(
                "vwap_reclaim_hold",
                {"bars": 2, "min_vwap_distance_pct": -999.0, "min_volume_accel": 0.0},
                "volume re-acceleration",
            ),
        )

    if mode == "continuation_short":
        invalidation = "reclaim VWAP or recover breakdown level"
        if False:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="no_edge",
                best_play="continuation_short",
                trigger=None,
                invalidation=invalidation,
                hold_style="intraday",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.2,
                reason_codes=base_reasons + ["timing_not_live"],
                expires_at=expires_at,
                size_posture="zero",
                entry_now=False,
                no_trade_reason="too_extended_to_downside" if features.range_pct <= 2.0 else "volume_not_confirming",
            )

        if features.volume_accel >= -0.5:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="enter_now",
                best_play="continuation_short",
                trigger="volume confirming downside",
                invalidation=invalidation,
                hold_style="intraday",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.78,
                reason_codes=base_reasons + ["timing_live"],
                expires_at=expires_at,
                size_posture="normal",
                entry_now=True,
                trigger_spec=TriggerSpec("already_live", {}, "entry conditions are already live"),
            )

        return PlayResolution(
            symbol=features.symbol,
            mode=mode,
            direction_constraint=direction_constraint,
            timing_state="wait_for_trigger",
            best_play="continuation_short",
            trigger="volume re-acceleration needed on downside",
            invalidation=invalidation,
            hold_style="intraday",
            classifier_confidence=classification.classifier_confidence,
            resolver_confidence=0.55,
            reason_codes=base_reasons + ["waiting_for_volume"],
            expires_at=expires_at,
            size_posture="normal",
            entry_now=False,
            trigger_spec=TriggerSpec(
                "vwap_reject_breakdown",
                {"bars": 2, "max_vwap_distance_pct": 999.0, "min_volume_accel": 0.0},
                "volume re-acceleration on downside",
            ),
        )

    if mode == "exhaustion_fade_short":
        invalidation = "reclaim HOD on expanding volume"
        if features.reclaiming_vwap and features.volume_accel > 0.2:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="no_edge",
                best_play="exhaustion_fade_short",
                trigger=None,
                invalidation=invalidation,
                hold_style="intraday",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.28,
                reason_codes=base_reasons + ["squeeze_not_failed"],
                expires_at=expires_at,
                size_posture="zero",
                entry_now=False,
                no_trade_reason="fade_not_confirmed",
            )

        # Exhaustion fade: if volume is decelerating, the move is dying. Enter now.
        if features.volume_accel <= 0.0:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="enter_now",
                best_play="exhaustion_fade_short",
                trigger="volume decelerating on extended move",
                invalidation=invalidation,
                hold_style="intraday",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.82,
                reason_codes=base_reasons + ["timing_live"],
                expires_at=expires_at,
                size_posture="reduced",
                entry_now=True,
                trigger_spec=TriggerSpec("already_live", {}, "entry conditions are already live"),
            )

        return PlayResolution(
            symbol=features.symbol,
            mode=mode,
            direction_constraint=direction_constraint,
            timing_state="wait_for_trigger",
            best_play="exhaustion_fade_short",
            trigger="volume needs to stop accelerating",
            invalidation=invalidation,
            hold_style="intraday",
            classifier_confidence=classification.classifier_confidence,
            resolver_confidence=0.65,
            reason_codes=base_reasons + ["waiting_for_deceleration"],
            expires_at=expires_at,
            size_posture="reduced",
            entry_now=False,
            trigger_spec=TriggerSpec(
                "fade_failure_reject",
                {"min_daily_pct": 15.0, "max_volume_accel": 0.1},
                "volume deceleration confirms exhaustion",
            ),
        )

    if mode == "swing_catalyst_long":
        invalidation = "lose the catalyst support level or lose VWAP on failed reclaim"
        if features.range_pct >= 97.0 and features.volume_accel < 0.0:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="wait_for_trigger",
                best_play="swing_catalyst_long",
                trigger="wait for pullback support to hold and reclaim",
                invalidation=invalidation,
                hold_style=features.holding_horizon or "swing",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.58,
                reason_codes=base_reasons + ["waiting_for_pullback"],
                expires_at=expires_at,
                size_posture="capped",
                entry_now=False,
                trigger_spec=TriggerSpec(
                    "pullback_hold_reclaim",
                    {"min_vwap_distance_pct": 0.0, "min_volume_accel": 0.05, "max_range_pct": 90.0},
                    "hold the pullback and reclaim with fresh volume",
                ),
            )

        if (features.vwap_distance_pct or 0.0) >= 0.0 and features.spread_pct < 0.8:
            return PlayResolution(
                symbol=features.symbol,
                mode=mode,
                direction_constraint=direction_constraint,
                timing_state="enter_now",
                best_play="swing_catalyst_long",
                trigger="fresh catalyst with trend support is already live",
                invalidation=invalidation,
                hold_style=features.holding_horizon or "swing",
                classifier_confidence=classification.classifier_confidence,
                resolver_confidence=0.73,
                reason_codes=base_reasons + ["timing_live"],
                expires_at=expires_at,
                size_posture="capped",
                entry_now=True,
                trigger_spec=TriggerSpec("already_live", {}, "entry conditions are already live"),
            )

        return PlayResolution(
            symbol=features.symbol,
            mode=mode,
            direction_constraint=direction_constraint,
            timing_state="wait_for_trigger",
            best_play="swing_catalyst_long",
            trigger="hold the pullback and reclaim with fresh volume",
            invalidation=invalidation,
            hold_style=features.holding_horizon or "swing",
            classifier_confidence=classification.classifier_confidence,
            resolver_confidence=0.62,
            reason_codes=base_reasons + ["waiting_for_pullback"],
            expires_at=expires_at,
            size_posture="capped",
            entry_now=False,
            trigger_spec=TriggerSpec(
                "pullback_hold_reclaim",
                {"min_vwap_distance_pct": 0.0, "min_volume_accel": 0.05, "max_range_pct": 90.0},
                "hold the pullback and reclaim with fresh volume",
            ),
        )

    # General momentum: the classifier says the stock is moving but doesn't fit a specific pattern.
    # Let the jury evaluate it at reduced confidence. Enter now -- the stock is moving.
    if mode in ("general_momentum_long", "general_momentum_short"):
        direction_label = "long" if "long" in mode else "short"
        invalidation = f"reversal against {direction_label} direction"
        return PlayResolution(
            symbol=features.symbol,
            mode=mode,
            direction_constraint=direction_constraint,
            timing_state="enter_now",
            best_play=mode,
            trigger="general momentum detected -- jury evaluates timing",
            invalidation=invalidation,
            hold_style="intraday",
            classifier_confidence=classification.classifier_confidence,
            resolver_confidence=0.55,
            reason_codes=base_reasons + ["general_momentum_entry"],
            expires_at=expires_at,
            size_posture="reduced",
            entry_now=True,
            trigger_spec=TriggerSpec("already_live", {}, "general momentum -- stock is moving"),
        )

    return PlayResolution(
        symbol=features.symbol,
        mode="invalid",
        direction_constraint="none",
        timing_state="no_edge",
        best_play="no_valid_setup",
        trigger=None,
        invalidation=None,
        hold_style=features.holding_horizon or "intraday",
        classifier_confidence=classification.classifier_confidence,
        resolver_confidence=0.0,
        reason_codes=["flat_stock"],
        expires_at=expires_at,
        size_posture="zero",
        entry_now=False,
        no_trade_reason="flat_no_directional_edge",
    )
