"""
Persistent trade history — survives restarts, feeds game film analysis.
File: data/trade_history.json
"""

import json
import math
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

from src import persistence
from src.data.trade_schema import normalize_trade_record

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
        return [normalize_trade_record(t) for t in data]
    except Exception:
        return []


def get_recent(n: int = 50) -> List[Dict]:
    """Get last N trades."""
    return load_all()[-n:]


def get_analytics() -> Dict:
    """Generate structured analytics for AI consumption."""
    history = load_all()
    if not history:
        return {
            "total_trades": 0,
            "message": "No trade history yet.",
            "win_rate": 0.0,
            "total_pnl": 0.0,
            "clean_pnl": 0.0,
            "sharpe_ratio": 0.0,
            "sharpe_ratio_recent_50": 0.0,
            "today": {
                "trades": 0,
                "raw_pnl": 0.0,
                "clean_pnl": 0.0,
                "anomaly_count": 0,
            },
        }

    wins = [t for t in history if t.get("pnl", 0) > 0]
    losses = [t for t in history if t.get("pnl", 0) < 0]
    breakevens = [t for t in history if t.get("pnl", 0) == 0]
    total_pnl = sum(t.get("pnl", 0) for t in history)
    clean_pnl = sum(t.get("pnl", 0) for t in history if not _trade_has_anomaly(t))
    latency_samples = [
        float(t.get("signal_to_fill_ms"))
        for t in history
        if isinstance(t.get("signal_to_fill_ms"), (int, float))
    ]

    # By symbol
    by_symbol = {}
    for t in history:
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
    for t in history:
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
    for t in history:
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
    for t in history:
        strategy = t.get("strategy_tag", "unknown") or "unknown"
        if strategy not in by_strategy:
            by_strategy[strategy] = _metric_bucket_init()
        _update_metric_bucket(by_strategy[strategy], t)
    for strategy, bucket in list(by_strategy.items()):
        by_strategy[strategy] = _finalize_metric_bucket(bucket)

    # Latency by strategy
    strategy_latency = {}
    for t in history:
        strategy = t.get("strategy_tag", "unknown") or "unknown"
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

    # By signal source (participation attribution)
    by_signal_source = {}
    for t in history:
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
    for t in history:
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
    for t in history:
        running_pnl += t.get("pnl", 0)
        equity_curve.append({
            "timestamp": t.get("exit_time", t.get("recorded_at", 0)),
            "cumulative_pnl": round(running_pnl, 2),
        })

    # By hold duration
    by_hold = {"<5m": _bucket_init(), "5-30m": _bucket_init(), "30m-2h": _bucket_init(), "2-4h": _bucket_init(), ">4h": _bucket_init()}
    for t in history:
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
    recent = history[-50:]
    recent_wins = len([t for t in recent if t.get("pnl", 0) > 0])
    recent_pnl = sum(t.get("pnl", 0) for t in recent)
    recent_20 = history[-20:]
    recent_20_wins = len([t for t in recent_20 if t.get("pnl", 0) > 0])
    sharpe_ratio = _compute_sharpe(history)
    sharpe_ratio_recent_50 = _compute_sharpe(recent)
    overall_metrics = _finalize_metric_bucket(_build_metric_bucket(history))
    today_key = _current_day_key()
    today_trades = [t for t in history if _trade_day_key(t) == today_key]
    today_metrics = _finalize_metric_bucket(_build_metric_bucket(today_trades))

    return {
        "total_trades": len(history),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / max(1, len(history)), 4),
        "total_pnl": round(total_pnl, 2),
        "clean_pnl": round(clean_pnl, 2),
        "sharpe_ratio": round(sharpe_ratio, 4),
        "sharpe_ratio_recent_50": round(sharpe_ratio_recent_50, 4),
        "overall": {
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / max(1, len(history)) * 100, 1),
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
        "by_signal_source": dict(sorted(by_signal_source.items(), key=lambda x: x[1]["pnl"], reverse=True)),
        "by_asset_type": dict(sorted(by_asset_type.items(), key=lambda x: x[1]["pnl"], reverse=True)),
        "by_hold_duration": by_hold,
        "equity_curve": equity_curve[-500:],
        "recent_50": {
            "wins": recent_wins,
            "win_rate_pct": round(recent_wins / max(1, len(recent)) * 100, 1),
            "pnl": round(recent_pnl, 2),
            "sharpe_ratio": round(sharpe_ratio_recent_50, 4),
        },
        "recent_20": {
            "wins": recent_20_wins,
            "win_rate_pct": round(recent_20_wins / max(1, len(recent_20)) * 100, 1),
            "pnl": round(sum(t.get("pnl", 0) for t in recent_20), 2),
        },
        "today": {
            "date": today_key,
            "trades": today_metrics["trades"],
            "raw_pnl": today_metrics["pnl"],
            "clean_pnl": today_metrics["clean_pnl"],
            "anomaly_count": today_metrics["anomaly_count"],
        },
    }


def _bucket_init():
    return {"trades": 0, "wins": 0, "pnl": 0.0}


def _metric_bucket_init() -> Dict:
    return {
        "trades": 0,
        "wins": 0,
        "pnl": 0.0,
        "clean_pnl": 0.0,
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


def _update_metric_bucket(bucket: Dict, trade: Dict):
    pnl = float(trade.get("pnl", 0) or 0)
    bucket["trades"] += 1
    bucket["pnl"] += pnl
    if pnl > 0:
        bucket["wins"] += 1
    if _trade_has_anomaly(trade):
        bucket["anomaly_count"] += 1
    else:
        bucket["clean_pnl"] += pnl

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


def _finalize_metric_bucket(bucket: Dict) -> Dict:
    trades = int(bucket.get("trades", 0) or 0)
    wins = int(bucket.get("wins", 0) or 0)
    return {
        "trades": trades,
        "wins": wins,
        "pnl": round(float(bucket.get("pnl", 0.0) or 0.0), 2),
        "clean_pnl": round(float(bucket.get("clean_pnl", 0.0) or 0.0), 2),
        "win_rate": round(wins / max(1, trades) * 100, 1),
        "anomaly_count": int(bucket.get("anomaly_count", 0) or 0),
        "first_1m_green_rate_pct": _rate_pct(bucket.get("green_1m_hits", 0), bucket.get("green_1m_seen", 0)),
        "first_3m_green_rate_pct": _rate_pct(bucket.get("green_3m_hits", 0), bucket.get("green_3m_seen", 0)),
        "first_5m_green_rate_pct": _rate_pct(bucket.get("green_5m_hits", 0), bucket.get("green_5m_seen", 0)),
        "avg_mfe_pct": _avg_or_none(bucket.get("mfe_sum", 0.0), bucket.get("mfe_count", 0)),
        "avg_mae_pct": _avg_or_none(bucket.get("mae_sum", 0.0), bucket.get("mae_count", 0)),
        "avg_hold_seconds": _avg_or_none(bucket.get("hold_sum", 0.0), bucket.get("hold_count", 0)),
        "avg_slippage_bps": _avg_or_none(bucket.get("slippage_sum", 0.0), bucket.get("slippage_count", 0)),
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


def _current_day_key() -> str:
    try:
        import zoneinfo

        return datetime.now(zoneinfo.ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _trade_day_key(trade: Dict) -> str:
    ts = float(trade.get("exit_time", trade.get("recorded_at", 0)) or 0)
    if ts <= 0:
        return ""
    try:
        import zoneinfo

        return datetime.fromtimestamp(ts, zoneinfo.ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


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
