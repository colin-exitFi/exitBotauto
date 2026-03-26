"""
Stable setup identity helpers.
"""

from __future__ import annotations

import hashlib
import json
from typing import Dict


SYMBOL_STATES = {
    "idle",
    "classified",
    "pending_trigger",
    "entry_submitted",
    "live_position",
    "cooldown",
    "shadow_only",
    "broker_blocked",
    "capital_blocked",
    "data_insufficient",
    "mode_conflict",
    "expired",
}


def normalize_symbol_state(value: object) -> str:
    state = str(value or "").strip().lower()
    if state in SYMBOL_STATES:
        return state
    return "idle"


def build_material_change_signature(
    *,
    mode: str,
    timing_state: str,
    direction_constraint: str,
    sentiment_pct: float,
    halt_count: int,
    reclaiming_vwap: bool,
    losing_vwap: bool,
    volume_accel: float,
) -> str:
    payload = {
        "mode": str(mode or "").strip().lower(),
        "timing_state": str(timing_state or "").strip().lower(),
        "direction_constraint": str(direction_constraint or "").strip().lower(),
        "sentiment_bucket": int(float(sentiment_pct or 0.0) // 10) * 10,
        "halt_count": int(halt_count or 0),
        "vwap_state": "reclaiming" if reclaiming_vwap else ("losing" if losing_vwap else "neutral"),
        "volume_accel_sign": -1 if float(volume_accel or 0.0) < 0 else (1 if float(volume_accel or 0.0) > 0 else 0),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def build_setup_id(symbol: str, mode: str, created_at: float, feature_signature: str) -> str:
    symbol_key = str(symbol or "").upper().strip() or "UNKNOWN"
    mode_key = str(mode or "invalid").strip().lower() or "invalid"
    bucket = int(float(created_at or 0.0) // 300)
    return f"{symbol_key}:{mode_key}:{bucket}:{feature_signature}"


def setup_identity_payload(
    *,
    symbol: str,
    mode: str,
    created_at: float,
    feature_signature: str,
    symbol_state: str,
) -> Dict:
    return {
        "setup_id": build_setup_id(symbol, mode, created_at, feature_signature),
        "material_change_signature": feature_signature,
        "symbol_state": normalize_symbol_state(symbol_state),
    }
