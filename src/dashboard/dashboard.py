"""
Dashboard - FastAPI backend + dark-theme HTML dashboard on port 8421.
Real-time positions, P&L, scanner, trades, controls.
"""

import os
import json
import time
import threading
from typing import Dict, List, Optional
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn
from loguru import logger

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings

app = FastAPI(title="exitBotauto", version="1.0.0")

# Global reference to bot instance (set by main.py)
_bot = None


def set_bot(bot):
    global _bot
    _bot = bot


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
        if _bot.polygon_client:
            try:
                price = _bot.polygon_client.get_price(p["symbol"])
            except:
                pass
        if not price:
            price = p.get("entry_price", 0)
        pnl = (price - p["entry_price"]) * p["quantity"] if p["entry_price"] else 0
        pnl_pct = ((price - p["entry_price"]) / p["entry_price"] * 100) if p["entry_price"] else 0
        hold_min = (time.time() - p.get("entry_time", time.time())) / 60
        enriched.append({
            "symbol": p["symbol"],
            "quantity": p["quantity"],
            "entry_price": round(p["entry_price"], 2),
            "current_price": round(price, 2),
            "pnl": round(pnl, 2),
            "pnl_pct": round(pnl_pct, 2),
            "hold_time": f"{int(hold_min)}m",
            "peak_price": round(p.get("peak_price", price), 2),
        })
    return enriched


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
    """Get consensus engine history and stats."""
    if not _bot or not _bot.entry_manager or not hasattr(_bot.entry_manager, 'consensus'):
        return {"enabled": False, "history": [], "stats": {}}
    engine = _bot.entry_manager.consensus
    return {
        "enabled": getattr(settings, 'CONSENSUS_ENABLED', True),
        "history": engine.get_history()[-10:],
        "stats": engine.get_stats(),
    }


@app.get("/api/candidates")
async def get_candidates():
    if not _bot or not _bot.scanner:
        return []
    return _bot.scanner.get_cached_candidates()


@app.get("/api/history")
async def get_history(limit: int = 20):
    if not _bot or not _bot.exit_manager:
        return []
    history = _bot.exit_manager.get_history(limit)
    for h in history:
        h["hold_time"] = f"{int(h.get('hold_seconds', 0) / 60)}m"
    return history


@app.get("/api/metrics")
async def get_metrics():
    if not _bot or not _bot.risk_manager:
        return {}
    return _bot.risk_manager.get_status()


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

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>exitBotauto Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;font-size:14px}
.header{background:#161b22;border-bottom:1px solid #30363d;padding:16px 24px;display:flex;justify-content:space-between;align-items:center}
.header h1{font-size:20px;color:#58a6ff}
.header .status{display:flex;gap:12px;align-items:center}
.badge{padding:4px 12px;border-radius:12px;font-size:12px;font-weight:600}
.badge.running{background:#238636;color:#fff}
.badge.paused{background:#d29922;color:#000}
.badge.stopped{background:#da3633;color:#fff}
.container{max-width:1400px;margin:0 auto;padding:20px;display:grid;grid-template-columns:1fr 1fr;gap:16px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px}
.card h2{font-size:14px;color:#8b949e;text-transform:uppercase;letter-spacing:1px;margin-bottom:12px;border-bottom:1px solid #21262d;padding-bottom:8px}
.full{grid-column:1/-1}
.metrics{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px}
.metric{text-align:center;padding:12px;background:#0d1117;border-radius:6px}
.metric .value{font-size:24px;font-weight:700;color:#58a6ff}
.metric .value.positive{color:#3fb950}
.metric .value.negative{color:#f85149}
.metric .label{font-size:11px;color:#8b949e;margin-top:4px}
table{width:100%;border-collapse:collapse}
th{text-align:left;font-size:11px;color:#8b949e;text-transform:uppercase;padding:8px;border-bottom:1px solid #21262d}
td{padding:8px;border-bottom:1px solid #21262d}
.positive{color:#3fb950}.negative{color:#f85149}
.controls{display:flex;gap:8px}
.btn{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px}
.btn-start{background:#238636;color:#fff}
.btn-pause{background:#d29922;color:#000}
.btn-stop{background:#da3633;color:#fff}
.btn:hover{opacity:0.85}
.empty{color:#484f58;text-align:center;padding:24px;font-style:italic}
</style>
</head>
<body>
<div class="header">
  <h1>🤖 exitBotauto</h1>
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
  <div class="card full">
    <h2>Performance</h2>
    <div class="metrics" id="metrics"></div>
  </div>
  <div class="card full">
    <h2>🧠 AI Layers <span id="aiEnabled" style="float:right;color:#8b949e"></span></h2>
    <div id="aiStatus" class="empty">Loading AI status...</div>
  </div>
  <div class="card full">
    <h2>🗳️ AI Consensus <span id="consensusStats" style="float:right;color:#8b949e"></span></h2>
    <table><thead><tr><th>Symbol</th><th>Claude</th><th>GPT</th><th>Perplexity</th><th>Decision</th><th>Confidence</th><th>Size</th></tr></thead>
    <tbody id="consensus"></tbody></table>
  </div>
  <div class="card full">
    <h2>💼 Alpaca Portfolio <span id="portfolioValue" style="float:right;color:#58a6ff"></span></h2>
    <table><thead><tr><th>Symbol</th><th>Shares</th><th>Avg Price</th><th>Current</th><th>Value</th><th>P&L</th></tr></thead>
    <tbody id="portfolio"></tbody></table>
  </div>
  <div class="card">
    <h2>Bot Positions</h2>
    <table><thead><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Current</th><th>P&L</th><th>Hold</th></tr></thead>
    <tbody id="positions"></tbody></table>
  </div>
  <div class="card">
    <h2>Scanner Candidates</h2>
    <table><thead><tr><th>Symbol</th><th>Price</th><th>Change</th><th>Vol Spike</th><th>Sentiment</th><th>Score</th></tr></thead>
    <tbody id="candidates"></tbody></table>
  </div>
  <div class="card full">
    <h2>Recent Trades</h2>
    <table><thead><tr><th>Symbol</th><th>Entry</th><th>Exit</th><th>Qty</th><th>P&L</th><th>%</th><th>Reason</th><th>Hold</th></tr></thead>
    <tbody id="history"></tbody></table>
  </div>
</div>
<script>
const $ = s => document.getElementById(s);
async function api(url, method='GET') {
  try { const r = await fetch(url, {method}); return await r.json(); } catch(e) { return null; }
}
function cls(v) { return v >= 0 ? 'positive' : 'negative'; }
function fmt(v, d=2) { return v != null ? (v >= 0 ? '+' : '') + v.toFixed(d) : '—'; }

async function refresh() {
  // Status
  const s = await api('/api/status');
  if (s) {
    const b = $('statusBadge');
    if (!s.running) { b.textContent='STOPPED'; b.className='badge stopped'; }
    else if (s.paused) { b.textContent='PAUSED'; b.className='badge paused'; }
    else { b.textContent='RUNNING'; b.className='badge running'; }
  }
  // Metrics
  const m = await api('/api/metrics');
  if (m) {
    $('metrics').innerHTML = `
      <div class="metric"><div class="value" style="color:#d2a8ff;font-size:18px">${m.tier_name||'?'}</div><div class="label">Risk Tier</div></div>
      <div class="metric"><div class="value">$${(m.equity||0).toLocaleString()}</div><div class="label">Equity</div></div>
      <div class="metric"><div class="value ${cls(m.daily_pnl||0)}">$${(m.daily_pnl||0).toFixed(2)}</div><div class="label">Daily P&L</div></div>
      <div class="metric"><div class="value ${cls(m.daily_pnl_pct||0)}">${fmt(m.daily_pnl_pct||0)}%</div><div class="label">Daily %</div></div>
      <div class="metric"><div class="value">${m.total_trades||0}</div><div class="label">Trades</div></div>
      <div class="metric"><div class="value">${((m.win_rate||0)*100).toFixed(0)}%</div><div class="label">Win Rate</div></div>
      <div class="metric"><div class="value">${m.consecutive_wins||0}W/${m.consecutive_losses||0}L</div><div class="label">Streak</div></div>
      <div class="metric"><div class="value">${(m.heat_pct||0).toFixed(0)}%</div><div class="label">Heat</div></div>
      <div class="metric"><div class="value">${(m.tier_size_pct||0)}%</div><div class="label">Position Size</div></div>
      <div class="metric"><div class="value">${m.tier_max_positions||0}</div><div class="label">Max Positions</div></div>
      <div class="metric"><div class="value">$${(m.ath_equity||0).toLocaleString()}</div><div class="label">ATH Equity</div></div>
      <div class="metric"><div class="value ${m.drawdown_pct > 0 ? 'negative' : ''}">${(m.drawdown_pct||0).toFixed(1)}%</div><div class="label">Drawdown</div></div>
      <div class="metric"><div class="value">${(m.total_return_pct||0).toFixed(1)}%</div><div class="label">Total Return</div></div>
      <div class="metric"><div class="value">$${(m.next_milestone||0).toLocaleString()}</div><div class="label">Next Milestone</div></div>
      <div class="metric"><div class="value">${(m.milestone_progress_pct||0).toFixed(0)}%</div><div class="label">→ Progress</div></div>
      <div class="metric"><div class="value">${(m.days_trading||0).toFixed(0)}d</div><div class="label">Days Trading</div></div>
    `;
  }
  // AI Status
  const ai = await api('/api/ai-status');
  if (ai) {
    $('aiEnabled').textContent = ai.enabled ? '✅ Active' : '❌ Disabled';
    if (ai.enabled) {
      let html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">';
      html += '<div><strong>Last Observation:</strong><br>' + (ai.last_observation ? ai.last_observation.substring(0, 200) + '...' : 'Pending...') + '</div>';
      html += '<div><strong>Last Advice:</strong><br>' + (ai.last_advice ? ai.last_advice.substring(0, 200) + '...' : 'Pending...') + '</div>';
      html += '</div>';
      if (ai.last_tuner_changes) html += '<div style="margin-top:8px"><strong>Tuner:</strong> ' + ai.last_tuner_changes + '</div>';
      $('aiStatus').innerHTML = html;
    } else {
      $('aiStatus').innerHTML = '<span class="empty">AI layers not initialized</span>';
    }
  }
  // Portfolio (Alpaca)
  const pf = await api('/api/portfolio');
  if (pf) {
    $('portfolioValue').textContent = `Cash: $${(pf.cash||0).toFixed(2)} | Total: $${(pf.total_value||0).toFixed(2)}`;
    $('portfolio').innerHTML = pf.positions && pf.positions.length ? pf.positions.map(p => {
      const val = (p.current_price * p.quantity).toFixed(2);
      const pnl = p.open_pnl || ((p.current_price - p.average_price) * p.quantity);
      const pnlPct = p.average_price ? ((p.current_price - p.average_price) / p.average_price * 100) : 0;
      return `<tr>
        <td><strong>${p.symbol}</strong></td><td>${p.quantity.toFixed(4)}</td>
        <td>$${(p.average_price||0).toFixed(2)}</td><td>$${(p.current_price||0).toFixed(2)}</td>
        <td>$${val}</td><td class="${cls(pnl)}">${fmt(pnl)} (${fmt(pnlPct)}%)</td>
      </tr>`;
    }).join('') : '<tr><td colspan="6" class="empty">No holdings</td></tr>';
  }
  // Consensus
  const con = await api('/api/consensus');
  if (con) {
    const st = con.stats || {};
    $('consensusStats').textContent = con.enabled ?
      `Agree: ${((st.agreement_rate||0)*100).toFixed(0)}% | Calls: ${st.total||0} | Cost: $${st.estimated_cost||0}` : '❌ Disabled';
    $('consensus').innerHTML = con.history && con.history.length ? con.history.slice().reverse().map(h => {
      const cv = h.claude, gv = h.gpt, pv = h.perplexity;
      const voteStr = v => v ? `<span class="${v.decision==='BUY'?'positive':'negative'}">${v.decision} ${v.confidence}%</span>` : '—';
      return `<tr><td><strong>${h.symbol}</strong></td><td>${voteStr(cv)}</td><td>${voteStr(gv)}</td><td>${voteStr(pv)}</td>
        <td class="${h.final_decision==='BUY'?'positive':'negative'}"><strong>${h.final_decision}</strong></td>
        <td>${(h.avg_confidence||0).toFixed(0)}%</td><td>${((h.size_modifier||0)*100).toFixed(0)}%</td></tr>`;
    }).join('') : '<tr><td colspan="7" class="empty">No consensus decisions yet</td></tr>';
  }
  // Bot Positions
  const pos = await api('/api/positions');
  $('positions').innerHTML = pos && pos.length ? pos.map(p => `<tr>
    <td><strong>${p.symbol}</strong></td><td>${p.quantity}</td><td>$${p.entry_price}</td>
    <td>$${p.current_price}</td><td class="${cls(p.pnl)}">${fmt(p.pnl)} (${fmt(p.pnl_pct)}%)</td><td>${p.hold_time}</td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty">No open positions</td></tr>';
  // Candidates
  const cand = await api('/api/candidates');
  $('candidates').innerHTML = cand && cand.length ? cand.slice(0,10).map(c => `<tr>
    <td><strong>${c.symbol}</strong></td><td>$${(c.price||0).toFixed(2)}</td>
    <td class="${cls(c.change_pct||0)}">${fmt(c.change_pct||0,1)}%</td>
    <td>${(c.volume_spike||0).toFixed(1)}x</td><td>${(c.sentiment_score||0).toFixed(2)}</td>
    <td>${(c.score||0).toFixed(3)}</td>
  </tr>`).join('') : '<tr><td colspan="6" class="empty">No candidates yet</td></tr>';
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
