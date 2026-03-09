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
    t.setdefault("entry_path", "unknown")

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
