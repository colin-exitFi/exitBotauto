"""
Setup replay utilities for classifier/trigger postmortems.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List, Optional

from src.ai import trade_history
from src.data.setup_snapshots import load_setup_snapshots
from src.signals.mode_classifier import mode_features_from_dict
from src.signals.play_resolver import TriggerSpec, evaluate_trigger


def _day_key(ts: float) -> str:
    try:
        import zoneinfo

        return datetime.fromtimestamp(float(ts), zoneinfo.ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d")


def _normalize_symbol(symbol: Optional[str]) -> Optional[str]:
    text = str(symbol or "").upper().strip()
    return text or None


def _normalize_setup_id(setup_id: Optional[str]) -> Optional[str]:
    text = str(setup_id or "").strip()
    return text or None


def _trigger_live_for_snapshot(row: Dict) -> Optional[bool]:
    stored = row.get("trigger_live")
    if isinstance(stored, bool):
        return stored

    trigger_spec_payload = dict(row.get("trigger_spec", {}) or {})
    if not trigger_spec_payload:
        return None
    features = mode_features_from_dict(dict(row.get("mode_features", {}) or {}))
    if features is None:
        return None
    trigger = TriggerSpec(
        trigger_type=str(trigger_spec_payload.get("trigger_type", "") or ""),
        params=dict(trigger_spec_payload.get("params", {}) or {}),
        description=str(trigger_spec_payload.get("description", "") or ""),
    )
    try:
        return bool(evaluate_trigger(features, trigger))
    except Exception:
        return None


def _prepare_snapshot(row: Dict) -> Dict:
    jury = dict(row.get("jury_response", {}) or {})
    return {
        "snapshot_id": str(row.get("snapshot_id", "") or ""),
        "setup_id": str(row.get("setup_id", "") or ""),
        "symbol": str(row.get("symbol", "") or "").upper(),
        "recorded_at": float(row.get("recorded_at", 0) or 0),
        "day": _day_key(float(row.get("recorded_at", 0) or 0)) if float(row.get("recorded_at", 0) or 0) > 0 else "",
        "symbol_state": str(row.get("symbol_state", "idle") or "idle"),
        "setup_mode": str(row.get("setup_mode", "invalid") or "invalid"),
        "direction_constraint": str(row.get("direction_constraint", "none") or "none"),
        "timing_state": str(row.get("timing_state", "no_edge") or "no_edge"),
        "best_play": str(row.get("best_play", "") or ""),
        "trigger": str(row.get("trigger", "") or ""),
        "trigger_spec": dict(row.get("trigger_spec", {}) or {}),
        "trigger_live": _trigger_live_for_snapshot(row),
        "invalidation": str(row.get("invalidation", "") or ""),
        "hold_style": str(row.get("hold_style", "") or ""),
        "size_posture": str(row.get("size_posture", "normal") or "normal"),
        "no_trade_reason": row.get("no_trade_reason"),
        "classifier_confidence": float(row.get("classifier_confidence", 0.0) or 0.0),
        "resolver_confidence": float(row.get("resolver_confidence", 0.0) or 0.0),
        "feature_quality_score": float(row.get("feature_quality_score", 0.0) or 0.0),
        "feature_quality": str(row.get("feature_quality", "") or ""),
        "data_age_seconds": float(row.get("data_age_seconds", 0.0) or 0.0),
        "expires_at": float(row.get("expires_at", 0) or 0) or None,
        "classifier_reason_codes": list(row.get("classifier_reason_codes", []) or []),
        "jury_decision": jury.get("decision"),
        "jury_confidence": jury.get("confidence"),
        "jury_provider": jury.get("provider_used"),
        "pnl": row.get("pnl"),
        "pnl_pct": row.get("pnl_pct"),
        "profitable": row.get("profitable"),
        "reason": row.get("reason"),
    }


def _prepare_trade(row: Dict) -> Dict:
    return {
        "symbol": str(row.get("symbol", "") or "").upper(),
        "setup_id": str(row.get("setup_id", "") or ""),
        "setup_mode": str(row.get("setup_mode", "invalid") or "invalid"),
        "entry_time": float(row.get("entry_time", 0) or 0),
        "exit_time": float(row.get("exit_time", 0) or 0),
        "pnl": float(row.get("pnl", 0.0) or 0.0),
        "pnl_pct": float(row.get("pnl_pct", 0.0) or 0.0),
        "reason": str(row.get("reason", "") or ""),
        "hold_seconds": float(row.get("hold_seconds", 0.0) or 0.0),
        "profitable": bool(row.get("profitable", False)),
        "hard_stopped": bool(row.get("hard_stopped", False)),
        "ratchet_activated": bool(row.get("ratchet_activated", False)),
    }


def build_setup_replay(
    *,
    symbol: Optional[str] = None,
    setup_id: Optional[str] = None,
    day: Optional[str] = None,
    limit: int = 250,
    now_ts: Optional[float] = None,
) -> Dict:
    symbol_key = _normalize_symbol(symbol)
    setup_key = _normalize_setup_id(setup_id)
    now_ts = float(now_ts or time.time())

    snapshots = load_setup_snapshots()
    filtered_rows: List[Dict] = []
    for row in snapshots:
        row_symbol = _normalize_symbol(row.get("symbol"))
        row_setup_id = _normalize_setup_id(row.get("setup_id"))
        recorded_at = float(row.get("recorded_at", 0) or 0)
        if symbol_key and row_symbol != symbol_key:
            continue
        if setup_key and row_setup_id != setup_key:
            continue
        if day and (recorded_at <= 0 or _day_key(recorded_at) != str(day)):
            continue
        filtered_rows.append(dict(row))

    filtered_rows.sort(key=lambda row: float(row.get("recorded_at", 0) or 0))
    if isinstance(limit, int) and limit > 0 and len(filtered_rows) > limit:
        filtered_rows = filtered_rows[-limit:]

    timeline = [_prepare_snapshot(row) for row in filtered_rows]

    trade_rows = []
    for row in trade_history.load_all():
        trade_symbol = _normalize_symbol(row.get("symbol"))
        trade_setup_id = _normalize_setup_id(row.get("setup_id"))
        exit_time = float(row.get("exit_time", row.get("recorded_at", 0)) or 0)
        if symbol_key and trade_symbol != symbol_key:
            continue
        if setup_key and trade_setup_id != setup_key:
            continue
        if day and (exit_time <= 0 or _day_key(exit_time) != str(day)):
            continue
        trade_rows.append(_prepare_trade(row))
    trade_rows.sort(key=lambda row: float(row.get("exit_time", 0) or 0))

    setup_groups: Dict[str, Dict] = {}
    for row in timeline:
        group_key = row.get("setup_id") or f"{row.get('symbol', '?')}:{int(float(row.get('recorded_at', 0) or 0) // 300)}"
        group = setup_groups.setdefault(
            group_key,
            {
                "setup_id": group_key,
                "symbol": row.get("symbol"),
                "setup_mode": row.get("setup_mode"),
                "first_seen_at": row.get("recorded_at"),
                "last_seen_at": row.get("recorded_at"),
                "states_seen": [],
                "timing_states_seen": [],
                "trigger_live_any": False,
                "entered": False,
                "expired": False,
                "pnl": 0.0,
                "trade_count": 0,
            },
        )
        group["last_seen_at"] = row.get("recorded_at")
        group["setup_mode"] = row.get("setup_mode") or group.get("setup_mode")
        state = str(row.get("symbol_state", "idle") or "idle")
        if state not in group["states_seen"]:
            group["states_seen"].append(state)
        timing_state = str(row.get("timing_state", "no_edge") or "no_edge")
        if timing_state not in group["timing_states_seen"]:
            group["timing_states_seen"].append(timing_state)
        group["trigger_live_any"] = bool(group["trigger_live_any"] or row.get("trigger_live") is True)
        group["entered"] = bool(group["entered"] or state == "live_position")
        expires_at = row.get("expires_at")
        if (
            not group["entered"]
            and state == "pending_trigger"
            and isinstance(expires_at, (int, float))
            and float(expires_at) > 0
            and float(expires_at) <= now_ts
        ):
            group["expired"] = True

    for trade in trade_rows:
        group_key = trade.get("setup_id")
        if not group_key:
            continue
        group = setup_groups.setdefault(
            group_key,
            {
                "setup_id": group_key,
                "symbol": trade.get("symbol"),
                "setup_mode": trade.get("setup_mode"),
                "first_seen_at": trade.get("entry_time") or trade.get("exit_time"),
                "last_seen_at": trade.get("exit_time") or trade.get("entry_time"),
                "states_seen": ["live_position", "cooldown"],
                "timing_states_seen": ["enter_now"],
                "trigger_live_any": True,
                "entered": True,
                "expired": False,
                "pnl": 0.0,
                "trade_count": 0,
            },
        )
        group["entered"] = True
        group["pnl"] = round(float(group.get("pnl", 0.0) or 0.0) + float(trade.get("pnl", 0.0) or 0.0), 2)
        group["trade_count"] = int(group.get("trade_count", 0) or 0) + 1
        group["last_seen_at"] = max(float(group.get("last_seen_at", 0) or 0), float(trade.get("exit_time", 0) or 0))

    ordered_groups = sorted(
        setup_groups.values(),
        key=lambda row: float(row.get("last_seen_at", row.get("first_seen_at", 0)) or 0),
        reverse=True,
    )

    mode_transitions = []
    last_mode = None
    for row in timeline:
        current_mode = str(row.get("setup_mode", "invalid") or "invalid")
        if last_mode and current_mode != last_mode:
            mode_transitions.append(
                {
                    "recorded_at": row.get("recorded_at"),
                    "from_mode": last_mode,
                    "to_mode": current_mode,
                    "setup_id": row.get("setup_id"),
                    "symbol": row.get("symbol"),
                }
            )
        last_mode = current_mode

    summary = {
        "setup_count": len(ordered_groups),
        "snapshot_count": len(timeline),
        "trade_count": len(trade_rows),
        "trigger_live_snapshots": sum(1 for row in timeline if row.get("trigger_live") is True),
        "pending_setup_count": sum(1 for row in ordered_groups if "pending_trigger" in row.get("states_seen", [])),
        "entered_setup_count": sum(1 for row in ordered_groups if row.get("entered")),
        "expired_setup_count": sum(1 for row in ordered_groups if row.get("expired")),
        "triggered_not_entered_count": sum(
            1 for row in ordered_groups if row.get("trigger_live_any") and not row.get("entered")
        ),
        "mode_flip_count": len(mode_transitions),
        "net_pnl": round(sum(float(row.get("pnl", 0.0) or 0.0) for row in trade_rows), 2),
    }

    return {
        "symbol": symbol_key,
        "setup_id": setup_key,
        "day": str(day or ""),
        "generated_at": now_ts,
        "summary": summary,
        "mode_transitions": mode_transitions,
        "setups": ordered_groups,
        "timeline": timeline,
        "trades": trade_rows,
    }
