"""
Dashboard - FastAPI backend + dark-theme HTML dashboard on port 8421.
Real-time positions, P&L, scanner, trades, controls.
"""

import os
import json
import time
import threading
import hmac
from typing import Dict, List, Optional
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn
from loguru import logger

from config import settings

app = FastAPI(title="Velox", version="2.0.0")

# Global reference to bot instance (set by main.py)
_bot = None

# Activity feed — circular buffer of bot thoughts/actions
_activity_feed: List[Dict] = []
_MAX_FEED_SIZE = 100


def set_bot(bot):
    global _bot
    _bot = bot


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
    }


@app.get("/api/consensus")
async def get_consensus():
    """Get agent orchestrator history and stats."""
    if not _bot or not hasattr(_bot, 'orchestrator') or not _bot.orchestrator:
        return {"enabled": False, "history": [], "stats": {}}
    return {
        "enabled": True,
        "history": _bot.orchestrator.get_history()[-10:],
        "stats": _bot.orchestrator.get_stats(),
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

    # Realized P&L from persistence
    pnl = getattr(_bot, 'pnl_state', {})
    total_realized = pnl.get("total_realized_pnl", 0)
    today_realized = pnl.get("today_realized_pnl", 0)
    options_realized = pnl.get("options_total_realized_pnl", 0)
    total_trades = pnl.get("total_trades", 0)
    wins = pnl.get("winning_trades", 0)
    losses = pnl.get("losing_trades", 0)
    best = pnl.get("best_trade", 0)
    worst = pnl.get("worst_trade", 0)

    # Account equity + unrealized from Alpaca (source of truth)
    equity = 25000.0
    unrealized = 0
    options_unrealized = 0.0
    starting = pnl.get("starting_equity", 25000.0)
    peak = pnl.get("peak_equity", 25000.0)
    if _bot.alpaca_client:
        try:
            acct = _bot.alpaca_client.get_account()
            equity = float(acct.get("equity", 25000.0))
            # Get unrealized directly from Alpaca positions
            alpaca_positions = _bot.alpaca_client.get_positions()
            unrealized = sum(float(p.get("unrealized_pnl", p.get("unrealized_pl", p.get("open_pnl", 0)))) for p in alpaca_positions)
            if equity > peak:
                peak = equity
                pnl["peak_equity"] = peak
        except Exception:
            pass

    if _bot and getattr(_bot, "options_engine", None):
        try:
            opt_positions = _bot.options_engine.get_positions_snapshot(refresh_quotes=False)
            options_unrealized = sum(float(p.get("pnl", 0) or 0) for p in opt_positions)
        except Exception:
            options_unrealized = 0.0

    # Total P&L = equity - starting (the only truth that matters)
    total_pnl = equity - starting
    today_pnl = total_pnl - total_realized + today_realized  # approximate today
    win_rate = (wins / total_trades * 100) if total_trades > 0 else 0
    drawdown = ((peak - equity) / peak * 100) if peak > 0 else 0
    roi = ((equity - starting) / starting * 100) if starting > 0 else 0

    return {
        "equity": round(equity, 2),
        "starting_equity": round(starting, 2),
        "peak_equity": round(peak, 2),
        "total_pnl": round(total_pnl, 2),
        "total_realized": round(total_realized, 2),
        "unrealized": round(unrealized, 2),
        "options_realized_pnl": round(options_realized, 2),
        "options_unrealized_pnl": round(options_unrealized, 2),
        "today_realized": round(total_pnl, 2),  # On day 1, today = total
        "total_trades": total_trades,
        "winning_trades": wins,
        "losing_trades": losses,
        "win_rate": round(win_rate, 1),
        "best_trade": round(best, 2),
        "worst_trade": round(worst, 2),
        "drawdown_pct": round(drawdown, 2),
        "roi_pct": round(roi, 2),
        "open_positions": len(_bot.entry_manager.get_positions()) if _bot and _bot.entry_manager else 0,
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

    # Sector rotation
    if hasattr(_bot, 'sector_model') and _bot.sector_model:
        result["sectors"] = _bot.sector_model.get_dashboard_data()
        result["sector_bias"] = _bot.sector_model.get_sector_bias()
        focus = _bot.sector_model.suggest_focus()
        result["sector_focus"] = focus

    return result


@app.get("/api/streams")
async def get_streams():
    """Get WebSocket stream status."""
    if not _bot:
        return {}
    return {
        "market": _bot.market_stream.get_stats() if hasattr(_bot, 'market_stream') and _bot.market_stream else {},
        "trade": _bot.trade_stream.get_stats() if hasattr(_bot, 'trade_stream') and _bot.trade_stream else {},
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
    set_bot(bot)
    host = settings.DASHBOARD_HOST
    port = settings.DASHBOARD_PORT
    logger.info(f"📊 Dashboard starting on http://{host}:{port}")

    def _run():
        uvicorn.run(app, host=host, port=port, log_level="warning")

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


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
.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:10px}
.metrics.pnl-grid{grid-template-columns:repeat(5,1fr)}
.metric{text-align:center;padding:14px 10px;background:linear-gradient(145deg,#0d1117,#161b22);border-radius:8px;border:1px solid #21262d;transition:all .3s}
.metric:hover{border-color:#30363d;transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.3)}
.metric .value{font-size:22px;font-weight:800;color:#58a6ff;transition:all .3s}
.metric .value.positive{color:#3fb950}
.metric .value.negative{color:#f85149}
.metric .value.animated{animation:countUp .4s ease-out}
.metric .label{font-size:10px;color:#6e7681;margin-top:6px;text-transform:uppercase;letter-spacing:.5px}
.big-pnl{font-size:36px!important;font-weight:900!important;animation:neonPulse 2s ease-in-out infinite}
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
.btn:hover{transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.4)}
.btn:active{transform:translateY(0)}
.empty{color:#484f58;text-align:center;padding:24px;font-style:italic}
.summary-row{display:flex;gap:16px;flex-wrap:wrap;margin-bottom:14px}
.summary-item{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:10px 16px;flex:1;min-width:120px;text-align:center}
.summary-item .val{font-size:20px;font-weight:800}
.summary-item .lbl{font-size:10px;color:#6e7681;text-transform:uppercase;margin-top:2px}
.ai-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.ai-card{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;font-size:12px;line-height:1.5;max-height:200px;overflow-y:auto;word-wrap:break-word;overflow-wrap:break-word}
.ai-card strong{color:#58a6ff;display:block;margin-bottom:4px}
.ai-card.tuner{border-color:#d2a8ff33}.ai-card.pm{border-color:#3fb95033}
.watermark{text-align:center;padding:20px;color:#21262d;font-size:11px;letter-spacing:2px}
</style>
</head>
<body>
<div class="header">
  <h1><span class="logo"><svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg"><defs><linearGradient id="vg" x1="0" y1="0" x2="100" y2="100"><stop offset="0%" stop-color="#58a6ff"/><stop offset="100%" stop-color="#a371f7"/></linearGradient></defs><path d="M15 75L45 15L55 45L85 15L55 75L45 50Z" fill="url(#vg)"/></svg></span>VELOX <span class="scan-dot idle" id="scanDot"></span></h1>
  <div class="status">
    <span id="statusBadge" class="badge stopped">LOADING</span>
    <div class="controls">
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
    <div class="metrics pnl-grid" id="pnlMetrics">
      <div class="metric"><div class="value big-pnl" id="totalPnl">$0.00</div><div class="label">Total P&L</div></div>
      <div class="metric"><div class="value" id="equity">$1,000</div><div class="label">Equity</div></div>
      <div class="metric"><div class="value" id="todayPnl">$0.00</div><div class="label">Today P&L</div></div>
      <div class="metric"><div class="value" id="unrealized">$0.00</div><div class="label">Unrealized</div></div>
      <div class="metric"><div class="value" id="roi">0%</div><div class="label">ROI</div></div>
      <div class="metric"><div class="value" id="winRate">0%</div><div class="label">Win Rate</div></div>
      <div class="metric"><div class="value" id="totalTrades">0</div><div class="label">Trades</div></div>
      <div class="metric"><div class="value" id="drawdown">0%</div><div class="label">Drawdown</div></div>
      <div class="metric"><div class="value positive" id="bestTrade">$0</div><div class="label">Best Trade</div></div>
      <div class="metric"><div class="value negative" id="worstTrade">$0</div><div class="label">Worst Trade</div></div>
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
    <table><thead><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>P&L $</th><th>P&L %</th><th>Reason</th><th>Hold</th><th>Strategy</th><th>Sources</th><th>Slip(bps)</th></tr></thead>
    <tbody id="tradeHistory"></tbody></table>
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
    <table><thead><tr><th>Underlying</th><th>Contract</th><th>Type</th><th>Strike</th><th>Expiry</th><th>Qty</th><th>Entry</th><th>Current</th><th>Bid</th><th>Ask</th><th>P&L%</th><th>DTE</th><th>Status</th></tr></thead>
    <tbody id="optionsPositions"></tbody></table>
  </div>

  <!-- Activity Feed + Watchlist side by side -->
  <div class="card">
    <h2><span class="icon">🧠</span> Bot Activity Feed</h2>
    <div id="activityFeed" style="max-height:400px;overflow-y:auto;font-size:12px;line-height:1.8;word-wrap:break-word;overflow-wrap:break-word"></div>
  </div>
  <div class="card">
    <h2><span class="icon">📋</span> Watchlist <span id="watchlistCount" style="margin-left:auto;color:#6e7681;font-size:11px;font-weight:400"></span></h2>
    <table><thead><tr><th>Ticker</th><th>Side</th><th>Conv</th><th>Source</th><th>Reason</th></tr></thead>
    <tbody id="watchlist"></tbody></table>
  </div>

  <!-- Positions + Candidates side by side -->
  <div class="card">
    <h2><span class="icon">📈</span> Bot Positions</h2>
    <table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th>Trail%</th><th>Protection</th><th>Hold</th></tr></thead>
    <tbody id="positions"></tbody></table>
  </div>
  <div class="card">
    <h2><span class="icon">🔍</span> Scanner Candidates</h2>
    <table><thead><tr><th>Symbol</th><th>Price</th><th>Change</th><th>Vol</th><th>Sent</th><th>Score</th></tr></thead>
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

<script>
const $ = s => document.getElementById(s);
let _prevPnl = null;
const _dashToken = new URLSearchParams(window.location.search).get('token') || '';
function withToken(url) {
  if (!_dashToken) return url;
  return url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(_dashToken);
}
async function api(url, method='GET') {
  try {
    const headers = _dashToken ? {'Authorization': `Bearer ${_dashToken}`} : {};
    const r = await fetch(withToken(url), {method, headers});
    return await r.json();
  } catch(e) { return null; }
}
function cls(v) { return v >= 0 ? 'positive' : 'negative'; }
function fmt(v, d=2) { return v != null ? (v >= 0 ? '+' : '') + v.toFixed(d) : '—'; }
function holdStr(secs) { if(!secs) return '—'; const m=Math.floor(secs/60); const h=Math.floor(m/60); return h>0?h+'h '+m%60+'m':m+'m'; }
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
    const setPnl = (id, val, prefix='$') => {
      const el = $(id);
      if (!el) return;
      el.textContent = prefix + (typeof val === 'number' ? val.toLocaleString(undefined, {minimumFractionDigits:2, maximumFractionDigits:2}) : val);
      el.className = 'value' + (typeof val === 'number' && val < 0 ? ' negative' : typeof val === 'number' && val > 0 ? ' positive' : '');
    };
    $('totalPnl').textContent = '$' + (pnl.total_pnl||0).toFixed(2);
    $('totalPnl').className = 'value big-pnl ' + cls(pnl.total_pnl||0);
    setPnl('equity', pnl.equity||0);
    setPnl('todayPnl', pnl.today_realized||0);
    setPnl('unrealized', pnl.unrealized||0);
    $('winRate').textContent = (pnl.win_rate||0).toFixed(0) + '%';
    $('winRate').className = 'value' + ((pnl.win_rate||0) >= 50 ? ' positive' : (pnl.total_trades > 0 ? ' negative' : ''));
    $('totalTrades').textContent = pnl.total_trades||0;
    $('totalTrades').className = 'value info';
    $('roi').textContent = (pnl.roi_pct||0).toFixed(1) + '%';
    $('roi').className = 'value ' + cls(pnl.roi_pct||0);
    $('drawdown').textContent = (pnl.drawdown_pct||0).toFixed(1) + '%';
    $('drawdown').className = 'value' + ((pnl.drawdown_pct||0) > 2 ? ' negative' : '');
    $('bestTrade').textContent = '$' + (pnl.best_trade||0).toFixed(2);
    $('bestTrade').className = 'value positive';
    $('worstTrade').textContent = '$' + (pnl.worst_trade||0).toFixed(2);
    $('worstTrade').className = 'value negative';
    $('pnlTimestamp').textContent = 'Updated: ' + new Date().toLocaleTimeString()
      + ` | Opt R/U: $${(pnl.options_realized_pnl||0).toFixed(2)} / $${(pnl.options_unrealized_pnl||0).toFixed(2)}`;
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
      <div class="metric"><div class="value" style="color:#d2a8ff;font-size:16px">${m.tier_name||'?'}</div><div class="label">Risk Tier</div></div>
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
      <div class="summary-item"><div class="val info">${st.total||0}</div><div class="lbl">Total Evals</div></div>
      <div class="summary-item"><div class="val positive">${st.buys||0}</div><div class="lbl">BUY Signals</div></div>
      <div class="summary-item"><div class="val" style="color:#d2a8ff">${st.shorts||0}</div><div class="lbl">SHORT Signals</div></div>
      <div class="summary-item"><div class="val negative">${st.skips||0}</div><div class="lbl">SKIP Signals</div></div>
      <div class="summary-item"><div class="val" style="color:#e3b341">${st.avg_confidence?(st.avg_confidence).toFixed(0)+'%':'—'}</div><div class="lbl">Avg Confidence</div></div>
      <div class="summary-item"><div class="val" style="color:#8b949e;font-size:11px">🟣${ac.claude||0} 🟢${ac.gpt||0} 🔵${ac.grok||0} 🟠${ac.perplexity||0}</div><div class="lbl">API Calls</div></div>
    ` : '';
    $('consensus').innerHTML = con.history && con.history.length ? con.history.slice().reverse().map(h => {
      const decCls = h.decision==='BUY'?'tag-buy':h.decision==='SHORT'?'tag-short':'tag-skip';
      const reason = (h.reasoning||'').substring(0, 120) + ((h.reasoning||'').length > 120 ? '...' : '');
      return `<tr><td><strong>${h.symbol}</strong></td>
        <td><span class="tag ${decCls}">${h.decision}</span></td>
        <td>${(h.confidence||0).toFixed(0)}%</td>
        <td>${(h.size_pct||0).toFixed(1)}%</td>
        <td>${(h.trail_pct||0).toFixed(1)}%</td>
        <td style="font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${reason}</td></tr>`;
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
      <div class="summary-item"><div class="val info">${th.stats?.total_trades||0}</div><div class="lbl">Total Trades</div></div>
      <div class="summary-item"><div class="val ${(s.win_rate_pct||0)>=50?'positive':'negative'}">${(s.win_rate_pct||0).toFixed(1)}%</div><div class="lbl">Win Rate</div></div>
      <div class="summary-item"><div class="val ${cls(s.total_pnl||0)}">${fmt(s.total_pnl||0)}</div><div class="lbl">Total P&L</div></div>
      <div class="summary-item"><div class="val positive">${best?'$'+fmt(best.pnl||0):'—'}</div><div class="lbl">Best Trade ${best?best.symbol:''}</div></div>
      <div class="summary-item"><div class="val negative">${worst?'$'+fmt(worst.pnl||0):'—'}</div><div class="lbl">Worst Trade ${worst?worst.symbol:''}</div></div>
      <div class="summary-item"><div class="val ${bestStrategy&&bestStrategy.pnl>=0?'positive':'negative'}">${bestStrategy?bestStrategy.name:'—'}</div><div class="lbl">Top Strategy</div></div>
      <div class="summary-item"><div class="val ${bestSource&&bestSource.pnl>=0?'positive':'negative'}">${bestSource?bestSource.name:'—'}</div><div class="lbl">Top Source</div></div>
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
    </tr>`).join('') : '<tr><td colspan="10" class="empty">No completed trades yet</td></tr>';
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
  if (ops) {
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
    <td><strong>${c.symbol}</strong></td><td>$${(c.price||0).toFixed(2)}</td>
    <td class="${cls(c.change_pct||0)}">${fmt(c.change_pct||0,1)}%</td>
    <td>${(c.volume_spike||0).toFixed(1)}x</td><td>${(c.sentiment_score||0).toFixed(2)}</td>
    <td><strong>${(c.score||0).toFixed(3)}</strong></td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty">No candidates yet</td></tr>';
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
