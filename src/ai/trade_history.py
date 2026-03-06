"""
Persistent trade history — survives restarts, feeds game film analysis.
File: data/trade_history.json
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

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
        HISTORY_FILE.write_text(json.dumps(history))
    except Exception as e:
        logger.warning(f"Failed to save trade history: {e}")


def load_all() -> List[Dict]:
    """Load all trade history from disk."""
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text())
        # Support both formats: raw list or {"trades": [...]}
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
        return {"total_trades": 0, "message": "No trade history yet."}

    wins = [t for t in history if t.get("pnl", 0) > 0]
    losses = [t for t in history if t.get("pnl", 0) <= 0]
    total_pnl = sum(t.get("pnl", 0) for t in history)

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
            by_strategy[strategy] = {"trades": 0, "wins": 0, "pnl": 0.0}
        by_strategy[strategy]["trades"] += 1
        by_strategy[strategy]["pnl"] += t.get("pnl", 0)
        if t.get("pnl", 0) > 0:
            by_strategy[strategy]["wins"] += 1
    for v in by_strategy.values():
        v["win_rate"] = round(v["wins"] / max(1, v["trades"]) * 100, 1)
        v["pnl"] = round(v["pnl"], 2)

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

    return {
        "total_trades": len(history),
        "overall": {
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": round(len(wins) / max(1, len(history)) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(sum(t.get("pnl", 0) for t in wins) / max(1, len(wins)), 2),
            "avg_loss": round(sum(t.get("pnl", 0) for t in losses) / max(1, len(losses)), 2),
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
        },
    }


def _bucket_init():
    return {"trades": 0, "wins": 0, "pnl": 0.0}
