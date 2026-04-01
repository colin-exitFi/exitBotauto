"""
Trade schema helpers.
Keeps attribution normalization in one place for all persistence/analytics paths.
"""

from typing import Dict, List

from src.data.strategy_tags import is_artifact_strategy_tag, normalize_strategy_tag


_VALID_SETUP_MODES = {
    "continuation_long",
    "continuation_short",
    "exhaustion_fade_short",
    "swing_catalyst_long",
    "general_momentum_long",
    "general_momentum_short",
}

_VALID_TIMING_STATES = {
    "enter_now",
    "wait_for_trigger",
    "shadow_only",
    "broker_blocked",
    "capital_blocked",
    "mode_conflict",
    "data_insufficient",
}

_SHORT_SETUP_MODES = {
    "continuation_short",
    "exhaustion_fade_short",
    "general_momentum_short",
}

_LONG_SETUP_MODES = {
    "continuation_long",
    "swing_catalyst_long",
    "general_momentum_long",
}

_SHORT_STRATEGY_TAGS = {
    "momentum_short",
    "social_momentum_short",
    "uw_flow_short",
    "fade_short",
    "copy_trader_short",
    "watchlist_short",
}

_LONG_STRATEGY_TAGS = {
    "momentum_long",
    "social_momentum_long",
    "uw_flow_long",
    "copy_trader_long",
    "watchlist_long",
    "pharma_catalyst",
    "congress_follow",
}


def _text(value: object) -> str:
    return str(value or "").strip()


def _text_lower(value: object) -> str:
    return _text(value).lower()


def _is_blank(value: object) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return len(value) == 0
    return False


def _normalize_signal_sources(value: object) -> List[str]:
    if isinstance(value, str):
        items = [part.strip().lower() for part in value.split(",") if part.strip()]
    elif isinstance(value, list):
        items = [str(part).strip().lower() for part in value if str(part or "").strip()]
    else:
        items = []
    deduped = []
    seen = set()
    for item in items:
        if item and item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def _infer_entry_direction(payload: Dict) -> str:
    direction_constraint = _text_lower(payload.get("direction_constraint"))
    if direction_constraint == "short_only":
        return "short"
    if direction_constraint == "long_only":
        return "long"

    side = _text_lower(payload.get("side"))
    if side in {"short", "sell_short", "buy_to_cover"}:
        return "short"
    if side in {"long", "buy", "sell"}:
        return "long"

    verdict = _text_lower(((payload.get("jury_response") or {}).get("decision")))
    if verdict == "short":
        return "short"
    if verdict == "buy":
        return "long"

    strategy_tag = normalize_strategy_tag(payload.get("strategy_tag"), fallback="", allow_artifacts=True)
    if strategy_tag in _SHORT_STRATEGY_TAGS or strategy_tag.endswith("_short"):
        return "short"
    if strategy_tag in _LONG_STRATEGY_TAGS or strategy_tag.endswith("_long"):
        return "long"

    return "long"


def _infer_strategy_tag(payload: Dict) -> str:
    strategy_tag = normalize_strategy_tag(payload.get("strategy_tag"), fallback="", allow_artifacts=True)
    if strategy_tag and strategy_tag != "unknown" and not is_artifact_strategy_tag(strategy_tag):
        return strategy_tag

    direction = _infer_entry_direction(payload)
    direction_suffix = "short" if direction == "short" else "long"
    signal_sources = _normalize_signal_sources(payload.get("signal_sources"))
    source_text = " ".join(
        part
        for part in (
            *signal_sources,
            _text(payload.get("source")),
            _text(payload.get("provider_used")),
            _text(payload.get("entry_path")),
            _text(payload.get("entry_reason_code")),
            _text(payload.get("reason")),
            _text(payload.get("copy_trader_context")),
            _text(payload.get("watchlist_reason")),
        )
        if part
    ).lower()

    if "fade" in source_text:
        return "fade_short"
    if "copy_trader" in source_text:
        return f"copy_trader_{direction_suffix}"
    if "watchlist" in source_text:
        return f"watchlist_{direction_suffix}"
    if "congress" in source_text:
        return "congress_follow"
    if "pharma" in source_text or "fda" in source_text or payload.get("pharma_signal"):
        return "pharma_catalyst"
    if any(term in source_text for term in ("unusual_whales", "options_flow", "unusual_options", "uw_")):
        return f"uw_flow_{direction_suffix}"
    if any(term in source_text for term in ("stocktwits", "grok_x", "twitter", "social")):
        return f"social_momentum_{direction_suffix}"
    return f"momentum_{direction_suffix}"


def _infer_setup_mode(payload: Dict) -> str:
    current = _text_lower(payload.get("setup_mode"))
    if current in _VALID_SETUP_MODES:
        return current

    current_play = _text_lower(payload.get("best_play"))
    if current_play in _VALID_SETUP_MODES:
        return current_play

    strategy_tag = _infer_strategy_tag(payload)
    direction = _infer_entry_direction(payload)
    holding_horizon = _text_lower(payload.get("holding_horizon") or payload.get("hold_style"))
    entry_quality = _text_lower(payload.get("entry_quality") or "neutral")
    reason_text = " ".join(
        part
        for part in (
            _text(payload.get("entry_reason_code")),
            _text(payload.get("reason")),
            _text(payload.get("no_trade_reason")),
            _text(payload.get("trigger")),
            _text(payload.get("watchlist_reason")),
        )
        if part
    ).lower()

    if strategy_tag == "fade_short" or ("fade" in reason_text and direction == "short"):
        return "exhaustion_fade_short"
    if strategy_tag in {"pharma_catalyst", "congress_follow"}:
        return "swing_catalyst_long"
    if holding_horizon in {"swing", "multiday"} and direction != "short":
        return "swing_catalyst_long"

    if direction == "short":
        if entry_quality == "at_highs" and "continuation" not in reason_text:
            return "exhaustion_fade_short"
        if strategy_tag in _SHORT_STRATEGY_TAGS or strategy_tag.endswith("_short"):
            return "continuation_short"
        return "general_momentum_short"

    if strategy_tag in _LONG_STRATEGY_TAGS or strategy_tag.endswith("_long"):
        if holding_horizon in {"swing", "multiday"} and strategy_tag not in {"momentum_long", "social_momentum_long", "uw_flow_long"}:
            return "swing_catalyst_long"
        return "continuation_long"
    return "general_momentum_long"


def _infer_best_play(payload: Dict, setup_mode: str) -> str:
    current_play = _text_lower(payload.get("best_play"))
    if current_play and current_play not in {"unknown", "invalid", "no_edge"}:
        return current_play
    if setup_mode in _VALID_SETUP_MODES:
        return setup_mode
    direction = _infer_entry_direction(payload)
    return "general_momentum_short" if direction == "short" else "general_momentum_long"


def _infer_direction_constraint(payload: Dict, setup_mode: str) -> str:
    current = _text_lower(payload.get("direction_constraint"))
    if current in {"long_only", "short_only"}:
        return current
    if setup_mode in _SHORT_SETUP_MODES:
        return "short_only"
    if setup_mode in _LONG_SETUP_MODES:
        return "long_only"
    return "short_only" if _infer_entry_direction(payload) == "short" else "long_only"


def _infer_timing_state(payload: Dict) -> str:
    current = _text_lower(payload.get("timing_state"))
    if current in _VALID_TIMING_STATES:
        return current

    symbol_state = _text_lower(payload.get("symbol_state"))
    if symbol_state in _VALID_TIMING_STATES:
        return symbol_state
    if symbol_state == "pending_trigger":
        return "wait_for_trigger"

    if payload.get("entered") is True:
        return "enter_now"
    if payload.get("entry_time") or payload.get("exit_time") or payload.get("quantity") or payload.get("pnl") is not None:
        return "enter_now"
    if symbol_state in {"live_position", "cooldown"}:
        return "enter_now"
    return "mode_conflict"


def _normalize_play_context(payload: Dict) -> Dict:
    t = dict(payload or {})

    inferred_strategy_tag = _infer_strategy_tag(t)
    current_strategy_tag = normalize_strategy_tag(t.get("strategy_tag"), fallback="", allow_artifacts=True)
    if not current_strategy_tag or current_strategy_tag == "unknown" or is_artifact_strategy_tag(current_strategy_tag):
        t["strategy_tag"] = inferred_strategy_tag

    setup_mode = _infer_setup_mode({**t, "strategy_tag": inferred_strategy_tag})
    t["setup_mode"] = setup_mode
    t["best_play"] = _infer_best_play(t, setup_mode)
    t["direction_constraint"] = _infer_direction_constraint(t, setup_mode)
    t["timing_state"] = _infer_timing_state(t)

    if _is_blank(t.get("hold_style")):
        t["hold_style"] = _text(t.get("holding_horizon") or "intraday") or "intraday"
    if _is_blank(t.get("entry_reason_code")) or _text_lower(t.get("entry_reason_code")) == "unknown":
        t["entry_reason_code"] = t.get("best_play") or inferred_strategy_tag or "derived_play"
    if _is_blank(t.get("entry_path")) or _text_lower(t.get("entry_path")) == "unknown":
        provider_used = _text_lower(t.get("provider_used"))
        if provider_used.startswith("classifier_auto"):
            t["entry_path"] = "classifier_auto"
        elif provider_used.startswith("council"):
            t["entry_path"] = "council"
        elif provider_used == "alpaca_reconciler":
            t["entry_path"] = "broker_reconciliation"
        else:
            t["entry_path"] = "derived_play"

    sources = _normalize_signal_sources(t.get("signal_sources"))
    if not sources or sources == ["unknown"]:
        strategy_tag = _text_lower(t.get("strategy_tag"))
        if strategy_tag.startswith("uw_flow"):
            sources = ["unusual_whales"]
        elif strategy_tag.startswith("social_momentum"):
            sources = ["stocktwits"]
        elif strategy_tag == "pharma_catalyst":
            sources = ["pharma"]
        elif strategy_tag == "congress_follow":
            sources = ["congress"]
        elif _text_lower(t.get("entry_path")) == "broker_reconciliation":
            sources = ["broker_reconciliation"]
        elif _text_lower(t.get("entry_path")) == "broker_sync":
            sources = ["broker_sync"]
        else:
            sources = ["derived_play"]
    t["signal_sources"] = sources
    return t


def normalize_setup_snapshot(snapshot: Dict) -> Dict:
    payload = _normalize_play_context(dict(snapshot or {}))
    payload.setdefault("symbol_state", "idle")
    payload.setdefault("entry_quality", "neutral")
    payload.setdefault("holding_horizon", payload.get("hold_style", "intraday") or "intraday")
    return payload


def normalize_position_context(position: Dict) -> Dict:
    payload = _normalize_play_context(dict(position or {}))
    payload.setdefault("symbol_state", "live_position")
    payload.setdefault("holding_horizon", "intraday")
    payload.setdefault("entry_quality", "neutral")
    payload.setdefault("hard_stop_pct", None)
    payload.setdefault("hard_stop_flags", [])
    payload.setdefault("allocator_status", "hold")
    payload.setdefault("allocator_recommended_action", "hold")
    payload.setdefault("allocator_control_state", "active")
    return payload


def normalize_trade_record(trade: Dict) -> Dict:
    """Backfill attribution fields for analytics compatibility."""
    t = dict(trade)
    original_strategy_tag = normalize_strategy_tag(
        t.get("strategy_tag"),
        fallback="",
        allow_artifacts=True,
    )
    t.setdefault("asset_type", "equity")
    t.setdefault("strategy_tag", "unknown")
    t.setdefault("signal_tier", "tier_2")
    t.setdefault("holding_horizon", "intraday")
    t.setdefault("entry_quality", "neutral")
    t.setdefault("market_regime", "mixed")
    t.setdefault("session_type", "")
    t.setdefault("entry_path", "unknown")
    t.setdefault("entry_reason_code", "unknown")
    t.setdefault("entry_model_votes", {})
    t.setdefault("risk_constraints_applied", [])
    t.setdefault("ratchet_peak_pnl_pct", 0.0)
    t.setdefault("ratchet_floor_pct", None)
    t.setdefault("ratchet_limit_order_id", "")
    t.setdefault("hard_stop_pct", None)
    t.setdefault("hard_stop_flags", [])
    t.setdefault("hard_stop_order_id", "")
    t.setdefault("order_state", {})
    t.setdefault("overnight_context", "")
    t.setdefault("extended_hours_entry", False)
    t.setdefault("allocator_status", "hold")
    t.setdefault("allocator_recommended_action", "hold")
    t.setdefault("allocator_control_state", "active")
    t.setdefault("dead_money_tightened", False)
    t.setdefault("dead_money", False)
    t.setdefault("giveback_pct", None)
    t.setdefault("loss_category", None)
    t.setdefault("post_exit_1h_price", None)
    t.setdefault("post_exit_4h_price", None)
    t.setdefault("post_exit_1d_price", None)
    t.setdefault("post_exit_continued_move_pct", None)
    t.setdefault("setup_id", "")
    t.setdefault("setup_mode", "invalid")
    t.setdefault("direction_constraint", "none")
    t.setdefault("timing_state", "mode_conflict")
    t.setdefault("best_play", "")
    t.setdefault("trigger", "")
    t.setdefault("trigger_spec", {})
    t.setdefault("invalidation", "")
    t.setdefault("hold_style", t.get("holding_horizon", "intraday"))
    t.setdefault("size_posture", "normal")
    t.setdefault("no_trade_reason", None)
    t.setdefault("classifier_confidence", 0.0)
    t.setdefault("resolver_confidence", 0.0)
    t.setdefault("execution_confidence", 0.0)
    t.setdefault("feature_snapshot_id", "")
    t.setdefault("feature_quality_score", 0.0)
    t.setdefault("feature_quality", "")
    t.setdefault("missing_fields", [])
    t.setdefault("material_change_signature", "")
    t.setdefault("symbol_state", "idle")
    t.setdefault("jury_entry_now", False)
    t.setdefault("jury_trigger", "")
    t.setdefault("jury_invalidation", "")
    t.setdefault("jury_hold_style", "")
    t.setdefault("jury_size_posture", "")
    t.setdefault("jury_no_trade_reason", None)
    t.setdefault("triggered", True)
    t.setdefault("entered", True)
    t.setdefault("profitable", None)
    t.setdefault("ratchet_activated", False)
    t.setdefault("hard_stopped", False)

    sources = t.get("signal_sources", [])
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    if not isinstance(sources, list):
        sources = []
    t["signal_sources"] = sources or ["unknown"]

    anomaly_flags = t.get("anomaly_flags", [])
    if isinstance(anomaly_flags, str):
        anomaly_flags = [f.strip() for f in anomaly_flags.split(",") if f.strip()]
    if not isinstance(anomaly_flags, list):
        anomaly_flags = []
    t["anomaly_flags"] = anomaly_flags

    missing_fields = t.get("missing_fields", [])
    if isinstance(missing_fields, str):
        missing_fields = [f.strip() for f in missing_fields.split(",") if f.strip()]
    if not isinstance(missing_fields, list):
        missing_fields = []
    t["missing_fields"] = missing_fields

    t.setdefault("decision_confidence", 0)
    t.setdefault("provider_used", "")
    t.setdefault("signal_price", t.get("entry_price", 0))
    t.setdefault("decision_price", t.get("entry_price", 0))
    t.setdefault("fill_price", t.get("exit_price", 0))
    t.setdefault("slippage_bps", 0.0)
    t.setdefault("entry_order_id", None)
    t.setdefault("signal_timestamp", None)
    t.setdefault("entry_order_timestamp", None)
    t.setdefault("fill_timestamp", None)
    t.setdefault("fill_timestamp_source", "unknown")
    t.setdefault("signal_to_order_ms", None)
    t.setdefault("signal_to_fill_ms", None)
    t.setdefault("intended_notional", 0.0)
    t.setdefault("actual_notional", float(t.get("entry_price", 0) or 0) * float(t.get("quantity", 0) or 0))
    t.setdefault("intended_qty", float(t.get("quantity", 0) or 0))
    t.setdefault("actual_qty", float(t.get("quantity", 0) or 0))
    t.setdefault("price_at_1m", None)
    t.setdefault("price_at_3m", None)
    t.setdefault("price_at_5m", None)
    t.setdefault("time_to_green_seconds", None)
    t.setdefault("time_to_peak_seconds", None)
    t.setdefault("mfe_pct", None)
    t.setdefault("mae_pct", None)
    if t.get("asset_type") == "option":
        t.setdefault("contract_symbol", t.get("symbol", ""))
        t.setdefault("entry_premium", t.get("entry_price", 0))
        t.setdefault("exit_premium", t.get("exit_price", 0))
        t.setdefault("underlying", "")
        t.setdefault("delta_at_entry", 0.0)
        t.setdefault("underlying_move_pct", 0.0)
    normalized = _normalize_play_context(t)
    if is_artifact_strategy_tag(original_strategy_tag):
        normalized["strategy_tag"] = original_strategy_tag
    return normalized
