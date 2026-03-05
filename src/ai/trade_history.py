"""
Persistent trade history — survives restarts, feeds game film analysis.
File: data/trade_history.json
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

DATA_DIR = Path(__file__).parent.parent.parent / "data"
HISTORY_FILE = DATA_DIR / "trade_history.json"
MAX_TRADES = 5000


def record_trade(trade: Dict):
    """Append a completed trade to persistent history."""
    DATA_DIR.mkdir(exist_ok=True)
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
            return data.get("trades", [])
        return data if isinstance(data, list) else []
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
        "by_hold_duration": by_hold,
        "recent_50": {
            "wins": recent_wins,
            "win_rate_pct": round(recent_wins / max(1, len(recent)) * 100, 1),
            "pnl": round(recent_pnl, 2),
        },
    }


def _bucket_init():
    return {"trades": 0, "wins": 0, "pnl": 0.0}
