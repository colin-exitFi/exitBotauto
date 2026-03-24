"""
Trade schema helpers.
Keeps attribution normalization in one place for all persistence/analytics paths.
"""

from typing import Dict, List


def normalize_trade_record(trade: Dict) -> Dict:
    """Backfill attribution fields for analytics compatibility."""
    t = dict(trade)
    t.setdefault("asset_type", "equity")
    t.setdefault("strategy_tag", "unknown")
    t.setdefault("signal_tier", "tier_2")
    t.setdefault("holding_horizon", "intraday")
    t.setdefault("entry_quality", "neutral")
    t.setdefault("market_regime", "mixed")
    t.setdefault("entry_path", "unknown")
    t.setdefault("entry_reason_code", "unknown")
    t.setdefault("entry_model_votes", {})
    t.setdefault("risk_constraints_applied", [])
    t.setdefault("ratchet_peak_pnl_pct", 0.0)
    t.setdefault("ratchet_floor_pct", None)
    t.setdefault("ratchet_limit_order_id", "")
    t.setdefault("hard_stop_order_id", "")
    t.setdefault("order_state", {})
    t.setdefault("overnight_context", "")
    t.setdefault("extended_hours_entry", False)
    t.setdefault("dead_money_tightened", False)
    t.setdefault("dead_money", False)
    t.setdefault("giveback_pct", None)
    t.setdefault("loss_category", None)
    t.setdefault("post_exit_1h_price", None)
    t.setdefault("setup_id", "")
    t.setdefault("setup_mode", "invalid")
    t.setdefault("direction_constraint", "none")
    t.setdefault("timing_state", "no_edge")
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
    return t
