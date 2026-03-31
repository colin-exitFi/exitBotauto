"""
Helpers that slim runtime payloads before they are sent to LLM layers.

Entry-time setup snapshots are useful for auditability, but fields like
`bar_context` and `mode_features` can become misleading once a position is live.
These helpers keep the live state the AI actually needs and relabel historical
entry metadata so it is not mistaken for current market structure.
"""

from typing import Dict, Iterable, List


_POSITION_KEYS = (
    "symbol",
    "side",
    "quantity",
    "entry_price",
    "fill_price",
    "current_price",
    "peak_price",
    "entry_time",
    "holding_horizon",
    "conviction_level",
    "risk_tier",
    "notional",
    "strategy_tag",
    "signal_tier",
    "market_regime",
    "entry_reason_code",
    "signal_sources",
    "entry_quality",
    "setup_id",
    "setup_mode",
    "direction_constraint",
    "timing_state",
    "best_play",
    "trigger",
    "invalidation",
    "hold_style",
    "size_posture",
    "no_trade_reason",
    "hard_stop_price",
    "trail_pct",
    "ratchet_floor_pct",
    "has_trailing_stop",
    "partial_exit",
    "allocator_state",
    "allocator_alignment",
    "allocator_budget_pct",
    "allocator_size_multiplier",
    "order_state",
    "broker_synced_at",
    "dead_money",
    "time_to_green_seconds",
    "mfe_pct",
    "mae_pct",
    "price_at_1m",
    "price_at_3m",
    "price_at_5m",
)

_CANDIDATE_KEYS = (
    "symbol",
    "price",
    "change_pct",
    "volume",
    "volume_spike",
    "vol_accel",
    "spread_pct",
    "range_pct",
    "source",
    "score",
    "strategy_tag",
    "signal_tier",
    "holding_horizon",
    "entry_quality",
    "market_regime",
    "setup_mode",
    "timing_state",
    "best_play",
    "trigger",
    "invalidation",
    "no_trade_reason",
    "watchlist_reason",
    "copy_trader_context",
    "overnight_context",
    "earnings",
    "earnings_date",
    "catalyst_date",
    "pharma_signal",
    "pharma_catalyst_type",
)


def _has_meaningful_live_context(position: Dict) -> bool:
    strategy_tag = str(position.get("strategy_tag", "") or "").strip().lower()
    setup_mode = str(position.get("setup_mode", "") or "").strip().lower()
    best_play = str(position.get("best_play", "") or "").strip().lower()
    direction_constraint = str(position.get("direction_constraint", "") or "").strip().lower()
    hold_style = str(position.get("hold_style", "") or "").strip().lower()
    entry_time_source = str(position.get("entry_time_source", "") or "").strip().lower()
    has_protection = bool(
        position.get("hard_stop_price")
        or position.get("ratchet_floor_pct")
        or position.get("trail_pct")
        or position.get("hard_stop_order_id")
        or position.get("ratchet_limit_order_id")
    )
    meaningful_tags = {
        value
        for value in (strategy_tag, setup_mode, best_play, direction_constraint, hold_style)
        if value and value not in {"unknown", "invalid", "none"}
    }
    return bool(meaningful_tags) and (has_protection or entry_time_source == "broker_orders")


def _position_origin(position: Dict) -> str:
    entry_path = str(position.get("entry_path", "") or "").strip().lower()
    is_broker_confirmed = bool(position.get("from_brokerage")) or ("broker" in entry_path)
    if not is_broker_confirmed:
        return "native_live_entry"
    if _has_meaningful_live_context(position):
        return "tracked_live_position"
    return "broker_restored_live"


def sanitize_positions_for_ai(positions: Iterable[Dict]) -> List[Dict]:
    sanitized: List[Dict] = []
    for pos in positions or []:
        if not isinstance(pos, dict):
            continue
        row = {key: pos.get(key) for key in _POSITION_KEYS if key in pos}
        if "data_age_seconds" in pos:
            row["entry_signal_age_seconds"] = float(pos.get("data_age_seconds", 0.0) or 0.0)
        if "feature_quality_score" in pos:
            row["entry_feature_quality_score"] = float(pos.get("feature_quality_score", 0.0) or 0.0)
        if "feature_quality" in pos:
            row["entry_feature_quality"] = pos.get("feature_quality")
        if "setup_created_at" in pos:
            row["setup_created_at"] = pos.get("setup_created_at")
        if "setup_last_refreshed_at" in pos:
            row["setup_last_refreshed_at"] = pos.get("setup_last_refreshed_at")
        origin = _position_origin(pos)
        row["position_origin"] = origin
        if origin == "tracked_live_position":
            row.pop("broker_synced_at", None)
            entry_reason_code = str(row.get("entry_reason_code", "") or "").strip().lower()
            if entry_reason_code == "broker_sync":
                for fallback_key in ("setup_mode", "best_play", "strategy_tag"):
                    fallback_value = str(row.get(fallback_key, "") or "").strip()
                    if fallback_value:
                        row["entry_reason_code"] = fallback_value
                        break
            raw_sources = pos.get("signal_sources") or []
            cleaned_sources = [
                str(source).strip()
                for source in raw_sources
                if str(source).strip().lower() not in {"broker_sync", "broker_reconciliation"}
            ]
            if cleaned_sources:
                row["signal_sources"] = cleaned_sources
            else:
                row.pop("signal_sources", None)
        sanitized.append(row)
    return sanitized


def sanitize_candidates_for_ai(candidates: Iterable[Dict], limit: int = 5) -> List[Dict]:
    sanitized: List[Dict] = []
    for candidate in list(candidates or [])[:limit]:
        if not isinstance(candidate, dict):
            continue
        row = {key: candidate.get(key) for key in _CANDIDATE_KEYS if key in candidate}
        if "data_age_seconds" in candidate:
            row["signal_age_seconds"] = float(candidate.get("data_age_seconds", 0.0) or 0.0)
        if "feature_quality_score" in candidate:
            row["feature_quality_score"] = float(candidate.get("feature_quality_score", 0.0) or 0.0)
        if "feature_quality" in candidate:
            row["feature_quality"] = candidate.get("feature_quality")
        sanitized.append(row)
    return sanitized
