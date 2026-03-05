# Velox Mission Plan: $25K to $5M

Last updated: 2026-03-05
Owner: Engineering + Trading Ops
Scope: End-to-end execution plan for turning the current codebase into a durable, compounding trading system.

---

## 0) Audit Synthesis And Current Reality

This plan combines:
- Internal code audit findings (logic, risk, reliability, security)
- External audit findings (Opus) supplied by user
- Current repo implementation details under `src/`

### 0.1 What is already fixed (in this repo snapshot)

P0/P1 fixes implemented:
1. Duplicate trailing-stop exit recording guard via `_exit_recorded` in both polling and websocket paths.
2. ATR call parameter mismatch fixed (`timespan="minute"`, `multiplier=5`).
3. `asyncio.coroutine(...)` removed from trade stream callback path for Python 3.11+ compatibility.
4. `httpx` explicitly added to `requirements.txt`.
5. Dashboard auth hardening added with bearer-token middleware (`DASHBOARD_TOKEN`) for `/` and `/api/*`.
6. AI model consistency updated in AI layers that hardcoded Claude model.
7. Jury fallback chain implemented: Claude -> GPT -> Grok.
8. `.env.example` rewritten to match `config/settings.py`.
9. High-risk short-side exit path hardening:
   - Correct buy-to-cover behavior in `ExitManager`.
   - Short trailing-stop placement support in broker client.
   - Exit-agent and monitor flow updated to use short-specific trailing stops.
   - Extended-hours guard upgraded to support short protection orders.
10. Timeout hardening for key direct REST calls in order-critical paths.
11. Restart-safe short position sync:
    - Alpaca position normalization now preserves side and absolute quantity.
    - Entry manager now loads both long and short brokerage positions on boot.
12. Dashboard surface lock-down:
    - Token middleware now also protects `/docs`, `/redoc`, `/openapi.json`.
13. First automated test harness delivered:
    - Exit dedupe correctness (WS path)
    - Trailing-stop accounting selection (latest matching fill)
    - Short restart sync coverage
    - Dashboard docs/auth protection coverage
14. Phase-1 trade telemetry foundation:
    - Strategy/source/provider/confidence/slippage fields flow from candidate -> position -> trade record.
    - Trade history analytics now include `by_strategy_tag`, `by_signal_source`, and realized `equity_curve`.
15. Latent short-side trigger risk removed in `ExitManager.check_and_exit()`:
    - ATR stop and trailing-stop trigger math is now side-aware for shorts.
    - Dedicated unit tests added for short ATR stop and short trailing retrace behavior.
16. Import hardening pass completed:
    - Removed dynamic `sys.path.insert(...)` usage across `src/`.
    - Standardized package-safe imports for orchestrator/agents/AI modules.
17. Adaptive scanner scoring (Phase 1 item 3) implemented:
    - Scanner now infers regime (`risk_on`/`risk_off`/`choppy`/`mixed`) per scan.
    - Candidate ranking now applies bounded performance multipliers from recent strategy/source hit-rate attribution.
18. Trade attribution logic consolidated:
    - Shared signal attribution utilities now drive both scanner tagging and entry-time trade attribution.
    - Removes drift risk between candidate scoring tags and persisted trade tags.
19. Adaptive scan frequency implemented:
    - Main loop now adjusts scan cadence by inferred regime (fast in high-vol, slow in choppy conditions).
    - Scan regime + active cadence are surfaced in AI-layer state for observability.
20. Concurrency replay test coverage added:
    - New integration-style test validates monitor-path and websocket-path exit handling under overlap, proving single-record behavior.
21. Regime engine upgraded with index context:
    - Scanner now blends candidate tape with SPY/QQQ/DIA direction for less biased regime classification.
    - Candidate telemetry now includes broad-market index average change context.
22. Adaptive-cadence hysteresis added:
    - Scan regime now uses confirmation-based smoothing to avoid frequent interval flapping on noisy transitions.
    - Raw regime and smoothed regime are both surfaced for observability.

### 0.2 Remaining high-risk debt (must be tracked)

- Test coverage is still limited (no full broker-event e2e replay harness; concurrency coverage is currently unit/integration-style only).
- Large use of `except Exception: pass` can suppress production failures.

---

## 1) The Math

## 1.1 Core growth equation

Target multiple:
- `M = 5,000,000 / 25,000 = 200x`

Compounding equation:
- `Final = Initial * (1 + r)^N`
- `r = (Final / Initial)^(1/N) - 1`

Where:
- `r` = required average daily return
- `N` = number of trading days

## 1.2 Required daily return by timeline

Assumption: US trading days (`252/year`).

| Timeline | Trading Days | Required Avg Daily Return |
|---|---:|---:|
| 12 months | 252 | 2.125% |
| 18 months | 378 | 1.412% |
| 24 months | 504 | 1.057% |

Calendar-day equivalent (for perspective):

| Timeline | Calendar Days | Required Avg Daily Return |
|---|---:|---:|
| 12 months | 365 | 1.462% |
| 18 months | 548 | 0.972% |
| 24 months | 730 | 0.728% |

## 1.3 Risk tier compression and return drag

Current deployable exposure by tier (max theoretical, ignoring heat/circuit constraints):
- `deployed_pct ~= size_pct * max_positions`

| Tier | Size % | Max Pos | Deployed % | Effect on account-level growth |
|---|---:|---:|---:|---|
| COMPOUND | 2.5 | 10 | 25% | Highest growth sensitivity |
| PROTECT | 1.5 | 12 | 18% | ~28% less throughput than COMPOUND |
| PRESERVE | 0.8 | 15 | 12% | ~52% less throughput than COMPOUND |
| FORTRESS (proposed) | 0.3 | 20 | 6% | Preservation-first |

Implication:
- If position-level edge is constant, account-level return naturally decays as tiers tighten.
- Mission success requires either:
  1. More capital velocity (more high-quality turnover),
  2. Better per-trade expectancy,
  3. More strategy breadth (multiple independent edges),
  4. Controlled leverage overlays.

## 1.4 Win-rate and payoff requirements

Per-trade expectancy (on position):
- `E = p * W - (1-p) * L`
- `p` = win rate
- `W` = average win % on position
- `L` = average loss % on position

Break-even win rate at reward:risk multiple `R = W/L`:
- `p_break_even = 1 / (1 + R)`

Examples:

| Reward:Risk (`R`) | Break-even Win Rate |
|---:|---:|
| 1.0 | 50.0% |
| 1.2 | 45.5% |
| 1.5 | 40.0% |
| 2.0 | 33.3% |

Current practical direction:
- The bot needs to maintain positive expectancy with low friction and low slippage.
- For the mission pace, positive expectancy alone is not enough; turnover and utilization matter equally.

## 1.5 Probability analysis at 45/50/55/60% win rates

Assumption for sensitivity analysis:
- Avg win = +1.8%, avg loss = -1.0% on position (roughly aligned with present architecture intent).

| Win Rate | Position Expectancy / Trade | Profit Factor |
|---:|---:|---:|
| 45% | +0.26% | 1.47 |
| 50% | +0.40% | 1.80 |
| 55% | +0.54% | 2.20 |
| 60% | +0.68% | 2.70 |

Interpretation:
- 45% can still be profitable if payout asymmetry holds.
- Mission feasibility depends on preserving asymmetry after slippage, spread, and failed fills.

## 1.6 Critical mission insight

- Early stage (`$25K-$50K`): high velocity and edge validation dominate.
- Mid stage (`$50K-$250K`): efficient scaling without expectancy decay.
- Late stage (`$250K+`): drawdown control and operational resilience dominate.
- By `$1M+`, preservation quality is as important as raw return.

---

## 2) The 4-Phase Growth Strategy

## 2.1 Phase 1: PROVE THE EDGE ($25K -> $50K)

Window:
- 1 to 3 months

Primary objective:
- Prove positive expectancy across at least 200 closed trades.

Tier context:
- COMPOUND tier (2.5% size, 10 max positions, 1.0% stop policy)

Daily return target:
- 0.5% to 1.0%

Non-negotiable KPIs:
- Win rate >= 52%
- Profit factor >= 1.4
- Avg win / avg loss >= 1.3
- Max intraday drawdown <= 2.0%
- No unprotected positions

What must work in code:
- Scanner quality (`src/scanner/scanner.py`)
- Jury decision consistency (`src/agents/jury.py`)
- Trailing-stop lifecycle correctness (`src/main.py`, `src/streams/trade_stream.py`, `src/broker/alpaca_client.py`)
- PnL accounting integrity (`src/main.py`, `src/persistence.py`, `src/risk/risk_manager.py`)

Mandatory additions this phase:
- Equity curve snapshots
- Slippage tracking (signal vs fill)
- Per-source signal attribution tag on each trade

## 2.2 Phase 2: ACCELERATE ($50K -> $250K)

Window:
- 3 to 9 months

Primary objective:
- Scale only what Phase 1 proves, with controlled variance.

Tier context:
- PROTECT tier (1.5% size, 12 max positions)

Daily return target:
- 0.8% to 1.5%

Required feature upgrades:
1. Winner pyramiding
2. Strategy tagging and attribution
3. Adaptive scan frequency
4. Market regime detector
5. Correlation monitor

Acceptance gates:
- 8-week rolling Sharpe > 1.0
- Drawdown recovery time < 15 trading days
- Correlated portfolio heat alarms functional

## 2.3 Phase 3: COMPOUND ($250K -> $1M)

Window:
- 6 to 12 months

Primary objective:
- Compound with reduced blow-up risk.

Tier context:
- PRESERVE tier (0.8% size, 15 max positions)

Daily return target:
- 0.3% to 0.8%

Required feature upgrades:
1. Multi-strategy engine (momentum + fade + earnings + catalysts)
2. Drawdown recovery protocol
3. Optional options-hedging overlays
4. VIX-adaptive sizing
5. Portfolio heat map + concentration map

Acceptance gates:
- Max rolling 20-day drawdown <= 6%
- Strategy-level attribution in dashboard
- Automatic risk-off mode tested in replay

## 2.4 Phase 4: PRESERVE ($1M -> $5M)

Window:
- 12 to 24 months

Primary objective:
- Avoid large equity giveback while still compounding.

New tier required:
- FORTRESS (`$1M+`): 0.3% size, 20 max positions, 0.5% stop reference, 1.5% daily max loss

Daily return target:
- 0.1% to 0.3%

Required upgrades:
1. Multi-account support (broker/account partitioning)
2. Tax-lot aware automation and loss harvesting hooks
3. Institutional audit logs (immutable event log)
4. Daily executive PnL reports
5. Position-level attribution + regime attribution

Acceptance gates:
- Operational incident rate near-zero
- Full reconciliation pipeline for positions/orders/trades
- Externalized risk controls (human override channel)

---

## 3) System Architecture Target State

Each module below lists: Current -> Target -> Acceptance

## 3.1 Scanner (`src/scanner/scanner.py`)

Current:
- Multi-source merge with static scoring weights.

Target:
- Adaptive scoring weights by regime and recent source hit-rate.
- Strategy-tag candidate output (`momentum`, `fade`, `earnings_reaction`, etc.).
- Correlation pre-filter before candidate promotion.

Acceptance:
- Each candidate includes `strategy_tag`, `source_confidence`, and `correlation_cluster`.
- Backtest/replay shows uplift in top-decile candidate quality.

## 3.2 Orchestrator (`src/agents/orchestrator.py`)

Current:
- Single evaluation path for all setups.

Target:
- Strategy router: prompt templates and decision thresholds per strategy type.
- Distinct cooldown logic by setup type.

Acceptance:
- Route map is explicit and test-covered.
- Dashboard shows strategy mix over time.

## 3.3 Jury (`src/agents/jury.py`)

Current:
- Final decision with provider fallback implemented.

Target:
- Confidence calibration by realized outcomes.
- Strategy-specific rubric and rejection reasons taxonomy.

Acceptance:
- Confidence deciles correlate with forward returns.
- Clear rejection analytics available in dashboard.

## 3.4 Entry Manager (`src/entry/entry_manager.py`)

Current:
- Core entry and short flow, chase prevention, trailing stop attach.

Target:
- Pyramiding policy: only add to winners under strict risk envelope.
- Scheduled scale-ins for high-conviction setups.
- VWAP-aware and spread-aware execution mode.

Acceptance:
- Pyramiding improves expectancy without increasing tail loss.
- Execution slippage decreases vs baseline.

## 3.5 Exit Agent (`src/agents/exit_agent.py`)

Current:
- AI-driven trail adjustments and emergency exits.

Target:
- Time-based trail tightening schedule.
- Liquidity/volume stress exit heuristics.
- Optional partial profit laddering policy.

Acceptance:
- Reduced giveback on winners.
- Lower variance in late-trade outcomes.

## 3.6 Risk Manager (`src/risk/risk_manager.py`)

Current:
- Tiered sizing, daily/weekly brakes, wash-sale + PDT checks.

Target:
- Add FORTRESS tier.
- Correlation-aware exposure budget.
- Drawdown recovery protocol with auto-throttle.
- Optional Kelly-fraction cap (never uncapped Kelly).

Acceptance:
- Heat metric includes correlation-adjusted exposure.
- Recovery mode triggers and exits automatically.

## 3.7 Game Film (`src/ai/game_film.py`)

Current:
- Deterministic analytics over trade history.

Target:
- Strategy-level and source-level attribution.
- Controlled A/B parameter experiments with rollback flags.

Acceptance:
- Every tuner change linked to measured before/after cohort.

## 3.8 Tuner (`src/ai/tuner.py`)

Current:
- AI suggestions bounded by hard limits.

Target:
- Experiment framework with holdout windows.
- Automatic rollback on degradation thresholds.
- Strategy-local parameter bundles.

Acceptance:
- No direct unvalidated global parameter jumps.
- Degradation rollback works without manual intervention.

## 3.9 Dashboard (`src/dashboard/dashboard.py`)

Current:
- Auth added, runtime status and controls.

Target:
- Equity curve chart + rolling drawdown chart.
- Strategy PnL panel + source attribution panel.
- Heat map and correlation matrix snapshots.
- PnL calendar and incident timeline.

Acceptance:
- Dashboard becomes operations console, not just telemetry panel.

---

## 4) Signal Edge Analysis

## 4.1 Signal inventory

| Source | Path | Primary Entry Points | Pipeline Role | Phase Priority | Known Risks |
|---|---|---|---|---|---|
| Polygon gainers | `src/data/polygon_client.py` | `get_gainers()` | Raw momentum discovery | P1 critical | API rate limits, stale snapshots |
| StockTwits trending | `src/signals/stocktwits.py` | `get_trending()`, `get_sentiment()` | Social momentum filter | P1 critical | Social noise/manipulation |
| Grok X trending | `src/signals/grok_x_trending.py` | `scan()` | Narrative/buzz injector | P1 high | LLM extraction noise |
| Pharma catalyst | `src/signals/pharma_catalyst.py` | `scan()`, `_refresh_pdufa_calendar()` | Event-driven alpha | P1 high | Catalyst date drift, binary gaps |
| Fade runners | `src/signals/fade_runner.py` | `scan()`, `get_fade_candidates()` | Mean-reversion short setups | P2 critical | Short locate/borrow and squeezes |
| EDGAR filings | `src/signals/edgar.py` | `scan_recent_filings()` | Catalyst watchlist | P2 high | Parsing ambiguity, ticker extraction errors |
| Earnings calendar | `src/signals/earnings.py` | `refresh()`, `scan()` | Scheduled event setups | P1 high | Surprise direction uncertainty |
| Unusual options | `src/signals/unusual_options.py` | `scan()`, `check_ticker()` | Smart-flow proxy | P2 high | Delayed/partial options data |
| Congressional trades | `src/signals/congress.py` | `scan()`, `get_buy_signals()` | Slow informational edge | P2 medium | Reporting lag |
| Short interest | `src/signals/short_interest.py` | `scan()`, `get_squeeze_candidates()` | Squeeze risk/opportunity | P2 high | Source quality, stale short data |
| Sector rotation | `src/signals/sector_rotation.py` | `update()`, `suggest_focus()` | Top-down focus control | P2 high | Regime whipsaws |
| Dynamic watchlist | `src/signals/watchlist.py` | `rebuild_overnight()`, `validate_with_prices()` | Curated execution universe | P1 critical | Reasoning drift, stale symbols |

## 4.2 Expected contribution profile

- Phase 1 core edge should come from: Polygon + StockTwits + earnings reaction + strict execution.
- Phase 2+ adds diversification alpha from fade, options flow, sector rotation, and event streams.
- No source should be trusted standalone; edge comes from overlap and post-filtering.

## 4.3 Signal quality controls required

1. Source-level hit-rate tracking (30-day rolling)
2. Time-decay weighting for stale signals
3. Hard market-liquidity filter before execution
4. Correlation suppression (avoid stacked beta masquerading as diversification)

---

## 5) AI Cost Model

## 5.1 Current call-shape estimate

Given current runtime behavior:
- Candidate evaluations: up to ~8 per scan
- Trading scans: ~12/day (5-min cadence during active windows)
- Agent stack: ~6 calls/candidate (5 specialists + jury)

Base estimate:
- `8 * 12 * 6 = 576` calls/day (candidate decisioning)

Plus long-running layers (approx per day):
- Observer: ~39
- Advisor: ~13
- Tuner: ~13
- Game Film: ~7
- Position Manager: ~195
- Exit Agent: position-dependent (can be substantial)

Total practical range:
- ~850 to 1,400 calls/day depending on open positions and regime activity.

## 5.2 Cost sensitivity drivers

1. Prompt size growth due to verbose context
2. Model mix skewed toward expensive models
3. Re-evaluation churn on low-quality candidates
4. Lack of token budgeting and route-specific max token caps

## 5.3 Cost controls (must implement)

1. Candidate pre-gate before AI (cheap deterministic filter)
2. Use cheaper model for low-conviction first pass
3. Cache shared context once per scan cycle
4. Tight max tokens by strategy route
5. Hard daily call budget + graceful degradation policy

Target:
- Reduce AI calls per day by 35-50% with no expectancy degradation.

---

## 6) Risk Kill Switches

Current safety mechanisms and where they live:

1. Daily circuit breaker by tier
- `src/risk/risk_manager.py` (`can_trade`, `record_trade`)

2. Weekly circuit breaker
- `src/risk/risk_manager.py` (`_check_weekly_circuit_breaker`)

3. PDT guard (<$25K)
- `src/risk/risk_manager.py` (`can_open_position`, `_count_recent_day_trades`)

4. Wash sale tracking (30+ day guard)
- `src/risk/risk_manager.py` (`is_wash_sale`, `_clean_expired_wash_sales`)

5. Max positions by tier
- `src/risk/risk_manager.py` (`can_open_position`)

6. Sector concentration limit
- `src/risk/risk_manager.py` (`can_enter_sector`)

7. Portfolio heat tracking
- `src/risk/risk_manager.py` (`update_open_risk`, `adjust_for_heat`)

8. Emergency forced exit after repeated trail-stop failures
- `src/main.py` (`_monitor_positions`)

9. Position manager veto
- `src/ai/position_manager.py` (`can_enter`)

10. Risk-agent hard overrides in jury path
- `src/agents/risk_agent.py` + `src/agents/jury.py`

Hardening required:
- Add explicit "position unprotected" alert severity ladder.
- Add broker reconciliation kill switch when local state diverges from broker state.
- Add stale-data kill switch (if market-data heartbeat is stale).

---

## 7) Overnight Research Pipeline (Current + Target)

Primary flow in `src/main.py::_overnight_session(...)`:

1. Load overnight state
2. Game Film review after 9PM ET
3. Pharma calendar refresh every 6h
4. Full watchlist + thesis rebuild after 10PM ET:
   - Perplexity thesis
   - StockTwits sentiment/trending
   - Twitter overlays
   - Pharma catalysts
   - Fade candidates
   - Earnings, unusual options, congress, short interest
5. Post-earnings reaction validation
6. Watchlist price cross-check against live snapshots
7. EDGAR scan every 30m
8. Overnight news scan every 2h
9. Persist overnight state and summarize outputs

Target improvements:
- Add source confidence score to each watchlist item.
- Force watchlist item provenance (`why`, `when`, `which data`).
- Add "thesis drift" detection: compare tonight vs prior night.
- Precompute strategy routes for market open.

---

## 8) Monitoring And Observability Buildout

## 8.1 What to build next

1. Equity curve persistence
- Snapshot equity each minute to `data/equity_curve.json`
- Build drawdown and recovery duration metrics

2. Strategy-level PnL attribution
- Every trade tagged with `strategy_tag`, `signal_sources`
- Dashboard panels by strategy and source

3. AI decision quality
- Track verdict confidence vs realized outcome
- Reliability diagram and calibration error

4. Slippage tracking
- Capture: signal price, decision price, order price, fill price
- Alert on slippage spikes

5. Provider health monitoring
- Per-provider latency/error/rate-limit counters
- Fast failover when provider quality degrades

6. Alert escalation policy
- Slack for warning/info
- Pager/SMS class for critical protection failures and circuit breaks

## 8.2 Required telemetry schema additions

For each trade record:
- `strategy_tag`
- `signal_sources[]`
- `decision_confidence`
- `provider_used`
- `signal_price`
- `decision_price`
- `fill_price`
- `slippage_bps`
- `correlation_bucket`

---

## 9) Codex Handoff Checklist

Use this as execution queue. Every item has files and acceptance criteria.

## 9.1 Phase 0 (Now): P0 Bug Fixes

1. Duplicate trailing-stop accounting guard
- Files: `src/main.py`, `src/entry/entry_manager.py`
- Acceptance:
  - Same exit cannot increment `pnl_state` twice
  - Same exit cannot increment `risk_manager` stats twice

2. ATR parameter mismatch
- Files: `src/exit/exit_manager.py`
- Acceptance:
  - ATR requests use 5-minute bars (`minute x 5`)

3. Python 3.11 callback compatibility
- Files: `src/streams/trade_stream.py`
- Acceptance:
  - No use of removed `asyncio.coroutine`

4. Explicit `httpx` dependency
- Files: `requirements.txt`
- Acceptance:
  - Clean install has `httpx` without transitive dependency reliance

## 9.2 Phase 0.5: Infrastructure Hardening

1. Dashboard auth middleware
- Files: `src/dashboard/dashboard.py`, `config/settings.py`, `.env.example`
- Acceptance:
  - `/` and `/api/*` return unauthorized without valid token
  - Bearer header and `?token=` both supported

2. AI model consistency
- Files: `src/ai/observer.py`, `src/ai/advisor.py`, `src/ai/tuner.py`, `src/ai/position_manager.py`
- Acceptance:
  - Claude model comes from `settings.CLAUDE_MODEL`

3. Jury fallback chain
- Files: `src/agents/jury.py`, `src/agents/base_agent.py`
- Acceptance:
  - If Claude unavailable, jury automatically tries GPT then Grok

4. Environment contract update
- Files: `.env.example`
- Acceptance:
  - Variables match `config/settings.py` exactly

## 9.3 Phase 1 Feature Set (Edge Validation)

1. Trade attribution fields
- Files: `src/main.py`, `src/persistence.py`, `src/ai/trade_history.py`
- Acceptance:
  - Every trade has strategy/source tags and slippage fields

2. Dashboard analytics panels
- Files: `src/dashboard/dashboard.py`
- Acceptance:
  - Equity curve, source PnL, strategy PnL visible and correct

3. Adaptive candidate scoring
- Files: `src/scanner/scanner.py`
- Acceptance:
  - Scoring weights dynamic by regime and recent hit-rate

## 9.4 Phase 2 Feature Set (Scaling)

1. Pyramiding and scale-in logic
- Files: `src/entry/entry_manager.py`, `src/risk/risk_manager.py`
- Acceptance:
  - Adds only to profitable positions and never breaks heat limits

2. Regime detector + routing
- Files: `src/agents/orchestrator.py`, `src/signals/sector_rotation.py`
- Acceptance:
  - Distinct behavior in trend vs chop regimes

3. Correlation-aware exposure cap
- Files: `src/risk/risk_manager.py`
- Acceptance:
  - Correlated basket limit enforced before entry

## 9.5 Phase 3 Feature Set (Compounding Reliability)

1. Multi-strategy execution engine
- Files: `src/main.py`, `src/agents/orchestrator.py`
- Acceptance:
  - Strategies coexist without state collision

2. Drawdown recovery protocol
- Files: `src/risk/risk_manager.py`
- Acceptance:
  - Auto-throttle when drawdown breaches threshold; auto-recover on stability

3. VIX-adaptive sizing
- Files: `src/risk/risk_manager.py`, `src/agents/macro_agent.py`
- Acceptance:
  - Exposure scales down in elevated volatility regimes

## 9.6 Phase 4 Feature Set (Institutional Preservation)

1. FORTRESS tier introduction
- Files: `src/risk/risk_manager.py`, `config/settings.py`, `.env.example`
- Acceptance:
  - New tier activates at `$1M+` with stricter limits

2. Multi-account orchestration
- Files: `src/broker/*`, `src/main.py`
- Acceptance:
  - Position/risk/account state segmented and reconciled

3. Compliance-grade audit trail
- Files: new module `src/audit/*`, plus integration in order/trade paths
- Acceptance:
  - Immutable append-only events for all execution-critical actions

---

## 10) Mission Reality Rules

These are operating constraints for the whole project:

1. No unprotected position, ever.
2. State updates must be idempotent.
3. Broker state is source of truth; local state must reconcile to broker state.
4. Any failed safety action must escalate, not silently pass.
5. Every parameter change must be measurable and reversible.
6. Capital velocity matters, but only when expectancy is proven.
7. Preservation quality must increase as equity scales.

This document is the operating blueprint. Any feature work that does not improve expectancy, safety, or reliability against these rules should be deprioritized.
