"""
Persistent trade history — survives restarts, feeds game film analysis.
File: data/trade_history.json
"""

import json
import math
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

from src import persistence
from src.data import strategy_controls
from src.data.setup_snapshots import load_setup_snapshots
from src.data.trading_calendar import trading_session_day
from src.data.trade_schema import normalize_trade_record
from src.data.strategy_tags import is_artifact_strategy_tag, normalize_strategy_tag
from src.signals.mode_classifier import mode_features_from_dict
from src.signals.play_resolver import TriggerSpec, evaluate_trigger

DATA_DIR = Path(__file__).parent.parent.parent / "data"
HISTORY_FILE = DATA_DIR / "trade_history.json"
MAX_TRADES = 5000


def record_trade(trade: Dict):
    """Append a completed trade to persistent history."""
    DATA_DIR.mkdir(exist_ok=True)
    trade = normalize_trade_record(trade)
    trade["recorded_at"] = time.time()
    history = load_all()
    history.append(trade)
    if len(history) > MAX_TRADES:
        history = history[-MAX_TRADES:]
    try:
        persistence.atomic_write_json(HISTORY_FILE, history, indent=0)
    except Exception as e:
        logger.warning(f"Failed to save trade history: {e}")


def load_all() -> List[Dict]:
    """Load all trade history from disk."""
    try:
        data = persistence.safe_load_json(HISTORY_FILE, default=list)
        if isinstance(data, dict):
            data = data.get("trades", [])
        if not isinstance(data, list):
            return []
        return _dedupe_history([normalize_trade_record(t) for t in data])
    except Exception:
        return []


def _dedupe_history(history: List[Dict]) -> List[Dict]:
    """
    Deduplicate reconstructed broker fills against already-recorded exits.

    Preference order:
    1. Non-broker_reconstructed reasons over broker_fill_reconstructed
    2. First seen record for the same exit-order or same symbol/time bucket
    """
    if not history:
        return []

    def _quality(trade: Dict) -> int:
        reason = str(trade.get("reason", "") or "")
        if reason == "broker_fill_reconstructed":
            return 0
        return 1

    deduped: List[Dict] = []
    seen_by_order: Dict[str, Dict] = {}
    seen_by_bucket: Dict[tuple, Dict] = {}

    ordered = sorted(
        history,
        key=lambda t: float(
            t.get("exit_time", t.get("fill_timestamp", t.get("recorded_at", 0))) or 0
        ),
    )

    for trade in ordered:
        order_id = str(
            trade.get("exit_order_id")
            or trade.get("order_id")
            or ""
        ).strip()
        symbol = str(trade.get("symbol", "") or "").upper()
        exit_time = float(trade.get("exit_time", trade.get("fill_timestamp", trade.get("recorded_at", 0))) or 0)
        qty = round(float(trade.get("quantity", 0) or 0), 4)
        bucket = (symbol, int(exit_time // 30), qty)

        existing = None
        existing_key = None
        use_order_key = False
        if order_id and order_id in seen_by_order:
            existing = seen_by_order[order_id]
            existing_key = order_id
            use_order_key = True
        elif bucket in seen_by_bucket:
            existing = seen_by_bucket[bucket]
            existing_key = bucket

        if existing is None:
            deduped.append(trade)
            if order_id:
                seen_by_order[order_id] = trade
            seen_by_bucket[bucket] = trade
            continue

        if _quality(trade) > _quality(existing):
            try:
                deduped.remove(existing)
            except ValueError:
                pass
            deduped.append(trade)
            if order_id:
                seen_by_order[order_id] = trade
            if not use_order_key and existing_key is not None:
                seen_by_bucket[existing_key] = trade
            seen_by_bucket[bucket] = trade

    return deduped


def get_recent(n: int = 50) -> List[Dict]:
    """Get last N trades."""
    return load_all()[-n:]


def get_analytic_history(*, clean_only: bool = False) -> List[Dict]:
    """Get strategy-analytic trades, optionally excluding quarantined rows."""
    history = [t for t in load_all() if _is_strategy_analytic_trade(t)]
    if clean_only:
        history = [t for t in history if not _trade_has_anomaly(t)]
    return history


def get_learning_history() -> List[Dict]:
    """Get trades safe to feed back into game film and tuner."""
    return get_analytic_history(clean_only=True)


def get_quarantined_history() -> List[Dict]:
    """Get analytic trades excluded from learning due to anomaly flags."""
    return [t for t in get_analytic_history(clean_only=False) if _trade_has_anomaly(t)]


def _trade_signal_sources(trade: Dict) -> List[str]:
    sources = trade.get("signal_sources", []) or ["unknown"]
    if isinstance(sources, str):
        sources = [s.strip() for s in sources.split(",") if s.strip()]
    if not isinstance(sources, list):
        return ["unknown"]
    normalized = []
    seen = set()
    for source in sources:
        tag = str(source or "").strip().lower()
        if not tag or tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)
    return normalized or ["unknown"]


def _build_unusual_whales_analytics(trades: List[Dict]) -> Dict:
    groups = {
        "overall": _metric_bucket_init(),
        "flow_book": _metric_bucket_init(),
        "stream_assisted": _metric_bucket_init(),
        "rest_assisted": _metric_bucket_init(),
        "congress_follow": _metric_bucket_init(),
    }

    for trade in trades or []:
        strategy = normalize_strategy_tag(
            trade.get("strategy_tag", "unknown"),
            fallback="unknown",
            allow_artifacts=True,
        )
        sources = set(_trade_signal_sources(trade))
        is_flow_book = strategy.startswith("uw_flow_")
        is_stream = "unusual_whales_stream" in sources
        is_rest = any(
            source in sources
            for source in ("unusual_whales", "unusual_options", "options_flow")
        )
        is_congress = strategy == "congress_follow" or "congress" in sources

        if any((is_flow_book, is_stream, is_rest, is_congress)):
            _update_metric_bucket(groups["overall"], trade)
        if is_flow_book:
            _update_metric_bucket(groups["flow_book"], trade)
        if is_stream:
            _update_metric_bucket(groups["stream_assisted"], trade)
        if is_rest:
            _update_metric_bucket(groups["rest_assisted"], trade)
        if is_congress:
            _update_metric_bucket(groups["congress_follow"], trade)

    return {
        key: _finalize_metric_bucket(bucket)
        for key, bucket in groups.items()
    }


def get_analytics() -> Dict:
    """Generate structured analytics for AI consumption."""
    history = load_all()
    if not history:
        return {
            "total_trades": 0,
            "clean_total_trades": 0,
            "message": "No trade history yet.",
            "win_rate": 0.0,
            "clean_win_rate": 0.0,
            "total_pnl": 0.0,
            "clean_pnl": 0.0,
            "sharpe_ratio": 0.0,
            "sharpe_ratio_recent_50": 0.0,
            "today": {
                "trades": 0,
                "clean_trades": 0,
                "quarantined_trades": 0,
                "raw_pnl": 0.0,
                "clean_pnl": 0.0,
                "anomaly_count": 0,
            },
            "quarantine": {
                "trades": 0,
                "pnl": 0.0,
                "today_trades": 0,
                "today_pnl": 0.0,
                "by_flag": {},
                "by_reason": {},
            },
            "book_report": {
                "summary": {
                    "books": 0,
                    "scale": 0,
                    "hold": 0,
                    "probation": 0,
                    "disable": 0,
                    "observe": 0,
                },
                "books": [],
                "generated_at": time.time(),
            },
        }

    raw_wins = [t for t in history if t.get("pnl", 0) > 0]
    raw_losses = [t for t in history if t.get("pnl", 0) < 0]
    raw_total_pnl = sum(t.get("pnl", 0) for t in history)
    raw_clean_pnl = sum(t.get("pnl", 0) for t in history if not _trade_has_anomaly(t))

    analytics_history = get_analytic_history(clean_only=False)
    clean_history = [t for t in analytics_history if not _trade_has_anomaly(t)]
    quarantined_history = [t for t in analytics_history if _trade_has_anomaly(t)]
    wins = [t for t in analytics_history if t.get("pnl", 0) > 0]
    losses = [t for t in analytics_history if t.get("pnl", 0) < 0]
    breakevens = [t for t in analytics_history if t.get("pnl", 0) == 0]
    clean_wins = [t for t in clean_history if t.get("pnl", 0) > 0]
    clean_losses = [t for t in clean_history if t.get("pnl", 0) < 0]
    total_pnl = sum(t.get("pnl", 0) for t in analytics_history)
    clean_pnl = sum(t.get("pnl", 0) for t in analytics_history if not _trade_has_anomaly(t))
    latency_samples = [
        float(t.get("signal_to_fill_ms"))
        for t in analytics_history
        if isinstance(t.get("signal_to_fill_ms"), (int, float))
    ]

    # By symbol
    by_symbol = {}
    for t in analytics_history:
        sym = t.get("symbol", "?")
        if sym not in by_symbol:
            by_symbol[sym] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_symbol[sym]["trades"] += 1
        by_symbol[sym]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_symbol[sym]["wins"] += 1
    for v in by_symbol.values():
        v["win_rate"] = round(v["wins"] / max(1, v["trades"]) * 100, 1)
        v["pnl"] = round(v["pnl"], 2)

    # By hour of day
    by_hour = {}
    for t in analytics_history:
        entry_time = t.get("entry_time", t.get("recorded_at", 0))
        if entry_time:
            from datetime import datetime
            try:
                hour = datetime.fromtimestamp(entry_time).strftime("%H")
            except Exception:
                hour = "?"
        else:
            hour = "?"
        if hour not in by_hour:
            by_hour[hour] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_hour[hour]["trades"] += 1
        by_hour[hour]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_hour[hour]["wins"] += 1
    for v in by_hour.values():
        v["win_rate"] = round(v["wins"] / max(1, v["trades"]) * 100, 1)
        v["pnl"] = round(v["pnl"], 2)

    # By exit reason
    by_reason = {}
    for t in analytics_history:
        reason = t.get("reason", "unknown")
        if reason not in by_reason:
            by_reason[reason] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_reason[reason]["trades"] += 1
        by_reason[reason]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_reason[reason]["wins"] += 1
    for v in by_reason.values():
        v["win_rate"] = round(v["wins"] / max(1, v["trades"]) * 100, 1)
        v["pnl"] = round(v["pnl"], 2)

    # By strategy tag
    by_strategy = {}
    for t in analytics_history:
        strategy = normalize_strategy_tag(t.get("strategy_tag", "unknown"), fallback="unknown", allow_artifacts=True)
        if is_artifact_strategy_tag(strategy):
            continue
        if strategy not in by_strategy:
            by_strategy[strategy] = _metric_bucket_init()
        _update_metric_bucket(by_strategy[strategy], t)
    for strategy, bucket in list(by_strategy.items()):
        by_strategy[strategy] = _finalize_metric_bucket(bucket)

    # Latency by strategy
    strategy_latency = {}
    for t in analytics_history:
        strategy = normalize_strategy_tag(t.get("strategy_tag", "unknown"), fallback="unknown", allow_artifacts=True)
        if is_artifact_strategy_tag(strategy):
            continue
        ms = t.get("signal_to_fill_ms")
        if not isinstance(ms, (int, float)):
            continue
        bucket = strategy_latency.setdefault(strategy, {"sum": 0.0, "count": 0})
        bucket["sum"] += float(ms)
        bucket["count"] += 1
    for strategy, bucket in by_strategy.items():
        agg = strategy_latency.get(strategy)
        bucket["avg_signal_to_fill_ms"] = (
            round(agg["sum"] / agg["count"], 1)
            if agg and agg["count"] > 0
            else None
        )

    # By setup mode
    by_setup_mode = {}
    for t in analytics_history:
        mode = str(t.get("setup_mode", "invalid") or "invalid").strip().lower() or "invalid"
        if mode not in by_setup_mode:
            by_setup_mode[mode] = _metric_bucket_init()
        _update_metric_bucket(by_setup_mode[mode], t)
    for mode, bucket in list(by_setup_mode.items()):
        by_setup_mode[mode] = _finalize_metric_bucket(bucket)

    # By signal source (participation attribution)
    by_signal_source = {}
    for t in analytics_history:
        sources = t.get("signal_sources", []) or ["unknown"]
        if isinstance(sources, str):
            sources = [s.strip() for s in sources.split(",") if s.strip()]
        if not sources:
            sources = ["unknown"]
        for source in sources:
            if source not in by_signal_source:
                by_signal_source[source] = {"trades": 0, "wins": 0, "pnl": 0.0}
            by_signal_source[source]["trades"] += 1
            by_signal_source[source]["pnl"] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                by_signal_source[source]["wins"] += 1
    for v in by_signal_source.values():
        v["win_rate"] = round(v["wins"] / max(1, v["trades"]) * 100, 1)
        v["pnl"] = round(v["pnl"], 2)

    # By asset type (equity vs option)
    by_asset_type = {}
    for t in analytics_history:
        asset_type = (t.get("asset_type", "equity") or "equity").lower()
        if asset_type not in by_asset_type:
            by_asset_type[asset_type] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_asset_type[asset_type]["trades"] += 1
        by_asset_type[asset_type]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_asset_type[asset_type]["wins"] += 1
    for v in by_asset_type.values():
        v["win_rate"] = round(v["wins"] / max(1, v["trades"]) * 100, 1)
        v["pnl"] = round(v["pnl"], 2)

    # Equity curve (realized P&L accumulation over time)
    equity_curve = []
    running_pnl = 0.0
    for t in analytics_history:
        running_pnl += t.get("pnl", 0)
        equity_curve.append({
            "timestamp": t.get("exit_time", t.get("recorded_at", 0)),
            "cumulative_pnl": round(running_pnl, 2),
        })

    # By hold duration
    by_hold = {"<5m": _bucket_init(), "5-30m": _bucket_init(), "30m-2h": _bucket_init(), "2-4h": _bucket_init(), ">4h": _bucket_init()}
    for t in analytics_history:
        secs = t.get("hold_seconds", 0)
        mins = secs / 60 if secs else 0
        if mins < 5:
            b = "<5m"
        elif mins < 30:
            b = "5-30m"
        elif mins < 120:
            b = "30m-2h"
        elif mins < 240:
            b = "2-4h"
        else:
            b = ">4h"
        by_hold[b]["trades"] += 1
        by_hold[b]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_hold[b]["wins"] += 1
    for v in by_hold.values():
        v["win_rate"] = round(v["wins"] / max(1, v["trades"]) * 100, 1)
        v["pnl"] = round(v["pnl"], 2)

    # Recent performance
    recent = analytics_history[-50:]
    recent_wins = len([t for t in recent if t.get("pnl", 0) > 0])
    recent_pnl = sum(t.get("pnl", 0) for t in recent)
    recent_20 = analytics_history[-20:]
    recent_20_wins = len([t for t in recent_20 if t.get("pnl", 0) > 0])
    recent_clean = clean_history[-50:]
    recent_clean_wins = len([t for t in recent_clean if t.get("pnl", 0) > 0])
    recent_20_clean = clean_history[-20:]
    recent_20_clean_wins = len([t for t in recent_20_clean if t.get("pnl", 0) > 0])
    sharpe_ratio = _compute_sharpe(analytics_history)
    sharpe_ratio_recent_50 = _compute_sharpe(recent)
    overall_metrics = _finalize_metric_bucket(_build_metric_bucket(analytics_history))
    unusual_whales_metrics = _build_unusual_whales_analytics(analytics_history)
    book_report = _build_book_report(analytics_history)
    play_report = _build_play_report(analytics_history)
    today_key = _current_day_key()
    today_trades = [t for t in analytics_history if _trade_day_key(t) == today_key]
    today_metrics = _finalize_metric_bucket(_build_metric_bucket(today_trades))
    quarantine_summary = _build_quarantine_summary(quarantined_history, today_key=today_key)

    return {
        "total_trades": len(analytics_history),
        "clean_total_trades": len(clean_history),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / max(1, len(analytics_history)), 4),
        "clean_wins": len(clean_wins),
        "clean_losses": len(clean_losses),
        "clean_win_rate": round(len(clean_wins) / max(1, len(clean_history)), 4) if clean_history else 0.0,
        "total_pnl": round(total_pnl, 2),
        "clean_pnl": round(clean_pnl, 2),
        "raw_total_trades": len(history),
        "raw_wins": len(raw_wins),
        "raw_losses": len(raw_losses),
        "raw_total_pnl": round(raw_total_pnl, 2),
        "raw_clean_pnl": round(raw_clean_pnl, 2),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "sharpe_ratio_recent_50": round(sharpe_ratio_recent_50, 4),
        "overall": {
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / max(1, len(analytics_history)) * 100, 1),
            "clean_trades": len(clean_history),
            "clean_wins": len(clean_wins),
            "clean_losses": len(clean_losses),
            "clean_win_rate_pct": round(len(clean_wins) / max(1, len(clean_history)) * 100, 1) if clean_history else 0.0,
            "total_pnl": round(total_pnl, 2),
            "clean_pnl": round(clean_pnl, 2),
            "avg_win": round(sum(t.get("pnl", 0) for t in wins) / max(1, len(wins)), 2),
            "avg_loss": round(sum(t.get("pnl", 0) for t in losses) / max(1, len(losses)), 2),
            "sharpe_ratio": round(sharpe_ratio, 4),
            "avg_signal_to_fill_ms": (
                round(sum(latency_samples) / len(latency_samples), 1)
                if latency_samples
                else None
            ),
            "anomaly_count": overall_metrics["anomaly_count"],
            "first_1m_green_rate_pct": overall_metrics["first_1m_green_rate_pct"],
            "first_3m_green_rate_pct": overall_metrics["first_3m_green_rate_pct"],
            "first_5m_green_rate_pct": overall_metrics["first_5m_green_rate_pct"],
            "avg_mfe_pct": overall_metrics["avg_mfe_pct"],
            "avg_mae_pct": overall_metrics["avg_mae_pct"],
            "avg_hold_seconds": overall_metrics["avg_hold_seconds"],
            "avg_slippage_bps": overall_metrics["avg_slippage_bps"],
        },
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda x: x[1]["pnl"], reverse=True)[:20]),
        "by_hour": by_hour,
        "by_exit_reason": by_reason,
        "by_strategy_tag": dict(sorted(by_strategy.items(), key=lambda x: x[1]["pnl"], reverse=True)),
        "by_setup_mode": dict(sorted(by_setup_mode.items(), key=lambda x: x[1]["pnl"], reverse=True)),
        "by_signal_source": dict(sorted(by_signal_source.items(), key=lambda x: x[1]["pnl"], reverse=True)),
        "by_asset_type": dict(sorted(by_asset_type.items(), key=lambda x: x[1]["pnl"], reverse=True)),
        "unusual_whales": unusual_whales_metrics,
        "book_report": book_report,
        "play_report": play_report,
        "by_hold_duration": by_hold,
        "equity_curve": equity_curve[-500:],
        "recent_50": {
            "wins": recent_wins,
            "win_rate_pct": round(recent_wins / max(1, len(recent)) * 100, 1),
            "pnl": round(recent_pnl, 2),
            "sharpe_ratio": round(sharpe_ratio_recent_50, 4),
            "clean_trades": len(recent_clean),
            "clean_wins": recent_clean_wins,
            "clean_win_rate_pct": round(recent_clean_wins / max(1, len(recent_clean)) * 100, 1) if recent_clean else 0.0,
            "clean_pnl": round(sum(t.get("pnl", 0) for t in recent_clean), 2),
        },
        "recent_20": {
            "wins": recent_20_wins,
            "win_rate_pct": round(recent_20_wins / max(1, len(recent_20)) * 100, 1),
            "pnl": round(sum(t.get("pnl", 0) for t in recent_20), 2),
            "clean_trades": len(recent_20_clean),
            "clean_wins": recent_20_clean_wins,
            "clean_win_rate_pct": round(recent_20_clean_wins / max(1, len(recent_20_clean)) * 100, 1) if recent_20_clean else 0.0,
            "clean_pnl": round(sum(t.get("pnl", 0) for t in recent_20_clean), 2),
        },
        "today": {
            "date": today_key,
            "trades": today_metrics["trades"],
            "clean_trades": today_metrics["clean_trades"],
            "quarantined_trades": quarantine_summary["today_trades"],
            "raw_pnl": today_metrics["pnl"],
            "clean_pnl": today_metrics["clean_pnl"],
            "anomaly_count": today_metrics["anomaly_count"],
        },
        "quarantine": quarantine_summary,
    }


def get_mode_confusion_report(day: Optional[str] = None, now_ts: Optional[float] = None) -> Dict:
    now_ts = float(now_ts or time.time())
    target_day = str(day or _current_day_key())

    snapshot_rows = []
    for row in load_setup_snapshots():
        recorded_at = float(row.get("recorded_at", 0) or 0)
        if recorded_at <= 0 or _snapshot_day_key(row) != target_day:
            continue
        snapshot_rows.append(dict(row))
    snapshot_rows.sort(key=lambda row: float(row.get("recorded_at", 0) or 0))

    day_trades = [
        trade
        for trade in load_all()
        if _trade_day_key(trade) == target_day and _is_strategy_analytic_trade(trade)
    ]

    setup_groups: Dict[str, Dict] = {}
    symbol_modes: Dict[str, set] = {}
    for row in snapshot_rows:
        setup_id = str(row.get("setup_id", "") or "").strip()
        if not setup_id:
            symbol = str(row.get("symbol", "") or "").upper() or "UNKNOWN"
            setup_id = f"{symbol}:{int(float(row.get('recorded_at', 0) or 0) // 300)}"
        mode = str(row.get("setup_mode", "invalid") or "invalid").strip().lower() or "invalid"
        symbol = str(row.get("symbol", "") or "").upper().strip() or "UNKNOWN"
        symbol_modes.setdefault(symbol, set()).add(mode)
        group = setup_groups.setdefault(
            setup_id,
            {
                "setup_id": setup_id,
                "symbol": symbol,
                "mode": mode,
                "first_seen_at": float(row.get("recorded_at", 0) or 0),
                "last_seen_at": float(row.get("recorded_at", 0) or 0),
                "states_seen": [],
                "trigger_live_any": False,
                "latest_classifier_confidence": 0.0,
                "latest_resolver_confidence": 0.0,
                "latest_no_trade_reason": None,
                "latest_trigger": "",
                "latest_invalidation": "",
                "expires_at": None,
                "entered": False,
                "expired": False,
            },
        )
        group["mode"] = mode or group.get("mode", "invalid")
        group["last_seen_at"] = float(row.get("recorded_at", 0) or 0)
        state = str(row.get("symbol_state", "idle") or "idle")
        if state not in group["states_seen"]:
            group["states_seen"].append(state)
        group["latest_classifier_confidence"] = float(row.get("classifier_confidence", 0.0) or 0.0)
        group["latest_resolver_confidence"] = float(row.get("resolver_confidence", 0.0) or 0.0)
        group["latest_no_trade_reason"] = row.get("no_trade_reason")
        group["latest_trigger"] = str(row.get("trigger", "") or "")
        group["latest_invalidation"] = str(row.get("invalidation", "") or "")
        expires_at = row.get("expires_at")
        if isinstance(expires_at, (int, float)) and float(expires_at) > 0:
            group["expires_at"] = float(expires_at)
        trigger_live = _snapshot_trigger_live(row)
        group["trigger_live_any"] = bool(group["trigger_live_any"] or trigger_live is True)
        group["entered"] = bool(group["entered"] or state == "live_position")

    trades_by_setup: Dict[str, List[Dict]] = {}
    trades_without_pending = 0
    for trade in day_trades:
        setup_id = str(trade.get("setup_id", "") or "").strip()
        if not setup_id:
            continue
        trades_by_setup.setdefault(setup_id, []).append(trade)

    for setup_id, group in setup_groups.items():
        trade_rows = trades_by_setup.get(setup_id, [])
        if trade_rows:
            group["entered"] = True
            group["trade_count"] = len(trade_rows)
            group["trade_pnl"] = round(sum(float(t.get("pnl", 0) or 0) for t in trade_rows), 2)
            group["profitable"] = group["trade_pnl"] > 0
            if "pending_trigger" not in group.get("states_seen", []):
                trades_without_pending += len(trade_rows)
        else:
            group["trade_count"] = 0
            group["trade_pnl"] = 0.0
            group["profitable"] = None

        expires_at = group.get("expires_at")
        if (
            not group.get("entered")
            and "pending_trigger" in group.get("states_seen", [])
            and isinstance(expires_at, (int, float))
            and float(expires_at) <= now_ts
        ):
            group["expired"] = True

    mode_counts: Dict[str, int] = {}
    mode_rollup: Dict[str, Dict] = {}
    for group in setup_groups.values():
        mode = str(group.get("mode", "invalid") or "invalid")
        mode_counts[mode] = int(mode_counts.get(mode, 0) or 0) + 1
        bucket = mode_rollup.setdefault(
            mode,
            {
                "setups": 0,
                "pending": 0,
                "entered": 0,
                "expired": 0,
                "trigger_misses": 0,
                "trade_count": 0,
                "trade_pnl": 0.0,
            },
        )
        bucket["setups"] += 1
        if "pending_trigger" in group.get("states_seen", []):
            bucket["pending"] += 1
        if group.get("entered"):
            bucket["entered"] += 1
        if group.get("expired"):
            bucket["expired"] += 1
        if group.get("trigger_live_any") and not group.get("entered"):
            bucket["trigger_misses"] += 1
        bucket["trade_count"] += int(group.get("trade_count", 0) or 0)
        bucket["trade_pnl"] += float(group.get("trade_pnl", 0.0) or 0.0)

    trade_buckets_by_mode: Dict[str, Dict] = {}
    for trade in day_trades:
        mode = str(trade.get("setup_mode", "invalid") or "invalid").strip().lower() or "invalid"
        bucket = trade_buckets_by_mode.setdefault(mode, _metric_bucket_init())
        _update_metric_bucket(bucket, trade)

    expectancy_by_mode = {
        mode: _finalize_metric_bucket(bucket)
        for mode, bucket in sorted(trade_buckets_by_mode.items(), key=lambda item: item[0])
    }

    top_trigger_misses = sorted(
        (
            {
                "setup_id": group.get("setup_id"),
                "symbol": group.get("symbol"),
                "mode": group.get("mode"),
                "trigger": group.get("latest_trigger"),
                "invalidation": group.get("latest_invalidation"),
                "classifier_confidence": group.get("latest_classifier_confidence"),
                "resolver_confidence": group.get("latest_resolver_confidence"),
                "no_trade_reason": group.get("latest_no_trade_reason"),
                "expired": bool(group.get("expired")),
            }
            for group in setup_groups.values()
            if group.get("trigger_live_any") and not group.get("entered")
        ),
        key=lambda row: (
            float(row.get("classifier_confidence", 0.0) or 0.0)
            + float(row.get("resolver_confidence", 0.0) or 0.0)
        ),
        reverse=True,
    )[:5]

    top_false_positives = sorted(
        (
            {
                "symbol": str(trade.get("symbol", "") or "").upper(),
                "setup_id": str(trade.get("setup_id", "") or ""),
                "mode": str(trade.get("setup_mode", "invalid") or "invalid"),
                "pnl": round(float(trade.get("pnl", 0.0) or 0.0), 2),
                "pnl_pct": round(float(trade.get("pnl_pct", 0.0) or 0.0), 2),
                "reason": str(trade.get("reason", "") or ""),
                "hold_seconds": float(trade.get("hold_seconds", 0.0) or 0.0),
            }
            for trade in day_trades
            if float(trade.get("pnl", 0.0) or 0.0) < 0
        ),
        key=lambda row: float(row.get("pnl", 0.0) or 0.0),
    )[:5]

    return {
        "day": target_day,
        "generated_at": now_ts,
        "classification_counts": dict(sorted(mode_counts.items(), key=lambda item: item[0])),
        "mode_rollup": {
            mode: {
                **bucket,
                "trade_pnl": round(float(bucket.get("trade_pnl", 0.0) or 0.0), 2),
            }
            for mode, bucket in sorted(mode_rollup.items(), key=lambda item: item[0])
        },
        "pending_setups_created": sum(1 for group in setup_groups.values() if "pending_trigger" in group.get("states_seen", [])),
        "pending_setups_triggered": sum(
            1
            for group in setup_groups.values()
            if "pending_trigger" in group.get("states_seen", []) and group.get("entered")
        ),
        "pending_setups_expired": sum(1 for group in setup_groups.values() if group.get("expired")),
        "entries_without_pending_setup": trades_without_pending,
        "entries_with_pending_setup": sum(
            int(group.get("trade_count", 0) or 0)
            for group in setup_groups.values()
            if group.get("entered") and "pending_trigger" in group.get("states_seen", [])
        ),
        "mode_flip_symbols": sorted(symbol for symbol, modes in symbol_modes.items() if len(modes - {"invalid"}) > 1),
        "mode_flip_count": sum(1 for modes in symbol_modes.values() if len(modes - {"invalid"}) > 1),
        "executed_trades_by_mode": expectancy_by_mode,
        "top_trigger_misses": top_trigger_misses,
        "top_false_positives": top_false_positives,
        "snapshot_count": len(snapshot_rows),
        "setup_count": len(setup_groups),
        "trade_count": len(day_trades),
    }


def _bucket_init():
    return {"trades": 0, "wins": 0, "pnl": 0.0}


def _metric_bucket_init() -> Dict:
    return {
        "trades": 0,
        "clean_trades": 0,
        "wins": 0,
        "clean_wins": 0,
        "losses": 0,
        "clean_losses": 0,
        "pnl": 0.0,
        "clean_pnl": 0.0,
        "win_pnl_sum": 0.0,
        "loss_pnl_sum": 0.0,
        "anomaly_count": 0,
        "green_1m_hits": 0,
        "green_1m_seen": 0,
        "green_3m_hits": 0,
        "green_3m_seen": 0,
        "green_5m_hits": 0,
        "green_5m_seen": 0,
        "mfe_sum": 0.0,
        "mfe_count": 0,
        "mae_sum": 0.0,
        "mae_count": 0,
        "hold_sum": 0.0,
        "hold_count": 0,
        "slippage_sum": 0.0,
        "slippage_count": 0,
        "ratchet_activations": 0,
        "latency_sum": 0.0,
        "latency_count": 0,
        "best_trade_pnl": None,
        "worst_trade_pnl": None,
    }


def _build_metric_bucket(trades: List[Dict]) -> Dict:
    bucket = _metric_bucket_init()
    for trade in trades or []:
        _update_metric_bucket(bucket, trade)
    return bucket


def _trade_has_anomaly(trade: Dict) -> bool:
    flags = trade.get("anomaly_flags", [])
    if isinstance(flags, str):
        flags = [f.strip() for f in flags.split(",") if f.strip()]
    return bool(flags)


def _build_quarantine_summary(trades: List[Dict], *, today_key: Optional[str] = None) -> Dict:
    by_flag = Counter()
    by_reason = Counter()
    today_rows = []
    target_today = str(today_key or _current_day_key())
    for trade in trades or []:
        reason = str(trade.get("reason", "") or "").strip() or "unknown"
        by_reason[reason] += 1
        flags = trade.get("anomaly_flags", []) or []
        if isinstance(flags, str):
            flags = [f.strip() for f in flags.split(",") if f.strip()]
        for flag in flags:
            normalized = str(flag or "").strip()
            if normalized:
                by_flag[normalized] += 1
        if _trade_day_key(trade) == target_today:
            today_rows.append(trade)
    return {
        "trades": len(trades or []),
        "pnl": round(sum(float(t.get("pnl", 0) or 0) for t in (trades or [])), 2),
        "today_trades": len(today_rows),
        "today_pnl": round(sum(float(t.get("pnl", 0) or 0) for t in today_rows), 2),
        "by_flag": dict(sorted(by_flag.items(), key=lambda item: (-item[1], item[0]))),
        "by_reason": dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
    }


def _update_metric_bucket(bucket: Dict, trade: Dict):
    pnl = float(trade.get("pnl", 0) or 0)
    bucket["trades"] += 1
    bucket["pnl"] += pnl
    best_trade = bucket.get("best_trade_pnl")
    worst_trade = bucket.get("worst_trade_pnl")
    bucket["best_trade_pnl"] = pnl if best_trade is None else max(float(best_trade), pnl)
    bucket["worst_trade_pnl"] = pnl if worst_trade is None else min(float(worst_trade), pnl)
    if pnl > 0:
        bucket["wins"] += 1
        bucket["win_pnl_sum"] += pnl
    elif pnl < 0:
        bucket["losses"] += 1
        bucket["loss_pnl_sum"] += pnl
    if _trade_has_anomaly(trade):
        bucket["anomaly_count"] += 1
    else:
        bucket["clean_trades"] += 1
        bucket["clean_pnl"] += pnl
        if pnl > 0:
            bucket["clean_wins"] += 1
        elif pnl < 0:
            bucket["clean_losses"] += 1
    if _trade_reached_ratchet_activation(trade):
        bucket["ratchet_activations"] += 1

    for seconds, price_field, hits_key, seen_key in (
        (60, "price_at_1m", "green_1m_hits", "green_1m_seen"),
        (180, "price_at_3m", "green_3m_hits", "green_3m_seen"),
        (300, "price_at_5m", "green_5m_hits", "green_5m_seen"),
    ):
        price = trade.get(price_field)
        if not isinstance(price, (int, float)):
            continue
        bucket[seen_key] += 1
        if _directional_trade_move_pct(trade, float(price)) > 0:
            bucket[hits_key] += 1

    mfe_pct = trade.get("mfe_pct")
    if isinstance(mfe_pct, (int, float)):
        bucket["mfe_sum"] += float(mfe_pct)
        bucket["mfe_count"] += 1

    mae_pct = trade.get("mae_pct")
    if isinstance(mae_pct, (int, float)):
        bucket["mae_sum"] += float(mae_pct)
        bucket["mae_count"] += 1

    hold_seconds = trade.get("hold_seconds")
    if isinstance(hold_seconds, (int, float)):
        bucket["hold_sum"] += float(hold_seconds)
        bucket["hold_count"] += 1

    slippage = trade.get("slippage_bps")
    if isinstance(slippage, (int, float)):
        bucket["slippage_sum"] += float(slippage)
        bucket["slippage_count"] += 1

    latency = trade.get("signal_to_fill_ms")
    if isinstance(latency, (int, float)):
        bucket["latency_sum"] += float(latency)
        bucket["latency_count"] += 1


def _finalize_metric_bucket(bucket: Dict) -> Dict:
    trades = int(bucket.get("trades", 0) or 0)
    clean_trades = int(bucket.get("clean_trades", 0) or 0)
    wins = int(bucket.get("wins", 0) or 0)
    clean_wins = int(bucket.get("clean_wins", 0) or 0)
    losses = int(bucket.get("losses", 0) or 0)
    clean_losses = int(bucket.get("clean_losses", 0) or 0)
    avg_win = _avg_or_none(bucket.get("win_pnl_sum", 0.0), wins)
    avg_loss = _avg_or_none(bucket.get("loss_pnl_sum", 0.0), losses)
    win_rate_pct = round(wins / max(1, trades) * 100, 1)
    win_rate_ratio = wins / max(1, trades)
    avg_win_abs = float(avg_win or 0.0)
    avg_loss_abs = abs(float(avg_loss or 0.0))
    expectancy = (win_rate_ratio * avg_win_abs) - ((1.0 - win_rate_ratio) * avg_loss_abs)
    gross_profit = round(float(bucket.get("win_pnl_sum", 0.0) or 0.0), 2)
    gross_loss = round(abs(float(bucket.get("loss_pnl_sum", 0.0) or 0.0)), 2)
    profit_factor = None
    if gross_loss > 0:
        profit_factor = round(gross_profit / gross_loss, 4)
    return {
        "trades": trades,
        "clean_trades": clean_trades,
        "wins": wins,
        "clean_wins": clean_wins,
        "losses": losses,
        "clean_losses": clean_losses,
        "pnl": round(float(bucket.get("pnl", 0.0) or 0.0), 2),
        "clean_pnl": round(float(bucket.get("clean_pnl", 0.0) or 0.0), 2),
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": profit_factor,
        "win_rate": win_rate_pct,
        "win_rate_pct": win_rate_pct,
        "clean_win_rate_pct": round(clean_wins / max(1, clean_trades) * 100, 1) if clean_trades else 0.0,
        "avg_pnl": round(float(bucket.get("pnl", 0.0) or 0.0) / max(1, trades), 2),
        "avg_win": round(float(avg_win or 0.0), 2) if avg_win is not None else None,
        "avg_loss": round(float(avg_loss or 0.0), 2) if avg_loss is not None else None,
        "expectancy": round(expectancy, 2),
        "best_trade_pnl": round(float(bucket.get("best_trade_pnl", 0.0) or 0.0), 2)
        if bucket.get("best_trade_pnl") is not None
        else None,
        "worst_trade_pnl": round(float(bucket.get("worst_trade_pnl", 0.0) or 0.0), 2)
        if bucket.get("worst_trade_pnl") is not None
        else None,
        "anomaly_count": int(bucket.get("anomaly_count", 0) or 0),
        "ratchet_activation_rate_pct": _rate_pct(bucket.get("ratchet_activations", 0), trades),
        "first_1m_green_rate_pct": _rate_pct(bucket.get("green_1m_hits", 0), bucket.get("green_1m_seen", 0)),
        "first_3m_green_rate_pct": _rate_pct(bucket.get("green_3m_hits", 0), bucket.get("green_3m_seen", 0)),
        "first_5m_green_rate_pct": _rate_pct(bucket.get("green_5m_hits", 0), bucket.get("green_5m_seen", 0)),
        "avg_mfe_pct": _avg_or_none(bucket.get("mfe_sum", 0.0), bucket.get("mfe_count", 0)),
        "avg_mae_pct": _avg_or_none(bucket.get("mae_sum", 0.0), bucket.get("mae_count", 0)),
        "avg_hold_seconds": _avg_or_none(bucket.get("hold_sum", 0.0), bucket.get("hold_count", 0)),
        "avg_slippage_bps": _avg_or_none(bucket.get("slippage_sum", 0.0), bucket.get("slippage_count", 0)),
        "avg_signal_to_fill_ms": _avg_or_none(bucket.get("latency_sum", 0.0), bucket.get("latency_count", 0)),
    }


def _avg_or_none(total: float, count: int) -> Optional[float]:
    if not count:
        return None
    return round(float(total) / float(count), 4)


def _rate_pct(hits: int, seen: int) -> Optional[float]:
    if not seen:
        return None
    return round((float(hits) / float(seen)) * 100.0, 1)


def _directional_trade_move_pct(trade: Dict, observed_price: float) -> float:
    entry_price = float(trade.get("entry_price", 0) or 0)
    if entry_price <= 0 or observed_price <= 0:
        return 0.0
    side = str(trade.get("side", "sell") or "sell").lower()
    if side in ("sell_short", "short", "buy_to_cover"):
        return ((entry_price - observed_price) / entry_price) * 100.0
    return ((observed_price - entry_price) / entry_price) * 100.0


def _is_strategy_analytic_trade(trade: Dict) -> bool:
    strategy = normalize_strategy_tag(trade.get("strategy_tag", "unknown"), fallback="unknown", allow_artifacts=True)
    return not is_artifact_strategy_tag(strategy)


def _trade_reached_ratchet_activation(trade: Dict) -> bool:
    if trade.get("ratchet_floor_pct") is not None:
        return True
    peak_pct = trade.get("ratchet_peak_pnl_pct", trade.get("mfe_pct"))
    if not isinstance(peak_pct, (int, float)):
        return False
    holding_horizon = str(trade.get("holding_horizon", "intraday") or "intraday").lower()
    activation_threshold = 3.0 if holding_horizon == "swing" else 1.5
    return float(peak_pct) >= activation_threshold


def _current_day_key() -> str:
    return trading_session_day()


def _trade_day_key(trade: Dict) -> str:
    ts = float(trade.get("exit_time", trade.get("recorded_at", 0)) or 0)
    if ts <= 0:
        return ""
    return trading_session_day(ts)


def _snapshot_day_key(snapshot: Dict) -> str:
    ts = float(snapshot.get("recorded_at", 0) or 0)
    if ts <= 0:
        return ""
    return trading_session_day(ts)


def _snapshot_trigger_live(snapshot: Dict) -> Optional[bool]:
    stored = snapshot.get("trigger_live")
    if isinstance(stored, bool):
        return stored
    trigger_spec_payload = dict(snapshot.get("trigger_spec", {}) or {})
    if not trigger_spec_payload:
        return None
    features = mode_features_from_dict(dict(snapshot.get("mode_features", {}) or {}))
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


def _trade_return(trade: Dict) -> Optional[float]:
    if isinstance(trade.get("pnl_pct"), (int, float)):
        return float(trade.get("pnl_pct", 0) or 0) / 100.0
    entry_price = float(trade.get("entry_price", 0) or 0)
    quantity = float(trade.get("quantity", 0) or 0)
    if entry_price <= 0 or quantity <= 0:
        return None
    notional = entry_price * quantity
    if notional <= 0:
        return None
    return float(trade.get("pnl", 0) or 0) / notional


def _trade_timestamp(trade: Dict) -> float:
    return float(
        trade.get("exit_time", trade.get("recorded_at", trade.get("entry_time", 0))) or 0
    )


def _normalize_regime(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    return text or "unknown"


def _trade_session_type(trade: Dict) -> str:
    session_type = str(trade.get("session_type", "") or "").strip().lower()
    if session_type in {"pre", "regular", "after", "overnight"}:
        return session_type
    entry_time = float(trade.get("entry_time", 0) or 0)
    if entry_time > 0:
        try:
            from zoneinfo import ZoneInfo

            dt = datetime.fromtimestamp(entry_time, ZoneInfo("America/New_York"))
            minutes = (dt.hour * 60) + dt.minute
            if 240 <= minutes < 570:
                return "pre"
            if 570 <= minutes < 960:
                return "regular"
            if 960 <= minutes < 1200:
                return "after"
            return "overnight"
        except Exception:
            pass
    if bool(trade.get("extended_hours_entry", False)):
        return "extended"
    if entry_time > 0:
        return "regular"
    return "unknown"


def _compute_max_drawdown(trades: List[Dict]) -> float:
    if not trades:
        return 0.0
    running_pnl = 0.0
    peak_pnl = 0.0
    max_drawdown = 0.0
    for trade in sorted(trades, key=_trade_timestamp):
        running_pnl += float(trade.get("pnl", 0.0) or 0.0)
        peak_pnl = max(peak_pnl, running_pnl)
        max_drawdown = max(max_drawdown, peak_pnl - running_pnl)
    return round(max_drawdown, 2)


def _build_dimension_metrics(groups: Dict[str, List[Dict]]) -> Dict:
    metrics_by_key = {}
    for key, trades in (groups or {}).items():
        if not trades:
            continue
        metrics = _finalize_metric_bucket(_build_metric_bucket(trades))
        metrics["sharpe_ratio"] = round(_compute_sharpe(trades), 4)
        metrics["max_drawdown"] = _compute_max_drawdown(trades)
        metrics_by_key[str(key)] = metrics
    return dict(sorted(metrics_by_key.items(), key=lambda item: item[1]["pnl"], reverse=True))


def _best_and_worst_dimension(metrics_by_key: Dict[str, Dict]) -> Dict[str, Optional[Dict]]:
    rows = [(key, value) for key, value in (metrics_by_key or {}).items() if int(value.get("trades", 0) or 0) > 0]
    if not rows:
        return {"best": None, "worst": None}

    qualified = [item for item in rows if int(item[1].get("trades", 0) or 0) >= 3] or rows
    best_key, best_value = max(
        qualified,
        key=lambda item: (
            int(item[1].get("trades", 0) or 0),
            float(item[1].get("expectancy", 0.0) or 0.0),
            float(item[1].get("pnl", 0.0) or 0.0),
            item[0],
        ),
    )
    worst_key, worst_value = min(
        qualified,
        key=lambda item: (
            -int(item[1].get("trades", 0) or 0),
            float(item[1].get("expectancy", 0.0) or 0.0),
            float(item[1].get("pnl", 0.0) or 0.0),
            item[0],
        ),
    )
    return {
        "best": {"name": best_key, **best_value},
        "worst": {"name": worst_key, **worst_value},
    }


def _book_control_metadata(strategy_tag: str, controls: Dict) -> Dict:
    tag = normalize_strategy_tag(strategy_tag, fallback="unknown")
    manual_enabled = dict((controls or {}).get("manual_enabled", {}) or {})
    manual_disabled = dict((controls or {}).get("manual_disabled", {}) or {})
    hard_disabled = dict((controls or {}).get("hard_disabled", {}) or {})
    soft_disabled = dict((controls or {}).get("soft_disabled", {}) or {})
    probation = dict((controls or {}).get("probation", {}) or {})
    size_reductions = dict((controls or {}).get("size_reductions", {}) or {})

    control_state = "active"
    control_reason = ""
    if tag in manual_disabled:
        control_state = "manual_disabled"
        control_reason = str(manual_disabled.get(tag, {}).get("reason", "") or "")
    elif tag in hard_disabled and tag not in manual_enabled:
        control_state = "hard_disabled"
        control_reason = str(hard_disabled.get(tag, {}).get("reason", "") or "")
    elif tag in soft_disabled and tag not in manual_enabled:
        control_state = "soft_disabled"
        control_reason = str(soft_disabled.get(tag, {}).get("reason", "") or "")
    elif isinstance(probation.get(tag), dict) and str(probation[tag].get("status", "active") or "active") == "active":
        control_state = "probation"
        control_reason = str(probation[tag].get("reason", "") or "")
    elif tag in size_reductions:
        control_state = "size_reduced"
        control_reason = str(size_reductions.get(tag, {}).get("reason", "") or "")
    elif tag in manual_enabled:
        control_state = "manual_enabled"
        control_reason = str(manual_enabled.get(tag, {}).get("reason", "") or "")

    size_multiplier = float(strategy_controls.get_size_multiplier(tag, controls) or 1.0)
    return {
        "control_state": control_state,
        "control_reason": control_reason,
        "size_multiplier": round(size_multiplier, 4),
        "disabled": control_state in {"manual_disabled", "hard_disabled", "soft_disabled"},
    }


def _effective_metric_inputs(metrics: Dict) -> Dict:
    raw_trades = int(metrics.get("trades", 0) or 0)
    clean_trades = int(metrics.get("clean_trades", 0) or 0)
    anomaly_count = int(metrics.get("anomaly_count", 0) or 0)
    anomaly_ratio = (float(anomaly_count) / float(raw_trades)) if raw_trades > 0 else 0.0
    use_clean = clean_trades > 0
    trades = clean_trades if use_clean else raw_trades
    pnl = float(
        metrics.get("clean_pnl", metrics.get("pnl", 0.0)) if use_clean else metrics.get("pnl", 0.0)
        or 0.0
    )
    win_rate = float(
        metrics.get(
            "clean_win_rate_pct",
            metrics.get("win_rate_pct", metrics.get("win_rate", 0.0)),
        )
        if use_clean
        else metrics.get("win_rate_pct", metrics.get("win_rate", 0.0))
        or 0.0
    )
    expectancy = round(pnl / max(1, trades), 2) if trades > 0 else 0.0
    evidence_source = "clean" if use_clean else "raw"
    return {
        "raw_trades": raw_trades,
        "clean_trades": clean_trades,
        "trades": trades,
        "pnl": pnl,
        "win_rate": win_rate,
        "expectancy": expectancy,
        "anomaly_count": anomaly_count,
        "anomaly_ratio": anomaly_ratio,
        "evidence_source": evidence_source,
    }


def _recent_trade_window(trades: List[Dict], limit: int) -> List[Dict]:
    if not trades or limit <= 0:
        return []
    rows = sorted((trades or []), key=_trade_timestamp, reverse=True)
    return rows[: max(1, int(limit or 0))]


def _attach_recent_metrics(summary: Dict, trades: List[Dict], *, limit: int) -> Dict:
    recent_trades = _recent_trade_window(trades, limit)
    if not recent_trades:
        return summary

    recent_summary = _finalize_metric_bucket(_build_metric_bucket(recent_trades))
    recent_summary["max_drawdown"] = _compute_max_drawdown(recent_trades)
    recent_summary["sharpe_ratio"] = round(_compute_sharpe(recent_trades), 4)

    summary.update(
        {
            "recent_window_trades": int(recent_summary.get("trades", 0) or 0),
            "recent_window_clean_trades": int(recent_summary.get("clean_trades", 0) or 0),
            "recent_window_pnl": round(float(recent_summary.get("pnl", 0.0) or 0.0), 2),
            "recent_window_clean_pnl": round(float(recent_summary.get("clean_pnl", 0.0) or 0.0), 2),
            "recent_window_win_rate_pct": round(
                float(recent_summary.get("win_rate_pct", recent_summary.get("win_rate", 0.0)) or 0.0),
                1,
            ),
            "recent_window_clean_win_rate_pct": round(
                float(recent_summary.get("clean_win_rate_pct", 0.0) or 0.0),
                1,
            ),
            "recent_window_expectancy": round(float(recent_summary.get("expectancy", 0.0) or 0.0), 2),
            "recent_window_profit_factor": recent_summary.get("profit_factor"),
            "recent_window_sharpe_ratio": round(float(recent_summary.get("sharpe_ratio", 0.0) or 0.0), 4),
            "recent_window_max_drawdown": round(float(recent_summary.get("max_drawdown", 0.0) or 0.0), 2),
        }
    )
    return summary


def _sample_is_degraded(metrics: Dict, *, minimum_trades: int) -> bool:
    effective = _effective_metric_inputs(metrics)
    raw_trades = int(effective.get("raw_trades", 0) or 0)
    clean_trades = int(effective.get("clean_trades", 0) or 0)
    anomaly_ratio = float(effective.get("anomaly_ratio", 0.0) or 0.0)
    anomaly_count = int(effective.get("anomaly_count", 0) or 0)

    if anomaly_count <= 0:
        return False
    if clean_trades <= 0:
        return True
    if raw_trades >= minimum_trades and clean_trades < max(3, minimum_trades // 2):
        return True
    return anomaly_ratio >= 0.5


def _recent_recovery_action(
    metrics: Dict,
    *,
    label: str,
    hold_trades: int,
    scale_trades: int,
    scale_win_rate: float,
    scale_profit_factor: float,
) -> Optional[Dict]:
    recent_clean_trades = int(metrics.get("recent_window_clean_trades", 0) or 0)
    recent_raw_trades = int(metrics.get("recent_window_trades", 0) or 0)
    recent_trades = recent_clean_trades if recent_clean_trades > 0 else recent_raw_trades
    if recent_trades < max(1, int(hold_trades or 0)):
        return None

    recent_pnl = float(
        metrics.get("recent_window_clean_pnl", metrics.get("recent_window_pnl", 0.0)) or 0.0
    )
    recent_win_rate = float(
        metrics.get(
            "recent_window_clean_win_rate_pct",
            metrics.get("recent_window_win_rate_pct", 0.0),
        )
        or 0.0
    )
    recent_expectancy = float(metrics.get("recent_window_expectancy", 0.0) or 0.0)
    recent_profit_factor = metrics.get("recent_window_profit_factor")
    recent_sharpe = float(metrics.get("recent_window_sharpe_ratio", 0.0) or 0.0)

    if recent_pnl <= 0 or recent_expectancy <= 0:
        return None

    if (
        recent_trades >= max(hold_trades, scale_trades)
        and recent_win_rate >= float(scale_win_rate or 0.0)
        and (recent_profit_factor is None or float(recent_profit_factor or 0.0) >= float(scale_profit_factor or 0.0))
        and recent_sharpe >= 0.0
    ):
        return {
            "status": "scale",
            "recommended_action": "scale",
            "status_reason": (
                f"Recent clean {label} recovery: expectancy {recent_expectancy:.2f}, "
                f"win rate {recent_win_rate:.1f}% over {recent_trades} trades"
            ),
        }

    return {
        "status": "hold",
        "recommended_action": "hold",
        "status_reason": (
            f"Recent clean {label} recovery: expectancy {recent_expectancy:.2f}, "
            f"pnl ${recent_pnl:.2f} over {recent_trades} trades"
        ),
    }


def _recommend_book_action(metrics: Dict, control_metadata: Dict) -> Dict:
    control_state = str(control_metadata.get("control_state", "active") or "active")
    control_reason = str(control_metadata.get("control_reason", "") or "")
    if control_state in {"manual_disabled", "hard_disabled", "soft_disabled"}:
        return {
            "status": "disable",
            "recommended_action": "disable",
            "status_reason": control_reason or f"{control_state} control active",
        }
    if control_state == "probation":
        return {
            "status": "probation",
            "recommended_action": "probation",
            "status_reason": control_reason or "Probation active",
        }

    recent_recovery = _recent_recovery_action(
        metrics,
        label="book",
        hold_trades=5,
        scale_trades=8,
        scale_win_rate=58.0,
        scale_profit_factor=1.15,
    )
    if recent_recovery:
        return recent_recovery

    effective = _effective_metric_inputs(metrics)
    trades = int(effective.get("trades", 0) or 0)
    expectancy = float(effective.get("expectancy", 0.0) or 0.0)
    pnl = float(effective.get("pnl", 0.0) or 0.0)
    evidence_source = str(effective.get("evidence_source", "raw") or "raw")
    anomaly_ratio = float(effective.get("anomaly_ratio", 0.0) or 0.0)
    profit_factor = metrics.get("profit_factor")
    max_drawdown = float(metrics.get("max_drawdown", 0.0) or 0.0)
    sharpe_ratio = float(metrics.get("sharpe_ratio", 0.0) or 0.0)

    if _sample_is_degraded(metrics, minimum_trades=10):
        return {
            "status": "hold",
            "recommended_action": "observe",
            "status_reason": (
                f"Book sample degraded by anomalies ({effective.get('clean_trades', 0)}/"
                f"{effective.get('raw_trades', 0)} clean trades)"
            ),
        }
    if trades < 10:
        return {
            "status": "hold",
            "recommended_action": "observe",
            "status_reason": (
                f"Only {trades} {evidence_source} closed trades; not enough sample size yet"
            ),
        }
    if expectancy <= 0 or pnl <= 0:
        recommended_action = "disable" if trades >= 25 and pnl < 0 else "probation"
        return {
            "status": recommended_action,
            "recommended_action": recommended_action,
            "status_reason": (
                f"{evidence_source.capitalize()} expectancy {expectancy:.2f}, pnl ${pnl:.2f} over {trades} trades"
            ),
        }
    if (
        trades >= 20
        and anomaly_ratio < 0.25
        and (profit_factor is None or float(profit_factor) >= 1.25)
        and sharpe_ratio > 0
    ):
        drawdown_limit = max(75.0, pnl * 0.75)
        if max_drawdown <= drawdown_limit:
            return {
                "status": "scale",
                "recommended_action": "scale",
                "status_reason": (
                    f"Positive {evidence_source} expectancy {expectancy:.2f}, PF {profit_factor if profit_factor is not None else 'n/a'}, "
                    f"Sharpe {sharpe_ratio:.2f}, max DD ${max_drawdown:.2f}"
                ),
            }
    return {
        "status": "hold",
        "recommended_action": "hold",
        "status_reason": (
            f"Positive {evidence_source} expectancy {expectancy:.2f} but still stabilizing over {trades} trades"
        ),
    }


def _build_book_report(trades: List[Dict]) -> Dict:
    analytic_trades = [t for t in trades or [] if _is_strategy_analytic_trade(t)]
    controls = strategy_controls.load_controls()
    by_strategy: Dict[str, List[Dict]] = {}
    for trade in analytic_trades:
        strategy = normalize_strategy_tag(trade.get("strategy_tag", "unknown"), fallback="unknown", allow_artifacts=True)
        if is_artifact_strategy_tag(strategy):
            continue
        by_strategy.setdefault(strategy, []).append(trade)

    rows: List[Dict] = []
    for strategy, strategy_trades in by_strategy.items():
        summary = _finalize_metric_bucket(_build_metric_bucket(strategy_trades))
        summary = _attach_recent_metrics(summary, strategy_trades, limit=10)
        summary["max_drawdown"] = _compute_max_drawdown(strategy_trades)
        summary["sharpe_ratio"] = round(_compute_sharpe(strategy_trades), 4)
        summary["net_pnl"] = summary["pnl"]

        regime_groups: Dict[str, List[Dict]] = {}
        session_groups: Dict[str, List[Dict]] = {}
        for trade in strategy_trades:
            regime_groups.setdefault(_normalize_regime(trade.get("market_regime")), []).append(trade)
            session_groups.setdefault(_trade_session_type(trade), []).append(trade)

        regimes = _build_dimension_metrics(regime_groups)
        sessions = _build_dimension_metrics(session_groups)
        regime_extremes = _best_and_worst_dimension(regimes)
        session_extremes = _best_and_worst_dimension(sessions)
        control_metadata = _book_control_metadata(strategy, controls)
        action_metadata = _recommend_book_action(summary, control_metadata)

        row = {
            "strategy_tag": strategy,
            **summary,
            **control_metadata,
            **action_metadata,
            "trade_count": summary["trades"],
            "regimes": regimes,
            "sessions": sessions,
            "best_regime": regime_extremes["best"],
            "worst_regime": regime_extremes["worst"],
            "best_session": session_extremes["best"],
            "worst_session": session_extremes["worst"],
        }
        rows.append(row)

    rows.sort(
        key=lambda row: (
            {"scale": 0, "hold": 1, "probation": 2, "disable": 3}.get(str(row.get("status", "hold")), 9),
            -float(row.get("pnl", 0.0) or 0.0),
            -float(row.get("expectancy", 0.0) or 0.0),
            row.get("strategy_tag", ""),
        )
    )

    summary = {
        "books": len(rows),
        "scale": sum(1 for row in rows if row.get("status") == "scale"),
        "hold": sum(1 for row in rows if row.get("status") == "hold"),
        "probation": sum(1 for row in rows if row.get("status") == "probation"),
        "disable": sum(1 for row in rows if row.get("status") == "disable"),
        "observe": sum(1 for row in rows if row.get("recommended_action") == "observe"),
    }
    return {
        "summary": summary,
        "books": rows,
        "generated_at": time.time(),
    }


def _recommend_play_action(metrics: Dict) -> Dict:
    recent_recovery = _recent_recovery_action(
        metrics,
        label="play",
        hold_trades=4,
        scale_trades=6,
        scale_win_rate=60.0,
        scale_profit_factor=1.1,
    )
    if recent_recovery:
        return recent_recovery

    effective = _effective_metric_inputs(metrics)
    trades = int(effective.get("trades", 0) or 0)
    expectancy = float(effective.get("expectancy", 0.0) or 0.0)
    pnl = float(effective.get("pnl", 0.0) or 0.0)
    evidence_source = str(effective.get("evidence_source", "raw") or "raw")
    anomaly_ratio = float(effective.get("anomaly_ratio", 0.0) or 0.0)
    profit_factor = metrics.get("profit_factor")
    sharpe_ratio = float(metrics.get("sharpe_ratio", 0.0) or 0.0)

    if _sample_is_degraded(metrics, minimum_trades=6):
        return {
            "status": "hold",
            "recommended_action": "observe",
            "status_reason": (
                f"Play sample degraded by anomalies ({effective.get('clean_trades', 0)}/"
                f"{effective.get('raw_trades', 0)} clean trades)"
            ),
        }
    if trades < 6:
        return {
            "status": "hold",
            "recommended_action": "observe",
            "status_reason": f"Only {trades} {evidence_source} play samples; still observing",
        }
    if expectancy <= 0 or pnl <= 0:
        recommended_action = "disable" if trades >= 16 and pnl < 0 else "probation"
        return {
            "status": recommended_action,
            "recommended_action": recommended_action,
            "status_reason": (
                f"Play edge weak on {evidence_source} sample: expectancy {expectancy:.2f}, "
                f"pnl ${pnl:.2f} over {trades} trades"
            ),
        }
    if (
        trades >= 12
        and anomaly_ratio < 0.25
        and (profit_factor is None or float(profit_factor) >= 1.15)
        and sharpe_ratio >= 0
    ):
        return {
            "status": "scale",
            "recommended_action": "scale",
            "status_reason": (
                f"Play edge confirmed on {evidence_source} sample: expectancy {expectancy:.2f}, "
                f"PF {profit_factor if profit_factor is not None else 'n/a'}, Sharpe {sharpe_ratio:.2f}"
            ),
        }
    return {
        "status": "hold",
        "recommended_action": "hold",
        "status_reason": f"Positive {evidence_source} play edge, still stabilizing over {trades} trades",
    }


def _build_play_report(trades: List[Dict]) -> Dict:
    analytic_trades = [t for t in trades or [] if _is_strategy_analytic_trade(t)]
    groups: Dict[tuple, List[Dict]] = {}
    for trade in analytic_trades:
        strategy = normalize_strategy_tag(trade.get("strategy_tag", "unknown"), fallback="unknown", allow_artifacts=True)
        if is_artifact_strategy_tag(strategy):
            continue
        setup_mode = str(trade.get("setup_mode", "invalid") or "invalid").strip().lower() or "invalid"
        if setup_mode in {"", "invalid", "unknown"}:
            continue
        regime = _normalize_regime(trade.get("market_regime"))
        session = _trade_session_type(trade)
        groups.setdefault((strategy, setup_mode, regime, session), []).append(trade)

    rows: List[Dict] = []
    for (strategy, setup_mode, regime, session), play_trades in groups.items():
        summary = _finalize_metric_bucket(_build_metric_bucket(play_trades))
        summary = _attach_recent_metrics(summary, play_trades, limit=8)
        summary["max_drawdown"] = _compute_max_drawdown(play_trades)
        summary["sharpe_ratio"] = round(_compute_sharpe(play_trades), 4)
        action_metadata = _recommend_play_action(summary)
        row = {
            "play_key": f"{strategy}|{setup_mode}|{regime}|{session}",
            "strategy_tag": strategy,
            "setup_mode": setup_mode,
            "market_regime": regime,
            "session_type": session,
            **summary,
            **action_metadata,
        }
        rows.append(row)

    rows.sort(
        key=lambda row: (
            {"scale": 0, "hold": 1, "probation": 2, "disable": 3}.get(str(row.get("status", "hold")), 9),
            -float(row.get("pnl", 0.0) or 0.0),
            -float(row.get("expectancy", 0.0) or 0.0),
            row.get("play_key", ""),
        )
    )

    summary = {
        "plays": len(rows),
        "scale": sum(1 for row in rows if row.get("status") == "scale"),
        "hold": sum(1 for row in rows if row.get("status") == "hold"),
        "probation": sum(1 for row in rows if row.get("status") == "probation"),
        "disable": sum(1 for row in rows if row.get("status") == "disable"),
        "observe": sum(1 for row in rows if row.get("recommended_action") == "observe"),
    }
    return {
        "summary": summary,
        "plays": rows,
        "generated_at": time.time(),
    }


def _compute_sharpe(trades: List[Dict], risk_free_rate: float = 0.05) -> float:
    returns = [r for r in (_trade_return(t) for t in trades or []) if r is not None]
    if len(returns) < 2:
        return 0.0
    mean_return = sum(returns) / len(returns)
    variance = sum((r - mean_return) ** 2 for r in returns) / max(1, len(returns) - 1)
    std_dev = math.sqrt(variance)
    if std_dev <= 0:
        return 0.0

    if len(trades) >= 2:
        timestamps = [float(t.get("exit_time", t.get("recorded_at", 0)) or 0) for t in trades if t.get("exit_time") or t.get("recorded_at")]
        timestamps = [ts for ts in timestamps if ts > 0]
        if len(timestamps) >= 2:
            days = max(1.0, (max(timestamps) - min(timestamps)) / 86400.0)
        else:
            days = max(1.0, len(trades) / 2.0)
    else:
        days = 1.0

    trades_per_day = max(1.0, len(returns) / days)
    annualized_return = mean_return * trades_per_day * 252.0
    annualized_std = std_dev * math.sqrt(trades_per_day * 252.0)
    if annualized_std <= 0:
        return 0.0
    return (annualized_return - risk_free_rate) / annualized_std
