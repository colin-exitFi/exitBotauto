"""
Dashboard - FastAPI backend + dark-theme HTML dashboard on port 8421.
Real-time positions, P&L, scanner, trades, controls.
"""

import os
import json
import time
import threading
import hmac
import socket
import re
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
from loguru import logger

from config import settings
from src import persistence
from src.agents.base_agent import (
    call_claude_text,
    call_gpt_text,
    call_perplexity_text,
    get_api_cost_stats,
)
from src.data import entry_controls, strategy_controls
try:
    from src.data import governance_registry
except ImportError:
    governance_registry = None
try:
    from src.ai import committee_memo
except ImportError:
    committee_memo = None
from src.data.pending_setups import list_pending_setups
from src.data.trade_schema import normalize_position_context
from src.data.strategy_tags import PRIMARY_BOOKS, is_artifact_strategy_tag, normalize_strategy_tag

app = FastAPI(title="Velox", version="2.0.0")

# Global reference to bot instance (set by main.py)
_bot = None
_dashboard_thread = None
_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_RUNNERS_FILE = _DATA_DIR / "yesterdays_runners.json"
_WATCHLIST_FILE = _DATA_DIR / "watchlist.json"
_SHADOW_TRADES_FILE = _DATA_DIR / "shadow_trades.json"
_CHAT_HISTORY_LIMIT = 8
_CHAT_ACTIVITY_LIMIT = 15
_PANEL_STARTING_EQUITY = 27500.0
_ALPACA_TERMINAL_CACHE_TTL = 5.0
_EQUITY_CURVE_PRESETS = {
    "1D": {"period": "1D", "timeframe": "5Min"},
    "1W": {"period": "1W", "timeframe": "15Min"},
    "1M": {"period": "1M", "timeframe": "1D"},
}
_VALID_EQUITY_TIMEFRAMES = {"1Min", "5Min", "15Min", "1H", "1D"}
_CHAT_STOPWORDS = {
    "A", "AN", "AND", "ARE", "AS", "AT", "BE", "BUT", "BY", "DO", "FOR", "FROM",
    "FRIDAY", "HOURS", "I", "IF", "IN", "IS", "IT", "ITS", "LOOK", "ME", "MONDAY",
    "MY", "NOT", "NOW", "OF", "ON", "OR", "OUT", "SAW", "SO", "STOCK", "TELL",
    "THAT", "THE", "THIS", "TO", "UP", "WAS", "WHAT", "WITH", "YOU",
}

# Activity feed — circular buffer of bot thoughts/actions
_activity_feed: List[Dict] = []
_MAX_FEED_SIZE = 100
_alpaca_terminal_cache = {"updated_at": 0.0, "payload": None}


def set_bot(bot):
    global _bot, _alpaca_terminal_cache
    _bot = bot
    _alpaca_terminal_cache = {"updated_at": 0.0, "payload": None}


def _get_reconciliation_state() -> Dict:
    """
    Read the latest persisted reconciliation snapshot.
    Important: avoid calling reconciler.snapshot() in dashboard request handlers,
    which can amplify broker API load and create self-inflicted recon churn.
    """
    try:
        if (
            _bot
            and getattr(_bot, "reconciler", None)
            and not getattr(_bot, "running", False)
            and hasattr(_bot.reconciler, "snapshot")
        ):
            try:
                fallback = _bot.reconciler.snapshot() or {}
                if isinstance(fallback, dict) and fallback:
                    return fallback
            except Exception:
                pass
        state = persistence.load_reconciliation_state() or {}
        if isinstance(state, dict) and state:
            return state
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _get_cached_alpaca_terminal_snapshot() -> Dict:
    global _alpaca_terminal_cache
    cached = _alpaca_terminal_cache.get("payload")
    updated_at = float(_alpaca_terminal_cache.get("updated_at", 0.0) or 0.0)
    now_ts = time.time()
    if cached and (now_ts - updated_at) <= _ALPACA_TERMINAL_CACHE_TTL:
        return dict(cached)
    if not _bot or not getattr(_bot, "alpaca_client", None):
        return dict(cached or {})
    try:
        account = _bot.alpaca_client.get_account()
        positions = _bot.alpaca_client.get_positions()
        equity = float(account.get("equity", _PANEL_STARTING_EQUITY) or _PANEL_STARTING_EQUITY)
        last_equity = float(account.get("last_equity", equity) or equity)
        cash = float(account.get("cash", 0) or 0)
        buying_power = float(account.get("buying_power", 0) or 0)
        unrealized = round(
            sum(
                float(
                    row.get(
                        "unrealized_pl",
                        row.get("unrealized_pnl", row.get("open_pnl", 0)),
                    )
                    or 0
                )
                for row in positions
            ),
            2,
        )
        prior_peak = 0.0
        if _bot and hasattr(_bot, "pnl_state"):
            try:
                prior_peak = float((_bot.pnl_state or {}).get("peak_equity", 0) or 0)
            except Exception:
                prior_peak = 0.0
        if cached:
            try:
                prior_peak = max(prior_peak, float(cached.get("peak_equity", 0) or 0))
            except Exception:
                pass
        peak_equity = max(_PANEL_STARTING_EQUITY, prior_peak, equity)
        if _bot and hasattr(_bot, "pnl_state"):
            try:
                _bot.pnl_state["peak_equity"] = peak_equity
            except Exception:
                pass
        payload = {
            "equity": round(equity, 2),
            "last_equity": round(last_equity, 2),
            "cash": round(cash, 2),
            "buying_power": round(buying_power, 2),
            "unrealized": round(unrealized, 2),
            "day_pnl": round(equity - last_equity, 2),
            "day_pnl_pct": round(((equity - last_equity) / last_equity * 100.0), 2) if last_equity else 0.0,
            "peak_equity": round(peak_equity, 2),
            "updated_at": now_ts,
        }
        _alpaca_terminal_cache = {"updated_at": now_ts, "payload": payload}
        return dict(payload)
    except Exception as e:
        logger.debug(f"Alpaca terminal snapshot unavailable: {e}")
        return dict(cached or {})


def _get_trade_analytics_scoreboard() -> Dict:
    try:
        from src.ai import trade_history

        analytics = trade_history.get_analytics() or {}
        total_trades = int(analytics.get("clean_total_trades", analytics.get("total_trades", 0)) or 0)
        clean_wins = int(analytics.get("clean_wins", analytics.get("wins", 0)) or 0)
        clean_losses = int(analytics.get("clean_losses", analytics.get("losses", 0)) or 0)
        win_rate_pct = round((clean_wins / total_trades * 100.0), 2) if total_trades > 0 else 0.0
        return {
            "total_trades": total_trades,
            "winning_trades": clean_wins,
            "losing_trades": clean_losses,
            "win_rate_pct": win_rate_pct,
        }
    except Exception:
        return {
            "total_trades": 0,
            "winning_trades": 0,
            "losing_trades": 0,
            "win_rate_pct": 0.0,
        }


def _apply_restart_safe_runtime_metrics(payload: Dict, reconciliation_state: Optional[Dict] = None) -> Dict:
    enriched = dict(payload or {})
    state = reconciliation_state if isinstance(reconciliation_state, dict) else _get_reconciliation_state()
    broker = state.get("broker", {}) if isinstance(state, dict) else {}
    internal = state.get("internal", {}) if isinstance(state, dict) else {}
    reconciliation = state.get("reconciliation", {}) if isinstance(state, dict) else {}
    snap = _get_cached_alpaca_terminal_snapshot()

    equity = snap.get("equity", broker.get("equity", enriched.get("equity", 0)))
    if equity is not None:
        enriched["equity"] = round(float(equity or 0), 2)
    last_equity = snap.get("last_equity", broker.get("last_equity"))
    if last_equity is not None:
        enriched["last_equity"] = round(float(last_equity or 0), 2)

    daily_pnl = snap.get("day_pnl", broker.get("day_pnl", enriched.get("daily_pnl", 0)))
    enriched["daily_pnl"] = round(float(daily_pnl or 0), 2)
    daily_pnl_pct = snap.get("day_pnl_pct", broker.get("day_pnl_pct", enriched.get("daily_pnl_pct", 0)))
    enriched["daily_pnl_pct"] = round(float(daily_pnl_pct or 0), 2)

    scoreboard = _get_trade_analytics_scoreboard()
    if scoreboard["total_trades"] > 0:
        enriched["total_trades"] = scoreboard["total_trades"]
        enriched["winning_trades"] = scoreboard["winning_trades"]
        enriched["losing_trades"] = scoreboard["losing_trades"]
        enriched["win_rate"] = round(scoreboard["win_rate_pct"], 2)
        enriched["win_rate_pct"] = round(scoreboard["win_rate_pct"], 2)

    if "canonical_realized_pnl" in reconciliation:
        enriched["today_realized"] = round(float(reconciliation.get("canonical_realized_pnl", 0) or 0), 2)

    if isinstance(internal, dict) and internal:
        enriched["today_trade_count"] = int(internal.get("trade_history_trade_count", 0) or 0)
        enriched["today_win_rate_pct"] = round(float(internal.get("trade_history_win_rate_pct", 0) or 0), 2)
        enriched["today_realized"] = round(float(enriched.get("today_realized", internal.get("trade_history_realized", enriched.get("daily_pnl", 0))) or 0), 2)

    return enriched


def _dashboard_connect_host(host: str) -> str:
    host = str(host or "").strip()
    if host in {"", "0.0.0.0", "::"}:
        return "127.0.0.1"
    return host


def _dashboard_port_in_use(host: str, port: int) -> bool:
    try:
        with socket.create_connection((_dashboard_connect_host(host), int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def log_activity(category: str, message: str, data: dict = None):
    """Log an activity to the dashboard feed. Categories: thinking, scan, trade, ai, alert, research."""
    global _activity_feed
    entry = {
        "timestamp": time.time(),
        "time_str": time.strftime("%H:%M:%S"),
        "category": category,
        "message": message,
        "data": data or {},
    }
    _activity_feed.append(entry)
    if len(_activity_feed) > _MAX_FEED_SIZE:
        _activity_feed = _activity_feed[-_MAX_FEED_SIZE:]


def _extract_query_symbols(text: str) -> List[str]:
    known_symbols = set()
    if _bot and getattr(_bot, "scanner", None):
        known_symbols.update(
            str(row.get("symbol", "")).upper()
            for row in (_bot.scanner.get_cached_candidates() or [])
            if str(row.get("symbol", "")).strip()
        )
    if _bot and getattr(_bot, "watchlist", None):
        known_symbols.update(
            str(row.get("ticker", "")).upper()
            for row in (_bot.watchlist.get_all() or [])
            if str(row.get("ticker", "")).strip()
        )
    if _bot and getattr(_bot, "entry_manager", None):
        known_symbols.update(
            str(row.get("symbol", "")).upper()
            for row in (_bot.entry_manager.get_positions() or [])
            if str(row.get("symbol", "")).strip()
        )
    if _bot and getattr(_bot, "human_intel_store", None):
        known_symbols.update(
            str(row.get("ticker", "")).upper()
            for row in (_bot.human_intel_store.list_entries(limit=100) or [])
            if str(row.get("ticker", "")).strip()
        )

    symbols: List[str] = []
    for raw in re.findall(r"\$?[A-Za-z]{1,5}\b", str(text or "")):
        token = raw.lstrip("$").upper()
        if not token or token in _CHAT_STOPWORDS:
            continue
        if raw.startswith("$") or raw.isupper() or token in known_symbols:
            if token not in symbols:
                symbols.append(token)
    return symbols[:8]


def _recent_log_highlights(limit: int = 12) -> List[str]:
    if not _LOG_DIR.exists():
        return []

    highlights: List[str] = []
    seen = set()
    keywords = ("chg=", "BREAKOUT", "after-hours", "after hours", "runner", "FDA", "PDUFA", "WHALE")
    paths = sorted(_LOG_DIR.glob("bot*.log"), key=lambda p: p.stat().st_mtime, reverse=True)[:2]
    for path in paths:
        try:
            lines = deque(path.read_text(errors="ignore").splitlines(), maxlen=500)
        except Exception:
            continue
        for line in reversed(lines):
            if not any(keyword.lower() in line.lower() for keyword in keywords):
                continue
            cleaned = re.sub(r"^\d{4}-\d{2}-\d{2}.*?\|\s*", "", line).strip()
            cleaned = re.sub(r"\s+", " ", cleaned)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            highlights.append(cleaned[:220])
            if len(highlights) >= limit:
                return highlights
    return highlights


def _load_json_artifact(path: Path) -> Dict:
    try:
        raw = json.loads(path.read_text()) if path.exists() else {}
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _load_shadow_trades() -> List[Dict]:
    data = persistence.safe_load_json(_SHADOW_TRADES_FILE, default=list)
    if isinstance(data, list):
        return data
    return []


def _position_unrealized_pnl(position: Dict) -> float:
    entry_price = float(position.get("entry_price", 0) or 0)
    current_price = float(position.get("current_price", entry_price) or entry_price)
    quantity = float(position.get("quantity", 0) or 0)
    side = str(position.get("side", "long") or "long").lower()
    if entry_price <= 0 or quantity <= 0:
        return 0.0
    if side == "short":
        return round((entry_price - current_price) * quantity, 2)
    return round((current_price - entry_price) * quantity, 2)


def _book_live_position_context() -> Dict[str, Dict]:
    positions = _bot.entry_manager.get_positions() if _bot and getattr(_bot, "entry_manager", None) else []
    books: Dict[str, Dict] = {}
    for position in positions or []:
        strategy_tag = normalize_strategy_tag(position.get("strategy_tag", "unknown"), fallback="unknown")
        if is_artifact_strategy_tag(strategy_tag):
            continue
        row = books.setdefault(
            strategy_tag,
            {"open_position_count": 0, "unrealized_pnl": 0.0},
        )
        row["open_position_count"] += 1
        row["unrealized_pnl"] = round(
            float(row.get("unrealized_pnl", 0.0) or 0.0) + _position_unrealized_pnl(position),
            2,
        )
    return books


def _book_shadow_context() -> Dict[str, Dict]:
    books: Dict[str, Dict] = {}
    for shadow_trade in _load_shadow_trades():
        strategy_tag = normalize_strategy_tag(shadow_trade.get("strategy_tag", "unknown"), fallback="unknown")
        if is_artifact_strategy_tag(strategy_tag):
            continue
        row = books.setdefault(strategy_tag, {"shadow_count": 0})
        row["shadow_count"] += 1
    return books


def _build_book_scoreboard_rows() -> List[Dict]:
    from src.ai import trade_history

    analytics = trade_history.get_analytics()
    strategy_stats = dict((analytics.get("by_strategy_tag", {}) or {}))
    report_books = {
        str(row.get("strategy_tag", "") or ""): dict(row)
        for row in ((analytics.get("book_report", {}) or {}).get("books", []) or [])
        if isinstance(row, dict)
    }
    runtime_books = {}
    if _bot and getattr(_bot, "book_scoreboard", None):
        try:
            runtime_books = {
                str(row.get("strategy_tag", "") or ""): dict(row)
                for row in ((_bot.book_scoreboard.get_summary() or {}).get("books", []) or [])
                if isinstance(row, dict)
            }
        except Exception:
            runtime_books = {}
    live_context = _book_live_position_context()
    shadow_context = _book_shadow_context()
    books: Dict[str, Dict] = {}

    def _ensure_row(strategy_tag: str) -> Dict:
        normalized = normalize_strategy_tag(strategy_tag, fallback="unknown")
        return books.setdefault(
            normalized,
            {
                "strategy_tag": normalized,
                "realized_pnl": 0.0,
                "unrealized_pnl": 0.0,
                "open_position_count": 0,
                "trade_count": 0,
                "shadow_count": 0,
                "win_rate_pct": 0.0,
                "avg_win": None,
                "avg_loss": None,
                "expectancy": 0.0,
                "ratchet_activation_rate_pct": None,
                "profit_factor": None,
                "max_drawdown": 0.0,
                "sharpe_ratio": 0.0,
                "status": "hold",
                "recommended_action": "observe",
                "control_state": "active",
                "status_reason": "",
            },
        )

    for strategy_tag in PRIMARY_BOOKS:
        _ensure_row(strategy_tag)

    for strategy_tag, bucket in strategy_stats.items():
        if is_artifact_strategy_tag(strategy_tag):
            continue
        row = _ensure_row(strategy_tag)
        row["realized_pnl"] = round(float(bucket.get("pnl", 0) or 0), 2)
        row["trade_count"] = int(bucket.get("trades", 0) or 0)
        row["win_rate_pct"] = round(float(bucket.get("win_rate_pct", bucket.get("win_rate", 0)) or 0), 1)
        row["avg_win"] = bucket.get("avg_win")
        row["avg_loss"] = bucket.get("avg_loss")
        row["expectancy"] = round(float(bucket.get("expectancy", 0) or 0), 2)
        row["ratchet_activation_rate_pct"] = bucket.get("ratchet_activation_rate_pct")
        report_row = report_books.get(strategy_tag, {})
        row["profit_factor"] = report_row.get("profit_factor")
        row["max_drawdown"] = report_row.get("max_drawdown", 0.0)
        row["sharpe_ratio"] = report_row.get("sharpe_ratio", 0.0)
        row["status"] = report_row.get("status", row.get("status", "hold"))
        row["recommended_action"] = report_row.get("recommended_action", row.get("recommended_action", "observe"))
        row["control_state"] = report_row.get("control_state", row.get("control_state", "active"))
        row["status_reason"] = report_row.get("status_reason", row.get("status_reason", ""))
        runtime_row = runtime_books.get(strategy_tag, {})
        if runtime_row:
            row["realized_pnl"] = round(float(runtime_row.get("realized_pnl", row.get("realized_pnl", 0)) or 0), 2)
            row["trade_count"] = int(runtime_row.get("trade_count", row.get("trade_count", 0)) or 0)
            row["expectancy"] = round(
                float(runtime_row.get("expectancy_per_trade", row.get("expectancy", 0)) or 0),
                2,
            )
            row["open_position_count"] = int(
                runtime_row.get("open_position_count", row.get("open_position_count", 0)) or 0
            )
            row["unrealized_pnl"] = round(
                float(runtime_row.get("unrealized_pnl", row.get("unrealized_pnl", 0)) or 0),
                2,
            )

    for strategy_tag, live_row in live_context.items():
        row = _ensure_row(strategy_tag)
        if strategy_tag not in runtime_books:
            row["open_position_count"] = int(live_row.get("open_position_count", 0) or 0)
            row["unrealized_pnl"] = round(float(live_row.get("unrealized_pnl", 0) or 0), 2)

    for strategy_tag, shadow_row in shadow_context.items():
        row = _ensure_row(strategy_tag)
        row["shadow_count"] = int(shadow_row.get("shadow_count", 0) or 0)

    primary_order = {tag: idx for idx, tag in enumerate(PRIMARY_BOOKS)}
    rows = list(books.values())
    rows.sort(
        key=lambda row: (
            primary_order.get(row["strategy_tag"], len(PRIMARY_BOOKS)),
            -float(row.get("realized_pnl", 0) or 0),
            row["strategy_tag"],
        )
    )
    return rows


def _build_book_report_rows() -> List[Dict]:
    from src.ai import trade_history

    analytics = trade_history.get_analytics()
    report = dict(analytics.get("book_report", {}) or {})
    report_rows = report.get("books", []) or []
    live_context = _book_live_position_context()
    books: Dict[str, Dict] = {}

    def _ensure_row(strategy_tag: str) -> Dict:
        normalized = normalize_strategy_tag(strategy_tag, fallback="unknown")
        return books.setdefault(
            normalized,
            {
                "strategy_tag": normalized,
                "trade_count": 0,
                "trades": 0,
                "pnl": 0.0,
                "net_pnl": 0.0,
                "open_position_count": 0,
                "unrealized_pnl": 0.0,
                "status": "hold",
                "recommended_action": "observe",
                "control_state": "active",
                "status_reason": "",
                "regimes": {},
                "sessions": {},
            },
        )

    for strategy_tag in PRIMARY_BOOKS:
        _ensure_row(strategy_tag)

    for raw_row in report_rows:
        if not isinstance(raw_row, dict):
            continue
        strategy_tag = raw_row.get("strategy_tag", "unknown")
        if is_artifact_strategy_tag(strategy_tag):
            continue
        row = _ensure_row(strategy_tag)
        row.update(raw_row)
        row["trade_count"] = int(raw_row.get("trade_count", raw_row.get("trades", 0)) or 0)
        row["open_position_count"] = int(row.get("open_position_count", 0) or 0)
        row["unrealized_pnl"] = round(float(row.get("unrealized_pnl", 0.0) or 0.0), 2)

    for strategy_tag, live_row in live_context.items():
        row = _ensure_row(strategy_tag)
        row["open_position_count"] = int(live_row.get("open_position_count", 0) or 0)
        row["unrealized_pnl"] = round(float(live_row.get("unrealized_pnl", 0) or 0), 2)

    primary_order = {tag: idx for idx, tag in enumerate(PRIMARY_BOOKS)}
    rows = list(books.values())
    rows.sort(
        key=lambda row: (
            {"scale": 0, "hold": 1, "probation": 2, "disable": 3}.get(str(row.get("status", "hold")), 9),
            primary_order.get(row["strategy_tag"], len(PRIMARY_BOOKS)),
            -float(row.get("pnl", 0) or 0),
            row["strategy_tag"],
        )
    )
    return rows


def _extract_query_dates(text: str) -> List[str]:
    dates: List[str] = []
    year = time.localtime().tm_year
    for month, day in re.findall(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b", str(text or "")):
        try:
            dates.append(f"{year:04d}-{int(month):02d}-{int(day):02d}")
        except Exception:
            continue
    return dates[:4]


def _persisted_runners_context(message: str, symbols: List[str], limit: int = 8) -> List[Dict]:
    data = _load_json_artifact(_RUNNERS_FILE)
    rows = data.get("runners", [])
    if not isinstance(rows, list):
        return []

    query_dates = set(_extract_query_dates(message))
    filtered = []
    for row in rows:
        symbol = str(row.get("symbol", "")).upper()
        if symbols and symbol not in symbols:
            continue
        if query_dates and str(row.get("date", "")) not in query_dates:
            continue
        filtered.append(
            {
                "symbol": symbol,
                "date": str(row.get("date", "") or ""),
                "change_pct": round(float(row.get("change_pct", 0.0) or 0.0), 2),
                "close_price": round(float(row.get("close_price", 0.0) or 0.0), 2),
                "volume_spike": round(float(row.get("volume_spike", 0.0) or 0.0), 2),
            }
        )

    if not filtered:
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if symbols and symbol not in symbols:
                continue
            filtered.append(
                {
                    "symbol": symbol,
                    "date": str(row.get("date", "") or ""),
                    "change_pct": round(float(row.get("change_pct", 0.0) or 0.0), 2),
                    "close_price": round(float(row.get("close_price", 0.0) or 0.0), 2),
                    "volume_spike": round(float(row.get("volume_spike", 0.0) or 0.0), 2),
                }
            )

    filtered.sort(key=lambda row: (row.get("date", ""), abs(float(row.get("change_pct", 0.0) or 0.0))), reverse=True)
    return filtered[:limit]


def _persisted_watchlist_context(symbols: List[str], limit: int = 8) -> List[Dict]:
    data = _load_json_artifact(_WATCHLIST_FILE)
    rows = data.get("items", [])
    if not isinstance(rows, list):
        return []

    items = []
    for row in rows:
        ticker = str(row.get("ticker", "")).upper()
        if symbols and ticker not in symbols:
            continue
        items.append(
            {
                "ticker": ticker,
                "side": row.get("side", "long"),
                "conviction": round(float(row.get("conviction", 0.0) or 0.0), 2),
                "reason": str(row.get("reason", "") or "")[:160],
                "sources": row.get("sources", ""),
            }
        )
    return items[:limit]


def _build_copilot_context(message: str, history: List[Dict]) -> Dict:
    symbols = _extract_query_symbols(message)
    candidates = []
    if _bot and getattr(_bot, "scanner", None):
        rows = _bot.scanner.get_cached_candidates() or []
        for row in rows:
            symbol = str(row.get("symbol", "")).upper()
            if symbols and symbol not in symbols:
                continue
            candidates.append(
                {
                    "symbol": symbol,
                    "score": round(float(row.get("score", 0.0) or 0.0), 3),
                    "price": round(float(row.get("price", 0.0) or 0.0), 2),
                    "change_pct": round(float(row.get("change_pct", 0.0) or 0.0), 2),
                    "volume_spike": round(float(row.get("volume_spike", 0.0) or 0.0), 2),
                    "side": row.get("side", "long"),
                    "strategy_tag": row.get("strategy_tag", ""),
                    "uw_news_summary": row.get("uw_news_summary", ""),
                    "uw_chain_summary": row.get("uw_chain_summary", ""),
                    "human_intel": row.get("human_intel", ""),
                }
            )
            if len(candidates) >= 8:
                break
        if not candidates:
            candidates = [
                {
                    "symbol": str(row.get("symbol", "")).upper(),
                    "score": round(float(row.get("score", 0.0) or 0.0), 3),
                    "price": round(float(row.get("price", 0.0) or 0.0), 2),
                    "change_pct": round(float(row.get("change_pct", 0.0) or 0.0), 2),
                    "strategy_tag": row.get("strategy_tag", ""),
                }
                for row in rows[:8]
            ]

    watchlist = []
    if _bot and getattr(_bot, "watchlist", None):
        for row in (_bot.watchlist.get_all() or []):
            symbol = str(row.get("ticker", "")).upper()
            if symbols and symbol not in symbols:
                continue
            watchlist.append(
                {
                    "ticker": symbol,
                    "side": row.get("side", "long"),
                    "conviction": round(float(row.get("conviction", 0.0) or 0.0), 2),
                    "reason": str(row.get("reason", "") or "")[:160],
                    "sources": row.get("sources", ""),
                }
            )
            if len(watchlist) >= 8:
                break

    positions = []
    if _bot and getattr(_bot, "entry_manager", None):
        for row in (_bot.entry_manager.get_positions() or []):
            symbol = str(row.get("symbol", "")).upper()
            if symbols and symbol not in symbols:
                continue
            positions.append(
                {
                    "symbol": symbol,
                    "side": row.get("side", "long"),
                    "entry_price": round(float(row.get("entry_price", 0.0) or 0.0), 2),
                    "quantity": round(float(row.get("quantity", 0.0) or 0.0), 4),
                    "trail_pct": round(float(row.get("trail_pct", 0.0) or 0.0), 2),
                }
            )

    intel_entries = []
    if _bot and getattr(_bot, "human_intel_store", None):
        rows = _bot.human_intel_store.list_entries(limit=20) or []
        for row in rows:
            symbol = str(row.get("ticker", "")).upper()
            if symbols and symbol not in symbols:
                continue
            intel_entries.append(
                {
                    "ticker": symbol,
                    "bias": row.get("bias", "neutral"),
                    "confidence": round(float(row.get("confidence", 0.0) or 0.0), 2),
                    "title": str(row.get("title", "") or "")[:120],
                    "notes": str(row.get("notes", "") or "")[:180],
                    "source": row.get("source", ""),
                    "url": row.get("url", ""),
                }
            )
            if len(intel_entries) >= 8:
                break

    copy_trader = {}
    if _bot and getattr(_bot, "copy_trader_monitor", None):
        try:
            raw = _bot.copy_trader_monitor.get_dashboard_data() or {}
            copy_trader = {
                "signals": list(raw.get("signals") or [])[:5],
                "exits": list(raw.get("exits") or [])[:5],
                "traders": list(raw.get("traders") or [])[:5],
            }
        except Exception:
            copy_trader = {}

    recent_trades = []
    try:
        from src.ai import trade_history

        for row in (trade_history.get_recent(8) or []):
            symbol = str(row.get("symbol", "")).upper()
            if symbols and symbol not in symbols:
                continue
            recent_trades.append(
                {
                    "symbol": symbol,
                    "pnl": round(float(row.get("pnl", 0.0) or 0.0), 2),
                    "pnl_pct": round(float(row.get("pnl_pct", 0.0) or 0.0), 2),
                    "exit_reason": row.get("exit_reason", row.get("reason", "")),
                    "strategy_tag": row.get("strategy_tag", ""),
                }
            )
            if len(recent_trades) >= 8:
                break
    except Exception:
        recent_trades = []

    activity = [
        {
            "time": row.get("time_str", ""),
            "category": row.get("category", ""),
            "message": str(row.get("message", "") or "")[:180],
        }
        for row in _activity_feed[-_CHAT_ACTIVITY_LIMIT:]
    ]
    historical_runners = _persisted_runners_context(message, symbols)
    persisted_watchlist = _persisted_watchlist_context(symbols)

    return {
        "symbols_from_query": symbols,
        "market_regime": (
            _bot.scanner.get_last_market_regime()
            if _bot and getattr(_bot, "scanner", None)
            else "unknown"
        ),
        "candidates": candidates,
        "watchlist": watchlist,
        "positions": positions,
        "human_intel": intel_entries,
        "copy_trader": copy_trader,
        "recent_trades": recent_trades,
        "historical_runners": historical_runners,
        "persisted_watchlist": persisted_watchlist,
        "activity_feed": activity,
        "recent_log_highlights": _recent_log_highlights(),
        "chat_history": [
            {
                "role": str(row.get("role", "user") or "user"),
                "content": str(row.get("content", "") or "")[:400],
            }
            for row in (history or [])[-_CHAT_HISTORY_LIMIT:]
            if str(row.get("content", "") or "").strip()
        ],
    }


def _maybe_answer_from_local_context(message: str, context: Dict) -> Optional[Dict]:
    lower = str(message or "").lower()
    recall_query = any(
        phrase in lower
        for phrase in ("can't remember", "dont remember", "don't remember", "what was it", "what ticker", "which ticker", "which stock")
    )
    if not recall_query:
        return None

    runners = list(context.get("historical_runners") or [])
    if not runners:
        return None

    exact = [row for row in runners if row.get("date") in _extract_query_dates(message)]
    candidates = exact or runners
    if not candidates:
        return None

    best = max(candidates, key=lambda row: abs(float(row.get("change_pct", 0.0) or 0.0)))
    if abs(float(best.get("change_pct", 0.0) or 0.0)) < 100:
        return None

    answer = (
        f"The most likely ticker was {best['symbol']}. "
        f"I found it in `data/yesterdays_runners.json`: {best['symbol']} closed {best['change_pct']:+.2f}% on {best['date']} "
        f"at ${best['close_price']:.2f}."
    )
    return {
        "ok": True,
        "answer": answer,
        "provider": "local",
        "context_symbols": context.get("symbols_from_query", []),
    }


async def _generate_copilot_reply(message: str, history: List[Dict]) -> Dict:
    context = _build_copilot_context(message, history)
    local = _maybe_answer_from_local_context(message, context)
    if local:
        log_activity("research", f"💬 Copilot question answered from local context: {str(message or '')[:120]}")
        return local
    prompt = f"""You are Velox Operator Copilot.

Your job is to answer the operator's question using the bot's INTERNAL ENGINE CONTEXT first.
If you make an inference, say that clearly. If the context is insufficient, say what is missing.
If the operator is trying to remember a ticker, use recent scanner/watchlist/log clues plus persisted artifacts like `historical_runners` and `persisted_watchlist` to infer the most likely symbol.
If the operator mentions a ticker, rumor, article, FDA event, or after-hours move, explain what Velox currently knows and what matters next.
Do not output JSON. Write a direct operator-facing answer with short paragraphs or flat bullets.

INTERNAL ENGINE CONTEXT:
{json.dumps(context, indent=2)}

OPERATOR QUESTION:
{message}
"""

    provider = ""
    answer = await call_perplexity_text(prompt, max_tokens=900)
    if answer:
        provider = "perplexity"
    if not answer:
        answer = await call_claude_text(prompt, max_tokens=900)
        if answer:
            provider = "claude"
    if not answer:
        answer = await call_gpt_text(prompt, max_tokens=900)
        if answer:
            provider = "gpt"

    if not answer:
        return {
            "ok": False,
            "error": "No AI provider available for copilot chat",
            "context_symbols": context.get("symbols_from_query", []),
        }

    log_activity("research", f"💬 Copilot question: {str(message or '')[:120]}")
    return {
        "ok": True,
        "answer": answer.strip(),
        "provider": provider,
        "context_symbols": context.get("symbols_from_query", []),
    }


def _extract_dashboard_token(request: Request) -> str:
    """Extract dashboard token from header or query string."""
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip()
    return request.query_params.get("token", "").strip()


@app.middleware("http")
async def dashboard_auth_middleware(request: Request, call_next):
    """Protect dashboard HTML + API endpoints with bearer token auth."""
    path = request.url.path
    protected = (
        path == "/"
        or path.startswith("/api/")
        or path == "/docs"
        or path.startswith("/docs/")
        or path == "/redoc"
        or path.startswith("/redoc")
        or path == "/openapi.json"
    )
    if protected:
        expected = (getattr(settings, "DASHBOARD_TOKEN", "") or "").strip()
        if not expected:
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "Dashboard token not configured",
                        "hint": "Set DASHBOARD_TOKEN in .env",
                    },
                )
            return HTMLResponse(
                "<h1>Dashboard unavailable</h1><p>Set DASHBOARD_TOKEN in .env.</p>",
                status_code=503,
            )

        provided = _extract_dashboard_token(request)
        if not provided or not hmac.compare_digest(provided, expected):
            if path.startswith("/api/"):
                return JSONResponse(
                    status_code=401,
                    content={
                        "error": "Unauthorized",
                        "hint": "Provide Authorization: Bearer <token> or ?token=<token>",
                    },
                )
            return HTMLResponse(
                "<h1>Unauthorized</h1><p>Provide ?token=&lt;token&gt; or Authorization bearer token.</p>",
                status_code=401,
            )

    return await call_next(request)


# ── API Endpoints ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """Serve the main dashboard HTML."""
    return DASHBOARD_HTML


@app.get("/api/status")
async def get_status():
    if not _bot:
        return {"running": False, "error": "Bot not connected"}
    risk_status = _bot.risk_manager.get_status() if _bot.risk_manager else {}
    positions = _bot.entry_manager.get_positions() if _bot.entry_manager else []
    reconciliation_state = _get_reconciliation_state()
    trust = reconciliation_state.get("trust", {}) if isinstance(reconciliation_state, dict) else {}
    recon = reconciliation_state.get("reconciliation", {}) if isinstance(reconciliation_state, dict) else {}
    broker_api = reconciliation_state.get("broker_api", {}) if isinstance(reconciliation_state, dict) else {}
    recon_meta = reconciliation_state.get("meta", {}) if isinstance(reconciliation_state, dict) else {}
    ai = getattr(_bot, "ai_layers", {}) or {}
    options_engine_ready = bool(getattr(_bot, "options_engine", None))
    options_enabled = bool(getattr(settings, "OPTIONS_ENABLED", False))
    options_entry_enabled = bool(
        options_enabled
        and options_engine_ready
        and bool(getattr(settings, "OPTIONS_PILOT_ENABLED", False))
    )
    payload = {
        "running": _bot.running,
        "paused": _bot.paused,
        "market_open": _bot.entry_manager.is_market_open() if _bot.entry_manager else False,
        "positions_count": len(positions),
        "uptime_seconds": int(time.time() - _bot.start_time) if hasattr(_bot, 'start_time') else 0,
        "options_enabled": options_enabled,
        "options_execution_enabled": options_engine_ready,
        "options_entry_enabled": options_entry_enabled,
        "options_pilot_enabled": bool(getattr(settings, "OPTIONS_PILOT_ENABLED", False)),
        "reconciliation_status": recon.get("status", "unknown"),
        "trust_flags": trust,
        "recon_health": {
            "recent_429_total": int(broker_api.get("recent_429_total", 0) or 0),
            "window_seconds": int(broker_api.get("window_seconds", 300) or 300),
            "consecutive_critical_mismatch": int(recon_meta.get("consecutive_critical_mismatch", 0) or 0),
            "entry_pipeline_paused": bool(trust.get("entry_pipeline_paused")),
            "degraded_mode_reasons": list(trust.get("degraded_mode_reasons", []) or []),
        },
        "provider_health": ai.get("provider_health", {}),
        **risk_status,
    }
    return _apply_restart_safe_runtime_metrics(payload, reconciliation_state=reconciliation_state)


@app.get("/api/recon-health")
async def get_recon_health():
    if not _bot or not getattr(_bot, "reconciler", None):
        return {"ok": False, "error": "Reconciler not available"}
    state = _get_reconciliation_state()
    if not state:
        return {"ok": False, "error": "Reconciliation state unavailable"}
    recon = state.get("reconciliation", {}) if isinstance(state, dict) else {}
    trust = state.get("trust", {}) if isinstance(state, dict) else {}
    broker_api = state.get("broker_api", {}) if isinstance(state, dict) else {}
    meta = state.get("meta", {}) if isinstance(state, dict) else {}
    return {
        "ok": True,
        "as_of": state.get("as_of"),
        "date": state.get("date"),
        "reconciliation": recon,
        "trust_flags": trust,
        "broker_api": broker_api,
        "meta": meta,
        "entry_pipeline_paused": bool(trust.get("entry_pipeline_paused")),
        "degraded_mode_reasons": list(trust.get("degraded_mode_reasons", []) or []),
    }


@app.get("/api/positions")
async def get_positions():
    if not _bot or not _bot.entry_manager:
        return []
    positions = _bot.entry_manager.get_positions()
    enriched = []
    for p in positions:
        p = normalize_position_context(p)
        price = 0
        # Use Alpaca as source of truth for current price (matches portfolio view)
        if _bot.alpaca_client:
            try:
                price = _bot.alpaca_client.get_latest_price(p["symbol"])
            except:
                pass
        # Fallback to Polygon, then entry price
        if not price and _bot.polygon_client:
            try:
                price = _bot.polygon_client.get_price(p["symbol"])
            except:
                pass
        if not price:
            price = p.get("entry_price", 0)
        entry_price = float(p.get("entry_price", 0) or 0)
        quantity = float(p.get("quantity", 0) or 0)
        if p.get("side", "long") == "short":
            pnl = (entry_price - price) * quantity if entry_price else 0
            pnl_pct = ((entry_price - price) / entry_price * 100) if entry_price else 0
        else:
            pnl = (price - entry_price) * quantity if entry_price else 0
            pnl_pct = ((price - entry_price) / entry_price * 100) if entry_price else 0
        hold_min = (time.time() - p.get("entry_time", time.time())) / 60
        order_state = dict(p.get("order_state", {}) or {})
        protection_bits = []
        if p.get("hard_stop_order_id"):
            protection_bits.append("hard stop")
        if p.get("ratchet_limit_order_id"):
            protection_bits.append("ratchet order")
        elif p.get("ratchet_floor_pct") is not None:
            protection_bits.append("ratchet armed")
        session_mode = order_state.get("session_protection")
        if session_mode == "software_managed":
            protection_bits.append("software")
        protection = " / ".join(protection_bits) if protection_bits else (session_mode or "none")

        enriched.append({
            "symbol": p["symbol"],
            "side": p.get("side", "long"),
            "quantity": p["quantity"],
            "entry_price": round(p["entry_price"], 2),
            "current_price": round(price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "hold_time": f"{int(hold_min)}m",
            "peak_price": round(p.get("peak_price", price), 2),
            "protection": protection,
            "trail_pct": p.get("trail_pct", 3.0),
            "strategy_tag": p.get("strategy_tag", "unknown"),
            "signal_tier": p.get("signal_tier", "tier_2"),
            "setup_mode": p.get("setup_mode", "invalid"),
            "best_play": p.get("best_play", ""),
            "timing_state": p.get("timing_state", "enter_now"),
            "hold_style": p.get("hold_style", p.get("holding_horizon", "")),
            "ratchet_floor_pct": p.get("ratchet_floor_pct"),
            "order_status": p.get("order_status", "open"),
        })
    trust = (_get_reconciliation_state() or {}).get("trust", {})
    return {"positions": enriched, "trust_flags": trust}


@app.get("/api/options")
async def get_options():
    """Get active options positions and premium-level metrics."""
    if not _bot or not getattr(_bot, "options_engine", None):
        return []
    try:
        return _bot.options_engine.get_positions_snapshot(refresh_quotes=True)
    except Exception as e:
        logger.error(f"Options positions fetch error: {e}")
        return []


@app.get("/api/portfolio")
async def get_portfolio():
    """Get brokerage portfolio (positions + balances from Alpaca)."""
    if not _bot or not _bot.alpaca_client:
        return {"positions": [], "cash": 0, "total_value": 0, "buying_power": 0}
    try:
        positions = _bot.alpaca_client.get_positions()
        account = _bot.alpaca_client.get_account()
        return {
            "positions": positions,
            "cash": round(float(account.get("cash", 0) or 0), 2),
            "buying_power": round(float(account.get("buying_power", 0) or 0), 2),
            "total_value": round(float(account.get("equity", 0) or 0), 2),
        }
    except Exception as e:
        logger.error(f"Portfolio fetch error: {e}")
        return {"positions": [], "cash": 0, "total_value": 0, "buying_power": 0, "error": str(e)}


@app.get("/api/trending")
async def get_trending():
    """Get StockTwits trending symbols."""
    if not _bot or not hasattr(_bot, 'stocktwits_client') or not _bot.stocktwits_client:
        return []
    try:
        return _bot.stocktwits_client.get_trending()
    except Exception as e:
        logger.error(f"Trending fetch error: {e}")
        return []


@app.get("/api/ai-status")
async def get_ai_status():
    """Get AI layer status: last observation, advice, tuner changes."""
    if not _bot or not hasattr(_bot, 'ai_layers') or not _bot.ai_layers:
        return {"enabled": False}
    ai = _bot.ai_layers
    return {
        "enabled": True,
        "last_observation": ai.get("last_observation"),
        "last_advice": ai.get("last_advice"),
        "last_tuner_changes": ai.get("last_tuner_changes"),
        "last_game_film": ai.get("last_game_film_summary"),
        "last_position_manager": ai.get("last_position_manager"),
        "overnight_bias_summary": ai.get("overnight_bias_summary"),
        "overnight_bias": ai.get("overnight_bias", {}),
        "short_verdicts_blocked": ai.get("short_verdicts_blocked", 0),
        "last_short_block_reason": ai.get("last_short_block_reason"),
        "provider_health": ai.get("provider_health", {}),
    }


@app.get("/api/consensus")
async def get_consensus():
    """Get agent orchestrator history and stats."""
    if not _bot or not hasattr(_bot, 'orchestrator') or not _bot.orchestrator:
        return {"enabled": False, "history": [], "stats": {}}
    ai = getattr(_bot, "ai_layers", {}) or {}
    trust = (_get_reconciliation_state() or {}).get("trust", {})
    return {
        "enabled": True,
        "history": _bot.orchestrator.get_history()[-10:],
        "stats": _bot.orchestrator.get_stats(),
        "last_consensus": ai.get("last_consensus"),
        "short_verdicts_blocked": ai.get("short_verdicts_blocked", 0),
        "last_short_block_reason": ai.get("last_short_block_reason"),
        "trust_flags": trust,
    }


@app.get("/api/candidates")
async def get_candidates():
    if not _bot or not _bot.scanner:
        return []
    return _bot.scanner.get_cached_candidates()


@app.get("/api/research-universe")
async def get_research_universe():
    if not _bot or not _bot.scanner:
        return []
    return _bot.scanner.get_research_universe()


@app.get("/api/scan-status")
async def get_scan_status():
    if not _bot or not _bot.scanner:
        return {}
    return _bot.scanner.get_last_scan_stats()


@app.get("/api/history")
async def get_history(limit: int = 20):
    # Pull from persistent trade history (includes trailing stop exits)
    try:
        from src.ai import trade_history
        trades = trade_history.get_recent(limit)
        # Format for dashboard
        result = []
        for t in trades:
            result.append({
                "symbol": t.get("symbol", "?"),
                "entry_price": t.get("entry_price", 0),
                "exit_price": t.get("exit_price", 0),
                "quantity": t.get("quantity", 0),
                "pnl": t.get("pnl", 0),
                "pnl_pct": t.get("pnl_pct", 0),
                "reason": t.get("exit_reason", t.get("reason", "trailing_stop")),
                "hold_time": f"{int(t.get('hold_seconds', 0) / 60)}m",
                "hold_seconds": t.get("hold_seconds", 0),
            })
        return result
    except Exception:
        # Fallback to exit manager
        if not _bot or not _bot.exit_manager:
            return []
        history = _bot.exit_manager.get_history(limit)
        for h in history:
            h["hold_time"] = f"{int(h.get('hold_seconds', 0) / 60)}m"
        return history


@app.get("/api/trade-history")
async def get_trade_history(limit: int = 20):
    """Get persistent trade history with analytics."""
    from src.ai import trade_history
    trades = trade_history.get_recent(limit)
    stats = trade_history.get_analytics()
    # Compute best/worst
    best = max(trades, key=lambda t: t.get("pnl", 0)) if trades else None
    worst = min(trades, key=lambda t: t.get("pnl", 0)) if trades else None
    trust = (_get_reconciliation_state() or {}).get("trust", {})
    broker_total_pnl = None
    try:
        snap = _get_cached_alpaca_terminal_snapshot()
        if snap:
            broker_total_pnl = round(float(snap.get("equity", 0) or 0) - _PANEL_STARTING_EQUITY, 2)
    except Exception:
        pass
    return {
        "trades": trades,
        "stats": stats,
        "best": best,
        "worst": worst,
        "trust_flags": trust,
        "broker_total_pnl": broker_total_pnl,
    }


@app.get("/api/shadow-trades")
async def get_shadow_trades(limit: int = 100):
    if _bot and hasattr(_bot, "refresh_shadow_trades"):
        try:
            await _bot.refresh_shadow_trades()
        except Exception as e:
            logger.debug(f"Shadow trade refresh unavailable: {e}")
    rows = _load_shadow_trades()
    rows = sorted(rows, key=lambda row: float(row.get("timestamp", 0) or 0), reverse=True)
    bounded_limit = max(1, min(int(limit or 100), 500))
    return {
        "count": len(rows),
        "trades": rows[:bounded_limit],
    }


@app.get("/api/book-scoreboard")
async def get_book_scoreboard():
    return {
        "books": _build_book_scoreboard_rows(),
        "generated_at": time.time(),
    }


@app.get("/api/book-report")
async def get_book_report():
    from src.ai import trade_history

    analytics = trade_history.get_analytics()
    report = dict(analytics.get("book_report", {}) or {})
    return {
        "summary": dict(report.get("summary", {}) or {}),
        "books": _build_book_report_rows(),
        "generated_at": time.time(),
    }


@app.get("/api/governance/committee")
async def get_governance_committee(include_docs: bool = False):
    if governance_registry is None:
        return {
            "available": False,
            "error": "governance_registry_unavailable",
            "generated_at": time.time(),
        }
    payload = governance_registry.get_governance_committee_summary(include_docs=bool(include_docs))
    payload["generated_at"] = time.time()
    return payload


@app.get("/api/governance/summary")
async def get_governance_summary():
    if committee_memo is None:
        return {
            "available": False,
            "error": "committee_memo_unavailable",
            "generated_at": time.time(),
        }
    payload = committee_memo.build_governance_summary()
    payload["generated_at"] = time.time()
    return payload


@app.get("/api/governance/weekly-memo")
async def get_governance_weekly_memo():
    if committee_memo is None:
        return {
            "available": False,
            "error": "committee_memo_unavailable",
            "generated_at": time.time(),
        }
    payload = committee_memo.build_weekly_committee_memo()
    payload["generated_at"] = time.time()
    return payload


@app.get("/api/pending-setups")
async def get_pending_setups(limit: int = 50):
    rows = list_pending_setups(limit=max(1, min(int(limit or 50), 200)))
    return {
        "count": len(rows),
        "setups": rows,
        "generated_at": time.time(),
    }


@app.get("/api/entry-controls")
async def get_entry_controls(limit: int = 50):
    entry_controls.prune_expired()
    bounded_limit = max(1, min(int(limit or 50), 200))
    return {
        "loss_locks": entry_controls.list_symbol_loss_locks(limit=bounded_limit),
        "trade_states": entry_controls.list_symbol_trade_states(limit=bounded_limit),
        "generated_at": time.time(),
    }


@app.get("/api/setup-replay")
async def get_setup_replay(symbol: Optional[str] = None, setup_id: Optional[str] = None, day: Optional[str] = None, limit: int = 250):
    from src.ai.setup_replay import build_setup_replay

    payload = build_setup_replay(
        symbol=symbol,
        setup_id=setup_id,
        day=day,
        limit=max(1, min(int(limit or 250), 1000)),
    )
    return payload


@app.get("/api/mode-report")
async def get_mode_report(day: Optional[str] = None):
    from src.ai import trade_history

    return trade_history.get_mode_confusion_report(day=day)


@app.get("/api/strategy-controls")
async def get_strategy_controls():
    """Get persisted strategy control state and effective disable list."""
    controls = strategy_controls.load_controls()
    return {
        "controls": controls,
        "effective_disabled": sorted(strategy_controls.get_effective_disabled(controls)),
    }


@app.post("/api/strategy/disable")
async def disable_strategy(tag: str, reason: str = ""):
    """Manually disable a strategy tag."""
    controls = strategy_controls.load_controls()
    controls = strategy_controls.manual_disable(tag, reason, controls)
    strategy_controls.save_controls(controls)
    return {
        "ok": True,
        "controls": controls,
        "effective_disabled": sorted(strategy_controls.get_effective_disabled(controls)),
    }


@app.post("/api/strategy/enable")
async def enable_strategy(tag: str, reason: str = ""):
    """Manually enable (override) a strategy tag."""
    controls = strategy_controls.load_controls()
    controls = strategy_controls.manual_enable(tag, reason, controls)
    strategy_controls.save_controls(controls)
    return {
        "ok": True,
        "controls": controls,
        "effective_disabled": sorted(strategy_controls.get_effective_disabled(controls)),
    }


def _resolve_equity_curve_request(period: Optional[str], timeframe: Optional[str]) -> Dict[str, str]:
    requested_period = str(period or "1D").upper()
    preset = dict(_EQUITY_CURVE_PRESETS.get(requested_period, _EQUITY_CURVE_PRESETS["1D"]))
    requested_timeframe = str(timeframe or "").strip()
    if requested_timeframe in _VALID_EQUITY_TIMEFRAMES:
        preset["timeframe"] = requested_timeframe
    preset["requested_period"] = requested_period
    return preset


@app.get("/api/equity-curve")
async def get_equity_curve(limit: int = 120, period: str = "1D", timeframe: Optional[str] = None):
    """Return broker-backed equity curve points, with internal fallback if unavailable."""
    if limit < 1:
        limit = 1
    curve_request = _resolve_equity_curve_request(period, timeframe)
    if _bot and getattr(_bot, "alpaca_client", None):
        try:
            history = _bot.alpaca_client.get_portfolio_history(
                period=curve_request["period"],
                timeframe=curve_request["timeframe"],
            ) or {}
            timestamps = list(history.get("timestamp") or []) if isinstance(history, dict) else []
            equities = list(history.get("equity") or []) if isinstance(history, dict) else []
            if timestamps and equities:
                count = min(len(timestamps), len(equities))
                series = [
                    {
                        "timestamp": timestamps[idx],
                        "cumulative_pnl": round(float(equities[idx]) - _PANEL_STARTING_EQUITY, 2),
                        "equity": round(float(equities[idx]), 2),
                    }
                    for idx in range(count)
                ]
                visible_points = series[-limit:]
                return {
                    "starting_equity": round(_PANEL_STARTING_EQUITY, 2),
                    "count": len(visible_points),
                    "total_count": count,
                    "points": visible_points,
                    "source": "alpaca",
                    "period": curve_request["period"],
                    "timeframe": curve_request["timeframe"],
                    "first_timestamp": visible_points[0]["timestamp"] if visible_points else None,
                    "last_timestamp": visible_points[-1]["timestamp"] if visible_points else None,
                }
        except Exception as e:
            logger.debug(f"Broker equity curve unavailable: {e}")

    from src.ai import trade_history

    stats = trade_history.get_analytics()
    curve = stats.get("equity_curve", [])
    points = curve[-limit:]
    starting = _PANEL_STARTING_EQUITY
    if _bot and getattr(_bot, "pnl_state", None):
        starting = _bot.pnl_state.get("starting_equity", _PANEL_STARTING_EQUITY)

    series = [
        {
            "timestamp": p.get("timestamp", 0),
            "cumulative_pnl": p.get("cumulative_pnl", 0),
            "equity": round(starting + p.get("cumulative_pnl", 0), 2),
        }
        for p in points
    ]
    return {
        "starting_equity": round(starting, 2),
        "count": len(series),
        "total_count": len(curve),
        "points": series,
        "source": "internal",
        "period": curve_request["period"],
        "timeframe": curve_request["timeframe"],
        "first_timestamp": series[0]["timestamp"] if series else None,
        "last_timestamp": series[-1]["timestamp"] if series else None,
    }


def _resolve_starting_equity(pnl: Dict, broker_truth: Dict) -> float:
    """Dashboard baseline is fixed to the funded v2 starting capital."""
    return float(_PANEL_STARTING_EQUITY)


@app.get("/api/pnl")
async def get_pnl():
    """Comprehensive P&L tracking — the Bloomberg terminal view."""
    if not _bot:
        return {}

    pnl = getattr(_bot, 'pnl_state', {})
    total_realized = pnl.get("total_realized_pnl", 0)
    options_realized = pnl.get("options_total_realized_pnl", 0)
    total_trades = pnl.get("total_trades", 0)
    wins = pnl.get("winning_trades", 0)
    losses = pnl.get("losing_trades", 0)
    best = pnl.get("best_trade", 0)
    worst = pnl.get("worst_trade", 0)

    default_equity = _resolve_starting_equity(pnl, {})
    equity = default_equity
    last_equity = equity
    cash = 0.0
    buying_power = 0.0
    broker_day_pnl = 0.0
    broker_day_pnl_pct = 0.0
    unrealized = 0
    options_unrealized = 0.0
    starting = default_equity
    peak = max(float(pnl.get("peak_equity", 0) or 0), default_equity)
    reconciliation_state = _get_reconciliation_state()
    broker_truth = reconciliation_state.get("broker", {}) or {}
    reconciliation = reconciliation_state.get("reconciliation", {}) or {}
    alpaca_snapshot = _get_cached_alpaca_terminal_snapshot()
    if alpaca_snapshot:
        equity = float(alpaca_snapshot.get("equity", equity) or equity)
        last_equity = float(alpaca_snapshot.get("last_equity", equity) or equity)
        cash = float(alpaca_snapshot.get("cash", 0) or 0)
        buying_power = float(alpaca_snapshot.get("buying_power", 0) or 0)
        broker_day_pnl = float(alpaca_snapshot.get("day_pnl", 0) or 0)
        broker_day_pnl_pct = float(alpaca_snapshot.get("day_pnl_pct", 0) or 0)
        unrealized = float(alpaca_snapshot.get("unrealized", 0) or 0)
        peak = max(peak, float(alpaca_snapshot.get("peak_equity", equity) or equity))
    elif broker_truth:
        equity = float(broker_truth.get("equity", equity) or equity)
        last_equity = float(broker_truth.get("last_equity", equity) or equity)
        cash = float(broker_truth.get("cash", 0) or 0)
        buying_power = float(broker_truth.get("buying_power", 0) or 0)
        broker_day_pnl = float(broker_truth.get("day_pnl", 0) or 0)
        broker_day_pnl_pct = float(broker_truth.get("day_pnl_pct", 0) or 0)
        unrealized = float(broker_truth.get("current_open_unrealized", 0) or 0)
    starting = _resolve_starting_equity(pnl, broker_truth)
    if equity > peak:
        peak = equity
        pnl["peak_equity"] = peak

    if _bot and getattr(_bot, "options_engine", None):
        try:
            opt_positions = _bot.options_engine.get_positions_snapshot(refresh_quotes=False)
            options_unrealized = sum(float(p.get("pnl", 0) or 0) for p in opt_positions)
        except Exception:
            options_unrealized = 0.0

    total_pnl = equity - starting
    drawdown = ((peak - equity) / peak * 100) if peak > 0 else 0
    roi = ((equity - starting) / starting * 100) if starting > 0 else 0
    analytics = {}
    avg_signal_to_fill_ms = None
    api_costs = {}
    internal_state = (reconciliation_state.get("internal", {}) or {})
    try:
        from src.ai import trade_history
        analytics = trade_history.get_analytics()
        total_trades = int(analytics.get("total_trades", total_trades) or total_trades)
        wins = int(analytics.get("wins", wins) or wins)
        losses = int(analytics.get("losses", losses) or losses)
        trade_rows = trade_history.load_all()
        if trade_rows:
            best = round(max(float(t.get("pnl", 0) or 0) for t in trade_rows), 2)
            worst = round(min(float(t.get("pnl", 0) or 0) for t in trade_rows), 2)
        avg_signal_to_fill_ms = (analytics.get("overall", {}) or {}).get("avg_signal_to_fill_ms")
    except Exception:
        avg_signal_to_fill_ms = None
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    try:
        api_costs = get_api_cost_stats()
    except Exception:
        api_costs = {}

    return {
        "equity": round(equity, 2),
        "starting_equity": round(starting, 2),
        "peak_equity": round(peak, 2),
        "cash": round(cash, 2),
        "buying_power": round(buying_power, 2),
        "portfolio_value": round(equity, 2),
        "total_pnl": round(total_pnl, 2),
        "total_realized": round(total_realized, 2),
        "unrealized": round(unrealized, 2),
        "last_equity": round(last_equity, 2),
        "broker_day_pnl": round(broker_day_pnl, 2),
        "broker_day_pnl_pct": round(broker_day_pnl_pct, 2),
        "options_realized_pnl": round(options_realized, 2),
        "options_unrealized_pnl": round(options_unrealized, 2),
        "internal_realized_pnl": round(float(internal_state.get("trade_history_realized", total_realized) or total_realized), 2),
        "internal_trade_count": int(total_trades),
        "internal_game_film_realized": round(float(internal_state.get("game_film_realized", 0) or 0), 2),
        "internal_win_rate_pct": round(float(win_rate or 0), 2),
        "today_realized": round(broker_day_pnl, 2),
        "total_trades": total_trades,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": round(win_rate, 1),
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "avg_signal_to_fill_ms": avg_signal_to_fill_ms,
        "clean_realized": round((analytics.get("today", {}) or {}).get("clean_pnl", 0.0), 2),
        "raw_realized_today": round((analytics.get("today", {}) or {}).get("raw_pnl", 0.0), 2),
        "today_anomaly_count": int((analytics.get("today", {}) or {}).get("anomaly_count", 0) or 0),
        "api_cost_estimate_usd": round(float(api_costs.get("estimated_cost_usd", 0.0) or 0.0), 6),
        "api_costs": api_costs,
        "drawdown_pct": round(drawdown, 2),
        "roi_pct": round(roi, 2),
        "open_positions": len(_bot.entry_manager.get_positions()) if _bot and _bot.entry_manager else 0,
        "reconciliation_status": reconciliation.get("status", "unknown"),
        "reconciliation_severity": reconciliation.get("severity", "unknown"),
        "reconciliation_diff": round(float(reconciliation.get("broker_vs_pnl_state_diff", 0) or 0), 2),
        "reconciliation_reasons": reconciliation.get("reasons", []) or [],
        "reconciliation_canaries": reconciliation_state.get("canaries", []) or [],
        "trust_flags": reconciliation_state.get("trust", {}) or {},
    }


@app.get("/api/intelligence")
async def get_intelligence():
    """Get all intelligence sources status for dashboard."""
    if not _bot:
        return {}
    from src.ai import trade_history

    result = {}

    # Earnings
    if hasattr(_bot, 'earnings_scanner') and _bot.earnings_scanner:
        today = await _bot.earnings_scanner.get_today()
        result["earnings"] = {
            "today_count": len(today),
            "today_tickers": [e["ticker"] for e in today[:10]],
        }

    # Unusual options
    if hasattr(_bot, 'options_scanner') and _bot.options_scanner:
        result["unusual_options"] = {
            "count": len(_bot.options_scanner._cache),
            "bullish": _bot.options_scanner.get_bullish_tickers()[:5],
            "bearish": _bot.options_scanner.get_bearish_tickers()[:5],
        }

    # Congress
    if hasattr(_bot, 'congress_scanner') and _bot.congress_scanner:
        buys = _bot.congress_scanner.get_buy_signals()
        result["congress"] = {
            "total_trades": len(_bot.congress_scanner._trades),
            "top_buys": [{"ticker": s["ticker"], "members": s["count"]} for s in buys[:5]],
        }

    # Short interest
    if hasattr(_bot, 'short_scanner') and _bot.short_scanner:
        squeeze = _bot.short_scanner.get_squeeze_candidates()
        result["short_interest"] = {
            "high_si_count": len(_bot.short_scanner._data),
            "squeeze_candidates": [s["ticker"] for s in squeeze[:5]],
        }

    if hasattr(_bot, "ark_trades") and _bot.ark_trades:
        result["ark_trades"] = {
            "buys": _bot.ark_trades.get_buy_signals()[:5],
            "sells": _bot.ark_trades.get_sell_signals()[:5],
        }

    if hasattr(_bot, "copy_trader_monitor") and _bot.copy_trader_monitor:
        result["copy_trader"] = _bot.copy_trader_monitor.get_dashboard_data()

    if hasattr(_bot, "unusual_whales") and _bot.unusual_whales:
        result["unusual_whales_api"] = _bot.unusual_whales.get_usage_stats()

    if hasattr(_bot, "unusual_whales_stream") and _bot.unusual_whales_stream:
        result["unusual_whales_stream"] = _bot.unusual_whales_stream.get_stats()
    try:
        result["unusual_whales_trade_analytics"] = (
            (trade_history.get_analytics() or {}).get("unusual_whales", {})
        )
    except Exception:
        result["unusual_whales_trade_analytics"] = {}

    try:
        result["api_costs"] = get_api_cost_stats()
    except Exception:
        result["api_costs"] = {}

    if hasattr(_bot, "scanner") and _bot.scanner:
        focus_rows = []
        for candidate in (_bot.scanner.get_cached_candidates() or [])[:5]:
            news_summary = str(candidate.get("uw_news_summary") or "").strip()
            chain_summary = str(candidate.get("uw_chain_summary") or "").strip()
            if not news_summary and not chain_summary:
                continue
            focus_rows.append(
                {
                    "symbol": candidate.get("symbol", ""),
                    "budget_mode": candidate.get("uw_budget_mode", "unknown"),
                    "news_summary": news_summary,
                    "chain_summary": chain_summary,
                }
            )
        result["unusual_whales_focus"] = focus_rows

    # Sector rotation
    if hasattr(_bot, 'sector_model') and _bot.sector_model:
        result["sectors"] = _bot.sector_model.get_dashboard_data()
        result["sector_bias"] = _bot.sector_model.get_sector_bias()
        focus = _bot.sector_model.suggest_focus()
        result["sector_focus"] = focus

    if hasattr(_bot, "human_intel_store") and _bot.human_intel_store:
        result["human_intel"] = {
            "count": len(_bot.human_intel_store.list_entries(limit=100)),
            "top_tickers": [entry["ticker"] for entry in _bot.human_intel_store.list_entries(limit=5)],
        }

    return result


@app.get("/api/streams")
async def get_streams():
    """Get WebSocket stream status."""
    if not _bot:
        return {}
    return {
        "market": _bot.market_stream.get_stats() if hasattr(_bot, 'market_stream') and _bot.market_stream else {},
        "trade": _bot.trade_stream.get_stats() if hasattr(_bot, 'trade_stream') and _bot.trade_stream else {},
        "unusual_whales": (
            _bot.unusual_whales_stream.get_stats()
            if hasattr(_bot, "unusual_whales_stream") and _bot.unusual_whales_stream
            else {}
        ),
    }


@app.get("/api/guards")
async def get_guards():
    """Get extended hours guard status plus operating guardrails."""
    if not _bot or not hasattr(_bot, 'extended_guard'):
        return {"active": False, "guards": {}, "guardrails": {}}
    guard = _bot.extended_guard
    return {
        "active": guard.is_extended_hours(),
        "regular_hours": guard.is_regular_hours(),
        "guards": guard.get_guard_status(),
        "guardrails": getattr(_bot, '_get_operating_guardrails', lambda: {})() or {},
    }


@app.get("/api/metrics")
async def get_metrics():
    if not _bot or not _bot.risk_manager:
        return {}
    payload = dict(_bot.risk_manager.get_status())
    reconciliation_state = _get_reconciliation_state() if getattr(_bot, "reconciler", None) else {}
    if reconciliation_state:
        payload["trust_flags"] = reconciliation_state.get("trust", {})
    return _apply_restart_safe_runtime_metrics(payload, reconciliation_state=reconciliation_state)


@app.get("/api/activity")
async def get_activity(limit: int = 50):
    """Get recent activity feed — bot's thoughts, research, decisions."""
    return _activity_feed[-limit:]


@app.get("/api/watchlist")
async def get_watchlist():
    """Get current dynamic watchlist."""
    if not _bot or not hasattr(_bot, 'watchlist'):
        return []
    return _bot.watchlist.get_all()


@app.get("/api/human-intel")
async def get_human_intel(limit: int = 20):
    """Get operator-supplied discretionary context."""
    if not _bot or not hasattr(_bot, "human_intel_store") or not _bot.human_intel_store:
        return []
    return _bot.human_intel_store.list_entries(limit=limit)


@app.post("/api/copilot/chat")
async def copilot_chat(request: Request):
    """Natural-language operator chat against live state plus persisted artifacts."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    message = str(payload.get("message", "") or "").strip()
    history = payload.get("history") or []
    if not message:
        return JSONResponse(status_code=400, content={"error": "message is required"})

    result = await _generate_copilot_reply(message, history if isinstance(history, list) else [])
    if not result.get("ok"):
        return JSONResponse(status_code=503, content=result)
    return result


@app.post("/api/human-intel")
async def add_human_intel(request: Request):
    """Persist human context and immediately promote it into the watchlist."""
    if not _bot or not hasattr(_bot, "human_intel_store") or not _bot.human_intel_store:
        return JSONResponse(status_code=503, content={"error": "Human intel store unavailable"})
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    ticker = str(payload.get("ticker", "") or "").upper().strip()
    if not ticker:
        return JSONResponse(status_code=400, content={"error": "ticker is required"})

    entry = _bot.human_intel_store.add_entry(
        ticker=ticker,
        title=str(payload.get("title", "") or ""),
        notes=str(payload.get("notes", "") or ""),
        url=str(payload.get("url", "") or ""),
        source=str(payload.get("source", "") or ""),
        kind=str(payload.get("kind", "note") or "note"),
        bias=str(payload.get("bias", "neutral") or "neutral"),
        confidence=float(payload.get("confidence", 0.5) or 0.5),
        ttl_hours=float(payload.get("ttl_hours", 96) or 96),
    )

    try:
        if hasattr(_bot, "watchlist") and _bot.watchlist:
            side = "short" if entry.get("bias") == "bearish" else "long"
            conviction = min(0.95, 0.35 + float(entry.get("confidence", 0.5) or 0.5) * 0.5)
            reason = entry.get("title") or entry.get("notes") or "operator context"
            _bot.watchlist.add(
                ticker,
                conviction=conviction,
                side=side,
                source="human_intel",
                reason=f"Human intel: {reason[:100]}",
                ttl_hours=float(entry.get("ttl_hours", 96) or 96),
            )
    except Exception as e:
        logger.debug(f"Human intel watchlist add failed: {e}")

    try:
        if hasattr(_bot, "orchestrator") and _bot.orchestrator:
            for cache in (getattr(_bot.orchestrator, "_cache", {}), getattr(_bot.orchestrator, "_skip_cache", {})):
                for key in list(cache.keys()):
                    if str(key).startswith(f"{ticker}:"):
                        cache.pop(key, None)
    except Exception as e:
        logger.debug(f"Human intel cache invalidation failed: {e}")

    log_activity("research", f"🧠 Human intel added: {ticker} {entry.get('bias', 'neutral')} — {(entry.get('title') or entry.get('notes') or '')[:120]}")
    return {"ok": True, "entry": entry}


@app.delete("/api/human-intel/{entry_id}")
async def delete_human_intel(entry_id: str):
    """Remove a persisted human-intel entry."""
    if not _bot or not hasattr(_bot, "human_intel_store") or not _bot.human_intel_store:
        return JSONResponse(status_code=503, content={"error": "Human intel store unavailable"})
    removed = _bot.human_intel_store.remove_entry(entry_id)
    if removed:
        log_activity("research", f"🧠 Human intel removed: {entry_id}")
    return {"ok": removed}


@app.post("/api/pause")
async def pause():
    if _bot:
        _bot.paused = True
        logger.warning("⏸️ Trading PAUSED via dashboard")
    return {"status": "paused"}


@app.post("/api/resume")
async def resume():
    if _bot:
        _bot.paused = False
        if _bot.risk_manager:
            _bot.risk_manager.resume()
        logger.info("▶️ Trading RESUMED via dashboard")
    return {"status": "resumed"}


@app.post("/api/stop")
async def stop():
    if _bot:
        _bot.stop()
        logger.warning("🛑 Bot STOPPED via dashboard")
    return {"status": "stopped"}


# ── Start server in background thread ─────────────────────────────

def start_dashboard(bot=None):
    global _dashboard_thread
    set_bot(bot)
    host = settings.DASHBOARD_HOST
    port = settings.DASHBOARD_PORT
    if _dashboard_thread and _dashboard_thread.is_alive():
        logger.debug(f"Dashboard already running on http://{host}:{port}")
        return _dashboard_thread
    logger.info(f"📊 Dashboard starting on http://{host}:{port}")

    def _run():
        retries = max(1, int(getattr(settings, "DASHBOARD_START_RETRIES", 15) or 15))
        retry_delay = max(0.5, float(getattr(settings, "DASHBOARD_START_RETRY_SECONDS", 1.0) or 1.0))
        for attempt in range(1, retries + 1):
            if _dashboard_port_in_use(host, port):
                logger.warning(
                    f"Dashboard port {port} already in use; retrying in {retry_delay:.1f}s "
                    f"({attempt}/{retries})"
                )
                time.sleep(retry_delay)
                continue
            try:
                uvicorn.run(app, host=host, port=port, log_level="warning")
                return
            except OSError as e:
                if "address already in use" in str(e).lower() and attempt < retries:
                    logger.warning(
                        f"Dashboard bind collision on {host}:{port}; retrying in {retry_delay:.1f}s "
                        f"({attempt}/{retries})"
                    )
                    time.sleep(retry_delay)
                    continue
                logger.error(f"Dashboard failed to start: {e}")
                return
            except Exception as e:
                logger.error(f"Dashboard server crashed during startup: {e}")
                return
        logger.error(f"Dashboard failed to acquire port {port} after {retries} attempts")

    _dashboard_thread = threading.Thread(target=_run, daemon=True, name="velox-dashboard")
    _dashboard_thread.start()
    return _dashboard_thread


# ── Dashboard HTML ─────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Velox Dashboard</title>
<style>
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.3}}
@keyframes glow{0%,100%{box-shadow:0 0 5px rgba(88,166,255,.3)}50%{box-shadow:0 0 20px rgba(88,166,255,.6)}}
@keyframes countUp{from{opacity:.5;transform:translateY(4px)}to{opacity:1;transform:translateY(0)}}
@keyframes slideIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:translateY(0)}}
@keyframes neonPulse{0%,100%{text-shadow:0 0 7px currentColor,0 0 10px currentColor}50%{text-shadow:0 0 20px currentColor,0 0 40px currentColor}}
@keyframes drift{0%,100%{transform:translate3d(0,0,0)}50%{transform:translate3d(0,-6px,0)}}
:root{
  --bg:#0d0b08;
  --bg2:#15120e;
  --panel:#17130f;
  --panel-2:#1d1812;
  --panel-3:#241d15;
  --panel-ink:#12161d;
  --panel-ink-2:#0c1016;
  --line:#2f271d;
  --line-strong:#4a3e2d;
  --text:#f4efe6;
  --muted:#b5a792;
  --accent:#d4b07a;
  --accent-2:#f1dec0;
  --accent-3:#c8ab7a;
  --good:#87af88;
  --warn:#c7a36b;
  --bad:#cb8575;
  --cool:#88a9d8;
  --shadow:0 34px 90px rgba(0,0,0,.30);
}
*{margin:0;padding:0;box-sizing:border-box}
html{
  width:100%;
  max-width:100%;
  overflow-x:hidden;
}
body{
  background:
    radial-gradient(circle at top left, rgba(212,176,122,.10), transparent 30%),
    radial-gradient(circle at top right, rgba(241,222,192,.07), transparent 20%),
    linear-gradient(180deg,var(--bg) 0%,var(--bg2) 100%);
  color:var(--text);
  font-family:"Avenir Next","Helvetica Neue","Segoe UI",Helvetica,Arial,sans-serif;
  font-size:14px;
  min-height:100vh;
  width:100%;
  max-width:100%;
  overflow-x:hidden;
}
body::before{
  content:"";
  position:fixed;
  inset:0;
  pointer-events:none;
  background:
    linear-gradient(90deg, rgba(255,245,229,.012) 0 1px, transparent 1px 132px),
    linear-gradient(180deg, rgba(255,241,215,.04), transparent 26%);
  opacity:.26;
  mix-blend-mode:screen;
}
body::after{
  content:"";
  position:fixed;
  inset:0;
  pointer-events:none;
  background:
    radial-gradient(circle at 50% -8%, rgba(212,176,122,.18), transparent 34%),
    radial-gradient(circle at 92% 22%, rgba(136,169,216,.05), transparent 22%),
    radial-gradient(circle at 6% 60%, rgba(203,133,117,.05), transparent 22%);
}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:#0a0e14}::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
.header{
  background:linear-gradient(135deg,rgba(10,8,6,.97) 0%,rgba(21,18,14,.94) 100%);
  border-bottom:1px solid rgba(212,176,122,.14);
  padding:20px 28px 18px;
  display:flex;
  flex-wrap:wrap;
  justify-content:space-between;
  align-items:center;
  gap:18px;
  position:sticky;
  top:0;
  z-index:100;
  backdrop-filter:blur(14px);
  box-shadow:0 18px 46px rgba(0,0,0,.32);
}
.header::after{
  content:"";
  position:absolute;
  left:28px;
  right:28px;
  bottom:0;
  height:1px;
  background:linear-gradient(90deg, transparent, rgba(212,176,122,.45), transparent);
}
.brand-block{display:flex;flex-direction:column;gap:4px;min-width:0}
.header h1{
  font-size:26px;
  color:var(--accent-2);
  display:flex;
  align-items:center;
  gap:10px;
  letter-spacing:.12em;
  font-family:"Iowan Old Style","Palatino Linotype","Book Antiqua",Georgia,serif;
  font-weight:600;
}
.header h1 .logo{font-size:28px}
.header-subtitle{font-size:12px;color:#c7b89d;letter-spacing:.14em;text-transform:uppercase}
.scan-dot{width:10px;height:10px;border-radius:50%;background:#3fb950;display:inline-block;animation:pulse 1.5s ease-in-out infinite}
.scan-dot.idle{background:#484f58;animation:none}
.header .status{display:flex;gap:12px;align-items:center;flex-wrap:wrap;justify-content:flex-end}
.badge{padding:8px 16px;border-radius:999px;font-size:11px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;border:1px solid rgba(255,255,255,.06);box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
.badge.running{background:linear-gradient(135deg,#163f28,#1d5f3a);color:#d7ffe8;box-shadow:0 0 18px rgba(46,160,67,.25)}
.badge.paused{background:linear-gradient(135deg,#5a4417,#7a5c1e);color:#fff1cf;box-shadow:0 0 18px rgba(227,179,65,.18)}
.badge.stopped{background:linear-gradient(135deg,#5c1918,#7c2422);color:#ffd7d4;box-shadow:0 0 18px rgba(248,81,73,.22)}
.jumpbar{
  position:sticky;
  top:77px;
  z-index:80;
  padding:12px 24px 4px;
  background:linear-gradient(180deg,rgba(13,11,8,.92) 0%,rgba(13,11,8,.70) 76%,transparent 100%);
  backdrop-filter:blur(10px);
}
.jumpbar-inner{
  max-width:1640px;
  margin:0 auto;
  display:flex;
  flex-wrap:wrap;
  gap:8px;
}
.jump-link{
  display:inline-flex;
  align-items:center;
  gap:8px;
  text-decoration:none;
  color:var(--muted);
  background:linear-gradient(180deg,rgba(27,22,16,.94),rgba(21,18,14,.98));
  border:1px solid rgba(212,176,122,.12);
  border-radius:999px;
  padding:10px 15px;
  font-size:11px;
  font-weight:700;
  letter-spacing:.14em;
  text-transform:uppercase;
  transition:all .2s ease;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.03);
}
.jump-link:hover{color:var(--text);border-color:rgba(212,176,122,.30);transform:translateY(-1px);background:linear-gradient(180deg,rgba(34,27,20,.96),rgba(24,20,15,.99))}
.container{
  width:100%;
  max-width:1640px;
  margin:0 auto;
  padding:18px clamp(14px,2vw,24px) 28px;
  display:grid;
  grid-template-columns:repeat(2,minmax(0,1fr));
  gap:16px;
}
.card{
  position:relative;
  background:linear-gradient(145deg,rgba(23,19,15,.98) 0%,rgba(18,15,11,.99) 100%);
  border:1px solid rgba(212,176,122,.12);
  border-radius:22px;
  padding:22px;
  min-width:0;
  animation:slideIn .4s ease-out;
  transition:border-color .3s,box-shadow .3s;
  box-shadow:var(--shadow), inset 0 1px 0 rgba(255,248,234,.03);
  overflow:hidden;
}
.card::before{
  content:"";
  position:absolute;
  left:18px;
  right:18px;
  top:0;
  height:1px;
  background:linear-gradient(90deg, transparent, rgba(255,238,209,.22), transparent);
  pointer-events:none;
}
.card::after{
  content:"";
  position:absolute;
  width:240px;
  height:240px;
  right:-120px;
  bottom:-140px;
  background:radial-gradient(circle, rgba(212,176,122,.08), transparent 70%);
  pointer-events:none;
}
.card > *{position:relative;z-index:1}
.card:hover{border-color:rgba(212,176,122,.18);box-shadow:0 38px 96px rgba(0,0,0,.32), inset 0 1px 0 rgba(255,248,234,.035)}
.priority-card{border-color:rgba(212,176,122,.18);box-shadow:0 40px 110px rgba(0,0,0,.34), inset 0 1px 0 rgba(255,248,234,.04)}
.secondary-card{opacity:.94}
.card h2{
  font-size:13px;
  color:#c9bca6;
  text-transform:uppercase;
  letter-spacing:.24em;
  margin-bottom:16px;
  border-bottom:1px solid rgba(241,222,192,.08);
  padding-bottom:14px;
  display:flex;
  align-items:center;
  gap:8px;
  position:relative;
}
.card h2::after{
  content:"";
  position:absolute;
  left:0;
  bottom:-1px;
  width:84px;
  height:1px;
  background:linear-gradient(90deg, rgba(212,176,122,.5), transparent);
}
.card h2 .icon{display:none}
.full{grid-column:1/-1}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px}
.metrics.pnl-grid{grid-template-columns:repeat(auto-fit,minmax(118px,1fr))}
.metrics.metrics-risk{grid-template-columns:repeat(6,minmax(0,1fr))}
.metrics.pnl-grid .metric{padding:14px 10px}
.metrics.pnl-grid .metric .value{
  display:block;
  width:100%;
  min-width:0;
  font-variant-numeric:tabular-nums;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:clip;
}
.metrics.pnl-grid .metric .label{
  display:block;
  letter-spacing:.12em;
}
.metric{text-align:center;padding:16px 12px;background:linear-gradient(180deg,rgba(28,22,16,.96),rgba(20,17,13,.98));border-radius:18px;border:1px solid rgba(241,222,192,.06);transition:all .3s;overflow:hidden;box-shadow:inset 0 1px 0 rgba(255,248,234,.025)}
.metric:hover{border-color:rgba(212,176,122,.20);box-shadow:0 18px 32px rgba(0,0,0,.22), inset 0 1px 0 rgba(255,248,234,.035)}
.metric .value{font-size:18px;font-weight:900;color:var(--accent-2);transition:all .3s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.03em}
.metric .value.positive{color:#3fb950}
.metric .value.negative{color:#f85149}
.metric .value.muted{color:#6e7681!important}
.metric .value.animated{animation:countUp .4s ease-out}
.metric .label{font-size:10px;color:var(--muted);margin-top:8px;text-transform:uppercase;letter-spacing:.16em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.big-pnl{font-weight:900;line-height:1.05;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;animation:none}
.recon-banner{display:none;margin:0 0 12px 0;padding:12px 14px;border:1px solid #8b0000;border-radius:10px;background:linear-gradient(145deg,#2a0f12,#1c0b0d);color:#ffb3b3;font-size:12px;line-height:1.45;white-space:normal;word-break:break-word;overflow-wrap:anywhere;box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
.recon-banner strong{display:block;font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:#ff8e8e;margin-bottom:4px}
.recon-banner .muted{color:#d88f8f}
table{width:100%;min-width:max-content;border-collapse:separate;border-spacing:0}
th{text-align:left;font-size:10px;color:#c7b89d;text-transform:uppercase;letter-spacing:.18em;padding:12px 14px;border-bottom:1px solid rgba(255,255,255,.06);background:linear-gradient(180deg,rgba(26,21,15,.98),rgba(21,18,14,.98));position:sticky;top:0;z-index:1}
td{padding:14px 14px;border-bottom:1px solid rgba(255,255,255,.05);font-size:13px;transition:background .2s;color:#ece3d5;vertical-align:top}
tbody tr:nth-child(even) td{background:rgba(255,255,255,.012)}
tbody tr:hover td{background:rgba(241,222,192,.028)}
td strong{color:var(--accent-2);font-weight:700}
.positive{color:#58d06b}.negative{color:#ff6e63}.info{color:#7cb8ff}
.tag{display:inline-block;padding:5px 10px;border-radius:999px;font-size:10px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;box-shadow:inset 0 1px 0 rgba(255,255,255,.04);white-space:nowrap}
.tag-buy{background:#23863622;color:#3fb950;border:1px solid #23863644}
.tag-short{background:#cb857522;color:#f2b8ad;border:1px solid #cb857544}
.tag-skip{background:#da363322;color:#f85149;border:1px solid #da363344}
.tag-live{background:#264d8226;color:#7cb8ff;border:1px solid #426ba244}
.tag-wait{background:#e3b34122;color:#e3b341;border:1px solid #e3b34144}
.tag-noedge{background:#6e768122;color:#8b949e;border:1px solid #6e768144}
.tag-lock{background:#f8514922;color:#f5a397;border:1px solid #f8514944}
.status-pill{display:inline-flex;align-items:center;padding:7px 12px;border-radius:999px;font-size:10px;font-weight:800;letter-spacing:.14em;text-transform:uppercase;border:1px solid transparent;box-shadow:inset 0 1px 0 rgba(255,255,255,.04)}
.status-pill.good{background:rgba(135,175,136,.12);border-color:rgba(135,175,136,.26);color:#d6f0d7}
.status-pill.warn{background:rgba(199,163,107,.12);border-color:rgba(199,163,107,.24);color:#f2dcba}
.status-pill.bad{background:rgba(203,133,117,.12);border-color:rgba(203,133,117,.24);color:#f3c3b9}
.status-pill.neutral{background:rgba(212,176,122,.10);border-color:rgba(212,176,122,.22);color:#ead4af}
.controls{display:flex;gap:8px}
.btn{
  padding:10px 18px;
  border-radius:999px;
  cursor:pointer;
  font-weight:700;
  font-size:11px;
  text-transform:uppercase;
  letter-spacing:.14em;
  transition:all .2s;
  background:transparent;
  border:1px solid var(--line-strong);
  box-shadow:inset 0 1px 0 rgba(255,255,255,.04);
}
.btn-start{color:#dbe7dc;background:rgba(135,175,136,.08);border-color:rgba(135,175,136,.24)}
.btn-pause{color:#f0dcc0;background:rgba(199,163,107,.08);border-color:rgba(199,163,107,.22)}
.btn-stop{color:#efcbc4;background:rgba(203,133,117,.08);border-color:rgba(203,133,117,.22)}
.btn-intel{color:var(--accent-2);background:rgba(212,176,122,.08);border-color:rgba(212,176,122,.22)}
.btn:hover{transform:translateY(-1px);box-shadow:0 12px 26px rgba(0,0,0,.20), inset 0 1px 0 rgba(255,255,255,.05);border-color:var(--accent)}
.btn:active{transform:translateY(0)}
.empty{color:#484f58;text-align:center;padding:24px;font-style:italic}
.summary-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px}
.summary-row.setup-summary{grid-template-columns:repeat(4,minmax(0,1fr))}
.summary-item{background:linear-gradient(180deg,rgba(28,22,16,.96),rgba(20,17,13,.98));border:1px solid rgba(241,222,192,.06);border-radius:18px;padding:14px 12px;text-align:center;overflow:hidden;min-width:0;box-shadow:inset 0 1px 0 rgba(255,248,234,.025)}
.summary-item .val{font-size:18px;font-weight:900;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;letter-spacing:-.03em}
.summary-item .val.val-sm{font-size:13px;font-weight:700}
.summary-item .lbl{font-size:10px;color:var(--muted);text-transform:uppercase;margin-top:6px;letter-spacing:.16em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.setup-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.subcard{background:linear-gradient(180deg,rgba(28,22,16,.96),rgba(20,17,13,.98));border:1px solid rgba(241,222,192,.06);border-radius:18px;padding:16px;overflow:hidden;min-width:0;box-shadow:inset 0 1px 0 rgba(255,248,234,.025)}
.subcard h3{font-size:11px;color:#bcae97;text-transform:uppercase;letter-spacing:.18em;margin-bottom:10px}
.pending-setup-head{
  display:grid;
  grid-template-columns:minmax(160px,1.1fr) minmax(170px,.95fr) minmax(88px,.42fr) minmax(0,1.85fr);
  gap:16px;
  align-items:end;
  padding:0 12px 12px;
  border-bottom:1px solid rgba(255,255,255,.06);
}
.pending-setup-head span{
  font-size:10px;
  color:#c7b89d;
  text-transform:uppercase;
  letter-spacing:.18em;
}
.pending-setups-list{
  display:flex;
  flex-direction:column;
}
.pending-setup-row{
  display:grid;
  grid-template-columns:minmax(160px,1.1fr) minmax(170px,.95fr) minmax(88px,.42fr) minmax(0,1.85fr);
  gap:16px;
  align-items:center;
  padding:18px 12px;
  border-bottom:1px solid rgba(255,255,255,.05);
}
.pending-setup-row:last-child{border-bottom:none}
.pending-setup-symbol,
.pending-setup-play,
.pending-setup-state,
.pending-setup-trigger{min-width:0}
.pending-setup-symbol strong{
  display:block;
  color:var(--accent-2);
  font-size:17px;
  font-weight:800;
  line-height:1.1;
}
.pending-setup-id{
  margin-top:8px;
  color:#aa9b84;
  font-size:11px;
  line-height:1.45;
  white-space:nowrap;
  overflow:hidden;
  text-overflow:ellipsis;
}
.pending-setup-play-title{
  color:#eee3d0;
  font-size:15px;
  font-weight:700;
  line-height:1.3;
}
.pending-setup-play-sub{
  margin-top:7px;
  color:#a9987d;
  font-size:12px;
  line-height:1.45;
}
.pending-setup-state{
  display:flex;
  justify-content:flex-start;
}
.pending-setup-state .tag{
  padding:7px 12px;
  font-size:10px;
}
.pending-setup-trigger-main{
  color:#ece3d5;
  font-size:14px;
  line-height:1.58;
  display:-webkit-box;
  -webkit-line-clamp:3;
  -webkit-box-orient:vertical;
  overflow:hidden;
  overflow-wrap:anywhere;
}
.pending-setup-trigger-meta{
  margin-top:8px;
  color:#a9987d;
  font-size:11px;
  letter-spacing:.06em;
  text-transform:uppercase;
}
.timeline-list{display:flex;flex-direction:column;gap:8px;max-height:280px;overflow-y:auto}
.timeline-item{padding:10px 12px;background:rgba(29,24,18,.88);border:1px solid rgba(241,222,192,.06);border-radius:14px;font-size:12px;line-height:1.55}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Monaco,Consolas,monospace}
.subtle{font-size:11px;color:var(--muted)}
.ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.ai-card{
  position:relative;
  display:flex;
  flex-direction:column;
  gap:12px;
  min-height:144px;
  padding:18px 18px 16px;
  background:
    radial-gradient(circle at 12% 8%, rgba(255,255,255,.03), transparent 34%),
    linear-gradient(180deg,rgba(25,22,18,.98),rgba(18,16,12,.99));
  border:1px solid rgba(241,222,192,.08);
  border-radius:18px;
  overflow:hidden;
  box-shadow:inset 0 1px 0 rgba(255,248,234,.025);
  text-align:left;
}
.ai-card.observer{border-color:rgba(212,176,122,.10)}
.ai-card.advisor{border-color:rgba(212,176,122,.10)}
.ai-card.tuner{border-color:rgba(210,168,255,.22)}
.ai-card.pm{border-color:rgba(63,185,80,.22)}
.ai-card-header{
  display:flex;
  flex-direction:column;
  gap:6px;
  min-width:0;
}
.ai-card-kicker{
  font-size:10px;
  line-height:1;
  letter-spacing:.18em;
  text-transform:uppercase;
  color:#8f8068;
  font-weight:800;
}
.ai-card-title{
  color:var(--accent-2);
  display:block;
  margin:0;
  letter-spacing:.01em;
  font-size:15px;
  font-weight:800;
}
.ai-card-body{
  color:#ebe2d4;
  font-size:15px;
  line-height:1.72;
  word-wrap:break-word;
  overflow-wrap:break-word;
  text-wrap:pretty;
}
.ai-card-body em{
  color:#c0b29b;
  font-style:normal;
}
.ai-card-body strong{
  display:inline;
  margin:0;
  color:#f5ead8;
}
.insight-stack{
  display:grid;
  gap:12px;
  margin-top:14px;
}
.insight-strip{
  display:grid;
  grid-template-columns:minmax(170px,220px) minmax(0,1fr);
  gap:16px;
  align-items:start;
  padding:15px 18px;
  background:linear-gradient(180deg,rgba(15,18,24,.84),rgba(11,14,18,.98));
  border:1px solid rgba(124,184,255,.14);
  border-radius:15px;
  color:#dde5f0;
  box-shadow:inset 0 1px 0 rgba(255,255,255,.025);
}
.insight-label{
  display:flex;
  flex-direction:column;
  gap:5px;
}
.insight-label strong{
  display:block;
  margin:0;
  color:#7cb8ff;
  font-size:12px;
  font-weight:800;
  text-transform:uppercase;
  letter-spacing:.16em;
}
.insight-label span{
  font-size:10px;
  color:#8fa0b6;
  text-transform:uppercase;
  letter-spacing:.14em;
}
.insight-body{
  min-width:0;
  color:#e7dde0;
  font-size:14px;
  line-height:1.72;
  text-wrap:pretty;
}
.insight-body strong{color:#f3eadb}
.insight-strip.tone-green{border-color:rgba(141,209,154,.14)}
.insight-strip.tone-violet{border-color:rgba(214,181,255,.14)}
.insight-strip.tone-rose{border-color:rgba(240,177,162,.14)}
.insight-strip.tone-amber{border-color:rgba(227,192,127,.16)}
.insight-strip.tone-green .insight-label strong{color:#8dd19a}
.insight-strip.tone-violet .insight-label strong{color:#d6b5ff}
.insight-strip.tone-rose .insight-label strong{color:#f0b1a2}
.insight-strip.tone-amber .insight-label strong{color:#e3c07f}
.provider-health-grid{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(150px,1fr));
  gap:10px;
}
.provider-pill{
  padding:12px 14px;
  border-radius:14px;
  background:rgba(255,255,255,.025);
  border:1px solid rgba(255,255,255,.06);
}
.provider-pill.ok{border-color:rgba(63,185,80,.20);background:rgba(24,42,25,.34)}
.provider-pill.fail{border-color:rgba(240,177,162,.20);background:rgba(49,23,21,.34)}
.provider-name{
  color:#f2e9d9;
  font-size:12px;
  font-weight:800;
  letter-spacing:.08em;
  text-transform:uppercase;
}
.provider-state{
  margin-top:8px;
  font-size:12px;
  font-weight:700;
}
.provider-pill.ok .provider-state{color:#8dd19a}
.provider-pill.fail .provider-state{color:#f0b1a2}
.provider-latency{
  margin-top:4px;
  color:#bcae97;
  font-size:12px;
}
.provider-detail{
  margin-top:8px;
  color:#9e8e76;
  font-size:11px;
  line-height:1.55;
}
.operator-deck{
  position:relative;
  overflow:hidden;
}
.operator-deck::after{
  content:"";
  position:absolute;
  inset:auto -60px -60px auto;
  width:180px;
  height:180px;
  background:radial-gradient(circle, rgba(79,193,181,.16), transparent 70%);
  animation:drift 8s ease-in-out infinite;
  pointer-events:none;
}
.deck-top{
  display:flex;
  justify-content:space-between;
  align-items:flex-start;
  gap:16px;
  margin-bottom:16px;
  min-width:0;
}
.deck-title{
  display:flex;
  flex-direction:column;
  gap:6px;
  min-width:0;
}
.deck-kicker{
  font-size:11px;
  letter-spacing:.18em;
  text-transform:uppercase;
  color:var(--accent);
  font-weight:800;
}
.deck-title h2{
  margin:0;
  padding:0;
  border:none;
  font-size:38px;
  line-height:.98;
  color:var(--accent-2);
  letter-spacing:-.03em;
  text-transform:none;
  font-family:"Iowan Old Style","Palatino Linotype","Book Antiqua",Georgia,serif;
  font-weight:600;
  max-width:760px;
}
.deck-lead{
  font-size:15px;
  line-height:1.7;
  color:#d0c3ae;
  max-width:820px;
}
.deck-pill-row{
  display:flex;
  flex-wrap:wrap;
  gap:8px;
  min-width:0;
}
.deck-grid{
  display:grid;
  grid-template-columns:repeat(auto-fit,minmax(180px,1fr));
  gap:12px;
  margin-bottom:14px;
}
.deck-tile{
  background:linear-gradient(160deg,rgba(18,15,11,.96),rgba(32,26,20,.98));
  border:1px solid rgba(241,222,192,.07);
  border-radius:18px;
  padding:16px;
  min-height:124px;
  min-width:0;
  box-shadow:inset 0 1px 0 rgba(255,248,234,.025);
}
.deck-label{
  font-size:10px;
  text-transform:uppercase;
  letter-spacing:.16em;
  color:var(--muted);
  margin-bottom:10px;
}
.deck-value{
  font-size:30px;
  font-weight:900;
  line-height:1.05;
  letter-spacing:-.03em;
}
.deck-value.positive{color:#8ff0bc}
.deck-value.negative{color:#ffb6af}
.deck-value.info{color:#ead3aa}
.deck-sub{
  margin-top:8px;
  font-size:12px;
  line-height:1.5;
  color:var(--muted);
}
.deck-lists{
  display:grid;
  grid-template-columns:1.2fr 1fr 1fr;
  gap:12px;
}
.brief-panel{
  background:linear-gradient(145deg,rgba(20,17,13,.95),rgba(29,24,18,.98));
  border:1px solid rgba(241,222,192,.06);
  border-radius:18px;
  padding:16px;
  min-width:0;
  box-shadow:inset 0 1px 0 rgba(255,248,234,.02);
}
.brief-panel h3{
  font-size:11px;
  color:var(--muted);
  text-transform:uppercase;
  letter-spacing:.16em;
  margin-bottom:12px;
}
.brief-list{
  display:flex;
  flex-direction:column;
  gap:8px;
}
.brief-item{
  display:flex;
  align-items:flex-start;
  justify-content:space-between;
  gap:10px;
  padding:12px 14px;
  border-radius:14px;
  background:rgba(255,255,255,.018);
  border:1px solid rgba(241,222,192,.06);
}
.brief-item-main{
  min-width:0;
}
.brief-item-title{
  font-size:13px;
  font-weight:800;
  color:var(--text);
  margin-bottom:4px;
}
.brief-item-sub{
  font-size:12px;
  color:var(--muted);
  line-height:1.45;
}
.brief-empty{
  padding:12px;
  border-radius:12px;
  background:rgba(255,255,255,.025);
  color:var(--muted);
  font-size:12px;
  line-height:1.5;
}
.table-wrap{
  width:100%;
  max-width:100%;
  overflow-x:auto;
  overflow-y:hidden;
  border-radius:18px;
  border:1px solid rgba(241,222,192,.06);
  background:rgba(10,8,6,.22);
  box-shadow:inset 0 1px 0 rgba(255,248,234,.025);
}
.table-scroll{
  width:100%;
  max-width:100%;
  overflow-x:auto;
  overflow-y:hidden;
  border-radius:18px;
  border:1px solid rgba(241,222,192,.06);
  background:rgba(10,8,6,.20);
  box-shadow:inset 0 1px 0 rgba(255,248,234,.02);
}
.book-name{
  display:flex;
  flex-direction:column;
  gap:3px;
}
.book-sub{
  font-size:11px;
  color:var(--muted);
}
.section-note{
  color:var(--muted);
  font-size:11px;
  font-weight:500;
  letter-spacing:.04em;
  margin-left:8px;
  text-transform:none;
}
.watermark{text-align:center;padding:28px 24px;color:#6a5b47;font-size:11px;letter-spacing:.28em;text-transform:uppercase}
.activity-line{padding:10px 0;border-bottom:1px solid rgba(255,255,255,.05);line-height:1.7}
.activity-time{display:inline-block;width:66px;color:#6d614f;font-size:11px;letter-spacing:.08em}
.activity-tag{display:inline-flex;align-items:center;justify-content:center;min-width:54px;padding:3px 8px;margin-right:8px;border-radius:999px;border:1px solid rgba(255,255,255,.06);font-size:10px;font-weight:800;letter-spacing:.12em;text-transform:uppercase}
.side-pill{display:inline-flex;align-items:center;gap:7px;font-size:12px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
.side-pill::before{content:"";width:10px;height:10px;border-radius:50%;display:inline-block;box-shadow:0 0 18px currentColor}
.side-pill.long{color:#41d35b}
.side-pill.short{color:#ff6258}
.modal-backdrop{position:fixed;inset:0;background:rgba(10,14,20,.78);display:none;align-items:center;justify-content:center;z-index:300}
.modal-backdrop.open{display:flex}
.modal{width:min(680px,92vw);background:linear-gradient(145deg,#161b22 0%,#0d1117 100%);border:1px solid #30363d;border-radius:14px;padding:18px;box-shadow:0 12px 60px rgba(0,0,0,.45)}
.modal h3{font-size:15px;color:#58a6ff;margin-bottom:12px}
.intel-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.field{display:flex;flex-direction:column;gap:6px}
.field label{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.6px}
.field input,.field select,.field textarea{background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#c9d1d9;padding:10px 12px;font-size:13px}
.field textarea{min-height:110px;resize:vertical}
.intel-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:14px}
.intel-entry{padding:10px 0;border-bottom:1px solid #21262d}
.intel-entry:last-child{border-bottom:none}
.intel-meta{display:flex;gap:10px;align-items:center;font-size:11px;color:#8b949e;margin-bottom:6px;flex-wrap:wrap}
.intel-title{font-weight:700;color:#c9d1d9}
.intel-notes{font-size:12px;color:#8b949e;line-height:1.5}
.intel-link{color:#58a6ff;text-decoration:none}
.mini-btn{padding:4px 8px;background:#0d1117;border:1px solid #30363d;color:#8b949e;border-radius:6px;cursor:pointer;font-size:11px}
.chat-thread{background:#0d1117;border:1px solid #21262d;border-radius:10px;padding:12px;min-height:320px;max-height:440px;overflow-y:auto}
.chat-bubble{padding:10px 12px;border-radius:10px;margin-bottom:10px;line-height:1.55;font-size:13px;white-space:pre-wrap}
.chat-bubble.user{background:#1f6feb22;border:1px solid #1f6feb44;margin-left:48px}
.chat-bubble.assistant{background:#161b22;border:1px solid #30363d;margin-right:48px}
.chat-role{font-size:11px;color:#8b949e;text-transform:uppercase;letter-spacing:.5px;margin-bottom:6px}
.chat-examples{font-size:12px;color:#8b949e;line-height:1.6;margin-bottom:12px}
.chat-input{width:100%;min-height:96px;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#c9d1d9;padding:12px;font-size:13px;resize:vertical}
.equity-chart-shell{
  width:100%;
  height:248px;
  border-radius:18px;
  border:1px solid rgba(212,176,122,.10);
  background:
    radial-gradient(circle at 18% 16%, rgba(212,176,122,.08), transparent 34%),
    radial-gradient(circle at 84% 18%, rgba(114,141,196,.10), transparent 28%),
    linear-gradient(180deg, rgba(15,18,28,.96), rgba(20,16,12,.98));
  box-shadow:
    inset 0 1px 0 rgba(255,248,234,.03),
    inset 0 -28px 80px rgba(6,8,14,.34);
  overflow:hidden;
}
.equity-chart-empty{
  height:100%;
  display:flex;
  align-items:center;
  justify-content:center;
  color:#9a907e;
  font-size:12px;
  letter-spacing:.08em;
  text-transform:uppercase;
}
.equity-chart-shell .apexcharts-canvas,
.equity-chart-shell .apexcharts-svg{background:transparent !important}
.equity-chart-shell .apexcharts-gridline{stroke:rgba(212,176,122,.08)}
.equity-chart-shell .apexcharts-tooltip{
  backdrop-filter:blur(14px);
  background:rgba(12,13,18,.92) !important;
  border:1px solid rgba(212,176,122,.20) !important;
  box-shadow:0 18px 45px rgba(0,0,0,.28);
}
.equity-chart-shell .apexcharts-tooltip-title{
  background:rgba(212,176,122,.08) !important;
  border-bottom:1px solid rgba(212,176,122,.12) !important;
  color:#f4e7d1 !important;
}
.equity-chart-shell .apexcharts-xaxistooltip,
.equity-chart-shell .apexcharts-yaxistooltip{
  background:rgba(12,13,18,.94) !important;
  border:1px solid rgba(212,176,122,.18) !important;
  color:#f3eadc !important;
}
.equity-curve-toolbar{
  display:flex;
  align-items:center;
  justify-content:space-between;
  gap:14px;
  margin-bottom:10px;
  flex-wrap:wrap;
}
.equity-curve-meta{
  flex:1 1 360px;
  min-width:260px;
  font-size:12px;
  color:#a9987d;
  line-height:1.55;
}
.equity-range-tabs{
  display:inline-flex;
  align-items:center;
  gap:6px;
  padding:4px;
  border-radius:999px;
  border:1px solid rgba(212,176,122,.12);
  background:rgba(21,18,13,.82);
  box-shadow:inset 0 1px 0 rgba(255,248,234,.03);
}
.equity-range-btn{
  border:none;
  outline:none;
  cursor:pointer;
  border-radius:999px;
  padding:7px 12px;
  background:transparent;
  color:#a9987d;
  font-size:11px;
  font-weight:700;
  letter-spacing:.12em;
  text-transform:uppercase;
  transition:background .18s ease,color .18s ease,box-shadow .18s ease,transform .18s ease;
}
.equity-range-btn:hover{
  color:#efe2cb;
  background:rgba(212,176,122,.08);
}
.equity-range-btn.active{
  color:#fcf5e8;
  background:linear-gradient(180deg, rgba(212,176,122,.22), rgba(212,176,122,.12));
  box-shadow:0 0 0 1px rgba(212,176,122,.14) inset, 0 10px 20px rgba(0,0,0,.18);
}
.equity-range-btn:focus-visible{
  box-shadow:0 0 0 2px rgba(212,176,122,.22);
}
@media (max-width:1500px){
  .metrics.metrics-risk{grid-template-columns:repeat(4,minmax(0,1fr))}
  .summary-row.setup-summary{grid-template-columns:repeat(4,minmax(0,1fr))}
  .pending-setup-head,
  .pending-setup-row{
    grid-template-columns:minmax(150px,1fr) minmax(150px,.88fr) minmax(80px,.38fr) minmax(0,1.7fr);
  }
}
@media (max-width:1100px){
  .container{grid-template-columns:1fr}
  .ai-grid,.setup-grid,.deck-grid,.deck-lists{grid-template-columns:1fr}
  .deck-top{flex-direction:column}
  .jumpbar{top:84px}
  .header{padding:18px 16px 16px}
  .jumpbar{padding:10px 16px 4px}
  .card{padding:18px}
  .deck-title h2{font-size:32px}
  .metrics.metrics-risk{grid-template-columns:repeat(3,minmax(0,1fr))}
  .insight-strip{grid-template-columns:1fr}
  .equity-curve-toolbar{align-items:flex-start}
  .summary-row.setup-summary{grid-template-columns:repeat(2,minmax(0,1fr))}
  .pending-setup-head{
    display:none;
  }
  .pending-setup-row{
    grid-template-columns:1.1fr .9fr;
    align-items:start;
  }
  .pending-setup-state{
    grid-column:1/2;
  }
  .pending-setup-trigger{
    grid-column:1/-1;
    padding-top:2px;
  }
}
@media (max-width:700px){
  .metrics.metrics-risk{grid-template-columns:repeat(2,minmax(0,1fr))}
  .ai-card-body{font-size:14px}
  .insight-body{font-size:13px}
  .summary-row.setup-summary{grid-template-columns:1fr}
  .pending-setup-row{
    grid-template-columns:1fr;
    gap:12px;
  }
  .pending-setup-state,
  .pending-setup-trigger{grid-column:auto}
}
</style>
</head>
<body>
<div class="header">
  <div class="brand-block">
    <h1><span class="logo"><svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="vg" x1="0" y1="0" x2="100" y2="100"><stop offset="0%" stop-color="#4fc1b5"/><stop offset="100%" stop-color="#f6b85f"/></linearGradient></defs><path d="M15 75L45 15L55 45L85 15L55 75L45 50Z" fill="url(#vg)"/></svg></span>VELOX <span class="scan-dot idle" id="scanDot"></span></h1>
    <div class="header-subtitle">Operator Console for Capital, Risk, Execution, and Governance</div>
  </div>
  <div class="status">
    <span id="statusBadge" class="badge stopped">LOADING</span>
    <div class="controls">
      <button class="btn btn-intel" onclick="openCopilotModal()">Copilot</button>
      <button class="btn btn-start" onclick="api('/api/resume','POST')">Resume</button>
      <button class="btn btn-pause" onclick="api('/api/pause','POST')">Pause</button>
      <button class="btn btn-stop" onclick="api('/api/stop','POST')">Stop</button>
    </div>
  </div>
</div>
<div class="jumpbar">
  <div class="jumpbar-inner">
    <a class="jump-link" href="#operatorDeck">Command Deck</a>
    <a class="jump-link" href="#capitalDesk">Capital</a>
    <a class="jump-link" href="#booksDesk">Books</a>
    <a class="jump-link" href="#liveDesk">Live Desk</a>
    <a class="jump-link" href="#systemDesk">System</a>
    <a class="jump-link" href="#researchDesk">Research</a>
  </div>
</div>
<div class="container">

  <div class="card full priority-card operator-deck" id="operatorDeck">
    <div class="deck-top">
      <div class="deck-title">
        <div class="deck-kicker">Operator Deck</div>
        <h2>Run the machine from the top down.</h2>
        <div class="deck-lead" id="operatorLead">Loading the current capital, risk, and execution picture...</div>
      </div>
      <div class="deck-pill-row" id="operatorPills"></div>
    </div>
    <div class="deck-grid">
      <div class="deck-tile">
        <div class="deck-label">Capital</div>
        <div class="deck-value info" id="deckCapitalValue">—</div>
        <div class="deck-sub" id="deckCapitalSub">Waiting for broker snapshot</div>
      </div>
      <div class="deck-tile">
        <div class="deck-label">Live Risk</div>
        <div class="deck-value" id="deckRiskValue">—</div>
        <div class="deck-sub" id="deckRiskSub">Heat, drawdown, and open exposure</div>
      </div>
      <div class="deck-tile">
        <div class="deck-label">Integrity</div>
        <div class="deck-value" id="deckIntegrityValue">—</div>
        <div class="deck-sub" id="deckIntegritySub">Reconciliation and trust state</div>
      </div>
      <div class="deck-tile">
        <div class="deck-label">Execution</div>
        <div class="deck-value" id="deckExecutionValue">—</div>
        <div class="deck-sub" id="deckExecutionSub">Positions, pending setups, and market state</div>
      </div>
    </div>
    <div class="deck-lists">
      <div class="brief-panel">
        <h3>What Needs Attention</h3>
        <div id="operatorAlerts" class="brief-list"></div>
      </div>
      <div class="brief-panel">
        <h3>Hot / Cold Books</h3>
        <div id="operatorBooks" class="brief-list"></div>
      </div>
      <div class="brief-panel">
        <h3>Live Focus</h3>
        <div id="operatorFocus" class="brief-list"></div>
      </div>
    </div>
  </div>

  <!-- P&L Terminal -->
  <div class="card full priority-card" id="capitalDesk">
    <h2><span class="icon">💰</span> P&L Terminal <span id="pnlTimestamp" style="margin-left:auto;color:#484f58;font-size:11px;font-weight:400"></span></h2>
    <div id="reconBanner" class="recon-banner"></div>
    <div class="metrics pnl-grid" id="pnlMetrics">
      <div class="metric"><div class="value big-pnl" id="totalPnl">$0.00</div><div class="label">Total P&L</div></div>
      <div class="metric"><div class="value" id="equity">$1,000</div><div class="label">Equity</div></div>
      <div class="metric"><div class="value" id="todayPnl">$0.00</div><div class="label">Broker Day P&L</div></div>
      <div class="metric"><div class="value" id="unrealized">$0.00</div><div class="label">Unrealized</div></div>
      <div class="metric"><div class="value" id="roi">0%</div><div class="label">ROI</div></div>
      <div class="metric"><div class="value" id="winRate">0%</div><div class="label">Win Rate</div></div>
      <div class="metric"><div class="value" id="totalTrades">0</div><div class="label">Trades</div></div>
      <div class="metric"><div class="value" id="drawdown">0%</div><div class="label">Drawdown</div></div>
      <div class="metric"><div class="value positive" id="bestTrade">$0</div><div class="label">Best Trade</div></div>
      <div class="metric"><div class="value negative" id="worstTrade">$0</div><div class="label">Worst Trade</div></div>
      <div class="metric"><div class="value info" id="avgLatency">—</div><div class="label">Signal→Fill</div></div>
    </div>
  </div>

  <!-- Equity Curve -->
  <div class="card full">
    <h2><span class="icon">📈</span> Equity Curve <span id="equityCurveStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="equity-curve-toolbar">
      <div id="equityCurveMeta" class="equity-curve-meta">Loading...</div>
      <div class="equity-range-tabs" id="equityCurveRanges" aria-label="Equity curve range">
        <button type="button" class="equity-range-btn active" data-range="1D" onclick="setEquityRange('1D')">1D</button>
        <button type="button" class="equity-range-btn" data-range="1W" onclick="setEquityRange('1W')">1W</button>
        <button type="button" class="equity-range-btn" data-range="1M" onclick="setEquityRange('1M')">1M</button>
      </div>
    </div>
    <div id="equityCurveChart" class="equity-chart-shell"></div>
  </div>

  <!-- Performance Metrics -->
  <div class="card full" id="riskDesk">
    <h2><span class="icon">📊</span> Risk Metrics</h2>
    <div class="metrics metrics-risk" id="metrics"></div>
  </div>

  <div class="card full priority-card" id="booksDesk">
    <h2><span class="icon">📚</span> Book Scoreboard <span class="section-note">Capital should follow proven expectancy, not noise.</span> <span id="bookScoreStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="summary-row" id="bookScoreSummary"></div>
    <div class="table-wrap">
      <table>
        <thead>
          <tr><th>Book</th><th>Status</th><th>Live</th><th>Realized</th><th>Unrealized</th><th>Trades</th><th>Win Rate</th><th>Expect.</th><th>PF</th><th>Max DD</th><th>Why</th></tr>
        </thead>
        <tbody id="bookScoreboard"></tbody>
      </table>
    </div>
  </div>

  <!-- AI Layers -->
  <div class="card full secondary-card" id="systemDesk">
    <h2><span class="icon">🧠</span> AI Layers <span id="aiEnabled" style="margin-left:auto;color:#8b949e;font-size:11px"></span></h2>
    <div id="aiStatus" class="empty">Loading AI status...</div>
  </div>

  <!-- Consensus Panel -->
  <div class="card full secondary-card">
    <h2><span class="icon">🗳️</span> AI Agent Jury <span style="font-size:11px;color:#6e7681;font-weight:400;margin-left:8px">Resolver-aware play selection</span> <span id="consensusStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="summary-row" id="consensusSummary"></div>
    <div class="table-scroll"><table><thead><tr><th>Symbol</th><th>Verdict</th><th>Play</th><th>Mode</th><th>State</th><th>Confidence</th><th>Size</th><th>Trigger / Why</th></tr></thead>
    <tbody id="consensus"></tbody></table></div>
  </div>

  <div class="card full secondary-card">
    <h2><span class="icon">🧭</span> Setup Resolver <span id="setupOpsStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="summary-row setup-summary" id="setupOpsSummary"></div>
    <div class="setup-grid">
      <div class="subcard">
        <h3>Pending Setups</h3>
        <div class="pending-setup-head">
          <span>Symbol</span>
          <span>Play</span>
          <span>State</span>
          <span>Trigger</span>
        </div>
        <div id="pendingSetups" class="pending-setups-list"><div class="empty">Loading pending setups...</div></div>
      </div>
      <div class="subcard">
        <h3>Mode Report</h3>
        <div style="overflow-x:auto">
          <table><thead><tr><th>Mode</th><th>Setups</th><th>Entered</th><th>Expired</th><th>P&L</th><th>Expect.</th></tr></thead>
          <tbody id="modeReport"></tbody></table>
        </div>
      </div>
      <div class="subcard">
        <h3>Entry Locks</h3>
        <div style="overflow-x:auto">
          <table><thead><tr><th>Symbol</th><th>Status</th><th>Losses</th><th>Until</th><th>Last</th></tr></thead>
          <tbody id="entryLocks"></tbody></table>
        </div>
      </div>
      <div class="subcard">
        <h3 id="setupReplayTitle">Setup Replay</h3>
        <div id="setupReplayTimeline" class="timeline-list"><div class="empty">No setup replay yet</div></div>
      </div>
    </div>
  </div>

  <!-- Trade History Panel -->
  <div class="card full secondary-card">
    <h2><span class="icon">💰</span> Trade History <span id="tradeStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="summary-row" id="tradeSummary"></div>
    <div style="overflow-x:auto"><table><thead><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th><th>%</th><th>Reason</th><th>Hold</th><th>Strategy</th><th>Tier</th><th>Ratchet</th><th>Sources</th><th>Slip</th><th>Latency</th></tr></thead>
    <tbody id="tradeHistory"></tbody></table></div>
  </div>

  <!-- Strategy Controls -->
  <div class="card full secondary-card">
    <h2><span class="icon">🧩</span> Strategy Controls <span id="strategyControlsStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="table-scroll"><table><thead><tr><th>Strategy Tag</th><th>Status</th><th>Reason</th><th>Timestamp</th><th>Source</th></tr></thead>
    <tbody id="strategyControls"></tbody></table></div>
  </div>

  <!-- Portfolio -->
  <div class="card full secondary-card">
    <h2><span class="icon">💼</span> Alpaca Portfolio <span id="portfolioValue" style="margin-left:auto;color:#58a6ff;font-size:12px"></span></h2>
    <div class="table-scroll"><table><thead><tr><th>Symbol</th><th>Shares</th><th>Avg Price</th><th>Current</th><th>Value</th><th>P&L</th></tr></thead>
    <tbody id="portfolio"></tbody></table></div>
  </div>

  <!-- Options Positions -->
  <div class="card full">
    <h2><span class="icon">🧩</span> Options Positions <span id="optionsValue" style="margin-left:auto;color:#58a6ff;font-size:12px"></span></h2>
    <div style="overflow-x:auto"><table><thead><tr><th>Underlying</th><th>Contract</th><th>Type</th><th>Strike</th><th>Exp</th><th>Qty</th><th>Entry</th><th>Curr</th><th>Bid</th><th>Ask</th><th>P&L%</th><th>DTE</th><th>Status</th></tr></thead>
    <tbody id="optionsPositions"></tbody></table></div>
  </div>

  <!-- Activity Feed + Watchlist side by side -->
  <div class="card priority-card" id="liveDesk">
    <h2><span class="icon">🧠</span> Bot Activity Feed</h2>
    <div id="activityFeed" style="max-height:600px;overflow-y:auto;font-size:12px;line-height:1.8;word-wrap:break-word;overflow-wrap:break-word"></div>
  </div>
  <div class="card priority-card">
    <h2><span class="icon">📋</span> Watchlist <span id="watchlistCount" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="table-scroll"><table><thead><tr><th>Ticker</th><th>Side</th><th>Conv</th><th>Source</th><th>Reason</th></tr></thead>
    <tbody id="watchlist"></tbody></table></div>
  </div>

  <div class="card full secondary-card">
    <h2><span class="icon">🧠</span> Human Intel <span id="humanIntelCount" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div id="humanIntelList" class="empty">No operator context yet</div>
  </div>

  <div class="card full secondary-card" id="researchDesk">
    <h2><span class="icon">📡</span> Copy Trader Intel</h2>
    <div class="summary-row" id="copyTraderSummary"></div>
    <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px">
      <div style="overflow-x:auto">
        <div style="font-size:12px;color:#8b949e;margin-bottom:6px">Entry Signals</div>
        <table><thead><tr><th>Symbol</th><th>Side</th><th>Handles</th><th>Size</th></tr></thead>
        <tbody id="copyTraderSignals"></tbody></table>
      </div>
      <div style="overflow-x:auto">
        <div style="font-size:12px;color:#8b949e;margin-bottom:6px">Exit Signals</div>
        <table><thead><tr><th>Symbol</th><th>Action</th><th>Handles</th><th>Count</th></tr></thead>
        <tbody id="copyTraderExits"></tbody></table>
      </div>
      <div style="overflow-x:auto">
        <div style="font-size:12px;color:#8b949e;margin-bottom:6px">Tracked Traders</div>
        <table><thead><tr><th>Handle</th><th>Weight</th><th>W/L</th><th>WR</th></tr></thead>
        <tbody id="copyTraderTraders"></tbody></table>
      </div>
    </div>
  </div>

  <!-- Positions + Candidates side by side -->
  <div class="card priority-card">
    <h2><span class="icon">📈</span> Bot Positions</h2>
    <div style="overflow-x:auto"><table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th>Play</th><th>Mode</th><th>Ratchet</th><th>Protection</th><th>Hold</th></tr></thead>
    <tbody id="positions"></tbody></table></div>
  </div>
  <div class="card priority-card">
    <h2><span class="icon">🔍</span> Live Scanner Candidates <span id="candidateStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div id="candidateMeta" style="font-size:12px;color:#8b949e;margin-bottom:8px"></div>
    <div class="table-scroll"><table><thead><tr><th>Symbol</th><th>Price</th><th>Change</th><th>Vol</th><th>Sent</th><th>Score</th><th>UW</th></tr></thead>
    <tbody id="candidates"></tbody></table></div>
  </div>

  <div class="card full secondary-card">
    <h2><span class="icon">🧭</span> Research Universe <span id="researchStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="table-scroll"><table><thead><tr><th>Symbol</th><th>Side</th><th>Source</th><th>Priority</th><th>Context</th></tr></thead>
    <tbody id="researchUniverse"></tbody></table></div>
  </div>

  <!-- Recent Exits -->
  <div class="card full secondary-card">
    <h2><span class="icon">📋</span> Recent Exits</h2>
    <div class="table-scroll"><table><thead><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&L</th><th>%</th><th>Reason</th><th>Hold</th></tr></thead>
    <tbody id="history"></tbody></table></div>
  </div>
</div>
<div class="watermark">Velox Private Operator Console</div>

<div id="copilotModal" class="modal-backdrop" onclick="if(event.target===this)closeCopilotModal()">
  <div class="modal" style="width:min(920px,95vw)">
    <h3>Ask Velox</h3>
    <div class="chat-examples">
      Ask naturally. Examples: "I saw a stock up 550% after hours on Friday, what was it?" ·
      "Tell me more about the SOTY pharma FDA setup." ·
      "Check this ticker and tell me if the rumor matters."
    </div>
    <div id="copilotMessages" class="chat-thread"></div>
    <div style="margin-top:12px">
      <textarea id="copilotInput" class="chat-input" placeholder="Ask about a ticker, a rumor, a catalyst, an after-hours mover, or paste context here..." onkeydown="if(event.key==='Enter' && !event.shiftKey){event.preventDefault();sendCopilotMessage();}"></textarea>
    </div>
    <div id="copilotStatus" style="font-size:12px;color:#8b949e;margin-top:8px"></div>
    <div class="intel-actions">
      <button class="btn btn-pause" onclick="closeCopilotModal()">Close</button>
      <button class="btn btn-pause" onclick="closeCopilotModal(); openIntelModal()">Structured Note</button>
      <button class="btn btn-intel" onclick="sendCopilotMessage()">Ask Velox</button>
    </div>
  </div>
</div>

<div id="intelModal" class="modal-backdrop" onclick="if(event.target===this)closeIntelModal()">
  <div class="modal">
    <h3>Submit Human Intel</h3>
    <div class="intel-grid">
      <div class="field"><label for="intelTicker">Ticker</label><input id="intelTicker" placeholder="BATL" maxlength="8"></div>
      <div class="field"><label for="intelBias">Bias</label><select id="intelBias"><option value="bullish">Bullish</option><option value="bearish">Bearish</option><option value="neutral">Neutral</option></select></div>
      <div class="field"><label for="intelKind">Type</label><select id="intelKind"><option value="article">Article</option><option value="chart">Chart</option><option value="rumor">Rumor</option><option value="note">Note</option></select></div>
      <div class="field"><label for="intelConfidence">Confidence (0.1-1.0)</label><input id="intelConfidence" type="number" min="0.1" max="1.0" step="0.05" value="0.7"></div>
      <div class="field"><label for="intelSource">Source</label><input id="intelSource" placeholder="Discord / article / personal read"></div>
      <div class="field"><label for="intelTtl">TTL Hours</label><input id="intelTtl" type="number" min="1" max="336" step="1" value="96"></div>
      <div class="field" style="grid-column:1/-1"><label for="intelTitle">Title</label><input id="intelTitle" placeholder="FDA adcom next week / cup-and-handle forming / squeeze chatter"></div>
      <div class="field" style="grid-column:1/-1"><label for="intelUrl">URL</label><input id="intelUrl" placeholder="https://..."></div>
      <div class="field" style="grid-column:1/-1"><label for="intelNotes">Notes</label><textarea id="intelNotes" placeholder="Why this matters, what the machine would miss, and what side it should lean."></textarea></div>
    </div>
    <div class="intel-actions">
      <button class="btn btn-pause" onclick="closeIntelModal()">Cancel</button>
      <button class="btn btn-intel" onclick="submitIntel()">Save Intel</button>
    </div>
  </div>
</div>

<script src="https://cdn.jsdelivr.net/npm/apexcharts"></script>
<script>
const $ = s => document.getElementById(s);
let _prevPnl = null;
let _copilotHistory = [];
let _equityChart = null;
let _apexLoader = null;
let _lastEquityPayload = {points: [], meta: {}};
let _equityRange = '1D';
const _equityRangeConfig = {
  '1D': {limit: 120, label: '1D'},
  '1W': {limit: 160, label: '1W'},
  '1M': {limit: 90, label: '1M'},
};
const _dashToken = new URLSearchParams(window.location.search).get('token') || '';
function withToken(url) {
  if (!_dashToken) return url;
  return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(_dashToken);
}
async function api(url, method='GET', body=null) {
  try {
    const headers = _dashToken ? {'Authorization': `Bearer ${_dashToken}`} : {};
    const opts = {method, headers};
    if (body !== null) {
      headers['Content-Type'] = 'application/json';
      opts.body = JSON.stringify(body);
    }
    const r = await fetch(withToken(url), opts);
    return await r.json();
  } catch(e) { return null; }
}
function cls(v) { return v >= 0 ? 'positive' : 'negative'; }
function fmt(v, d=2) { return v != null ? (v >= 0 ? '+' : '') + v.toFixed(d) : '—'; }
function holdStr(secs) { if(!secs) return '—'; const m=Math.floor(secs/60); const h=Math.floor(m/60); return h>0?h+'h '+m%60+'m':m+'m'; }
function esc(v) { return String(v ?? '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;'); }
function verdictTagClass(decision) {
  const text = String(decision || '').toUpperCase();
  return text === 'BUY' ? 'tag-buy' : text === 'SHORT' ? 'tag-short' : 'tag-skip';
}
function timingTagClass(state) {
  const text = String(state || '').toLowerCase();
  return text === 'enter_now' ? 'tag-live' : text === 'wait_for_trigger' ? 'tag-wait' : text === 'cooldown' ? 'tag-lock' : 'tag-noedge';
}
function humanizeKey(value) {
  const text = String(value ?? '').trim();
  if (!text) return '—';
  return text.replaceAll('_', ' ');
}
function truncateText(text, limit=120) {
  const value = String(text || '');
  return value.length > limit ? value.slice(0, limit) + '...' : value;
}
function fmtClock(ts) {
  if (!(typeof ts === 'number' && isFinite(ts) && ts > 0)) return '—';
  return new Date(ts * 1000).toLocaleTimeString();
}
function fmtRelativeSeconds(secs) {
  if (!(typeof secs === 'number' && isFinite(secs) && secs > 0)) return '—';
  const mins = Math.floor(secs / 60);
  if (mins < 1) return `${Math.round(secs)}s`;
  const hours = Math.floor(mins / 60);
  if (hours < 1) return `${mins}m`;
  return `${hours}h ${mins % 60}m`;
}
function compactSetupId(setupId) {
  const text = String(setupId || '').trim();
  if (!text) return '';
  const parts = text.split(':');
  const compact = parts.length > 2 ? parts.slice(2).join(' · ') : text;
  return compact.length > 36 ? compact.slice(0, 36) + '…' : compact;
}
function compactTimingLabel(state) {
  const text = String(state || '').toLowerCase();
  if (!text || text === 'wait_for_trigger') return 'Wait';
  if (text === 'enter_now') return 'Enter';
  if (text === 'broker_blocked') return 'Broker Blocked';
  if (text === 'capital_blocked') return 'Capital Blocked';
  if (text === 'shadow_only') return 'Shadow';
  if (text === 'data_insufficient') return 'Insufficient';
  if (text === 'mode_conflict') return 'Conflict';
  return humanizeKey(text);
}
function pickReplaySymbol(lastConsensus, pendingPayload, entryControls) {
  const lastSymbol = String(lastConsensus?.symbol || '').trim().toUpperCase();
  if (lastSymbol) return lastSymbol;
  const pendingSymbol = String((pendingPayload?.setups || [])[0]?.symbol || '').trim().toUpperCase();
  if (pendingSymbol) return pendingSymbol;
  const lockSymbol = String((entryControls?.loss_locks || [])[0]?.symbol || '').trim().toUpperCase();
  if (lockSymbol) return lockSymbol;
  const stateSymbol = String((entryControls?.trade_states || [])[0]?.symbol || '').trim().toUpperCase();
  return stateSymbol || '';
}
function renderCopilotMessages() {
  const el = $('copilotMessages');
  if (!el) return;
  if (!_copilotHistory.length) {
    el.innerHTML = '<div class="empty">Ask anything about the live engine, a ticker, a rumor, a catalyst, or a move you vaguely remember.</div>';
    return;
  }
  el.innerHTML = _copilotHistory.map(msg => `
    <div class="chat-bubble ${msg.role === 'user' ? 'user' : 'assistant'}">
      <div class="chat-role">${msg.role === 'user' ? 'You' : `Velox${msg.provider ? ' · ' + esc(msg.provider) : ''}`}</div>
      <div>${esc(msg.content).replace(/\n/g, '<br>')}</div>
    </div>
  `).join('');
  el.scrollTop = el.scrollHeight;
}
function openCopilotModal() {
  $('copilotModal').classList.add('open');
  renderCopilotMessages();
  if ($('copilotInput')) $('copilotInput').focus();
}
function closeCopilotModal() { $('copilotModal').classList.remove('open'); }
function openIntelModal() { $('intelModal').classList.add('open'); }
function closeIntelModal() { $('intelModal').classList.remove('open'); }
async function sendCopilotMessage() {
  const input = $('copilotInput');
  const message = (input && input.value || '').trim();
  if (!message) return;
  _copilotHistory.push({role: 'user', content: message});
  if (input) input.value = '';
  _copilotHistory.push({role: 'assistant', content: 'Thinking...', provider: '', pending: true});
  renderCopilotMessages();
  $('copilotStatus').textContent = 'Querying Velox...';
  const res = await api('/api/copilot/chat', 'POST', {
    message,
    history: _copilotHistory.filter(m => !m.pending).slice(-8),
  });
  _copilotHistory = _copilotHistory.filter(m => !m.pending);
  _copilotHistory.push({
    role: 'assistant',
    content: (res && (res.answer || res.error)) || 'No response available.',
    provider: (res && res.provider) || '',
  });
  $('copilotStatus').textContent = res && res.provider ? `Answered via ${res.provider}` : '';
  renderCopilotMessages();
}
async function submitIntel() {
  const ticker = ($('intelTicker').value || '').trim().toUpperCase();
  if (!ticker) return;
  const payload = {
    ticker,
    title: $('intelTitle').value || '',
    notes: $('intelNotes').value || '',
    url: $('intelUrl').value || '',
    source: $('intelSource').value || '',
    kind: $('intelKind').value || 'note',
    bias: $('intelBias').value || 'neutral',
    confidence: parseFloat($('intelConfidence').value || '0.7'),
    ttl_hours: parseFloat($('intelTtl').value || '96'),
  };
  const res = await api('/api/human-intel', 'POST', payload);
  if (res && res.ok) {
    closeIntelModal();
    ['intelTicker','intelTitle','intelNotes','intelUrl','intelSource'].forEach(id => { if ($(id)) $(id).value = ''; });
    $('intelConfidence').value = '0.7';
    $('intelTtl').value = '96';
    await refresh();
  }
}
async function deleteIntel(entryId) {
  await api(`/api/human-intel/${encodeURIComponent(entryId)}`, 'DELETE');
  await refresh();
}
function topPnlBucket(obj) {
  if (!obj) return null;
  const rows = Object.entries(obj);
  if (!rows.length) return null;
  rows.sort((a, b) => (b[1]?.pnl || 0) - (a[1]?.pnl || 0));
  return {name: rows[0][0], pnl: rows[0][1]?.pnl || 0};
}
function ensureApexCharts() {
  if (window.ApexCharts) return Promise.resolve(true);
  if (_apexLoader) return _apexLoader;
  _apexLoader = new Promise(resolve => {
    const script = document.createElement('script');
    script.src = 'https://cdn.jsdelivr.net/npm/apexcharts';
    script.async = true;
    script.dataset.apexchartsLoader = '1';
    script.onload = () => resolve(!!window.ApexCharts);
    script.onerror = () => resolve(false);
    document.head.appendChild(script);
  });
  return _apexLoader;
}
function destroyEquityChart() {
  if (_equityChart) {
    _equityChart.destroy();
    _equityChart = null;
  }
}
function renderEquityCurveFallback(points, meta={}) {
  const shell = $('equityCurveChart');
  if (!shell) return;
  destroyEquityChart();
  if (!points || points.length < 2) {
    shell.innerHTML = '<div class="equity-chart-empty">Not enough trade history for equity curve</div>';
    return;
  }
  const w = 900, h = 248, padX = 18, padY = 16;
  const ys = points.map(p => Number(p.equity || 0));
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanY = (maxY - minY) || 1;
  const first = ys[0];
  const last = ys[ys.length - 1];
  const positive = last >= first;
  const line = positive ? '#d7bc8a' : '#dba792';
  const glow = positive ? 'rgba(215,188,138,.32)' : 'rgba(219,167,146,.28)';
  const fill = positive ? 'rgba(215,188,138,.18)' : 'rgba(219,167,146,.14)';
  const coords = points.map((p, i) => {
    const x = padX + (i / (points.length - 1)) * (w - (padX * 2));
    const y = h - padY - (((Number(p.equity || 0) - minY) / spanY) * (h - (padY * 2)));
    return [x, y];
  });
  const pts = coords.map(([x, y]) => `${x.toFixed(2)},${y.toFixed(2)}`).join(' ');
  const areaPts = `${padX},${h - padY} ${pts} ${w - padX},${h - padY}`;
  const lastPoint = coords[coords.length - 1];
  const baseline = meta.startingEquity || first;
  const baselineY = h - padY - (((baseline - minY) / spanY) * (h - (padY * 2)));
  shell.innerHTML = `
    <svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none" style="width:100%;height:100%">
      <defs>
        <linearGradient id="equityPanelWash" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="rgba(17,22,34,.82)" />
          <stop offset="100%" stop-color="rgba(14,12,9,.98)" />
        </linearGradient>
        <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="${fill}" />
          <stop offset="100%" stop-color="rgba(0,0,0,0)" />
        </linearGradient>
        <filter id="curveGlow">
          <feGaussianBlur stdDeviation="5" result="blur" />
          <feMerge>
            <feMergeNode in="blur" />
            <feMergeNode in="SourceGraphic" />
          </feMerge>
        </filter>
      </defs>
      <rect x="0" y="0" width="${w}" height="${h}" rx="18" fill="url(#equityPanelWash)" />
      <line x1="${padX}" y1="${h - padY}" x2="${w - padX}" y2="${h - padY}" stroke="rgba(212,176,122,.08)" stroke-width="1" />
      <line x1="${padX}" y1="${baselineY.toFixed(2)}" x2="${w - padX}" y2="${baselineY.toFixed(2)}" stroke="rgba(212,176,122,.12)" stroke-dasharray="4 8" stroke-width="1" />
      <polygon points="${areaPts}" fill="url(#equityFill)" />
      <polyline points="${pts}" fill="none" stroke="${glow}" stroke-width="10" stroke-linecap="round" stroke-linejoin="round" filter="url(#curveGlow)" opacity=".42" />
      <polyline points="${pts}" fill="none" stroke="${line}" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round" />
      <circle cx="${lastPoint[0].toFixed(2)}" cy="${lastPoint[1].toFixed(2)}" r="5.5" fill="${line}" stroke="rgba(255,248,234,.82)" stroke-width="2" />
    </svg>
  `;
}
function renderEquityCurveApex(points, meta={}) {
  const shell = $('equityCurveChart');
  if (!shell || !window.ApexCharts) return false;
  if (!points || points.length < 2) {
    renderEquityCurveFallback(points, meta);
    return true;
  }
  const ys = points.map(p => Number(p.equity || 0));
  const first = ys[0];
  const last = ys[ys.length - 1];
  const positive = last >= first;
  const spanY = (Math.max(...ys) - Math.min(...ys)) || 1;
  const axisPad = Math.max(spanY * 0.18, 28);
  const useDatetime = points.every(p => Number(p.timestamp || 0) > 0);
  const series = points.map((p, idx) => ({
    x: useDatetime ? Number(p.timestamp) * 1000 : idx + 1,
    y: Number(p.equity || 0),
  }));
  const tone = positive
    ? {
        line: '#d8bf93',
        fillTo: '#5fd3c6',
        marker: '#f4e5cb',
        labelBorder: 'rgba(216,191,147,.26)',
        shadow: 'rgba(216,191,147,.22)',
      }
    : {
        line: '#d9b28e',
        fillTo: '#a96d64',
        marker: '#f2d7c3',
        labelBorder: 'rgba(217,178,142,.24)',
        shadow: 'rgba(169,109,100,.24)',
      };
  const latestPoint = series[series.length - 1];
  const baseline = Number(meta.startingEquity || first);
  const highY = Math.max(...ys);
  const highIndex = ys.indexOf(highY);
  const annotations = {
    yaxis: [
      {
        y: baseline,
        borderColor: 'rgba(212,176,122,.18)',
        strokeDashArray: 6,
        label: {
          text: 'START',
          borderColor: 'rgba(212,176,122,.18)',
          style: {
            background: 'rgba(15,14,12,.84)',
            color: '#d5c0a1',
            fontSize: '10px',
            fontWeight: 700,
          },
        },
      },
    ],
    points: [
      {
        x: latestPoint.x,
        y: latestPoint.y,
        marker: {
          size: 5,
          fillColor: tone.marker,
          strokeColor: tone.line,
          strokeWidth: 3,
        },
        label: {
          text: fmtUsd(latestPoint.y),
          borderColor: tone.labelBorder,
          offsetY: -8,
          style: {
            background: 'rgba(11,13,18,.88)',
            color: '#f5ead8',
            fontSize: '11px',
            fontWeight: 700,
          },
        },
      },
    ],
  };
  if (highIndex > 0 && highIndex < series.length - 1) {
    annotations.points.push({
      x: series[highIndex].x,
      y: series[highIndex].y,
      marker: {
        size: 0,
        fillColor: 'transparent',
        strokeColor: 'transparent',
      },
      label: {
        text: 'HIGH',
        borderColor: 'rgba(255,255,255,.10)',
        offsetY: -10,
        style: {
          background: 'rgba(11,13,18,.62)',
          color: '#e7d7bb',
          fontSize: '10px',
          fontWeight: 700,
        },
      },
    });
  }
  const options = {
    chart: {
      type: 'area',
      height: 248,
      background: 'transparent',
      toolbar: {show: false},
      zoom: {enabled: false},
      foreColor: '#cbbda5',
      fontFamily: '"Avenir Next", "SF Pro Display", "Helvetica Neue", sans-serif',
      animations: {
        enabled: true,
        easing: 'easeinout',
        speed: 650,
        dynamicAnimation: {enabled: true, speed: 420},
      },
      dropShadow: {
        enabled: true,
        top: 10,
        left: 0,
        blur: 16,
        color: tone.shadow,
        opacity: 0.28,
      },
    },
    series: [{name: 'Equity', data: series}],
    stroke: {
      curve: 'straight',
      width: 3.5,
      lineCap: 'round',
      colors: [tone.line],
    },
    fill: {
      type: 'gradient',
      gradient: {
        shade: 'dark',
        type: 'vertical',
        shadeIntensity: 0.18,
        gradientToColors: [tone.fillTo],
        inverseColors: false,
        opacityFrom: 0.32,
        opacityTo: 0.02,
        stops: [0, 68, 100],
      },
    },
    dataLabels: {enabled: false},
    legend: {show: false},
    markers: {
      size: 0,
      strokeWidth: 0,
      hover: {size: 5},
    },
    grid: {
      show: true,
      borderColor: 'rgba(212,176,122,.08)',
      strokeDashArray: 0,
      padding: {top: 8, right: 18, bottom: 8, left: 18},
      xaxis: {lines: {show: false}},
      yaxis: {lines: {show: true}},
    },
    xaxis: {
      type: useDatetime ? 'datetime' : 'numeric',
      tickAmount: String(meta.period || _equityRange || '1D').toUpperCase() === '1D' ? 6 : 5,
      labels: {
        show: true,
        trim: false,
        hideOverlappingLabels: true,
        style: {
          colors: '#93876f',
          fontSize: '11px',
          fontWeight: 600,
        },
        formatter: (value, timestamp) => useDatetime ? formatEquityAxisLabel(timestamp || value, meta) : value,
      },
      axisBorder: {show: false},
      axisTicks: {
        show: true,
        color: 'rgba(212,176,122,.08)',
      },
      tooltip: {enabled: false},
      crosshairs: {
        show: true,
        stroke: {
          color: 'rgba(212,176,122,.16)',
          width: 1,
          dashArray: 4,
        },
      },
    },
    yaxis: {
      min: Math.min(...ys) - (axisPad * 0.35),
      max: Math.max(...ys) + axisPad,
      tickAmount: 4,
      show: true,
      labels: {
        show: true,
        minWidth: 72,
        maxWidth: 72,
        style: {
          colors: '#8c826f',
          fontSize: '11px',
          fontWeight: 600,
        },
        formatter: value => fmtUsdCompact(value),
      },
    },
    tooltip: {
      theme: 'dark',
      marker: {show: false},
      x: {
        formatter: (_value, opts) => {
          const point = points[opts.dataPointIndex];
          if (useDatetime && point && Number(point.timestamp || 0) > 0) {
            return new Date(Number(point.timestamp) * 1000).toLocaleString([], {
              month: 'short',
              day: 'numeric',
              hour: 'numeric',
              minute: '2-digit',
            });
          }
          return `Point ${opts.dataPointIndex + 1} of ${points.length}`;
        },
      },
      y: {
        formatter: value => `${fmtUsd(value)} · ${(meta.source === 'alpaca') ? `${timeframeLabel(meta.timeframe)} bars` : 'replay step'}`,
      },
    },
    annotations,
    states: {
      hover: {filter: {type: 'lighten', value: 0.05}},
      active: {filter: {type: 'none'}},
    },
    noData: {text: 'Not enough trade history for equity curve'},
  };
  if (_equityChart) {
    _equityChart.updateOptions(options, false, true, true);
    _equityChart.updateSeries([{name: 'Equity', data: series}], true);
    return true;
  }
  shell.innerHTML = '';
  _equityChart = new window.ApexCharts(shell, options);
  _equityChart.render();
  return true;
}
function renderEquityCurve(points, meta={}) {
  _lastEquityPayload = {points: Array.isArray(points) ? points.slice() : [], meta: meta || {}};
  if (window.ApexCharts) {
    renderEquityCurveApex(_lastEquityPayload.points, _lastEquityPayload.meta);
    return;
  }
  renderEquityCurveFallback(_lastEquityPayload.points, _lastEquityPayload.meta);
  ensureApexCharts().then(loaded => {
    if (loaded) renderEquityCurveApex(_lastEquityPayload.points, _lastEquityPayload.meta);
  });
}
function fmtUsd(v) {
  if (!(typeof v === 'number' && isFinite(v))) return '—';
  return v.toLocaleString(undefined, {style:'currency', currency:'USD', minimumFractionDigits:2, maximumFractionDigits:2});
}
function fmtUsdCompact(v) {
  if (!(typeof v === 'number' && isFinite(v))) return '—';
  return new Intl.NumberFormat(undefined, {
    style: 'currency',
    currency: 'USD',
    notation: 'compact',
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  }).format(v);
}
function timeframeLabel(value) {
  const text = String(value || '').trim();
  if (!text) return '—';
  return text
    .replace('Min', 'm')
    .replace('Hour', 'h')
    .replace('H', 'h')
    .replace('Day', 'd')
    .replace('D', 'd');
}
function formatEquityAxisLabel(value, meta={}) {
  const stamp = Number(value || 0);
  if (!(stamp > 0)) return '';
  const date = new Date(stamp);
  const period = String(meta.period || _equityRange || '1D').toUpperCase();
  if (period === '1D') {
    return date.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'});
  }
  if (period === '1W') {
    return date.toLocaleDateString([], {weekday: 'short', day: 'numeric'});
  }
  return date.toLocaleDateString([], {month: 'short', day: 'numeric'});
}
function describeEquityTimeline(firstTs, lastTs, meta={}) {
  const first = Number(firstTs || 0);
  const last = Number(lastTs || 0);
  if (!(first > 0) || !(last > 0)) return '';
  const period = String(meta.period || _equityRange || '1D').toUpperCase();
  const firstDate = new Date(first * 1000);
  const lastDate = new Date(last * 1000);
  if (period === '1D') {
    return `${firstDate.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})} → ${lastDate.toLocaleTimeString([], {hour: 'numeric', minute: '2-digit'})}`;
  }
  return `${firstDate.toLocaleDateString([], {month: 'short', day: 'numeric'})} → ${lastDate.toLocaleDateString([], {month: 'short', day: 'numeric'})}`;
}
function updateEquityRangeButtons() {
  document.querySelectorAll('#equityCurveRanges .equity-range-btn').forEach(btn => {
    const active = btn.dataset.range === _equityRange;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  });
}
function setEquityRange(range) {
  const next = String(range || '').toUpperCase();
  if (!_equityRangeConfig[next] || next === _equityRange) return;
  _equityRange = next;
  updateEquityRangeButtons();
  if ($('equityCurveMeta')) {
    $('equityCurveMeta').textContent = 'Loading range...';
  }
  refresh();
}
function fitTextToWidth(el, minSize=12, important=false) {
  if (!el) return;
  el.style.removeProperty('font-size');
  el.style.whiteSpace = 'nowrap';
  el.style.overflowWrap = 'normal';
  el.style.removeProperty('line-height');
  let size = parseFloat(getComputedStyle(el).fontSize || '16');
  while (el.scrollWidth > el.clientWidth && size > minSize) {
    size -= 0.5;
    if (important) el.style.setProperty('font-size', `${size}px`, 'important');
    else el.style.fontSize = `${size}px`;
  }
  if (el.scrollWidth > el.clientWidth) {
    el.style.whiteSpace = 'normal';
    el.style.overflowWrap = 'anywhere';
    el.style.lineHeight = '1.02';
  }
  return size;
}
function fitTextGroup(selector, minSize=12, options={}) {
  const {important=false, allowWrap=false} = options;
  const nodes = Array.from(document.querySelectorAll(selector)).filter(Boolean);
  if (!nodes.length) return;
  let groupSize = Infinity;
  nodes.forEach(el => {
    const measured = fitTextToWidth(el, minSize, important);
    if (typeof measured === 'number' && isFinite(measured)) groupSize = Math.min(groupSize, measured);
  });
  if (!isFinite(groupSize)) return;
  nodes.forEach(el => {
    if (important) el.style.setProperty('font-size', `${groupSize}px`, 'important');
    else el.style.fontSize = `${groupSize}px`;
    el.style.whiteSpace = 'nowrap';
    el.style.overflowWrap = 'normal';
    el.style.removeProperty('line-height');
  });
  if (allowWrap) {
    nodes.forEach(el => {
      if (el.scrollWidth > el.clientWidth) {
        el.style.whiteSpace = 'normal';
        el.style.overflowWrap = 'anywhere';
        el.style.lineHeight = '1.08';
      }
    });
  }
}
function fitPnlMetricValues() {
  fitTextGroup('#pnlMetrics .value', 11, {important:true});
  fitTextGroup('#pnlMetrics .label', 8.5, {allowWrap:true});
}
function toneForStatus(value) {
  const text = String(value || '').toLowerCase();
  if (!text) return 'neutral';
  if (['healthy','active','scale','running','enabled','ok','normal'].some(k => text.includes(k))) return 'good';
  if (['probation','observe','paused','warn','waiting','hold','minor'].some(k => text.includes(k))) return 'warn';
  if (['disable','disabled','critical','stopped','blocked','degraded','mismatch','broker_only'].some(k => text.includes(k))) return 'bad';
  return 'neutral';
}
function statusPill(text, tone='neutral') {
  return `<span class="status-pill ${tone}">${esc(text)}</span>`;
}
function formatAiNarrative(text, fallback='<em>Pending…</em>') {
  const value = String(text || '').trim();
  return value ? value.replace(/\n/g, '<br>') : fallback;
}
function aiBriefCardHtml(title, kicker, body, tone='') {
  return `<div class="ai-card ${tone}">
    <div class="ai-card-header">
      <div class="ai-card-kicker">${esc(kicker)}</div>
      <strong class="ai-card-title">${esc(title)}</strong>
    </div>
    <div class="ai-card-body">${formatAiNarrative(body)}</div>
  </div>`;
}
function insightStripHtml(title, subtitle, bodyHtml, tone='blue') {
  return `<div class="insight-strip tone-${tone}">
    <div class="insight-label">
      <strong>${esc(title)}</strong>
      <span>${esc(subtitle)}</span>
    </div>
    <div class="insight-body">${bodyHtml}</div>
  </div>`;
}
function renderProviderHealthGrid(providerHealth) {
  const rows = Object.entries(providerHealth || {});
  if (!rows.length) return '<span style="color:#9e8e76">No providers reporting.</span>';
  return `<div class="provider-health-grid">${rows.map(([name, st]) => {
    const ok = !!(st && st.ok);
    const latency = (st && typeof st.latency_ms === 'number') ? `${st.latency_ms}ms` : '—';
    const err = (st && st.error) ? String(st.error) : '';
    return `<div class="provider-pill ${ok ? 'ok' : 'fail'}">
      <div class="provider-name">${esc(name)}</div>
      <div class="provider-state">${ok ? 'Operational' : 'Degraded'}</div>
      <div class="provider-latency">${esc(latency)}</div>
      ${err ? `<div class="provider-detail">${esc(err)}</div>` : ''}
    </div>`;
  }).join('')}</div>`;
}
function briefItemHtml(title, sub, tone='neutral', pill='') {
  return `<div class="brief-item">
    <div class="brief-item-main">
      <div class="brief-item-title">${esc(title)}</div>
      <div class="brief-item-sub">${esc(sub)}</div>
    </div>
    ${pill ? statusPill(pill, tone) : ''}
  </div>`;
}
function renderBookScoreboard(rows) {
  if (!$('bookScoreboard') || !$('bookScoreSummary') || !$('bookScoreStats')) return;
  const books = Array.isArray(rows) ? rows.slice() : [];
  const scaleCount = books.filter(row => String(row.status || '').toLowerCase() === 'scale').length;
  const probationCount = books.filter(row => String(row.status || '').toLowerCase() === 'probation').length;
  const disabledCount = books.filter(row => String(row.status || '').toLowerCase() === 'disable').length;
  const positiveCount = books.filter(row => Number(row.expectancy || 0) > 0).length;
  const liveBooks = books.filter(row => Number(row.open_position_count || 0) > 0).length;
  $('bookScoreStats').textContent = `${books.length} books tracked`;
  $('bookScoreSummary').innerHTML = `
    <div class="summary-item"><div class="val info">${books.length}</div><div class="lbl">Books</div></div>
    <div class="summary-item"><div class="val positive">${scaleCount}</div><div class="lbl">Scale</div></div>
    <div class="summary-item"><div class="val">${liveBooks}</div><div class="lbl">Live</div></div>
    <div class="summary-item"><div class="val" style="color:#d4b07a">${positiveCount}</div><div class="lbl">Positive Expect.</div></div>
    <div class="summary-item"><div class="val negative">${probationCount}</div><div class="lbl">Probation</div></div>
    <div class="summary-item"><div class="val negative">${disabledCount}</div><div class="lbl">Disabled</div></div>
  `;
  $('bookScoreboard').innerHTML = books.length ? books.map(row => {
    const status = String(row.status || 'hold');
    const action = String(row.recommended_action || 'observe');
    const tone = toneForStatus(status);
    const realized = Number(row.realized_pnl || 0);
    const unrealized = Number(row.unrealized_pnl || 0);
    const pf = row.profit_factor == null ? '—' : Number(row.profit_factor).toFixed(2);
    const dd = row.max_drawdown == null ? '—' : fmt(-Math.abs(Number(row.max_drawdown || 0)));
    return `<tr>
      <td>
        <div class="book-name">
          <strong>${esc(humanizeKey(row.strategy_tag || 'unknown'))}</strong>
          <div class="book-sub">${esc(humanizeKey(action))}</div>
        </div>
      </td>
      <td>${statusPill(humanizeKey(status), tone)}</td>
      <td>${Number(row.open_position_count || 0)}<div class="book-sub">${esc(humanizeKey(row.control_state || 'active'))}</div></td>
      <td class="${cls(realized)}">${fmt(realized)}</td>
      <td class="${cls(unrealized)}">${fmt(unrealized)}</td>
      <td>${Number(row.trade_count || 0)}${Number(row.shadow_count || 0) > 0 ? `<div class="book-sub">${Number(row.shadow_count || 0)} shadow</div>` : ''}</td>
      <td>${Number(row.win_rate_pct || 0).toFixed(1)}%</td>
      <td class="${cls(Number(row.expectancy || 0))}">${fmt(Number(row.expectancy || 0))}</td>
      <td>${pf}</td>
      <td class="negative">${dd}</td>
      <td style="font-size:11px;color:#b5a792;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(row.status_reason || '')}">${esc(truncateText(row.status_reason || '—', 92))}</td>
    </tr>`;
  }).join('') : '<tr><td colspan="11" class="empty">No book analytics yet</td></tr>';
}
function renderOperatorDeck(payload) {
  if (!$('operatorLead')) return;
  const status = payload.status || {};
  const pnl = payload.pnl || {};
  const metrics = payload.metrics || {};
  const books = Array.isArray(payload.books) ? payload.books.slice() : [];
  const pending = Array.isArray(payload.pending) ? payload.pending : [];
  const positions = Array.isArray(payload.positions) ? payload.positions.slice() : [];
  const candidates = Array.isArray(payload.candidates) ? payload.candidates.slice() : [];
  const trust = pnl.trust_flags || status.trust_flags || metrics.trust_flags || {};
  const reconStatus = humanizeKey(pnl.reconciliation_status || status.reconciliation_status || 'unknown');
  const brokerOnly = !!trust.broker_only_mode;
  const degraded = !!trust.internal_analytics_degraded;
  const paused = !!status.paused;
  const running = !!status.running;
  const marketOpen = !!status.market_open;
  const hotBooks = books
    .filter(row => Number(row.trade_count || 0) > 0)
    .sort((a, b) => (Number(b.realized_pnl || 0) + Number(b.unrealized_pnl || 0)) - (Number(a.realized_pnl || 0) + Number(a.unrealized_pnl || 0)));
  const bestBook = hotBooks[0];
  const worstBook = hotBooks.slice().reverse().find(row => Number(row.realized_pnl || 0) < 0 || Number(row.expectancy || 0) < 0);
  const topPosition = positions.slice().sort((a, b) => Math.abs(Number(b.pnl || 0)) - Math.abs(Number(a.pnl || 0)))[0];
  const topCandidate = candidates.slice().sort((a, b) => Number(b.score || 0) - Number(a.score || 0))[0];
  const probationBooks = books.filter(row => ['probation','disable'].includes(String(row.status || '').toLowerCase()));
  const lead = !running
    ? 'The engine is stopped. This surface should feel like an investment committee console, not a trading terminal.'
    : paused
      ? 'Trading is paused. Use this session to review integrity, book posture, and the next live promotion decision.'
      : brokerOnly
        ? 'Broker truth is leading right now. Treat internal analytics cautiously until reconciliation settles.'
        : topPosition
          ? `${positions.length} live position${positions.length === 1 ? '' : 's'} on. Primary focus: ${topPosition.symbol} ${fmt(Number(topPosition.pnl || 0))} with ${humanizeKey(topPosition.protection || 'active protection')}.`
          : pending.length
            ? `${pending.length} setup${pending.length === 1 ? '' : 's'} waiting for trigger. The machine is patient, not idle.`
            : topCandidate
              ? `No live positions. Scanner leadership is ${topCandidate.symbol} with score ${(Number(topCandidate.score || 0)).toFixed(3)} and ${fmt(Number(topCandidate.change_pct || 0), 1)}% change.`
              : 'No urgent live risk. This is a clean window to review books, governance, and allocator posture.';
  $('operatorLead').textContent = lead;
  $('operatorPills').innerHTML = [
    statusPill(running ? (paused ? 'Paused' : 'Running') : 'Stopped', running ? (paused ? 'warn' : 'good') : 'bad'),
    statusPill(marketOpen ? 'Market Open' : 'Market Closed', marketOpen ? 'good' : 'neutral'),
    statusPill(reconStatus, toneForStatus(reconStatus)),
    brokerOnly ? statusPill('Broker Only', 'bad') : '',
    degraded ? statusPill('Internal Degraded', 'warn') : '',
    pending.length ? statusPill(`${pending.length} Pending`, 'warn') : '',
  ].join('');

  $('deckCapitalValue').textContent = fmtUsd(Number(pnl.equity || 0));
  $('deckCapitalValue').className = `deck-value ${cls(Number(pnl.total_pnl || 0))}`;
  $('deckCapitalSub').textContent = `${fmtUsd(Number(pnl.total_pnl || 0))} total · ${fmtUsd(Number(pnl.today_realized || pnl.broker_day_pnl || 0))} day · ROI ${(Number(pnl.roi_pct || 0)).toFixed(1)}%`;

  $('deckRiskValue').textContent = `${Number(metrics.heat_pct || 0).toFixed(0)}%`;
  $('deckRiskValue').className = `deck-value ${Number(metrics.heat_pct || 0) > 70 ? 'negative' : Number(metrics.heat_pct || 0) > 35 ? 'info' : 'positive'}`;
  $('deckRiskSub').textContent = `Drawdown ${(Number(pnl.drawdown_pct || 0)).toFixed(1)}% · ${Number(status.positions_count || positions.length || 0)} open · tier ${humanizeKey(metrics.tier_name || '?')}`;

  $('deckIntegrityValue').textContent = brokerOnly ? 'Broker-led' : degraded ? 'Caution' : 'Clean';
  $('deckIntegrityValue').className = `deck-value ${brokerOnly ? 'negative' : degraded ? 'info' : 'positive'}`;
  $('deckIntegritySub').textContent = `${reconStatus} · ${(trust.degraded_mode_reasons || []).length ? (trust.degraded_mode_reasons || []).slice(0,2).map(humanizeKey).join(' · ') : 'No active trust flags'}`;

  $('deckExecutionValue').textContent = positions.length ? `${positions.length} live` : pending.length ? `${pending.length} queued` : 'Standby';
  $('deckExecutionValue').className = `deck-value ${positions.length ? 'info' : pending.length ? 'positive' : 'info'}`;
  $('deckExecutionSub').textContent = topPosition
    ? `${topPosition.symbol} ${fmt(Number(topPosition.pnl_pct || 0))}% · ${humanizeKey(topPosition.best_play || topPosition.strategy_tag || 'active position')}`
    : topCandidate
      ? `${topCandidate.symbol} leads scanner · ${fmt(Number(topCandidate.change_pct || 0), 1)}%`
      : 'No immediate execution focus';

  const alerts = [];
  if (!running) alerts.push({title:'Engine stopped', sub:'Autonomous execution is offline until you resume the process.', tone:'bad', pill:'stop'});
  if (paused) alerts.push({title:'Trading paused', sub:'No fresh entries should be promoted while pause is active.', tone:'warn', pill:'pause'});
  if (brokerOnly) alerts.push({title:'Broker-only mode', sub:'Internal analytics are being suppressed in favor of broker truth.', tone:'bad', pill:'integrity'});
  else if (degraded) alerts.push({title:'Internal analytics degraded', sub:'Use the dashboard cautiously until reconciliation returns to healthy.', tone:'warn', pill:'integrity'});
  if (Number(pnl.drawdown_pct || 0) >= 2.5) alerts.push({title:'Drawdown elevated', sub:`Current drawdown is ${(Number(pnl.drawdown_pct || 0)).toFixed(1)}% from peak equity.`, tone:'bad', pill:'risk'});
  if (probationBooks.length) alerts.push({title:'Books on probation', sub:`${probationBooks.length} book${probationBooks.length === 1 ? '' : 's'} need tighter capital discipline.`, tone:'warn', pill:'books'});
  if (pending.length) alerts.push({title:'Pending setups waiting', sub:`${pending.length} setup${pending.length === 1 ? '' : 's'} remain in trigger-watch state.`, tone:'neutral', pill:'setups'});
  $('operatorAlerts').innerHTML = alerts.length
    ? alerts.slice(0, 4).map(row => briefItemHtml(row.title, row.sub, row.tone, row.pill)).join('')
    : '<div class="brief-empty">No urgent capital or integrity alerts. This is a stable operating window.</div>';

  const bookBriefs = [];
  if (bestBook) bookBriefs.push({title:humanizeKey(bestBook.strategy_tag), sub:`Leading realized ${fmt(Number(bestBook.realized_pnl || 0))} · expectancy ${fmt(Number(bestBook.expectancy || 0))}`, tone:toneForStatus(bestBook.status || 'scale'), pill:humanizeKey(bestBook.status || 'hold')});
  if (worstBook) bookBriefs.push({title:humanizeKey(worstBook.strategy_tag), sub:`Weakest posture ${fmt(Number(worstBook.realized_pnl || 0))} · expectancy ${fmt(Number(worstBook.expectancy || 0))}`, tone:'bad', pill:humanizeKey(worstBook.status || 'hold')});
  probationBooks.slice(0,2).forEach(row => {
    bookBriefs.push({title:humanizeKey(row.strategy_tag), sub:truncateText(row.status_reason || 'Capital should remain constrained until evidence improves.', 92), tone:toneForStatus(row.status || 'probation'), pill:humanizeKey(row.status || 'probation')});
  });
  $('operatorBooks').innerHTML = bookBriefs.length
    ? bookBriefs.slice(0,4).map(row => briefItemHtml(row.title, row.sub, row.tone, row.pill)).join('')
    : '<div class="brief-empty">Book analytics will show up here once the trade history has enough evidence.</div>';

  const focusItems = positions.length
    ? positions
        .slice()
        .sort((a, b) => Math.abs(Number(b.pnl || 0)) - Math.abs(Number(a.pnl || 0)))
        .slice(0, 4)
        .map(row => ({
          title:`${row.symbol} · ${fmt(Number(row.pnl || 0))}`,
          sub:`${humanizeKey(row.best_play || row.strategy_tag || 'live position')} · ${row.protection || 'protection unknown'} · hold ${row.hold_time || '—'}`,
          tone:Number(row.pnl || 0) >= 0 ? 'good' : 'bad',
          pill:`${fmt(Number(row.pnl_pct || 0))}%`,
        }))
    : candidates.slice(0, 4).map(row => ({
        title:`${row.symbol} · score ${(Number(row.score || 0)).toFixed(3)}`,
        sub:`${fmt(Number(row.change_pct || 0),1)}% change · ${Number(row.volume_spike || 0).toFixed(1)}x volume · ${row.uw_chain_summary || row.uw_news_summary || 'scanner leadership'}`,
        tone:'neutral',
        pill:humanizeKey(row.strategy_tag || 'candidate'),
      }));
  $('operatorFocus').innerHTML = focusItems.length
    ? focusItems.map(row => briefItemHtml(row.title, row.sub, row.tone, row.pill)).join('')
    : '<div class="brief-empty">No live positions and no standout candidates yet.</div>';
}

async function refresh() {
  try {
  let _trustFlags = {};
  let _brokerOnlyMode = false;
  let _degradedInternal = false;
  let statusPayload = null;
  let pnlPayload = null;
  let metricsPayload = null;
  let bookScorePayload = null;
  // Status
  const s = await api('/api/status');
  statusPayload = s;
  if (s) {
    const b = $('statusBadge'), dot = $('scanDot');
    if (!s.running) { b.textContent='STOPPED'; b.className='badge stopped'; dot.className='scan-dot idle'; }
    else if (s.paused) { b.textContent='PAUSED'; b.className='badge paused'; dot.className='scan-dot idle'; }
    else { b.textContent='RUNNING'; b.className='badge running'; dot.className='scan-dot'; }
  }
  // P&L Terminal
  const pnl = await api('/api/pnl');
  pnlPayload = pnl;
  if (pnl) {
    const humanizeReason = (reason) => {
      const map = {
        broker_position_missing_internal: 'broker position missing from internal state',
        broker_symbols_missing_from_internal: 'broker activity missing from internal history',
        broker_activity_missing_internal_history: 'broker activity missing from internal history',
        broker_fill_ledger_unresolved: 'broker fill ledger unresolved for carryover basis',
        broker_truth_canary_triggered: 'broker-truth canary triggered',
        carryover_gap: 'overnight carryover gap detected',
        internal_position_missing_broker: 'internal position missing from broker state',
        internal_closed_trade_subset_only: 'internal analytics only reflect a trade subset',
        internal_ledgers_diverge: 'internal ledgers disagree',
        internal_symbols_missing_from_broker_day_bundle: 'internal history missing matching broker day activity',
        overnight_carryover_gap: 'overnight carryover gap detected',
        position_qty_mismatch: 'position quantity mismatch',
        realized_pnl_mismatch: 'realized P&L mismatch',
        residual_position_drift: 'residual position drift detected',
        broker_history_unavailable: 'broker portfolio history unavailable',
      };
      const key = String(reason || '').trim();
      if (!key) return '';
      return map[key] || key.replaceAll('_', ' ');
    };
    const setPnl = (id, val, prefix='$') => {
      const el = $(id);
      if (!el) return;
      el.textContent = prefix + (typeof val === 'number' ? val.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : val);
      el.className = 'value' + (typeof val === 'number' && val < 0 ? ' negative' : typeof val === 'number' && val > 0 ? ' positive' : '');
    };
    const setDegradedMetric = (id, text='—') => {
      const el = $(id);
      if (!el) return;
      el.textContent = text;
      el.className = 'value muted';
    };
    const trust = pnl.trust_flags || {};
    _trustFlags = trust;
    const brokerOnly = !!trust.broker_only_mode;
    const degradedInternal = !!trust.internal_analytics_degraded;
    _brokerOnlyMode = brokerOnly;
    _degradedInternal = degradedInternal;
    $('totalPnl').textContent = (pnl.total_pnl || 0).toLocaleString(undefined, {
      style: 'currency',
      currency: 'USD',
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    });
    $('totalPnl').className = 'value big-pnl ' + cls(pnl.total_pnl||0);
    setPnl('equity', pnl.equity||0);
    setPnl('todayPnl', pnl.today_realized||0);
    setPnl('unrealized', pnl.unrealized||0);
    $('roi').textContent = (pnl.roi_pct||0).toFixed(1) + '%';
    $('roi').className = 'value ' + cls(pnl.roi_pct||0);
    $('drawdown').textContent = (pnl.drawdown_pct||0).toFixed(1) + '%';
    $('drawdown').className = 'value' + ((pnl.drawdown_pct||0) > 2 ? ' negative' : '');
    if (brokerOnly) {
      setDegradedMetric('winRate', 'DEGRADED');
      setDegradedMetric('totalTrades', '—');
      setDegradedMetric('bestTrade', '—');
      setDegradedMetric('worstTrade', '—');
      setDegradedMetric('avgLatency', '—');
    } else {
      $('winRate').textContent = (pnl.internal_win_rate_pct ?? pnl.win_rate ?? 0).toFixed(0) + '%';
      $('winRate').className = 'value' + (degradedInternal ? ' muted' : ((pnl.internal_win_rate_pct ?? pnl.win_rate ?? 0) >= 50 ? ' positive' : (pnl.total_trades > 0 ? ' negative' : '')));
      $('totalTrades').textContent = pnl.internal_trade_count ?? pnl.total_trades ?? 0;
      $('totalTrades').className = 'value' + (degradedInternal ? ' muted' : ' info');
      $('bestTrade').textContent = '$' + (pnl.best_trade||0).toFixed(2);
      $('bestTrade').className = 'value' + (degradedInternal ? ' muted' : ' positive');
      $('worstTrade').textContent = '$' + (pnl.worst_trade||0).toFixed(2);
      $('worstTrade').className = 'value' + (degradedInternal ? ' muted' : ' negative');
      $('avgLatency').textContent = (typeof pnl.avg_signal_to_fill_ms === 'number')
        ? `${Math.round(pnl.avg_signal_to_fill_ms)}ms`
        : '—';
      $('avgLatency').className = 'value' + (degradedInternal ? ' muted' : ' info');
    }
    const reconBanner = $('reconBanner');
    if (reconBanner) {
      const status = pnl.reconciliation_status || 'unknown';
      const reasons = (pnl.reconciliation_reasons || []).map(humanizeReason).filter(Boolean);
      const canaries = (pnl.reconciliation_canaries || []).slice(0, 3);
      if (status && status !== 'healthy' && status !== 'minor_mismatch') {
        reconBanner.style.display = 'block';
        const shownReasons = canaries.length
          ? canaries.map((c) => humanizeReason(c.code))
          : reasons.slice(0, 3);
        const extraCount = Math.max(0, (canaries.length || reasons.length) - shownReasons.length);
        reconBanner.innerHTML = `<strong>Reconciliation warning</strong>`
          + `<span>Broker reconciliation is <b>${String(status).replaceAll('_', ' ')}</b>. `
          + `${brokerOnly ? 'Broker state only; internal analytics are suppressed.' : 'Internal analytics are degraded.'}</span>`
          + (shownReasons.length
              ? `<span class="muted"> Top causes: ${shownReasons.join(', ')}${extraCount ? ` +${extraCount} more` : ''}.</span>`
              : '');
      } else {
        reconBanner.style.display = 'none';
        reconBanner.innerHTML = '';
      }
    }
    $('pnlTimestamp').textContent = 'Updated: ' + new Date().toLocaleTimeString()
      + ` | Opt R/U: $${(pnl.options_realized_pnl||0).toFixed(2)} / $${(pnl.options_unrealized_pnl||0).toFixed(2)}`
      + (brokerOnly ? ' | Internal realized: suppressed' : ` | Internal realized: $${(pnl.internal_realized_pnl||0).toFixed(2)}`);
    requestAnimationFrame(fitPnlMetricValues);
  }
  // Equity curve
  const equityRangeCfg = _equityRangeConfig[_equityRange] || _equityRangeConfig['1D'];
  const ec = await api(`/api/equity-curve?limit=${equityRangeCfg.limit}&period=${encodeURIComponent(_equityRange)}`);
  if (ec) {
    const pts = ec.points || [];
    const latest = pts.length ? pts[pts.length - 1] : null;
    const first = pts.length ? pts[0] : null;
    const startEquity = Number(ec.starting_equity || 0);
    const latestEquity = Number(latest?.equity || 0);
    const deltaEquity = latestEquity - startEquity;
    const deltaPct = startEquity ? (deltaEquity / startEquity) * 100 : 0;
    const deltaLabel = `${deltaEquity >= 0 ? '+' : '-'}${fmtUsd(Math.abs(deltaEquity))}`;
    const pointLabel = (ec.source || '') === 'alpaca' ? 'bars' : 'steps';
    const timelineLabel = describeEquityTimeline(
      ec.first_timestamp || first?.timestamp,
      ec.last_timestamp || latest?.timestamp,
      ec,
    );
    const sourceLabel = ec.source ? String(ec.source).toUpperCase() : '—';
    const granularityLabel = (ec.source || '') === 'alpaca' ? timeframeLabel(ec.timeframe) : 'replay';
    $('equityCurveStats').textContent = `${String(ec.period || _equityRange).toUpperCase()} · ${granularityLabel} · ${sourceLabel}`;
    $('equityCurveMeta').textContent = latest
      ? `${timelineLabel || 'Recent history'} · ${pts.length || 0} ${pointLabel} · Start ${fmtUsd(startEquity)} → ${fmtUsd(latestEquity)} · ${deltaLabel} (${deltaPct >= 0 ? '+' : ''}${deltaPct.toFixed(2)}%)`
      : 'No completed trades yet';
    renderEquityCurve(pts, {
      startingEquity: startEquity,
      source: ec.source || '',
      count: ec.count || pts.length,
      period: ec.period || _equityRange,
      timeframe: ec.timeframe || '',
      firstTimestamp: ec.first_timestamp || first?.timestamp || null,
      lastTimestamp: ec.last_timestamp || latest?.timestamp || null,
    });
  }
  // Metrics
  const m = await api('/api/metrics');
  metricsPayload = m;
  if (m) {
    if (m.trust_flags) { _trustFlags = m.trust_flags; _brokerOnlyMode = !!_trustFlags.broker_only_mode; _degradedInternal = !!_trustFlags.internal_analytics_degraded; }
    const pnlChanged = _prevPnl !== null && _prevPnl !== (m.daily_pnl||0);
    _prevPnl = m.daily_pnl||0;
    const anim = pnlChanged ? ' animated' : '';
    const metricMuted = _degradedInternal ? ' muted' : '';
    $('metrics').innerHTML = `
      <div class="metric"><div class="value" style="color:#d2a8ff">${m.tier_name||'?'}</div><div class="label">Risk Tier</div></div>
      <div class="metric"><div class="value${metricMuted} ${_degradedInternal ? "" : (m.swing_mode?'negative':'positive')}">${m.swing_mode?'SWING':'NORMAL'}</div><div class="label">Mode</div></div>
      <div class="metric"><div class="value${metricMuted}">${m.remaining_day_trades??'—'}</div><div class="label">Day Trades</div></div>
      <div class="metric"><div class="value${metricMuted}">${m.consecutive_wins||0}W/${m.consecutive_losses||0}L</div><div class="label">Streak</div></div>
      <div class="metric"><div class="value${metricMuted}">${(m.heat_pct||0).toFixed(0)}%</div><div class="label">Heat</div></div>
      <div class="metric"><div class="value${metricMuted}">${(m.tier_size_pct||0)}%</div><div class="label">Pos Size</div></div>
      <div class="metric"><div class="value${metricMuted}">${m.tier_max_positions||0}</div><div class="label">Max Pos</div></div>
      <div class="metric"><div class="value${metricMuted}">$${(m.ath_equity||0).toLocaleString()}</div><div class="label">ATH</div></div>
      <div class="metric"><div class="value${metricMuted}">$${(m.next_milestone||0).toLocaleString()}</div><div class="label">Next Milestone</div></div>
      <div class="metric"><div class="value${metricMuted} ${_degradedInternal ? "" : "info"}">${(m.milestone_progress_pct||0).toFixed(0)}%</div><div class="label">→ Progress</div></div>
      <div class="metric"><div class="value${metricMuted} ${_degradedInternal ? "" : cls(m.total_return_pct||0)}">${(m.total_return_pct||0).toFixed(1)}%</div><div class="label">Total Return</div></div>
      <div class="metric"><div class="value${metricMuted}">${(m.days_trading||0).toFixed(0)}d</div><div class="label">Days</div></div>
    `;
  }
  // AI Status
  const ai = await api('/api/ai-status');
  if (ai) {
    $('aiEnabled').innerHTML = ai.enabled ? statusPill('Active', 'good') : statusPill('Disabled', 'bad');
    if (ai.enabled) {
      let html = '<div class="ai-grid">';
      html += aiBriefCardHtml('Observer', 'Market Read', ai.last_observation, 'observer');
      html += aiBriefCardHtml('Advisor', 'Capital Posture', ai.last_advice, 'advisor');
      html += aiBriefCardHtml('Tuner', 'Parameter Changes', ai.last_tuner_changes || '<em>No changes yet</em>', 'tuner');
      html += aiBriefCardHtml('Position Manager', 'Live Oversight', ai.last_position_manager, 'pm');
      html += '</div>';
      html += '<div class="insight-stack">';
      if (ai.overnight_bias_summary) html += insightStripHtml('Overnight Bias', 'Macro / Session', formatAiNarrative(ai.overnight_bias_summary), 'blue');
      if (ai.provider_health && Object.keys(ai.provider_health).length) {
        html += insightStripHtml('Provider Health', 'Models / Runtime', renderProviderHealthGrid(ai.provider_health), 'green');
      }
      if (ai.last_game_film) html += insightStripHtml('Game Film', 'Historical Review', formatAiNarrative(ai.last_game_film), 'violet');
      if (ai.short_verdicts_blocked) html += insightStripHtml('Short Blocks', 'Execution Friction', `${ai.short_verdicts_blocked}${ai.last_short_block_reason ? ' · ' + esc(ai.last_short_block_reason) : ''}`, 'rose');
      html += '</div>';
      $('aiStatus').innerHTML = _degradedInternal
        ? insightStripHtml('Internal Caution', 'Reconciliation State', 'Internal AI summaries are currently degraded by reconciliation state.', 'amber') + html
        : html;
    } else { $('aiStatus').innerHTML = '<span class="empty">AI layers not initialized</span>'; }
  }
  // Consensus
  const con = await api('/api/consensus');
  if (con) {
    if (con.trust_flags) { _trustFlags = con.trust_flags; _brokerOnlyMode = !!_trustFlags.broker_only_mode; _degradedInternal = !!_trustFlags.internal_analytics_degraded; }
    const st = con.stats || {};
    const ac = st.api_calls || {};
    const history = Array.isArray(con.history) ? con.history.slice().reverse() : [];
    const enterNowCount = history.filter(h => String(h.timing_state || '').toLowerCase() === 'enter_now').length;
    const waitCount = history.filter(h => String(h.timing_state || '').toLowerCase() === 'wait_for_trigger').length;
    const blockedCount = history.filter(h => ['data_insufficient','mode_conflict','capital_blocked','broker_blocked','shadow_only','no_edge'].includes(String(h.timing_state || '').toLowerCase())).length;
    const latestConsensus = con.last_consensus || history[0] || {};
    $('consensusStats').textContent = con.enabled ? `${st.total||0} evaluations${_brokerOnlyMode ? ' | degraded' : _degradedInternal ? ' | internal degraded' : ''}` : 'Disabled';
    $('consensusSummary').innerHTML = con.enabled ? `
      <div class="summary-item"><div class="val info">${st.total||0}</div><div class="lbl">Evals</div></div>
      <div class="summary-item"><div class="val positive">${enterNowCount}</div><div class="lbl">Enter Now</div></div>
      <div class="summary-item"><div class="val" style="color:#e3b341">${waitCount}</div><div class="lbl">Wait</div></div>
      <div class="summary-item"><div class="val negative">${blockedCount}</div><div class="lbl">Blocked</div></div>
      <div class="summary-item" title="${con.last_short_block_reason||''}"><div class="val negative">${con.short_verdicts_blocked||0}</div><div class="lbl">Short Blocked</div></div>
      <div class="summary-item"><div class="val val-sm info">${esc(humanizeKey(latestConsensus.setup_mode || '—'))}</div><div class="lbl">Latest Mode</div></div>
      <div class="summary-item"><div class="val val-sm" style="color:#d2a8ff">${esc(humanizeKey(latestConsensus.best_play || '—'))}</div><div class="lbl">Latest Play</div></div>
      <div class="summary-item"><div class="val" style="color:#e3b341">${(st.actionable_avg_confidence ?? st.avg_confidence)?(st.actionable_avg_confidence ?? st.avg_confidence).toFixed(0)+'%':'—'}</div><div class="lbl">Action Conf</div></div>
      <div class="summary-item"><div class="val val-sm" style="color:#8b949e">C ${ac.claude||0} · G ${ac.gpt||0} · X ${ac.grok||0} · P ${ac.perplexity||0}</div><div class="lbl">API Calls</div></div>
    ` : '';
    $('consensus').innerHTML = history.length ? history.map(h => {
      const decCls = verdictTagClass(h.decision);
      const timingState = String(h.timing_state || '').toLowerCase() || 'unknown';
      const play = h.best_play || '—';
      const mode = h.setup_mode || '—';
      const triggerBits = [
        h.trigger || h.no_trade_reason || '',
        h.invalidation ? `invalidates on ${h.invalidation}` : '',
      ].filter(Boolean);
      const reason = truncateText(triggerBits.join(' · ') || h.reasoning || '—', 120);
      const votes = h.consensus_detail && h.consensus_detail.votes ? Object.entries(h.consensus_detail.votes).map(([k, v]) => `${k}:${v}`).join(' · ') : '';
      const cd = h.consensus_detail || {};
      const unavailable = Array.isArray(cd.unavailable_providers) ? cd.unavailable_providers : [];
      const rateLimited = Array.isArray(cd.rate_limited_providers) ? cd.rate_limited_providers : [];
      const providerIssues = [
        rateLimited.length ? `rate-limited: ${rateLimited.join(', ')}` : '',
        unavailable.filter(p => !rateLimited.includes(p)).length ? `missing: ${unavailable.filter(p => !rateLimited.includes(p)).join(', ')}` : '',
      ].filter(Boolean).join(' · ');
      return `<tr><td><strong>${h.symbol}</strong><div class="subtle mono">${esc(h.provider_used || '')}</div></td>
        <td><span class="tag ${decCls}">${h.decision}</span></td>
        <td><div>${esc(humanizeKey(play))}</div><div class="subtle">${esc(h.hold_style || '')}</div></td>
        <td><div>${esc(humanizeKey(mode))}</div><div class="subtle">${esc(h.direction_constraint || 'none')}</div></td>
        <td><span class="tag ${timingTagClass(timingState)}">${esc(humanizeKey(timingState))}</span></td>
        <td>${(h.confidence||0).toFixed(0)}%</td>
        <td>${(h.size_pct||0).toFixed(1)}%<div class="subtle">${esc(h.size_posture || 'normal')}</div></td>
        <td style="font-size:11px;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc((h.reasoning||'') + ' | ' + (h.trigger||'') + ' | ' + (h.invalidation||''))}">${esc(reason)}${votes ? `<div style="color:#8b949e;margin-top:4px">${esc(votes)}</div>` : ''}${providerIssues ? `<div style="color:#f0883e;margin-top:4px">${esc(providerIssues)}</div>` : ''}</td></tr>`;
    }).join('') : '<tr><td colspan="8" class="empty">No agent decisions yet</td></tr>';
  }
  // Setup resolver
  const pendingPayload = await api('/api/pending-setups?limit=12');
  const entryControls = await api('/api/entry-controls?limit=12');
  const modeReport = await api('/api/mode-report');
  const replaySymbol = pickReplaySymbol(con?.last_consensus, pendingPayload, entryControls);
  const replay = replaySymbol ? await api(`/api/setup-replay?symbol=${encodeURIComponent(replaySymbol)}&limit=80`) : null;
  if ($('setupOpsStats')) {
    const pendingCount = pendingPayload?.count || 0;
    const lockCount = (entryControls?.loss_locks || []).length;
    $('setupOpsStats').textContent = `${pendingCount} pending · ${lockCount} locks`;
  }
  if ($('setupOpsSummary')) {
    const mr = modeReport || {};
    $('setupOpsSummary').innerHTML = `
      <div class="summary-item"><div class="val info">${mr.setup_count||0}</div><div class="lbl">Setups</div></div>
      <div class="summary-item"><div class="val" style="color:#e3b341">${mr.pending_setups_created||0}</div><div class="lbl">Pending</div></div>
      <div class="summary-item"><div class="val positive">${mr.pending_setups_triggered||0}</div><div class="lbl">Triggered</div></div>
      <div class="summary-item"><div class="val negative">${mr.pending_setups_expired||0}</div><div class="lbl">Expired</div></div>
      <div class="summary-item"><div class="val info">${mr.entries_with_pending_setup||0}</div><div class="lbl">Entered</div></div>
      <div class="summary-item"><div class="val negative">${mr.entries_without_pending_setup||0}</div><div class="lbl">Bypassed</div></div>
      <div class="summary-item"><div class="val" style="color:#d2a8ff">${mr.mode_flip_count||0}</div><div class="lbl">Mode Flips</div></div>
      <div class="summary-item"><div class="val ${cls((replay?.summary?.net_pnl)||0)}">${fmt((replay?.summary?.net_pnl)||0)}</div><div class="lbl">Replay P&L</div></div>
    `;
  }
  if ($('pendingSetups')) {
    const setups = pendingPayload?.setups || [];
    $('pendingSetups').innerHTML = setups.length ? setups.map(row => {
      const primaryPlay = humanizeKey(row.best_play || row.mode || '—');
      const secondaryMode = humanizeKey(row.mode || '');
      const showSecondary = secondaryMode && secondaryMode !== primaryPlay;
      const expiresLabel = fmtClock(row.expires_at);
      return `
        <div class="pending-setup-row">
          <div class="pending-setup-symbol">
            <strong>${esc(row.symbol || '?')}</strong>
            ${row.setup_id ? `<div class="pending-setup-id mono">${esc(compactSetupId(row.setup_id))}</div>` : ''}
          </div>
          <div class="pending-setup-play">
            <div class="pending-setup-play-title">${esc(primaryPlay)}</div>
            ${showSecondary ? `<div class="pending-setup-play-sub">${esc(secondaryMode)}</div>` : ''}
          </div>
          <div class="pending-setup-state">
            <span class="tag ${timingTagClass(row.timing_state || 'wait_for_trigger')}" title="${esc(humanizeKey(row.timing_state || 'wait_for_trigger'))}">${esc(compactTimingLabel(row.timing_state || 'wait_for_trigger'))}</span>
          </div>
          <div class="pending-setup-trigger">
            <div class="pending-setup-trigger-main" title="${esc(row.trigger || '')}">${esc(row.trigger || '—')}</div>
            <div class="pending-setup-trigger-meta">${expiresLabel !== '—' ? `Expires ${esc(expiresLabel)}` : 'No expiry posted'}</div>
          </div>
        </div>
      `;
    }).join('') : '<div class="empty">No waiting setups</div>';
  }
  if ($('modeReport')) {
    const rollup = Object.entries(modeReport?.mode_rollup || {}).sort((a, b) => (b[1]?.setups||0) - (a[1]?.setups||0));
    const expectancy = modeReport?.executed_trades_by_mode || {};
    $('modeReport').innerHTML = rollup.length ? rollup.map(([mode, row]) => `
      <tr>
        <td><strong>${esc(humanizeKey(mode))}</strong></td>
        <td>${row.setups||0}<div class="subtle">pending ${row.pending||0}</div></td>
        <td>${row.entered||0}</td>
        <td>${row.expired||0}<div class="subtle">miss ${row.trigger_misses||0}</div></td>
        <td class="${cls(row.trade_pnl||0)}">${fmt(row.trade_pnl||0)}</td>
        <td class="${cls(expectancy?.[mode]?.expectancy || 0)}">${expectancy?.[mode]?.expectancy != null ? fmt(expectancy[mode].expectancy) : '—'}</td>
      </tr>
    `).join('') : '<tr><td colspan="6" class="empty">No mode data yet</td></tr>';
  }
  if ($('entryLocks')) {
    const locks = entryControls?.loss_locks || [];
    const states = entryControls?.trade_states || [];
    if (locks.length) {
      $('entryLocks').innerHTML = locks.map(row => `
        <tr>
          <td><strong>${esc(row.symbol || '?')}</strong></td>
          <td><span class="tag tag-lock">LOCKED</span></td>
          <td>${row.consecutive_losses||0}</td>
          <td>${fmtRelativeSeconds(row.seconds_remaining)}</td>
          <td style="font-size:11px;color:#8b949e">${esc(humanizeKey(row.last_trade_reason || row.reason || '—'))}</td>
        </tr>
      `).join('');
    } else {
      $('entryLocks').innerHTML = states.length ? states.slice(0, 8).map(row => `
        <tr>
          <td><strong>${esc(row.symbol || '?')}</strong></td>
          <td><span class="tag ${row.active_lock ? 'tag-lock' : 'tag-live'}">${row.active_lock ? 'LOCKED' : 'OPEN'}</span></td>
          <td>${row.consecutive_losses||0}<div class="subtle">${row.consecutive_wins||0}W</div></td>
          <td>${row.active_lock ? fmtClock(row.active_lock.expires_at) : fmtClock(row.last_exit_time)}</td>
          <td style="font-size:11px;color:#8b949e">${esc(humanizeKey(row.last_reason || row.last_outcome || '—'))}</td>
        </tr>
      `).join('') : '<tr><td colspan="5" class="empty">No symbol locks or streaks yet</td></tr>';
    }
  }
  if ($('setupReplayTitle') && $('setupReplayTimeline')) {
    const summaryBits = replay?.summary ? [
      `${replay.summary.setup_count||0} setups`,
      `${replay.summary.trade_count||0} trades`,
      `${replay.summary.mode_flip_count||0} flips`,
    ] : [];
    $('setupReplayTitle').textContent = replaySymbol ? `Setup Replay · ${replaySymbol}` : 'Setup Replay';
    const timeline = Array.isArray(replay?.timeline) ? replay.timeline.slice(-8).reverse() : [];
    const trades = Array.isArray(replay?.trades) ? replay.trades.slice(-3).reverse() : [];
    const transitions = Array.isArray(replay?.mode_transitions) ? replay.mode_transitions.slice(-3).reverse() : [];
    const events = [
      ...timeline.map(row => ({ts: row.recorded_at, kind: 'snapshot', row})),
      ...trades.map(row => ({ts: row.exit_time || row.entry_time, kind: 'trade', row})),
      ...transitions.map(row => ({ts: row.recorded_at, kind: 'transition', row})),
    ].sort((a, b) => (b.ts || 0) - (a.ts || 0));
    $('setupReplayTimeline').innerHTML = events.length ? events.map(event => {
      if (event.kind === 'trade') {
        const row = event.row;
        return `<div class="timeline-item">
          <div><strong>${esc(row.symbol || '?')}</strong> closed <span class="${cls(row.pnl||0)}">${fmt(row.pnl||0)}</span> <span class="subtle">(${fmt(row.pnl_pct||0)}%)</span></div>
          <div class="subtle">${esc(humanizeKey(row.setup_mode || 'invalid'))} · ${esc(row.reason || '—')} · hold ${holdStr(row.hold_seconds)}</div>
        </div>`;
      }
      if (event.kind === 'transition') {
        const row = event.row;
        return `<div class="timeline-item">
          <div><strong>${esc(row.symbol || '?')}</strong> mode flip</div>
          <div class="subtle">${esc(humanizeKey(row.from_mode || 'invalid'))} → ${esc(humanizeKey(row.to_mode || 'invalid'))} at ${fmtClock(row.recorded_at)}</div>
        </div>`;
      }
      const row = event.row;
      return `<div class="timeline-item">
        <div><strong>${esc(row.symbol || '?')}</strong> <span class="tag ${timingTagClass(row.timing_state || 'mode_conflict')}">${esc(humanizeKey(row.timing_state || 'mode_conflict'))}</span> <span class="subtle">${fmtClock(row.recorded_at)}</span></div>
        <div class="subtle">${esc(humanizeKey(row.setup_mode || 'invalid'))} · ${esc(humanizeKey(row.best_play || '—'))}</div>
        <div>${esc(truncateText(row.trigger || row.no_trade_reason || '—', 110))}</div>
        <div class="subtle">trigger ${row.trigger_live === true ? 'live' : row.trigger_live === false ? 'not live' : 'unknown'} · clf ${Number(row.classifier_confidence||0).toFixed(2)} · res ${Number(row.resolver_confidence||0).toFixed(2)}</div>
      </div>`;
    }).join('') + (summaryBits.length ? `<div class="timeline-item"><div class="subtle">${esc(summaryBits.join(' · '))}</div></div>` : '') : '<div class="empty">No setup replay yet</div>';
  }
  // Trade History
  const th = await api('/api/trade-history?limit=20');
  if (th) {
    if (th.trust_flags) { _trustFlags = th.trust_flags; _brokerOnlyMode = !!_trustFlags.broker_only_mode; _degradedInternal = !!_trustFlags.internal_analytics_degraded; }
    const s = th.stats?.overall || {};
    const best = th.best, worst = th.worst;
    const bestStrategy = topPnlBucket(th.stats?.by_strategy_tag);
    const bestSource = topPnlBucket(th.stats?.by_signal_source);
    $('tradeStats').textContent = `${s.wins||0}W / ${s.losses||0}L`;
    $('tradeSummary').innerHTML = th.trades.length ? `
      <div class="summary-item"><div class="val info">${th.stats?.total_trades||0}</div><div class="lbl">Trades</div></div>
      <div class="summary-item"><div class="val ${(s.win_rate_pct||0)>=50?'positive':'negative'}">${(s.win_rate_pct||0).toFixed(1)}%</div><div class="lbl">Win Rate</div></div>
      <div class="summary-item"><div class="val ${cls(th.broker_total_pnl!=null?th.broker_total_pnl:(s.total_pnl||0))}">${fmt(th.broker_total_pnl!=null?th.broker_total_pnl:(s.total_pnl||0))}</div><div class="lbl">Total P&L</div></div>
      <div class="summary-item"><div class="val positive">${best?fmt(best.pnl||0):'—'}</div><div class="lbl">${best?best.symbol+' Best':'Best'}</div></div>
      <div class="summary-item"><div class="val negative">${worst?fmt(worst.pnl||0):'—'}</div><div class="lbl">${worst?worst.symbol+' Worst':'Worst'}</div></div>
      <div class="summary-item" title="${bestStrategy?bestStrategy.name:''}"><div class="val val-sm ${bestStrategy&&bestStrategy.pnl>=0?'positive':'negative'}">${bestStrategy?humanizeKey(bestStrategy.name):'—'}</div><div class="lbl">Top Strategy</div></div>
      <div class="summary-item" title="${bestSource?bestSource.name:''}"><div class="val val-sm ${bestSource&&bestSource.pnl>=0?'positive':'negative'}">${bestSource?humanizeKey(bestSource.name):'—'}</div><div class="lbl">Top Source</div></div>
      <div class="summary-item"><div class="val info">${typeof s.avg_signal_to_fill_ms==='number'?Math.round(s.avg_signal_to_fill_ms)+'ms':'—'}</div><div class="lbl">Avg Sig→Fill</div></div>
    ` : '';
    const tradeHistoryHtml = th.trades.length ? th.trades.slice().reverse().map(t => `<tr style="${_degradedInternal ? 'opacity:0.65' : ''}">
      <td><strong>${t.symbol||'?'}</strong></td>
      <td>$${(t.entry_price||0).toFixed(2)}</td><td>$${(t.exit_price||0).toFixed(2)}</td>
      <td class="${cls(t.pnl||0)}"><strong>${fmt(t.pnl||0)}</strong></td>
      <td class="${cls(t.pnl_pct||0)}">${fmt(t.pnl_pct||0)}%</td>
      <td>${t.reason||'—'}</td><td>${holdStr(t.hold_seconds)}</td>
      <td>${t.strategy_tag||'—'}</td>
      <td>${t.signal_tier||'—'}</td>
      <td>${typeof t.ratchet_floor_pct === 'number' ? fmt(t.ratchet_floor_pct,1)+'%' : '—'}</td>
      <td style="font-size:11px;color:#8b949e">${Array.isArray(t.signal_sources)?t.signal_sources.join(', '):(t.signal_sources||'—')}</td>
      <td class="${(t.slippage_bps||0) > 0 ? 'negative' : 'positive'}">${fmt(t.slippage_bps||0, 1)}</td>
      <td class="info">${typeof t.signal_to_fill_ms==='number'?Math.round(t.signal_to_fill_ms):'—'}</td>
    </tr>`).join('') : '<tr><td colspan="13" class="empty">No completed trades yet</td></tr>';
    $('tradeHistory').innerHTML = (_degradedInternal ? '<tr><td colspan="13" class="empty" style="color:#8b949e">Internal trade analytics degraded by reconciliation state</td></tr>' : '') + tradeHistoryHtml;
  }
  bookScorePayload = await api('/api/book-scoreboard');
  renderBookScoreboard((bookScorePayload && bookScorePayload.books) || []);
  // Strategy controls
  const sc = await api('/api/strategy-controls');
  if (sc) {
    const controls = sc.controls || {};
    const hard = controls.hard_disabled || {};
    const manualEnabled = controls.manual_enabled || {};
    const manualDisabled = controls.manual_disabled || {};
    const effectiveSet = new Set(sc.effective_disabled || []);
    const tags = Array.from(new Set([
      ...Object.keys(hard),
      ...Object.keys(manualEnabled),
      ...Object.keys(manualDisabled),
    ])).sort();
    $('strategyControlsStats').textContent = `${effectiveSet.size} effective disabled`;
    if (!tags.length) {
      $('strategyControls').innerHTML = '<tr><td colspan="5" class="empty">No strategy controls yet</td></tr>';
    } else {
      $('strategyControls').innerHTML = tags.map(tag => {
        const h = hard[tag];
        const me = manualEnabled[tag];
        const md = manualDisabled[tag];
        let status = effectiveSet.has(tag) ? 'DISABLED' : 'ENABLED';
        let reason = '—';
        let timestamp = '—';
        let source = '—';

        if (md) {
          status = 'MANUAL DISABLE';
          reason = md.reason || reason;
          timestamp = md.disabled_at || timestamp;
          source = md.disabled_by || 'dashboard';
        } else if (me) {
          status = effectiveSet.has(tag) ? 'ENABLED OVERRIDE' : 'MANUAL ENABLE';
          reason = me.reason || reason;
          timestamp = me.enabled_at || timestamp;
          source = me.enabled_by || 'dashboard';
        } else if (h) {
          status = effectiveSet.has(tag) ? 'AUTO DISABLED' : 'AUTO DISABLED (OVERRIDE)';
          reason = h.reason || reason;
          timestamp = h.disabled_at || timestamp;
          source = h.disabled_by || 'game_film';
        }

        const statusClass = status.includes('DISABLE') ? 'negative' : 'positive';
        return `<tr>
          <td><strong>${tag}</strong></td>
          <td class="${statusClass}">${status}</td>
          <td style="font-size:11px;color:#8b949e">${reason}</td>
          <td>${timestamp === '—' ? '—' : timestamp.replace('T', ' ').replace('Z', ' UTC')}</td>
          <td>${source}</td>
        </tr>`;
      }).join('');
    }
  }
  // Portfolio
  const pf = await api('/api/portfolio');
  if (pf) {
    $('portfolioValue').textContent = `Cash: $${(pf.cash||0).toFixed(2)} | Total: $${(pf.total_value||0).toFixed(2)}`;
    $('portfolio').innerHTML = pf.positions && pf.positions.length ? pf.positions.map(p => {
      const val = (p.current_price * p.quantity).toFixed(2);
      const pnl = p.open_pnl || ((p.current_price - p.average_price) * p.quantity);
      const pnlPct = p.average_price ? ((p.current_price - p.average_price) / p.average_price * 100) : 0;
      return `<tr><td><strong>${p.symbol}</strong></td><td>${p.quantity.toFixed(4)}</td>
        <td>$${(p.average_price||0).toFixed(2)}</td><td>$${(p.current_price||0).toFixed(2)}</td>
        <td>$${val}</td><td class="${cls(pnl)}">${fmt(pnl)} (${fmt(pnlPct)}%)</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">No holdings</td></tr>';
  }
  // Options positions
  const ops = await api('/api/options');
  if (s && !s.options_enabled) {
    $('optionsValue').textContent = 'execution disabled';
    $('optionsPositions').innerHTML = '<tr><td colspan="13" class="empty">Options execution is disabled in runtime config</td></tr>';
  } else if (s && s.options_enabled && !s.options_execution_enabled) {
    $('optionsValue').textContent = 'engine unavailable';
    $('optionsPositions').innerHTML = '<tr><td colspan="13" class="empty">Options are enabled in config but the execution engine did not initialize</td></tr>';
  } else if (s && s.options_enabled && s.options_execution_enabled && !s.options_entry_enabled && (!ops || !ops.length)) {
    $('optionsValue').textContent = 'pilot off';
    $('optionsPositions').innerHTML = '<tr><td colspan="13" class="empty">Options engine is live for management only; new options entries are disabled</td></tr>';
  } else if (ops) {
    const totalOptPnl = ops.reduce((acc, p) => acc + (p.pnl || 0), 0);
    $('optionsValue').textContent = `${ops.length||0} contracts | Unrealized $${totalOptPnl.toFixed(2)}`;
    $('optionsPositions').innerHTML = ops.length ? ops.map(p => `<tr>
      <td><strong>${p.underlying||'?'}</strong></td>
      <td>${p.contract_symbol||'?'}</td>
      <td>${(p.option_type||'').toUpperCase()}</td>
      <td>$${(p.strike||0).toFixed(2)}</td>
      <td>${p.expiry||'—'}</td>
      <td>${p.qty||0}</td>
      <td>$${(p.entry_premium||0).toFixed(2)}</td>
      <td>$${(p.current_premium||0).toFixed(2)}</td>
      <td>$${(p.bid||0).toFixed(2)}</td>
      <td>$${(p.ask||0).toFixed(2)}</td>
      <td class="${cls(p.pnl_pct||0)}">${fmt(p.pnl_pct||0)}%</td>
      <td>${p.days_to_expiry ?? '—'}</td>
      <td>${p.status||'open'}</td>
    </tr>`).join('') : '<tr><td colspan="13" class="empty">No open options positions</td></tr>';
  }
  // Bot Positions
  const posPayload = await api('/api/positions');
  const pos = posPayload && Array.isArray(posPayload.positions) ? posPayload.positions : [];
  if (posPayload && posPayload.trust_flags) { _trustFlags = posPayload.trust_flags; _brokerOnlyMode = !!_trustFlags.broker_only_mode; _degradedInternal = !!_trustFlags.internal_analytics_degraded; }
  $('positions').innerHTML = pos && pos.length ? pos.map(p => {
    const isPending = p.order_status === 'pending';
    const statusBadge = isPending ? '<span class="tag" style="background:#e3b34122;color:#e3b341;border:1px solid #e3b34144;margin-left:4px">PENDING</span>' : '';
    return `<tr style="${isPending ? 'opacity:0.7' : ''}">
    <td><strong>${p.symbol}</strong>${statusBadge}</td><td>${(p.side||'long').toUpperCase()}</td><td>${p.quantity}</td><td>$${p.entry_price}</td>
    <td>${isPending ? '<span style="color:#e3b341">awaiting fill</span>' : '$'+p.current_price}</td><td class="${cls(p.pnl)}">${isPending ? '—' : fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)'}</td><td><div>${esc(humanizeKey(p.best_play || p.strategy_tag || '—'))}</div><div class="subtle">${esc(p.strategy_tag||'—')} · ${esc(p.signal_tier||'—')}</div></td><td><div>${esc(humanizeKey(p.setup_mode || 'invalid'))}</div><div class="subtle"><span class="tag ${timingTagClass(p.timing_state || 'enter_now')}">${esc(humanizeKey(p.timing_state || 'enter_now'))}</span>${p.hold_style ? ' · ' + esc(p.hold_style) : ''}</div></td><td>${typeof p.ratchet_floor_pct === 'number' ? fmt(p.ratchet_floor_pct,1)+'%' : '—'}</td><td>${isPending ? 'limit order' : (p.protection||'?')}</td><td>${isPending ? '—' : p.hold_time}</td>
  </tr>`;
  }).join('') : '<tr><td colspan="11" class="empty">No open positions</td></tr>';
  // Candidates
  const cand = await api('/api/candidates');
  const scanStatus = await api('/api/scan-status');
  const research = await api('/api/research-universe');
  $('candidateStats').textContent = scanStatus ? `${scanStatus.live||0} live` : '';
  $('researchStats').textContent = scanStatus ? `${scanStatus.research||0} tracked` : '';
  $('candidates').innerHTML = cand && cand.length ? cand.slice(0,10).map(c => `<tr>
    <td><strong>${c.symbol}</strong>${c.uw_budget_mode ? `<div style="font-size:11px;color:#8b949e">${c.uw_budget_mode}</div>` : ''}</td><td>${c.price ? '$'+(c.price||0).toFixed(2) : '—'}</td>
    <td class="${cls(c.change_pct||0)}">${fmt(c.change_pct||0,1)}%</td>
    <td>${(c.volume_spike||0).toFixed(1)}x</td><td>${(c.sentiment_score||0).toFixed(2)}</td>
    <td><strong>${(c.score||0).toFixed(3)}</strong></td>
    <td style="font-size:11px;color:#8b949e;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${((c.uw_news_summary||'') + ' | ' + (c.uw_chain_summary||'')).replace(/"/g,'&quot;')}">${c.uw_chain_summary || c.uw_news_summary || '—'}</td>
  </tr>`).join('') : '<tr><td colspan="7" class="empty">No candidates yet</td></tr>';
  $('researchUniverse').innerHTML = research && research.length ? research.slice(0,15).map(r => `<tr>
    <td><strong>${r.symbol||'?'}</strong></td>
    <td>${(r.side||'long').toUpperCase()}</td>
    <td style="font-size:11px;color:#8b949e">${r.source||'—'}</td>
    <td>${typeof r.score === 'number' ? r.score.toFixed(3) : '—'}</td>
    <td style="font-size:11px;color:#8b949e;max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${((r.watchlist_reason||'') + ' | ' + (r.human_intel||'') + ' | ' + (r.copy_trader_context||'')).replace(/"/g,'&quot;')}">${r.watchlist_reason || r.human_intel || r.copy_trader_context || '—'}</td>
  </tr>`).join('') : '<tr><td colspan="5" class="empty">No research universe yet</td></tr>';
  // Activity Feed
  const activity = await api('/api/activity?limit=30');
  if (activity && activity.length) {
    const catColors = {thinking:'#8b949e',scan:'#58a6ff',trade:'#3fb950',ai:'#d2a8ff',alert:'#f85149',research:'#f0883e'};
    $('activityFeed').innerHTML = activity.reverse().map(a => {
      const color = catColors[a.category] || '#8b949e';
      return `<div class="activity-line"><span class="activity-time">${a.time_str}</span><span class="activity-tag" style="color:${color};border-color:${color}33;background:${color}10">${a.category}</span>${a.message}</div>`;
    }).join('');
  }

  // Watchlist
  const wl = await api('/api/watchlist');
  $('watchlistCount').textContent = wl ? `${wl.length} tickers` : '';
  $('watchlist').innerHTML = wl && wl.length ? wl.slice(0,15).map(w => `<tr>
    <td><strong>${w.ticker}</strong></td>
    <td>${w.side === 'short' ? '<span class="side-pill short">Short</span>' : '<span class="side-pill long">Long</span>'}</td>
    <td>${(w.conviction||0).toFixed(2)}</td>
    <td style="color:#6e7681">${w.sources||''}</td>
    <td style="font-size:11px;color:#8b949e">${(w.reason||'').substring(0,60)}</td>
  </tr>`).join('') : '<tr><td colspan="5" class="empty">Watchlist builds at 10PM ET</td></tr>';

  // Human intel
  const intel = await api('/api/human-intel?limit=12');
  $('humanIntelCount').textContent = intel ? `${intel.length} active notes` : '';
  $('humanIntelList').innerHTML = intel && intel.length ? intel.map(entry => `
    <div class="intel-entry">
      <div class="intel-meta">
        <span class="${entry.bias === 'bearish' ? 'negative' : entry.bias === 'bullish' ? 'positive' : 'info'}">${(entry.bias || 'neutral').toUpperCase()}</span>
        <span>${entry.ticker}</span>
        <span>conf ${(entry.confidence || 0).toFixed(2)}</span>
        <span>${entry.kind || 'note'}</span>
        <span>${entry.source || 'manual'}</span>
        <button class="mini-btn" onclick="deleteIntel('${entry.id}')">Delete</button>
      </div>
      <div class="intel-title">${entry.title || '(untitled)'}</div>
      <div class="intel-notes">${entry.notes || ''}</div>
      ${entry.url ? `<div style="margin-top:6px"><a class="intel-link" href="${entry.url}" target="_blank" rel="noopener noreferrer">${entry.url}</a></div>` : ''}
    </div>
  `).join('') : '<div class="empty">No operator context yet</div>';

  // Intelligence panel
  const intelligence = await api('/api/intelligence');
  const ct = (intelligence && intelligence.copy_trader) || {};
  const ctSignals = ct.signals || [];
  const ctExits = ct.exits || [];
  const ctTraders = ct.traders || [];
  const ark = (intelligence && intelligence.ark_trades) || {};
  const uwApi = (intelligence && intelligence.unusual_whales_api) || {};
  const uwFocus = (intelligence && intelligence.unusual_whales_focus) || [];
  const uwBudget = uwApi.budget_mode || 'unknown';
  const funnel = scanStatus || {};
  const sourceBits = [
    `alpaca ${funnel.alpaca_movers ?? '—'}`,
    `polygon ${funnel.polygon_gainers ?? '—'}`,
    `twits ${funnel.stocktwits_trending ?? '—'}`,
    `grok ${funnel.grok_x ?? '—'}`,
    `copy ${funnel.copy_trader ?? '—'}`,
    `watchlist ${funnel.watchlist ?? '—'}`,
    `uw ${funnel.unusual_whales ?? '—'}`,
    `human ${funnel.human_intel ?? '—'}`,
  ];
  $('candidateMeta').innerHTML = `
    <div>
      <span><strong>UW budget:</strong> ${uwBudget}</span>
      <span style="margin-left:12px"><strong>Minute remaining:</strong> ${uwApi.minute_remaining ?? '—'}</span>
      <span style="margin-left:12px"><strong>Last path:</strong> ${uwApi.last_request_path || '—'}</span>
      <span style="margin-left:12px"><strong>Regime:</strong> ${funnel.regime || '—'}</span>
    </div>
    <div style="margin-top:6px">
      <strong>Scan funnel:</strong> merged ${funnel.merged_unique ?? '—'} -> enriched ${funnel.enriched ?? '—'} -> filtered ${funnel.filtered ?? '—'} -> disabled ${funnel.disabled ?? '—'} -> live ${funnel.live ?? '—'}
    </div>
    <div style="margin-top:6px">
      <strong>Sources:</strong> ${sourceBits.join(' · ')}
    </div>
    ${uwFocus.length ? `<div style="margin-top:6px"><strong>Top UW context:</strong> ${uwFocus.map(row => `${row.symbol}: ${row.chain_summary || row.news_summary}`).join(' · ')}</div>` : ''}
  `;
  $('copyTraderSummary').innerHTML = `
    <div class="summary-item"><div class="val info">${ctSignals.length}</div><div class="lbl">Active Signals</div></div>
    <div class="summary-item"><div class="val negative">${ctExits.length}</div><div class="lbl">Exit Signals</div></div>
    <div class="summary-item"><div class="val">${ctTraders.length}</div><div class="lbl">Tracked Traders</div></div>
    <div class="summary-item"><div class="val positive">${ctTraders.length ? (ctTraders[0].weight||1).toFixed(2) : '—'}</div><div class="lbl">Top Weight</div></div>
    <div class="summary-item"><div class="val info">${(ark.buys||[]).length}/${(ark.sells||[]).length}</div><div class="lbl">ARK B/S</div></div>
  `;
  $('copyTraderSignals').innerHTML = ctSignals.length ? ctSignals.map(row => `<tr>
    <td><strong>${row.symbol||'?'}</strong></td>
    <td>${(row.side||'').toUpperCase()}</td>
    <td style="font-size:11px;color:#8b949e">${(row.copy_trader_handles||[]).slice(0,3).join(', ') || '—'}</td>
    <td>${(row.copy_trader_size_multiplier||1).toFixed(2)}x</td>
  </tr>`).join('') : '<tr><td colspan="4" class="empty">No recent entry signals</td></tr>';
  $('copyTraderExits').innerHTML = ctExits.length ? ctExits.map(row => `<tr>
    <td><strong>${row.symbol||'?'}</strong></td>
    <td class="${row.copy_trader_exit_action==='exit'?'negative':'info'}">${(row.copy_trader_exit_action||'trim').toUpperCase()}</td>
    <td style="font-size:11px;color:#8b949e">${(row.copy_trader_exit_handles||[]).slice(0,3).join(', ') || '—'}</td>
    <td>${row.copy_trader_exit_count||0}</td>
  </tr>`).join('') : '<tr><td colspan="4" class="empty">No recent exit signals</td></tr>';
  $('copyTraderTraders').innerHTML = ctTraders.length ? ctTraders.slice(0,6).map(row => `<tr>
    <td><strong>@${row.handle||'?'}</strong></td>
    <td>${(row.weight||1).toFixed(2)}</td>
    <td>${row.signals_correct||0}/${row.signals_wrong||0}</td>
    <td>${((row.realized_win_rate||0)*100).toFixed(0)}%</td>
  </tr>`).join('') : '<tr><td colspan="4" class="empty">No trader stats yet</td></tr>';

  // History
  const hist = await api('/api/history');
  $('history').innerHTML = hist && hist.length ? hist.reverse().slice(0,15).map(h => `<tr>
    <td><strong>${h.symbol}</strong></td><td>$${(h.entry_price||0).toFixed(2)}</td>
    <td>$${(h.exit_price||0).toFixed(2)}</td><td>${h.quantity}</td>
    <td class="${cls(h.pnl||0)}">${fmt(h.pnl||0)}</td><td class="${cls(h.pnl_pct||0)}">${fmt(h.pnl_pct||0)}%</td>
    <td>${h.reason||''}</td><td>${h.hold_time||''}</td>
  </tr>`).join('') : '<tr><td colspan="8" class="empty">No trades yet</td></tr>';
  renderOperatorDeck({
    status: statusPayload,
    pnl: pnlPayload,
    metrics: metricsPayload,
    books: (bookScorePayload && bookScorePayload.books) || [],
    pending: (pendingPayload && pendingPayload.setups) || [],
    positions: pos || [],
    candidates: cand || [],
  });
  } catch(e) { console.error('Dashboard refresh error:', e); }
}
updateEquityRangeButtons();
refresh();
setInterval(refresh, 5000);
window.addEventListener('resize', () => requestAnimationFrame(fitPnlMetricValues));
</script>
</body>
</html>"""
