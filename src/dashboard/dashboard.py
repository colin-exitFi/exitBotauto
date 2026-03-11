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
from src.agents.base_agent import (
    call_claude_text,
    call_gpt_text,
    call_perplexity_text,
    get_api_cost_stats,
)
from src.data import strategy_controls

app = FastAPI(title="Velox", version="2.0.0")

# Global reference to bot instance (set by main.py)
_bot = None
_dashboard_thread = None
_LOG_DIR = Path(__file__).resolve().parents[2] / "logs"
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_RUNNERS_FILE = _DATA_DIR / "yesterdays_runners.json"
_WATCHLIST_FILE = _DATA_DIR / "watchlist.json"
_CHAT_HISTORY_LIMIT = 8
_CHAT_ACTIVITY_LIMIT = 15
_CHAT_STOPWORDS = {
    "A", "AN", "AND", "ARE", "AS", "AT", "BE", "BUT", "BY", "DO", "FOR", "FROM",
    "FRIDAY", "HOURS", "I", "IF", "IN", "IS", "IT", "ITS", "LOOK", "ME", "MONDAY",
    "MY", "NOT", "NOW", "OF", "ON", "OR", "OUT", "SAW", "SO", "STOCK", "TELL",
    "THAT", "THE", "THIS", "TO", "UP", "WAS", "WHAT", "WITH", "YOU",
}

# Activity feed — circular buffer of bot thoughts/actions
_activity_feed: List[Dict] = []
_MAX_FEED_SIZE = 100


def set_bot(bot):
    global _bot
    _bot = bot


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
    return {
        "running": _bot.running,
        "paused": _bot.paused,
        "market_open": _bot.entry_manager.is_market_open() if _bot.entry_manager else False,
        "positions_count": len(positions),
        "uptime_seconds": int(time.time() - _bot.start_time) if hasattr(_bot, 'start_time') else 0,
        "options_enabled": bool(getattr(settings, "OPTIONS_ENABLED", False)),
        "options_execution_enabled": bool(getattr(_bot, "options_engine", None)),
        **risk_status,
    }


@app.get("/api/positions")
async def get_positions():
    if not _bot or not _bot.entry_manager:
        return []
    positions = _bot.entry_manager.get_positions()
    enriched = []
    for p in positions:
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
        pnl = (price - p["entry_price"]) * p["quantity"] if p["entry_price"] else 0
        pnl_pct = ((price - p["entry_price"]) / p["entry_price"] * 100) if p["entry_price"] else 0
        hold_min = (time.time() - p.get("entry_time", time.time())) / 60
        # Protection status
        has_trailing = p.get("has_trailing_stop", False)
        guard_info = {}
        if hasattr(_bot, 'extended_guard'):
            guard_info = _bot.extended_guard.get_guard_status().get(p["symbol"], {})
        has_guard = bool(guard_info.get("has_limit_order"))
        protection = "🟢 Trail" if has_trailing else ("🟡 Limit" if has_guard else "🔴 NONE")

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
            "guard_limit": guard_info.get("limit_price", 0),
        })
    return enriched


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
        balances = _bot.alpaca_client.get_balances()
        total_value = sum(p["current_price"] * p["quantity"] for p in positions) + balances.get("cash", 0)
        return {
            "positions": positions,
            "cash": round(balances.get("cash", 0), 2),
            "buying_power": round(balances.get("buying_power", 0), 2),
            "total_value": round(total_value, 2),
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
        "short_verdicts_blocked": ai.get("short_verdicts_blocked", 0),
        "last_short_block_reason": ai.get("last_short_block_reason"),
    }


@app.get("/api/consensus")
async def get_consensus():
    """Get agent orchestrator history and stats."""
    if not _bot or not hasattr(_bot, 'orchestrator') or not _bot.orchestrator:
        return {"enabled": False, "history": [], "stats": {}}
    ai = getattr(_bot, "ai_layers", {}) or {}
    return {
        "enabled": True,
        "history": _bot.orchestrator.get_history()[-10:],
        "stats": _bot.orchestrator.get_stats(),
        "short_verdicts_blocked": ai.get("short_verdicts_blocked", 0),
        "last_short_block_reason": ai.get("last_short_block_reason"),
    }


@app.get("/api/candidates")
async def get_candidates():
    if not _bot or not _bot.scanner:
        return []
    return _bot.scanner.get_cached_candidates()


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
    return {
        "trades": trades,
        "stats": stats,
        "best": best,
        "worst": worst,
    }


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


@app.get("/api/equity-curve")
async def get_equity_curve(limit: int = 120):
    """Return realized-equity curve points derived from trade history."""
    from src.ai import trade_history

    stats = trade_history.get_analytics()
    curve = stats.get("equity_curve", [])
    if limit < 1:
        limit = 1
    points = curve[-limit:]
    starting = settings.TOTAL_CAPITAL
    if _bot and getattr(_bot, "pnl_state", None):
        starting = _bot.pnl_state.get("starting_equity", settings.TOTAL_CAPITAL)

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
        "count": len(curve),
        "points": series,
    }


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

    equity = 25000.0
    last_equity = equity
    broker_day_pnl = 0.0
    broker_day_pnl_pct = 0.0
    unrealized = 0
    options_unrealized = 0.0
    starting = pnl.get("starting_equity", 25000.0)
    peak = pnl.get("peak_equity", 25000.0)
    reconciliation_state = {}
    broker_truth = {}
    reconciliation = {}
    if getattr(_bot, "reconciler", None):
        try:
            reconciliation_state = _bot.reconciler.snapshot()
            broker_truth = reconciliation_state.get("broker", {}) or {}
            reconciliation = reconciliation_state.get("reconciliation", {}) or {}
        except Exception:
            reconciliation_state = {}
    if broker_truth:
        equity = float(broker_truth.get("equity", equity) or equity)
        last_equity = float(broker_truth.get("last_equity", equity) or equity)
        broker_day_pnl = float(broker_truth.get("day_pnl", 0) or 0)
        broker_day_pnl_pct = float(broker_truth.get("day_pnl_pct", 0) or 0)
        unrealized = float(broker_truth.get("current_open_unrealized", 0) or 0)
    elif _bot.alpaca_client:
        try:
            acct = _bot.alpaca_client.get_account()
            equity = float(acct.get("equity", 25000.0))
            last_equity = float(acct.get("last_equity", equity) or equity)
            broker_day_pnl = round(equity - last_equity, 2)
            broker_day_pnl_pct = round((broker_day_pnl / last_equity * 100.0), 2) if last_equity else 0.0
            alpaca_positions = _bot.alpaca_client.get_positions()
            unrealized = sum(float(p.get("unrealized_pnl", p.get("unrealized_pl", p.get("open_pnl", 0)))) for p in alpaca_positions)
        except Exception:
            pass
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
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    drawdown = ((peak - equity) / peak * 100) if peak > 0 else 0
    roi = ((equity - starting) / starting * 100) if starting > 0 else 0
    analytics = {}
    avg_signal_to_fill_ms = None
    api_costs = {}
    internal_state = (reconciliation_state.get("internal", {}) or {})
    try:
        from src.ai import trade_history
        analytics = trade_history.get_analytics()
        avg_signal_to_fill_ms = (analytics.get("overall", {}) or {}).get("avg_signal_to_fill_ms")
    except Exception:
        avg_signal_to_fill_ms = None
    try:
        api_costs = get_api_cost_stats()
    except Exception:
        api_costs = {}

    return {
        "equity": round(equity, 2),
        "starting_equity": round(starting, 2),
        "peak_equity": round(peak, 2),
        "cash": round(float(broker_truth.get("cash", 0) or 0), 2),
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
        "internal_trade_count": int(internal_state.get("trade_history_trade_count", total_trades) or total_trades),
        "internal_game_film_realized": round(float(internal_state.get("game_film_realized", 0) or 0), 2),
        "internal_win_rate_pct": round(float(internal_state.get("trade_history_win_rate_pct", win_rate) or win_rate), 2),
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
    """Get extended hours guard status for all positions."""
    if not _bot or not hasattr(_bot, 'extended_guard'):
        return {"active": False, "guards": {}}
    guard = _bot.extended_guard
    return {
        "active": guard.is_extended_hours(),
        "regular_hours": guard.is_regular_hours(),
        "guards": guard.get_guard_status(),
    }


@app.get("/api/metrics")
async def get_metrics():
    if not _bot or not _bot.risk_manager:
        return {}
    return _bot.risk_manager.get_status()


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
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e14;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px}
::-webkit-scrollbar{width:6px}::-webkit-scrollbar-track{background:#0a0e14}::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
.header{background:linear-gradient(135deg,#0d1117 0%,#161b22 100%);border-bottom:1px solid #1f6feb33;padding:16px 24px;display:flex;justify-content:space-between;align-items:center;position:sticky;top:0;z-index:100;backdrop-filter:blur(10px)}
.header h1{font-size:22px;color:#58a6ff;display:flex;align-items:center;gap:10px}
.header h1 .logo{font-size:28px}
.scan-dot{width:10px;height:10px;border-radius:50%;background:#3fb950;display:inline-block;animation:pulse 1.5s ease-in-out infinite}
.scan-dot.idle{background:#484f58;animation:none}
.header .status{display:flex;gap:12px;align-items:center}
.badge{padding:5px 14px;border-radius:12px;font-size:12px;font-weight:700;letter-spacing:.5px;text-transform:uppercase}
.badge.running{background:linear-gradient(135deg,#238636,#2ea043);color:#fff;box-shadow:0 0 12px rgba(46,160,67,.4)}
.badge.paused{background:linear-gradient(135deg,#d29922,#e3b341);color:#000;box-shadow:0 0 12px rgba(227,179,65,.4)}
.badge.stopped{background:linear-gradient(135deg,#da3633,#f85149);color:#fff;box-shadow:0 0 12px rgba(248,81,73,.4)}
.container{max-width:1500px;margin:0 auto;padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:linear-gradient(145deg,#161b22 0%,#0d1117 100%);border:1px solid #30363d;border-radius:12px;padding:18px;animation:slideIn .4s ease-out;transition:border-color .3s,box-shadow .3s}
.card:hover{border-color:#1f6feb55;box-shadow:0 4px 20px rgba(0,0,0,.3)}
.card h2{font-size:13px;color:#8b949e;text-transform:uppercase;letter-spacing:1.5px;margin-bottom:14px;border-bottom:1px solid #21262d;padding-bottom:10px;display:flex;align-items:center;gap:8px}
.card h2 .icon{font-size:16px}
.full{grid-column:1/-1}
.metrics{display:grid;grid-template-columns:repeat(6,1fr);gap:10px}
.metrics.pnl-grid{grid-template-columns:repeat(auto-fit,minmax(110px,1fr))}
.metric{text-align:center;padding:12px 8px;background:linear-gradient(145deg,#0d1117,#161b22);border-radius:8px;border:1px solid #21262d;transition:all .3s;overflow:hidden}
.metric:hover{border-color:#30363d;transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.metric .value{font-size:16px;font-weight:800;color:#58a6ff;transition:all .3s;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.metric .value.positive{color:#3fb950}
.metric .value.negative{color:#f85149}
.metric .value.muted{color:#6e7681!important}
.metric .value.animated{animation:countUp .4s ease-out}
.metric .label{font-size:9px;color:#6e7681;margin-top:5px;text-transform:uppercase;letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.big-pnl{font-size:clamp(18px,2.2vw,28px)!important;font-weight:900!important;animation:neonPulse 2s ease-in-out infinite}
.recon-banner{display:none;margin:0 0 12px 0;padding:12px 14px;border:1px solid #8b0000;border-radius:10px;background:linear-gradient(145deg,#2a0f12,#1c0b0d);color:#ffb3b3;font-size:12px;line-height:1.45;white-space:normal;word-break:break-word;overflow-wrap:anywhere;box-shadow:inset 0 1px 0 rgba(255,255,255,.03)}
.recon-banner strong{display:block;font-size:11px;letter-spacing:.8px;text-transform:uppercase;color:#ff8e8e;margin-bottom:4px}
.recon-banner .muted{color:#d88f8f}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:10px;color:#6e7681;text-transform:uppercase;letter-spacing:.5px;padding:8px 10px;border-bottom:2px solid #21262d}
td{padding:8px 10px;border-bottom:1px solid #21262d44;font-size:13px;transition:background .2s}
tr:hover td{background:#161b2288}
.positive{color:#3fb950}.negative{color:#f85149}.info{color:#58a6ff}
.tag{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600}
.tag-buy{background:#23863622;color:#3fb950;border:1px solid #23863644}
.tag-short{background:#a371f722;color:#d2a8ff;border:1px solid #a371f744}
.tag-skip{background:#da363322;color:#f85149;border:1px solid #da363344}
.controls{display:flex;gap:8px}
.btn{padding:8px 18px;border:none;border-radius:8px;cursor:pointer;font-weight:700;font-size:12px;text-transform:uppercase;letter-spacing:.5px;transition:all .2s}
.btn-start{background:linear-gradient(135deg,#238636,#2ea043);color:#fff}
.btn-pause{background:linear-gradient(135deg,#d29922,#e3b341);color:#000}
.btn-stop{background:linear-gradient(135deg,#da3633,#f85149);color:#fff}
.btn-intel{background:linear-gradient(135deg,#1f6feb,#58a6ff);color:#fff}
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.4)}
.btn:active{transform:translateY(0)}
.empty{color:#484f58;text-align:center;padding:24px;font-style:italic}
.summary-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;margin-bottom:14px}
.summary-item{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px 12px;text-align:center;overflow:hidden}
.summary-item .val{font-size:17px;font-weight:800;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.summary-item .val.val-sm{font-size:13px;font-weight:700}
.summary-item .lbl{font-size:9px;color:#6e7681;text-transform:uppercase;margin-top:3px;letter-spacing:.3px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.ai-card{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;font-size:12px;line-height:1.5;max-height:200px;overflow-y:auto;word-wrap:break-word;overflow-wrap:break-word}
.ai-card strong{color:#58a6ff;display:block;margin-bottom:4px}
.ai-card.tuner{border-color:#d2a8ff33}.ai-card.pm{border-color:#3fb95033}
.watermark{text-align:center;padding:20px;color:#21262d;font-size:11px;letter-spacing:2px}
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
</style>
</head>
<body>
<div class="header">
  <h1><span class="logo"><svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="vg" x1="0" y1="0" x2="100" y2="100"><stop offset="0%" stop-color="#58a6ff"/><stop offset="100%" stop-color="#a371f7"/></linearGradient></defs><path d="M15 75L45 15L55 45L85 15L55 75L45 50Z" fill="url(#vg)"/></svg></span>VELOX <span class="scan-dot idle" id="scanDot"></span></h1>
  <div class="status">
    <span id="statusBadge" class="badge stopped">LOADING</span>
    <div class="controls">
      <button class="btn btn-intel" onclick="openCopilotModal()">💬 Ask AI</button>
      <button class="btn btn-start" onclick="api('/api/resume','POST')">▶ Resume</button>
      <button class="btn btn-pause" onclick="api('/api/pause','POST')">⏸ Pause</button>
      <button class="btn btn-stop" onclick="api('/api/stop','POST')">⏹ Stop</button>
    </div>
  </div>
</div>
<div class="container">

  <!-- P&L Terminal -->
  <div class="card full">
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
    <div id="equityCurveMeta" style="font-size:12px;color:#8b949e;margin-bottom:8px">Loading...</div>
    <svg id="equityCurveSvg" viewBox="0 0 900 180" preserveAspectRatio="none" style="width:100%;height:180px;background:#0b1020;border:1px solid #21262d;border-radius:8px"></svg>
  </div>

  <!-- Performance Metrics -->
  <div class="card full">
    <h2><span class="icon">📊</span> Risk Metrics</h2>
    <div class="metrics" id="metrics"></div>
  </div>

  <!-- AI Layers -->
  <div class="card full">
    <h2><span class="icon">🧠</span> AI Layers <span id="aiEnabled" style="margin-left:auto;color:#8b949e;font-size:11px"></span></h2>
    <div id="aiStatus" class="empty">Loading AI status...</div>
  </div>

  <!-- Consensus Panel -->
  <div class="card full">
    <h2><span class="icon">🗳️</span> AI Agent Jury <span style="font-size:11px;color:#6e7681;font-weight:400;margin-left:8px">6-Agent Specialized Architecture</span> <span id="consensusStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="summary-row" id="consensusSummary"></div>
    <table><thead><tr><th>Symbol</th><th>Decision</th><th>Confidence</th><th>Size %</th><th>Trail %</th><th>Reasoning</th></tr></thead>
    <tbody id="consensus"></tbody></table>
  </div>

  <!-- Trade History Panel -->
  <div class="card full">
    <h2><span class="icon">💰</span> Trade History <span id="tradeStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div class="summary-row" id="tradeSummary"></div>
    <div style="overflow-x:auto"><table><thead><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L</th><th>%</th><th>Reason</th><th>Hold</th><th>Strategy</th><th>Sources</th><th>Slip</th><th>Latency</th></tr></thead>
    <tbody id="tradeHistory"></tbody></table></div>
  </div>

  <!-- Strategy Controls -->
  <div class="card full">
    <h2><span class="icon">🧩</span> Strategy Controls <span id="strategyControlsStats" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <table><thead><tr><th>Strategy Tag</th><th>Status</th><th>Reason</th><th>Timestamp</th><th>Source</th></tr></thead>
    <tbody id="strategyControls"></tbody></table>
  </div>

  <!-- Portfolio -->
  <div class="card full">
    <h2><span class="icon">💼</span> Alpaca Portfolio <span id="portfolioValue" style="margin-left:auto;color:#58a6ff;font-size:12px"></span></h2>
    <table><thead><tr><th>Symbol</th><th>Shares</th><th>Avg Price</th><th>Current</th><th>Value</th><th>P&L</th></tr></thead>
    <tbody id="portfolio"></tbody></table>
  </div>

  <!-- Options Positions -->
  <div class="card full">
    <h2><span class="icon">🧩</span> Options Positions <span id="optionsValue" style="margin-left:auto;color:#58a6ff;font-size:12px"></span></h2>
    <div style="overflow-x:auto"><table><thead><tr><th>Underlying</th><th>Contract</th><th>Type</th><th>Strike</th><th>Exp</th><th>Qty</th><th>Entry</th><th>Curr</th><th>Bid</th><th>Ask</th><th>P&L%</th><th>DTE</th><th>Status</th></tr></thead>
    <tbody id="optionsPositions"></tbody></table></div>
  </div>

  <!-- Activity Feed + Watchlist side by side -->
  <div class="card">
    <h2><span class="icon">🧠</span> Bot Activity Feed</h2>
    <div id="activityFeed" style="max-height:600px;overflow-y:auto;font-size:12px;line-height:1.8;word-wrap:break-word;overflow-wrap:break-word"></div>
  </div>
  <div class="card">
    <h2><span class="icon">📋</span> Watchlist <span id="watchlistCount" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <table><thead><tr><th>Ticker</th><th>Side</th><th>Conv</th><th>Source</th><th>Reason</th></tr></thead>
    <tbody id="watchlist"></tbody></table>
  </div>

  <div class="card full">
    <h2><span class="icon">🧠</span> Human Intel <span id="humanIntelCount" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <div id="humanIntelList" class="empty">No operator context yet</div>
  </div>

  <div class="card full">
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
  <div class="card">
    <h2><span class="icon">📈</span> Bot Positions</h2>
    <div style="overflow-x:auto"><table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th>Trail%</th><th>Protection</th><th>Hold</th></tr></thead>
    <tbody id="positions"></tbody></table></div>
  </div>
  <div class="card">
    <h2><span class="icon">🔍</span> Scanner Candidates</h2>
    <div id="candidateMeta" style="font-size:12px;color:#8b949e;margin-bottom:8px"></div>
    <table><thead><tr><th>Symbol</th><th>Price</th><th>Change</th><th>Vol</th><th>Sent</th><th>Score</th><th>UW</th></tr></thead>
    <tbody id="candidates"></tbody></table>
  </div>

  <!-- Recent Exits -->
  <div class="card full">
    <h2><span class="icon">📋</span> Recent Exits</h2>
    <table><thead><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&L</th><th>%</th><th>Reason</th><th>Hold</th></tr></thead>
    <tbody id="history"></tbody></table>
  </div>
</div>
<div class="watermark">VELOX v2.0 — autonomous velocity trading</div>

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

<script>
const $ = s => document.getElementById(s);
let _prevPnl = null;
let _copilotHistory = [];
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
function renderEquityCurve(points) {
  const svg = $('equityCurveSvg');
  if (!svg) return;
  if (!points || points.length < 2) {
    svg.innerHTML = '<text x="20" y="90" fill="#8b949e" font-size="12">Not enough trade history for equity curve</text>';
    return;
  }
  const w = 900, h = 180, pad = 12;
  const ys = points.map(p => p.equity || 0);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanY = (maxY - minY) || 1;
  const pts = points.map((p, i) => {
    const x = pad + (i / (points.length - 1)) * (w - 2 * pad);
    const y = h - pad - (((p.equity || 0) - minY) / spanY) * (h - 2 * pad);
    return `${x.toFixed(2)},${y.toFixed(2)}`;
  }).join(' ');
  const last = points[points.length - 1]?.equity || 0;
  const first = points[0]?.equity || 0;
  const stroke = last >= first ? '#3fb950' : '#f85149';
  svg.innerHTML = `
    <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2.5" />
    <line x1="${pad}" y1="${h-pad}" x2="${w-pad}" y2="${h-pad}" stroke="#21262d" stroke-width="1" />
  `;
}

async function refresh() {
  // Status
  const s = await api('/api/status');
  if (s) {
    const b = $('statusBadge'), dot = $('scanDot');
    if (!s.running) { b.textContent='STOPPED'; b.className='badge stopped'; dot.className='scan-dot idle'; }
    else if (s.paused) { b.textContent='PAUSED'; b.className='badge paused'; dot.className='scan-dot idle'; }
    else { b.textContent='RUNNING'; b.className='badge running'; dot.className='scan-dot'; }
  }
  // P&L Terminal
  const pnl = await api('/api/pnl');
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
    const brokerOnly = !!trust.broker_only_mode;
    const degradedInternal = !!trust.internal_analytics_degraded;
    $('totalPnl').textContent = '$' + (pnl.total_pnl||0).toFixed(2);
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
      if (status && status !== 'healthy') {
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
  }
  // Equity curve
  const ec = await api('/api/equity-curve?limit=120');
  if (ec) {
    const pts = ec.points || [];
    const latest = pts.length ? pts[pts.length - 1] : null;
    $('equityCurveStats').textContent = `${ec.count || 0} trades`;
    $('equityCurveMeta').textContent = latest
      ? `Start $${(ec.starting_equity||0).toFixed(2)} -> ${pts.length ? '$'+(latest.equity||0).toFixed(2) : '—'}`
      : 'No completed trades yet';
    renderEquityCurve(pts);
  }
  // Metrics
  const m = await api('/api/metrics');
  if (m) {
    const pnlChanged = _prevPnl !== null && _prevPnl !== (m.daily_pnl||0);
    _prevPnl = m.daily_pnl||0;
    const anim = pnlChanged ? ' animated' : '';
    $('metrics').innerHTML = `
      <div class="metric"><div class="value" style="color:#d2a8ff">${m.tier_name||'?'}</div><div class="label">Risk Tier</div></div>
      <div class="metric"><div class="value ${m.swing_mode?'negative':'positive'}">${m.swing_mode?'SWING':'NORMAL'}</div><div class="label">Mode</div></div>
      <div class="metric"><div class="value">${m.remaining_day_trades??'—'}</div><div class="label">Day Trades</div></div>
      <div class="metric"><div class="value">${m.consecutive_wins||0}W/${m.consecutive_losses||0}L</div><div class="label">Streak</div></div>
      <div class="metric"><div class="value">${(m.heat_pct||0).toFixed(0)}%</div><div class="label">Heat</div></div>
      <div class="metric"><div class="value">${(m.tier_size_pct||0)}%</div><div class="label">Pos Size</div></div>
      <div class="metric"><div class="value">${m.tier_max_positions||0}</div><div class="label">Max Pos</div></div>
      <div class="metric"><div class="value">$${(m.ath_equity||0).toLocaleString()}</div><div class="label">ATH</div></div>
      <div class="metric"><div class="value">$${(m.next_milestone||0).toLocaleString()}</div><div class="label">Next Milestone</div></div>
      <div class="metric"><div class="value info">${(m.milestone_progress_pct||0).toFixed(0)}%</div><div class="label">→ Progress</div></div>
      <div class="metric"><div class="value ${cls(m.total_return_pct||0)}">${(m.total_return_pct||0).toFixed(1)}%</div><div class="label">Total Return</div></div>
      <div class="metric"><div class="value">${(m.days_trading||0).toFixed(0)}d</div><div class="label">Days</div></div>
    `;
  }
  // AI Status
  const ai = await api('/api/ai-status');
  if (ai) {
    $('aiEnabled').textContent = ai.enabled ? '✅ Active' : '❌ Disabled';
    if (ai.enabled) {
      let html = '<div class="ai-grid">';
      html += '<div class="ai-card"><strong>🔭 Observer</strong>' + (ai.last_observation || '<em>Pending…</em>') + '</div>';
      html += '<div class="ai-card"><strong>💡 Advisor</strong>' + (ai.last_advice || '<em>Pending…</em>') + '</div>';
      html += '<div class="ai-card tuner"><strong>🎛️ Tuner</strong>' + (ai.last_tuner_changes || '<em>No changes yet</em>') + '</div>';
      html += '<div class="ai-card pm"><strong>🛡️ Position Manager</strong>' + (ai.last_position_manager || '<em>Pending…</em>') + '</div>';
      html += '</div>';
      if (ai.last_game_film) html += '<div style="margin-top:10px;padding:8px 12px;background:#0d1117;border:1px solid #21262d;border-radius:8px;font-size:12px"><strong style="color:#d2a8ff">🎬 Game Film:</strong> ' + ai.last_game_film + '</div>';
      if (ai.short_verdicts_blocked) html += '<div style="margin-top:10px;padding:8px 12px;background:#0d1117;border:1px solid #21262d;border-radius:8px;font-size:12px"><strong style="color:#f85149">🩳 Short blocks:</strong> ' + ai.short_verdicts_blocked + (ai.last_short_block_reason ? ' · ' + ai.last_short_block_reason : '') + '</div>';
      $('aiStatus').innerHTML = html;
    } else { $('aiStatus').innerHTML = '<span class="empty">AI layers not initialized</span>'; }
  }
  // Consensus
  const con = await api('/api/consensus');
  if (con) {
    const st = con.stats || {};
    const ac = st.api_calls || {};
    $('consensusStats').textContent = con.enabled ? `${st.total||0} evaluations` : '❌ Disabled';
    $('consensusSummary').innerHTML = con.enabled ? `
      <div class="summary-item"><div class="val info">${st.total||0}</div><div class="lbl">Evals</div></div>
      <div class="summary-item"><div class="val positive">${st.buys||0}</div><div class="lbl">BUY</div></div>
      <div class="summary-item"><div class="val" style="color:#d2a8ff">${st.shorts||0}</div><div class="lbl">SHORT</div></div>
      <div class="summary-item"><div class="val negative">${st.skips||0}</div><div class="lbl">SKIP</div></div>
      <div class="summary-item" title="${con.last_short_block_reason||''}"><div class="val negative">${con.short_verdicts_blocked||0}</div><div class="lbl">Short Blocked</div></div>
      <div class="summary-item"><div class="val" style="color:#e3b341">${(st.actionable_avg_confidence ?? st.avg_confidence)?(st.actionable_avg_confidence ?? st.avg_confidence).toFixed(0)+'%':'—'}</div><div class="lbl">Action Conf</div></div>
      <div class="summary-item"><div class="val val-sm" style="color:#8b949e">🟣${ac.claude||0} 🟢${ac.gpt||0} 🔵${ac.grok||0} 🟠${ac.perplexity||0}</div><div class="lbl">API Calls</div></div>
    ` : '';
    $('consensus').innerHTML = con.history && con.history.length ? con.history.slice().reverse().map(h => {
      const decCls = h.decision==='BUY'?'tag-buy':h.decision==='SHORT'?'tag-short':'tag-skip';
      const reason = (h.reasoning||'').substring(0, 120) + ((h.reasoning||'').length > 120 ? '...' : '');
      const votes = h.consensus_detail && h.consensus_detail.votes ? Object.entries(h.consensus_detail.votes).map(([k, v]) => `${k}:${v}`).join(' · ') : '';
      return `<tr><td><strong>${h.symbol}</strong></td>
        <td><span class="tag ${decCls}">${h.decision}</span></td>
        <td>${(h.confidence||0).toFixed(0)}%</td>
        <td>${(h.size_pct||0).toFixed(1)}%</td>
        <td>${(h.trail_pct||0).toFixed(1)}%</td>
        <td style="font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${reason}${votes ? `<div style="color:#8b949e;margin-top:4px">${votes}</div>` : ''}</td></tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">No agent decisions yet</td></tr>';
  }
  // Trade History
  const th = await api('/api/trade-history?limit=20');
  if (th) {
    const s = th.stats?.overall || {};
    const best = th.best, worst = th.worst;
    const bestStrategy = topPnlBucket(th.stats?.by_strategy_tag);
    const bestSource = topPnlBucket(th.stats?.by_signal_source);
    $('tradeStats').textContent = `${s.wins||0}W / ${s.losses||0}L`;
    $('tradeSummary').innerHTML = th.trades.length ? `
      <div class="summary-item"><div class="val info">${th.stats?.total_trades||0}</div><div class="lbl">Trades</div></div>
      <div class="summary-item"><div class="val ${(s.win_rate_pct||0)>=50?'positive':'negative'}">${(s.win_rate_pct||0).toFixed(1)}%</div><div class="lbl">Win Rate</div></div>
      <div class="summary-item"><div class="val ${cls(s.total_pnl||0)}">${fmt(s.total_pnl||0)}</div><div class="lbl">Total P&L</div></div>
      <div class="summary-item"><div class="val positive">${best?'$'+fmt(best.pnl||0):'—'}</div><div class="lbl">${best?best.symbol+' Best':'Best'}</div></div>
      <div class="summary-item"><div class="val negative">${worst?'$'+fmt(worst.pnl||0):'—'}</div><div class="lbl">${worst?worst.symbol+' Worst':'Worst'}</div></div>
      <div class="summary-item" title="${bestStrategy?bestStrategy.name:''}"><div class="val val-sm ${bestStrategy&&bestStrategy.pnl>=0?'positive':'negative'}">${bestStrategy?bestStrategy.name.replace('_',' '):'—'}</div><div class="lbl">Top Strategy</div></div>
      <div class="summary-item" title="${bestSource?bestSource.name:''}"><div class="val val-sm ${bestSource&&bestSource.pnl>=0?'positive':'negative'}">${bestSource?bestSource.name:'—'}</div><div class="lbl">Top Source</div></div>
      <div class="summary-item"><div class="val info">${typeof s.avg_signal_to_fill_ms==='number'?Math.round(s.avg_signal_to_fill_ms)+'ms':'—'}</div><div class="lbl">Avg Sig→Fill</div></div>
    ` : '';
    $('tradeHistory').innerHTML = th.trades.length ? th.trades.slice().reverse().map(t => `<tr>
      <td><strong>${t.symbol||'?'}</strong></td>
      <td>$${(t.entry_price||0).toFixed(2)}</td><td>$${(t.exit_price||0).toFixed(2)}</td>
      <td class="${cls(t.pnl||0)}"><strong>${fmt(t.pnl||0)}</strong></td>
      <td class="${cls(t.pnl_pct||0)}">${fmt(t.pnl_pct||0)}%</td>
      <td>${t.reason||'—'}</td><td>${holdStr(t.hold_seconds)}</td>
      <td>${t.strategy_tag||'—'}</td>
      <td style="font-size:11px;color:#8b949e">${Array.isArray(t.signal_sources)?t.signal_sources.join(', '):(t.signal_sources||'—')}</td>
      <td class="${(t.slippage_bps||0) > 0 ? 'negative' : 'positive'}">${fmt(t.slippage_bps||0, 1)}</td>
      <td class="info">${typeof t.signal_to_fill_ms==='number'?Math.round(t.signal_to_fill_ms):'—'}</td>
    </tr>`).join('') : '<tr><td colspan="11" class="empty">No completed trades yet</td></tr>';
  }
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
  const pos = await api('/api/positions');
  $('positions').innerHTML = pos && pos.length ? pos.map(p => {
    const isPending = p.order_status === 'pending';
    const statusBadge = isPending ? '<span class="tag" style="background:#e3b34122;color:#e3b341;border:1px solid #e3b34144;margin-left:4px">PENDING</span>' : '';
    return `<tr style="${isPending ? 'opacity:0.7' : ''}">
    <td><strong>${p.symbol}</strong>${statusBadge}</td><td>${(p.side||'long').toUpperCase()}</td><td>${p.quantity}</td><td>$${p.entry_price}</td>
    <td>${isPending ? '<span style="color:#e3b341">awaiting fill</span>' : '$'+p.current_price}</td><td class="${cls(p.pnl)}">${isPending ? '—' : fmt(p.pnl)+' ('+fmt(p.pnl_pct)+'%)'}</td><td>${p.trail_pct||3}%</td><td>${isPending ? 'limit order' : (p.protection||'?')}</td><td>${isPending ? '—' : p.hold_time}</td>
  </tr>`;
  }).join('') : '<tr><td colspan="6" class="empty">No open positions</td></tr>';
  // Candidates
  const cand = await api('/api/candidates');
  $('candidates').innerHTML = cand && cand.length ? cand.slice(0,10).map(c => `<tr>
    <td><strong>${c.symbol}</strong>${c.uw_budget_mode ? `<div style="font-size:11px;color:#8b949e">${c.uw_budget_mode}</div>` : ''}</td><td>$${(c.price||0).toFixed(2)}</td>
    <td class="${cls(c.change_pct||0)}">${fmt(c.change_pct||0,1)}%</td>
    <td>${(c.volume_spike||0).toFixed(1)}x</td><td>${(c.sentiment_score||0).toFixed(2)}</td>
    <td><strong>${(c.score||0).toFixed(3)}</strong></td>
    <td style="font-size:11px;color:#8b949e;max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${((c.uw_news_summary||'') + ' | ' + (c.uw_chain_summary||'')).replace(/"/g,'&quot;')}">${c.uw_chain_summary || c.uw_news_summary || '—'}</td>
  </tr>`).join('') : '<tr><td colspan="7" class="empty">No candidates yet</td></tr>';
  // Activity Feed
  const activity = await api('/api/activity?limit=30');
  if (activity && activity.length) {
    const catColors = {thinking:'#8b949e',scan:'#58a6ff',trade:'#3fb950',ai:'#d2a8ff',alert:'#f85149',research:'#f0883e'};
    $('activityFeed').innerHTML = activity.reverse().map(a => {
      const color = catColors[a.category] || '#8b949e';
      return `<div style="padding:3px 0;border-bottom:1px solid #21262d33"><span style="color:#484f58;font-size:11px">${a.time_str}</span> <span style="color:${color};font-weight:600">[${a.category}]</span> ${a.message}</div>`;
    }).join('');
  }

  // Watchlist
  const wl = await api('/api/watchlist');
  $('watchlistCount').textContent = wl ? `${wl.length} tickers` : '';
  $('watchlist').innerHTML = wl && wl.length ? wl.slice(0,15).map(w => `<tr>
    <td><strong>${w.ticker}</strong></td>
    <td>${w.side === 'short' ? '<span style="color:#f85149">🔴 SHORT</span>' : '<span style="color:#3fb950">🟢 LONG</span>'}</td>
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
  $('candidateMeta').innerHTML = intelligence ? `
    <span><strong>UW budget:</strong> ${uwBudget}</span>
    <span style="margin-left:12px"><strong>Minute remaining:</strong> ${uwApi.minute_remaining ?? '—'}</span>
    <span style="margin-left:12px"><strong>Last path:</strong> ${uwApi.last_request_path || '—'}</span>
    ${uwFocus.length ? `<div style="margin-top:6px"><strong>Top UW context:</strong> ${uwFocus.map(row => `${row.symbol}: ${row.chain_summary || row.news_summary}`).join(' · ')}</div>` : ''}
  ` : '';
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
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>"""
