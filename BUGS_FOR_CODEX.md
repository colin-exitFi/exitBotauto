# 🐛 VELOX BUG REPORT — March 6, 2026
## Complete forensic audit for Codex to fix before Monday market open

**Account:** $24,910.30 (started $25,000, down $89.70 total across 2 days)
**Positions:** 8 open, -$86.45 unrealized
**Trades today:** 16 total, 31% win rate, but $1,393 of that is PHANTOM

---

## CRITICAL BUGS (Fix these first)

### BUG 1: BREAKOUT FAST-PATH BYPASSES JURY VETO
**Severity: CRITICAL — caused the BATL loss**
**File:** `src/main.py` → `_on_breakout_detected()` and `_handle_fast_path_breakout()`

**What happened:** The jury evaluated BATL **12+ times** between 3 AM and 9:28 AM. Every single time: SKIP. Reasons: "fatal volume" (0.0-0.3x avg), "decelerating momentum," "RSI 95 overbought."

At 9:28 AM, the breakout detector fired (`🚀 BREAKOUT: BATL +31.8% @ $25.14 vol 4.2x`) and entered the trade **without checking jury history**. The fast-path has NO memory of prior jury SKIPs.

**Result:** Bought BATL at $25.20, now at $22.36 = **-11.3% loss** ($39.76)

**Fix:**
1. Maintain a `jury_vetoed_symbols: Dict[str, datetime]` that persists across scan cycles
2. When jury returns SKIP, add symbol with timestamp
3. Fast-path breakout detector MUST check this set — if symbol was SKIPped in last 60 minutes, reject
4. Clear entries after 60 min or on jury BUY override

**Where in code:**
- `_on_breakout_detected()` line ~1853: Add check before calling `_handle_fast_path_breakout()`
- `_process_candidates()` line ~1385: When jury says SKIP, add to vetoed set

---

### BUG 2: PHANTOM FSLY OPTION P&L ($1,393 fake profit)
**Severity: CRITICAL — corrupts all P&L tracking**
**File:** `src/options/options_monitor.py` and/or `src/options/options_engine.py`

**What happened:** Bot placed a limit order for FSLY260320C00021000 at $1.24 at 3:10 AM. The option actually filled at 2:28 PM at $1.20. BUT the risk manager recorded a phantom $1,393 profit at 8:30 AM — 6 hours before the fill:

```
08:30:04.415 | Trade recorded: FSLY260320C00021000 pnl=$1393.00
08:30:04.415 | Round trip #6: FSLY260320C00021000 (held 5.3h, $+1393.00)
```

**The option is still OPEN at -25% ($-30.00 unrealized).** The $1,393 profit never happened.

**Fix:**
1. Options exits must ONLY be finalized on confirmed fill data (trade_stream fill event or broker reconciliation)
2. Never record P&L from order submission — only from confirmed fills
3. Add validation: if position still exists in brokerage after "exit" recorded, flag as error

---

### BUG 3: PDT GUARD IS WAY TOO CONSERVATIVE  
**Severity: CRITICAL — blocked 613 entries including DAWN +66%**
**File:** `src/risk/risk_manager.py` → `can_open_position()` line ~287

**What happened:** PDT guard shows `daytrade_count: 10/3` which is impossible — Alpaca API actually shows `daytrade_count: 3`. The bot is counting its OWN internal round trips (10) instead of using the Alpaca API's actual `daytrade_count` field.

The guard blocks ALL new entries when daytrade_count >= 3 AND equity < $25K. This blocked 613 entry attempts today, including **DAWN (+66% with 20x volume) which the jury approved 15 times**.

**Evidence:**
```
09:37:16 | Jury verdict for DAWN: BUY size=1.5% conf=68% — DAWN fits momentum-long framework
09:37:16 | 🔑 DAWN REACHED ENTRY BLOCK (orchestrator=True)
09:37:16 | 🚨 PDT GUARD: 9/3 day trades used, equity $24,977 < $25K — BLOCKED
```

**Fix (multi-part):**
1. Use Alpaca's `account.daytrade_count` (verified: currently 3), NOT internal round trip count
2. When PDT count >= 3 AND equity < $25K:
   - ALLOW new entries (they're not day trades yet)
   - Mark these positions as `swing_only = True`
   - BLOCK same-day exits on swing_only positions (that's what creates the day trade)
   - Allow next-day+ exits normally
3. On 403 code 40310100 from Alpaca ("pattern day trading protection"):
   - Stop retrying (currently retries every 30 seconds forever)
   - Mark position as swing_only
   - Remove from exit queue for today
4. When equity >= $25K: disable PDT guard entirely (no restriction with $25K+)

**The fresh $25K account Monday will fix the immediate issue, but the code still needs this logic for future sub-$25K scenarios.**

---

### BUG 4: EXTENDED HOURS GUARD CONFLICTS WITH EXIT AGENT
**Severity: HIGH — froze 7 positions for hours**
**File:** `src/exit/extended_hours_guard.py`

**What happened:** Bot placed extended-hours limit sell orders overnight on 7 positions (BW, NPT, KR, VG, UCO, SOC, TTDU). When market opened and Exit Agent tried to market-sell these positions, Alpaca rejected:

```
"insufficient qty available for order (requested: 35, available: 0)"
"related_orders": ["7c5441e9-..."]
```

The shares were locked by the overnight limit orders. The bot NEVER cancels old orders before placing new ones.

**Result:** 4,100 failed "extended guard" attempts. 886 failed trailing stop attempts. 7 positions frozen until 2:30 PM when the limit orders finally filled.

**Fix:**
1. Before ANY sell order (market, limit, trailing stop): 
   - Query open orders for that symbol: `GET /v2/orders?status=open&symbols={symbol}`
   - Cancel all existing sell orders: `DELETE /v2/orders/{order_id}`
   - Wait for cancellation confirmation
   - THEN place the new order
2. On "insufficient qty available" error:
   - Parse `related_orders` from error response
   - Cancel those specific orders
   - Retry the original sell
3. Extended hours guard should tag its orders so Exit Agent knows to cancel them at market open

---

### BUG 5: ADVISOR COMPLETELY BROKEN
**Severity: HIGH — self-improvement layer is dead**
**File:** `src/ai/advisor.py` → `run()` line ~147

**Error:** `'dict' object has no attribute 'append'` — 34 times today

**What happened:** The advisor is trying to `.append()` to a dict instead of a list. Probably a data structure changed upstream and advisor wasn't updated.

**Fix:** Find the `.append()` call in `advisor.py` around line 147. Either:
- Change the dict to a list
- Or use `dict[key] = value` instead of `dict.append()`

---

### BUG 6: FSLY BOUGHT THREE TIMES (Position Accumulation Bug)
**Severity: HIGH — no duplicate position protection**
**File:** `src/entry/entry_manager.py`

**What happened:**
```
03:33 - Buy 9 FSLY @ $20.45 (limit fill)
09:28 - Buy 17.59 FSLY @ $21.32 (market, breakout fast-path)  
09:28 - Buy 8.78 FSLY @ $21.35 (market, ???)
14:28 - Buy 1 FSLY260320C00021000 @ $1.20 (option)
```

Total: 35.37 shares of FSLY equity + 1 option contract. The bot bought the same stock THREE times without checking if it already holds a position.

**Fix:**
1. Before entering any position, check if symbol already exists in portfolio
2. If position exists: SKIP entry (or only allow if explicitly configured to scale in)
3. Options on the same underlying should also be tracked against the equity position

---

### BUG 7: DUAL PROCESS RUNNING
**Severity: HIGH — duplicate orders, duplicate fills**
**File:** Process management / startup script

**Evidence:** Two separate log files logging the same events:
- `bot_2026-03-06.log` — one process
- `bot_restart_2035.log` — another process

Both are logging fills at the same time. Trade stream messages appear TWICE. This means two bot instances are running simultaneously, potentially placing duplicate orders.

**Fix:**
1. On startup: check for existing process (`pidof` or lockfile)
2. Kill any existing process before starting new one
3. Use a lockfile (`/tmp/velox.lock`) to prevent concurrent instances
4. `pkill -9 -f "src.main"` before every restart

---

## MODERATE BUGS

### BUG 8: MASSIVE API RATE LIMITING (7,022 skipped calls)
**Severity: MODERATE — agents are flying blind**

Rate limit hits today:
- Claude: **2,781** skips
- GPT: **1,934** skips  
- Grok: **1,404** skips
- Perplexity: **903** skips
- **Total: 7,022 skipped AI calls**

This means the 5-agent consensus was running with 1-2 agents most of the time. Agent failures:
- Catalyst: 940 failures
- Sentiment: 909 failures  
- Macro: 723 failures
- Risk: 713 failures (BLOCKS by default when it fails!)
- Technical: 666 failures

**When Risk agent fails, it BLOCKS by default.** This caused DAWN to be blocked even when other agents approved.

**Fix:**
1. Implement proper rate limit backoff (exponential, not skip)
2. Cache agent results for same symbol within 5 minutes
3. When Risk agent fails, DEFAULT TO APPROVE with reduced size (not block)
4. Rotate providers more aggressively (if Claude is limited, use GPT, then Grok)
5. Consider reducing scan frequency during rate limit periods (every 10 min instead of 5)

---

### BUG 9: WASH SALE RULE TOO AGGRESSIVE ON PAPER ACCOUNT
**Severity: MODERATE — blocking valid re-entries**
**File:** `src/risk/risk_manager.py`

Wash sale protection is blocking re-entry on IREN, BW, TTDU for 30 days because they were sold at a loss (losses of $0.85 - $1.68). On a paper trading account, wash sale rules don't apply.

**Fix:**
1. Add config flag: `PAPER_MODE=true` → disable wash sale protection
2. Or reduce the wash sale window from 30 days to 1 day for paper accounts

---

### BUG 10: OPTIONS PDT RETRY SPAM (122 retries)
**Severity: MODERATE — wasting API calls**
**File:** Options order placement code

Bot tried to buy/sell options 122 times, all rejected with `40310100` (PDT protection). It retries every ~30 seconds without learning.

**Fix:**
1. On PDT rejection (40310100): stop retrying for that symbol/session
2. Cache PDT-blocked symbols for the day
3. Only retry after next trading day or when equity crosses $25K

---

### BUG 11: TRAILING STOP NOT PLACED ON ENTRY ("⚠️ NO TRAILING STOP")
**Severity: MODERATE — positions unprotected**
**File:** `src/entry/entry_manager.py` line ~354

Multiple entries logged `⚠️ NO TRAILING STOP` — the entry succeeds but trailing stop placement fails, leaving positions completely unprotected.

Then the monitor tries to place them later but gets "insufficient qty" errors because extended hours guards already hold the shares.

**Fix:**
1. If trailing stop fails on entry: retry 3 times with 1-second delay
2. If still fails: cancel any conflicting orders first, then retry
3. If STILL fails: log CRITICAL error and alert (this position is naked)
4. Never enter if trailing stop can't be placed (or make it configurable)

---

### BUG 12: TUNER PERMANENTLY LOCKED
**Severity: LOW — but self-improvement is dead**
**File:** `src/ai/tuner.py` line ~99

```
🔧 Tuner: LOCKED — need 4 more trades before tuning (have 16)
```

Tuner needs 20 trades to unlock, currently at 16. This is by design but the threshold may be too high given the bot only placed ~15 trades in 2 days. Consider lowering to 10.

---

## STRATEGY BUGS (Not code bugs, but logic issues)

### STRATEGY 1: NO "FADE THE RUNNER" CAPABILITY
**The biggest missed opportunity of the day.**

BATL ran +40% on March 5. The bot BOUGHT it again on March 6. It should have SHORTED it or bought puts. The `yesterdays_runners.json` file tracks yesterday's big movers but uses them as BUY candidates.

**What to build:**
1. If symbol is in `yesterdays_runners.json` with >20% gain:
   - Day 2 default strategy: FADE (short or puts)
   - Only go LONG if fresh catalyst + volume > day 1
2. Check RSI: if >80 on day 2, it's overbought → fade
3. Check volume: if day-2 volume < day-1 volume, conviction is fading → short

---

### STRATEGY 2: ONLY 4% CAPITAL DEPLOYED ($1,077 / $25K)
The bot is sitting on 96% cash. Even with PDT constraints, it should be deploying more into swing-trade-safe positions. The observer even noted this:

```
"$21K cash earning nothing while momentum runners like UVIX (+25%), DAWN (+64%) are moving"
```

**Fix:** When PDT-constrained, shift to swing mode with larger position sizes (2-4% per position vs 1.5%).

---

### STRATEGY 3: NO SHORT/PUT CAPABILITY BEING USED
168 BUY verdicts but only 45 SELL and 17 SHORT verdicts. On a day where the market dropped 1.5%, the bot should be net short, not trying to buy every dip.

The jury IS generating SHORT signals (17 total) but they never reach execution. Check if the entry manager even supports short entries.

---

## OPERATIONAL ISSUES

### OPS 1: WEBSOCKET DROPS (47 disconnects)
Alpaca trade WS disconnects roughly every hour. Auto-reconnects work but there's a gap where fills could be missed.

### OPS 2: LibreSSL WARNING
```
urllib3 v2 only supports OpenSSL 1.1.1+, compiled with 'LibreSSL 2.8.3'
```
Should upgrade Python or SSL library to avoid potential TLS issues.

### OPS 3: ALPACA AUTH FORMAT DEPRECATION
```
"this authentication format is being deprecated. Please use the format: {\"action\": \"auth\", \"key\": \"x\", \"secret\": \"x\"}"
```
Update WebSocket auth format before Alpaca removes the old one.

---

## PRIORITY ORDER FOR CODEX

### Before Monday Market Open (MUST FIX):
1. **BUG 3: PDT Guard** — Use Alpaca's actual daytrade_count, allow swing entries, stop retry spam
2. **BUG 1: Jury Veto** — Fast-path must respect jury history
3. **BUG 4: Extended Hours Guard** — Cancel old orders before new ones
4. **BUG 7: Dual Process** — Add lockfile, kill existing before restart
5. **BUG 5: Advisor dict bug** — One-line fix
6. **BUG 6: Duplicate position check** — Don't buy same stock 3 times

### Before End of Weekend (SHOULD FIX):
7. **BUG 2: Phantom option P&L** — Only finalize on confirmed fills
8. **BUG 8: Rate limiting** — Add caching + rotation
9. **BUG 11: Trailing stop on entry** — Retry or block entry
10. **STRATEGY 1: Fade the runner** — This is potentially the highest-edge addition

### Nice to Have:
11. BUG 9: Wash sale paper mode
12. BUG 10: Options PDT retry
13. BUG 12: Tuner threshold
14. STRATEGY 2-3: Capital deployment + shorts

---

## RAW NUMBERS FOR CONTEXT

| Metric | Value |
|--------|-------|
| Scan cycles | ~2,147 |
| Jury verdicts | 965 total (735 SKIP, 168 BUY, 45 SELL, 17 SHORT) |
| Entry signals generated | 4 (from 168 BUY verdicts = 2.4% conversion) |
| PDT blocks | 613 |
| Extended guard failures | 4,100 |
| Trailing stop failures | 886 |
| AI rate limit skips | 7,022 |
| Agent failures | 3,951 |
| Advisor crashes | 34 |
| Options PDT retries | 122 |
| WebSocket disconnects | 47 |
| Actual trades executed | ~15 |

---

## NEW FINDINGS FROM CODE AUDIT (Added 00:45 AM CST)

### BUG 13: FADE RUNNER EXISTS BUT NEVER GENERATES SHORT ENTRIES
**Severity: HIGH — the strategy Colin asked about already exists but is broken**
**File:** `src/signals/fade_runner.py` + `src/main.py`

`FadeRunnerScanner` is fully written, integrated, and running. It:
- Records yesterday's 40%+ runners ✅ (BATL was recorded on March 5)
- Gets loaded on startup: "Fade runner scanner initialized (1 cached runners)" ✅
- Generates fade candidates and passes them to the overnight thesis builder ✅

**But it never generates actual trade entries.** The fade_candidates get passed to `_build_overnight_thesis()` at line 646, NOT to the entry pipeline. They're used for planning context but never reach the orchestrator/jury for actual SHORT evaluation.

**Meanwhile, the bot's normal scanner picks up BATL as a LONG candidate** because it's still in Polygon's gainers list. The fade scanner says "short this" and the normal scanner says "buy this" — and only the buy signal has a path to execution.

**Fix:**
1. Feed `fade_candidates` into `_process_candidates()` with `side="short"`
2. When a symbol appears in BOTH fade candidates AND normal candidates, the fade signal should take priority (it has more context)
3. The jury prompt already supports SHORT verdicts — just pipe the fade signals through

**Also:** The `yesterdays_runners.json` threshold is 40% minimum, but BATL ran +40% which is RIGHT at the threshold. Today it was also in the runners file but the scan KEPT re-recording INDO every minute (seen 20+ times in logs). The `record_todays_runners` is being called every scan cycle instead of once at end of day.

---

### BUG 14: PDT GUARD CODE WAS ALREADY FIXED — BUT OLD PROCESS IS RUNNING OLD CODE
**Severity: CRITICAL — explains the 613 blocks**
**File:** Process management

The current `risk_manager.py` at line 283-291 says:
```python
# PDT awareness: entries allowed; same-day exits may be blocked by broker
logger.warning(f"⚠️ PDT SWING MODE: {day_trades}/3 day trades used...")
```

This is a WARNING, not a BLOCK. The code was already updated to allow entries.

**But the logs from `bot_2026-03-06.log` show:**
```
🚨 PDT GUARD: 10/3 day trades used, equity $24,893 < $25K — BLOCKED
```

This "BLOCKED" message doesn't exist in the current code. **This confirms BUG 7 (dual process)** — an OLD process with the OLD blocking PDT code is still running alongside the new one. The old process is placing orders and blocking entries with stale logic.

**Fix:** This is BUG 7 again — kill all processes, use lockfile, ensure only ONE process runs.

---

### BUG 15: `record_todays_runners()` CALLED EVERY SCAN CYCLE
**Severity: LOW — but wastes resources**
**File:** `src/main.py` (wherever `record_todays_runners` is called)

The fade scanner records INDO as a runner 20+ times (every minute from 7:19 AM to 8:01 AM). This should be called ONCE at end of day, not every scan.

**Fix:** Only call `record_todays_runners()` once per day (after market close, or on the first scan cycle after 4 PM ET).

---

### BUG 16: SHORT ENTRIES EXIST IN CODE BUT MAY NOT BE REACHING ALPACA
**File:** `src/entry/entry_manager.py` line 385 + `src/main.py` line 1509

The `enter_short()` method exists and is called at line 1509 of main.py. The jury generated 17 SHORT verdicts today. But ZERO short trades appear in the trade history.

**Possible causes:**
1. The 17 SHORT verdicts never generated entry signals (check the `_process_candidates` path for short handling)
2. Alpaca paper account may not have short selling enabled
3. The PDT guard blocked shorts too (it blocks ALL entries, not just longs)

**Fix:** Trace the full path from SHORT jury verdict → entry signal → `enter_short()` → broker order. Add logging at each step.

---

### NEW FEATURE: UNUSUAL WHALES API INTEGRATION
**File:** New — `src/signals/unusual_whales.py`

Colin just purchased an Unusual Whales Advanced API subscription ($375/mo) with real-time websockets. API key is in `.env` as `UW_API_TOKEN`.

**What to build:**
1. New signal source: `src/signals/unusual_whales.py`
2. Endpoints to integrate:
   - `/api/option-trades/flow-alerts` — whale options flow (biggest signal)
   - `/api/darkpool/recent` — dark pool prints
   - `/api/market/market-tide` — put/call sentiment
   - `/api/stock/{ticker}/spot-exposures/strike` — gamma exposure (support/resistance)
   - `/api/congress/recent-trades` — politician trades
3. Feed into scanner as a high-priority signal source
4. Whale flow should boost consensus confidence when it aligns with jury verdict
5. Dark pool accumulation on a fade candidate = stronger SHORT signal

**Auth headers required:**
```
Authorization: Bearer {UW_API_TOKEN}
UW-CLIENT-API-ID: 100001
```

**API docs installed at:** `skills/unusual-whales-api/SKILL.md`
**Hallucination blacklist:** Never use `/api/options/flow` (correct: `/api/option-trades/flow-alerts`)

---

## COMPLETE FILE MAP FOR CODEX

```
src/
├── main.py                      # Main loop, scanner, breakout fast-path, process_candidates
├── persistence.py               # Data persistence
├── agents/
│   ├── orchestrator.py          # 5-agent parallel evaluation
│   ├── jury.py                  # AI consensus (Claude → GPT → Grok → Perplexity)
│   ├── exit_agent.py            # Position exit decisions (EXIT_NOW, TIGHTEN, etc.)
│   ├── base_agent.py            # Rate limit handling for all agents
│   ├── catalyst_agent.py        # News/catalyst evaluation
│   ├── macro_agent.py           # Market regime detection
│   ├── risk_agent.py            # Position sizing and risk approval
│   ├── sentiment_agent.py       # Social sentiment analysis
│   └── technical_agent.py       # Technical indicators
├── ai/
│   ├── advisor.py               # BUG 5: dict.append() crash at line 160
│   ├── consensus.py             # Legacy consensus engine
│   ├── game_film.py             # Trade review and learning
│   ├── mission.py               # Mission/strategy context
│   ├── observer.py              # Market observation loop
│   ├── position_manager.py      # AI position management
│   ├── trade_history.py         # Trade history tracking
│   └── tuner.py                 # Self-tuning (locked at 16/20 trades)
├── broker/
│   └── alpaca_client.py         # Alpaca API (REST + auth deprecation warning)
├── entry/
│   └── entry_manager.py         # BUG 6: no duplicate check, BUG 11: no trailing stop
├── exit/
│   ├── exit_manager.py          # Exit logic
│   └── extended_hours_guard.py  # BUG 4: conflicts with exit agent
├── options/
│   ├── options_engine.py        # BUG 2: phantom P&L (lines 888, 962, 1120)
│   └── options_monitor.py       # BUG 2: records exit before fill (lines 51, 122, 152)
├── risk/
│   └── risk_manager.py          # BUG 3/14: PDT guard, BUG 9: wash sale
├── scanner/
│   └── scanner.py               # Market scanner
├── signals/
│   ├── fade_runner.py           # BUG 13: exists but doesn't generate entries
│   ├── grok_x_trending.py       # Grok-powered X/Twitter scanner
│   ├── unusual_options.py       # Barchart scraping (replace with UW API)
│   ├── congress.py              # Congressional trading signals
│   ├── earnings.py              # Earnings calendar
│   ├── edgar.py                 # SEC filings
│   ├── pharma_catalyst.py       # Pharma/biotech catalysts
│   ├── sector_rotation.py       # Sector momentum
│   ├── short_interest.py        # Short interest data
│   ├── stocktwits.py            # StockTwits sentiment
│   ├── twitter.py               # X/Twitter API
│   └── watchlist.py             # Dynamic watchlist
├── streams/
│   ├── market_stream.py         # Real-time price/breakout stream
│   └── trade_stream.py          # Order fill stream (OPS 3: deprecated auth)
├── sentiment/
│   └── sentiment_analyzer.py    # Sentiment aggregation
├── data/
│   ├── polygon_client.py        # Polygon.io market data
│   ├── signal_attribution.py    # Signal tracking
│   ├── strategy_controls.py     # Strategy enable/disable
│   ├── technicals.py            # Technical indicator calculations
│   └── trade_schema.py          # Trade data schema
└── dashboard/
    └── dashboard.py             # Web dashboard
```

---

*Report generated by exitBot (Opus 4.6) — March 7, 2026 00:45 AM CST*
*Source: bot_2026-03-06.log (18.4MB), bot_restart_2035.log (4.8MB), trade_history.json, game_film.json, risk_state.json, Alpaca API, full source code audit*
