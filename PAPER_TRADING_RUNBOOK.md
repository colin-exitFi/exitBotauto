# Paper Trading Runbook

Last updated: 2026-03-05

This document is the operational reference for Velox paper trading. The bot should read this at startup and follow it throughout the paper-trading validation phase. Nothing here is optional.

---

## 1) Objective and Exit Criteria

Paper trading is Phase 1 validation. The system is proving -- or disproving -- that positive expectancy exists before any real capital is deployed.

### 1.1 Minimum sample

200 closed trades. No go-live decision before this threshold.

### 1.2 Go-live gates

ALL of these must pass simultaneously over the trailing 100-trade window before transitioning to live capital.

| Gate | Threshold | How to measure |
|---|---|---|
| Win rate | >= 52% | `trade_history.get_analytics()["overall"]["win_rate_pct"]` |
| Profit factor | >= 1.4 | gross wins / gross losses from game film |
| Avg win / avg loss ratio | >= 1.3 | game film `avg_win` / abs(`avg_loss`) |
| Max intraday drawdown | <= 2.0% | daily peak-to-trough equity via equity curve |
| Unprotected positions | Zero incidents | log grep for `NO TRAILING STOP` or red protection status |
| Duplicate P&L recording | Zero incidents | log grep for `skipping duplicate` + verify `total_trades` matches trade_history count |
| Avg entry slippage | < 15 bps | `slippage_bps` field in trade records |
| Bug-driven circuit breakers | Zero | review circuit breaker trigger logs for non-market causes |
| Broker reconciliation | Always in sync | local position count == Alpaca position count at every 60s equity sync |

### 1.3 Kill criteria

If any of these occur, stop paper trading immediately and investigate before resuming.

- Win rate < 40% over 50+ trades
- Profit factor < 1.0 over 50+ trades (system is net-losing)
- Any unprotected position lasting > 60 seconds
- Any duplicate trade recording detected in logs or persistence files
- Local-vs-broker position count divergence persists > 2 minutes
- Daily loss exceeds tier limit from a software bug (not legitimate market conditions)

---

## 2) What to Log

The bot already writes to `logs/bot_YYYY-MM-DD.log` and the dashboard activity feed. During paper trading, ensure the following structured data is captured.

### 2.1 Every scan cycle

- Timestamp
- Regime raw and smoothed
- Scan interval used (seconds)
- Candidate count after filter
- Top 3 candidates: symbol, score, strategy_tag, change_pct

### 2.2 Every jury evaluation

- Symbol
- Provider chain attempted and which provider succeeded
- Confidence score
- Decision (BUY / SHORT / SKIP)
- Wall-clock time for the evaluation (milliseconds)

### 2.3 Every entry

- Symbol, side
- signal_price (price at scan time)
- decision_price (price at jury decision time)
- limit_price (price on the order)
- fill_price (actual fill from Alpaca)
- slippage_bps (computed: adverse fill distance in basis points)
- strategy_tag, signal_sources
- conviction, risk_tier
- time-to-fill (milliseconds from order submit to fill)

### 2.4 Every exit

- Symbol, side
- exit_price
- reason (trailing_stop, trailing_stop_ws, atr_stop_loss, etc.)
- pnl ($), pnl_pct (%)
- hold_seconds
- trailing_stop_order_id
- exit path (ws or monitor)

### 2.5 Every position protection check

- Symbol
- has_trailing_stop (bool)
- has_extended_guard (bool)
- guard_limit_price (if applicable)
- seconds_since_entry

### 2.6 Daily summary

Log at 4:01 PM ET each trading day:

- Total trades today
- Wins, losses, win_rate
- Profit factor
- Total P&L ($)
- Best trade ($), worst trade ($)
- Avg slippage (bps)
- Max intraday drawdown (%)
- Regime distribution (% time in risk_on, risk_off, choppy, mixed)
- Positions held overnight (count)

---

## 3) Alpaca Payload Capture

During paper trading, capture real Alpaca REST and WebSocket payloads to build production-shape test fixtures in `tests/fixtures/`.

### 3.1 Capture targets

Collect at minimum:

- 1 clean long lifecycle (scan -> entry fill -> trailing stop exit)
- 1 clean short lifecycle
- 1 extended-hours entry with guard transition to regular-hours trailing stop
- 1 stale order adjustment (limit not filled, price chased)
- 1 partial fill scenario (if observed)
- 1 circuit breaker trigger (if observed)
- 1 WS-first exit and 1 monitor-first exit (to validate dedup with real payloads)

### 3.2 How to capture

Set `CAPTURE_ALPACA_PAYLOADS=true` in `.env`. The bot will write:

- Raw REST responses from `get_positions()` and `get_orders()` to `data/alpaca_capture/rest_TIMESTAMP.json`
- Raw WebSocket `trade_updates` payloads to `data/alpaca_capture/ws_TIMESTAMP.json`

After each trading day, review `data/alpaca_capture/`, select interesting lifecycles, redact if needed, and convert into fixture JSONs under `tests/fixtures/`.

### 3.3 Go-live acceptance

At least 4 production-captured fixtures must replay green via `tests/test_transcript_replay.py` before transitioning to live capital.

---

## 4) Daily Operational Checklist

### 4.1 Pre-market (before 9:30 AM ET)

- [ ] Bot is running: `/api/status` returns `running: true`
- [ ] Dashboard is accessible with token
- [ ] Overnight watchlist was rebuilt: `/api/watchlist` is non-empty
- [ ] Alpaca account equity matches expected (no phantom trades)
- [ ] No stale positions from previous day (unless intentionally held overnight)

### 4.2 During market hours (spot-check 2-3 times)

- [ ] Dashboard positions show protection status (green = trailing stop, yellow = limit guard, red = NONE)
- [ ] No red protection status on any position for > 30 seconds
- [ ] Scan regime is updating (not stuck on one value all day)
- [ ] AI layers are running (observer and advisor timestamps are recent)
- [ ] Activity feed shows scan results and jury evaluations flowing

### 4.3 Post-market (after 4:00 PM ET)

- [ ] Review trade history for the day via dashboard or `/api/trade-history`
- [ ] Check daily P&L summary
- [ ] Verify no duplicate trades in `data/trade_history.json` (symbol+entry_time should be unique)
- [ ] Verify position count matches between bot (`/api/positions`) and Alpaca (`/api/portfolio`)
- [ ] Review `data/alpaca_capture/` for interesting lifecycles to save as fixtures

### 4.4 Weekly

- [ ] Review game film analytics: by strategy, by source, by hour, by hold duration
- [ ] Check if tuner has activated (needs 20+ trades)
- [ ] Review equity curve trend on dashboard
- [ ] Compare slippage distribution (are fills degrading over time?)
- [ ] Check API call counts per provider (`/api/consensus` stats)

---

## 5) KPI Tracking

Track these metrics on the dashboard and in game film. Report weekly.

### 5.1 Core edge metrics

| Metric | Target | Source |
|---|---|---|
| Win rate (trailing 100) | >= 52% | game film |
| Profit factor | >= 1.4 | game film |
| Avg win $ / avg loss $ | >= 1.3 | game film |
| Expectancy per trade ($) | > 0 | `p * avg_win - (1-p) * abs(avg_loss)` |

### 5.2 Execution quality

| Metric | Target | Source |
|---|---|---|
| Avg entry slippage | < 15 bps | trade records `slippage_bps` |
| Avg time-to-fill | < 5 seconds | entry logs |
| Fill rate | > 90% | entries attempted vs entries filled |
| Stale order adjustment rate | < 20% | count of stale-order adjustments / total entries |

### 5.3 Risk health

| Metric | Target | Source |
|---|---|---|
| Max intraday drawdown | < 2.0% | equity curve |
| Heat utilization | < 60% avg | risk manager status |
| Circuit breaker triggers | 0 from bugs | log review |
| Unprotected position incidents | 0 | protection check logs |

### 5.4 Strategy attribution

| Metric | Action | Source |
|---|---|---|
| P&L by strategy_tag | KEEP profitable, DISABLE losers after 30+ trades | game film `by_strategy_tag` |
| P&L by signal_source | Weight future scoring toward winning sources | game film `by_signal_source` |
| Win rate by strategy | Flag any below 40% for review | game film |

### 5.5 AI quality

| Metric | What to look for | Source |
|---|---|---|
| BUY verdict accuracy | % of BUY trades that were profitable | trade records filtered by decision |
| SHORT verdict accuracy | % of SHORT trades that were profitable | trade records filtered by decision |
| Confidence calibration | High-confidence (>80) should win more than low-confidence (<60) | trade records grouped by confidence bucket |
| Provider reliability | Which AI provider has best accuracy and lowest latency | trade records `provider_used` |
| Skip rate | If > 70%, bot is too cautious; if < 30%, too aggressive | orchestrator stats |

### 5.6 Regime behavior

| Metric | What to look for | Source |
|---|---|---|
| Time in each regime | Is the detector working or stuck? | AI layer state `scan_regime` over time |
| P&L by regime | Which regimes are profitable? | trade records correlated with regime at entry time |
| Scan cadence distribution | Reasonable split between fast and slow | activity feed cadence logs |

---

## 6) Known Risk Scenarios

These are the most likely failure modes during paper trading. Watch for each specifically.

### 6.1 Trailing stop not placed after entry

Bot enters a position but Alpaca rejects the trailing stop order (insufficient shares, symbol not shortable, market closed for that symbol, etc.). The monitor loop should catch this within 5 cycles and retry, then emergency market-sell after 5 consecutive failures.

What to watch: log messages containing `NO trailing stop` or `Trailing stop FAILED`. Dashboard protection column showing red for > 30 seconds.

### 6.2 Extended hours protection gap

Position entered during pre-market or after-hours. Alpaca trailing stops do not execute outside regular session (9:30 AM - 4:00 PM ET). The ExtendedHoursGuard should place dynamic limit sells.

What to watch: `/api/guards` endpoint should show active guards for any position held during extended hours. Verify guard limit prices are ratcheting up (longs) or down (shorts) as price moves favorably.

### 6.3 API rate limiting

Claude, GPT, Grok, or Perplexity rate limits hit during high-volume scanning. The bot should degrade gracefully by skipping evaluations (returning SKIP verdicts), not crashing.

What to watch: log messages containing `Rate limit reached`. Dashboard consensus stats showing provider call counts. If skip rate spikes to 100%, rate limits may be the cause.

### 6.4 Stale market data

Polygon or Alpaca data API returns stale prices, causing the bot to enter at a price that has already moved. The chase prevention check (`MAX_PRICE_CHASE_PCT`) should catch this.

What to watch: log messages containing `CHASE PREVENTION`. High slippage_bps values (> 30 bps) on individual trades.

### 6.5 Concurrent exit race

Both the WebSocket callback and the monitor polling loop detect the same trailing stop fill. The `_exit_recorded` / `_exit_recording` guard should prevent double recording.

What to watch: log messages containing `skipping duplicate`. Verify that every such message is paired with exactly one successful recording. If `total_trades` in `pnl_state` ever exceeds the count of records in `trade_history.json`, the guard has failed.

### 6.6 Short position restart

Bot restarts while holding a short position. On reboot, `_load_brokerage_positions` should correctly load it as `side="short"` with `quantity=abs(qty)`.

What to verify: restart the bot intentionally while holding a short during paper trading. After restart, check that the position appears in `/api/positions` with correct side and positive quantity.

### 6.7 Overnight position management

Positions held overnight should always have protection. At 4:00 PM ET the ExtendedHoursGuard should activate dynamic limit sells. At 9:30 AM ET, limit sells should be cancelled and trailing stops restored.

What to watch: `/api/guards` showing active guards after 4 PM ET. Log messages containing `TRAILING_STOP_RESTORED` around 9:30 AM ET.

---

## 7) When to Stop and Tune

### After 50 trades

First serious review.

- Is expectancy positive?
- Is any strategy consistently losing?
- Action: disable any strategy with win rate < 35% and negative P&L over its sample.

### After 100 trades

Mid-point review.

- Are go-live gates trending toward passing?
- If win rate is below 48%, investigate:
  - Are jury prompts too cautious (high skip rate)?
  - Is the scanner surfacing low-quality candidates (low volume, small moves)?
  - Is slippage eating the edge?
- Action: adjust jury prompt aggression or scanner thresholds if needed.

### After 150 trades

Tuner review.

- The tuner requires 20+ trades before activating. By 150 trades it should have made parameter adjustments.
- Review tuner change history in `data/tuner.json`.
- Did parameter changes improve or degrade trailing performance?
- Action: rollback any tuner changes that degraded the trailing-50 metrics.

### After 200 trades

Go / no-go decision.

- If all go-live gates pass: proceed to Section 8 (Transition to Live).
- If gates do not pass: identify the failing metric, make targeted changes, and run another 100-trade cycle.
- Do not go live with a failing gate. The math does not forgive negative expectancy at scale.

---

## 8) Transition to Live

When all go-live gates pass over 200+ trades:

1. Switch `ALPACA_PAPER=false` in `.env`
2. Set `TOTAL_CAPITAL` and `DEPLOYED_CAPITAL` to actual account values
3. Start with 50% position sizing for the first week:
   - Set `POSITION_SIZE_PCT` to half of the risk tier default
   - This limits exposure while validating live execution quality
4. Monitor daily for the first 2 weeks using the same checklist from Section 4
5. After first 50 live trades:
   - If edge profile matches paper trading (win rate within 5%, slippage within 10 bps), restore full position sizing
   - If edge degrades significantly, pause and investigate before continuing
6. Keep `CAPTURE_ALPACA_PAYLOADS=true` for the first month of live trading to build a library of real-world fixtures

---

## Appendix: Quick Reference

### Key dashboard endpoints

| Endpoint | What it shows |
|---|---|
| `/api/status` | Bot running/paused, market open, positions count, uptime |
| `/api/positions` | Open positions with protection status |
| `/api/portfolio` | Alpaca brokerage positions (source of truth) |
| `/api/pnl` | P&L terminal: equity, realized, unrealized, win rate, drawdown |
| `/api/equity-curve` | Realized equity curve points |
| `/api/trade-history` | Recent trades with strategy/source attribution |
| `/api/consensus` | Jury evaluation history and stats |
| `/api/activity` | Bot activity feed (scans, trades, AI decisions) |
| `/api/watchlist` | Current dynamic watchlist |
| `/api/guards` | Extended hours guard status |
| `/api/streams` | WebSocket connection health |
| `/api/intelligence` | Signal source status (earnings, options, congress, etc.) |

### Key log patterns to grep

| Pattern | What it means |
|---|---|
| `NO trailing stop` | Position is unprotected |
| `skipping duplicate` | Exit dedup guard fired (expected behavior) |
| `CHASE PREVENTION` | Entry blocked due to price movement |
| `CIRCUIT BREAKER` | Daily/weekly loss limit hit |
| `Rate limit reached` | AI provider rate limit |
| `TRAILING_STOP_RESTORED` | Extended hours guard transitioned to regular trailing stop |
| `Emergency market sell` | Trailing stop failed 5x, forced exit |
| `WASH SALE` | Entry blocked due to 30-day wash sale rule |
| `PDT GUARD` | Entry blocked due to pattern day trader rule |

### Key data files

| File | Contents |
|---|---|
| `data/trade_history.json` | All completed trades with attribution |
| `data/pnl_state.json` | Cumulative P&L counters |
| `data/positions.json` | Currently tracked positions |
| `data/game_film.json` | Latest game film analytics |
| `data/risk_state.json` | Risk manager state (ATH, wash sales, round trips) |
| `data/overnight_state.json` | Overnight research session state |
| `data/alpaca_capture/` | Captured Alpaca REST/WS payloads (when capture mode is on) |
| `logs/bot_YYYY-MM-DD.log` | Full daily log |
