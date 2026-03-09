# Week 1 Paper Trading Plan

Date anchor: Monday, March 9, 2026

## Purpose

Week 1 is not a scaling week. It is a discovery week.

The goal is not "make as much paper money as possible."
The goal is:

1. Stabilize execution and state management.
2. Measure which setups actually have edge.
3. Separate real alpha from bug-driven P&L.
4. Decide whether Velox is becoming:
   - a selective directional alpha bot, or
   - a high-velocity profit-taking factory.

## Current Read After Day 1

- Infrastructure is materially better than Day 1.
- Exit logic and trailing-stop behavior are improving.
- Entry quality is still unproven.
- One anomaly trade can distort the daily headline P&L.
- The system must earn green days honestly before capital is scaled.

Working assumption:

- Raw P&L is not enough.
- Clean P&L and setup-level expectancy are the decision metrics.

## Rules For The Rest Of This Week

### 1. Freeze strategy churn

For the next 3-5 paper sessions:

- Allow bug fixes.
- Allow telemetry/logging improvements.
- Allow safety controls.
- Do not keep changing thresholds, prompts, and signal weights every few hours.

Reason:
If strategy logic changes constantly, the data becomes useless.

### 2. Track two P&Ls

Every daily report must show:

- `raw_pnl`
- `clean_pnl`

`clean_pnl` excludes anomaly-tagged trades, including:

- oversized trades
- stale-position trades
- recovered/broker-sync artifacts
- duplicate-entry artifacts
- any trade materially helped by a known bug

QURE-style trades must not be allowed to masquerade as edge.

### 3. Narrow the setups under evaluation

This week, primary focus is limited to these setup buckets:

1. `fade_runner`
2. `biotech_catalyst`
3. `uw_flow_confirmation`
4. `short_risk_off`

Everything else is secondary.

The objective is not breadth. The objective is to find repeatable edge.

## Telemetry We Need On Every Trade

Each trade record should include:

- `symbol`
- `entry_path` (jury | fast_path | broker_sync | recovered_fill | short_cover)
- `strategy_tag`
- `signal_sources`
- `provider_used`
- `decision_confidence`
- `intended_notional`
- `actual_notional`
- `intended_qty`
- `actual_qty`
- `entry_time`
- `exit_time`
- `exit_reason`
- `price_at_1m`
- `price_at_3m`
- `price_at_5m`
- `time_to_green_seconds`
- `time_to_peak_seconds`
- `mfe_pct`
- `mae_pct`
- `anomaly_flags`

Minimum anomaly flags:

- `oversized_position`
- `stale_position`
- `broker_recovery`
- `forced_close_fallback`
- `duplicate_entry`
- `carryover_sync`

### Additional daily metrics (exitBot addendum)

- `estimated_api_cost` — Claude, GPT, Grok, Perplexity, Polygon calls and approximate spend
- If sample size < 10 per setup by Friday, extend evaluation to Wednesday March 18

## Daily Review Process

At end of each session, produce a setup scorecard.

For each `strategy_tag`, calculate:

- number of trades
- raw P&L
- clean P&L
- win rate
- first-1-minute green rate
- first-3-minute green rate
- first-5-minute green rate
- average `mfe_pct`
- average `mae_pct`
- average hold time
- average slippage
- number of anomalies

## What We Are Testing

### Hypothesis A: Selective alpha

Velox may work best as:

- fewer trades
- stronger setups
- asymmetric winners
- strong risk control

Signs this is true:

- first-5-minute green rate is mediocre
- win rate is not especially high
- winners are materially larger than losers
- clean expectancy is positive anyway

### Hypothesis B: Profit factory

Velox may need a separate capital-velocity mode built around:

- fast validation
- high hit rate
- small profit harvesting
- quick kills when a trade does not go green immediately

Signs this is true:

- some setup families go green very quickly
- small fixed-profit harvesting would outperform trailing logic
- hold time is too long relative to available edge

Important:

If this hypothesis wins, the correct answer is probably not "tweak the jury."
It is probably "build a separate factory-mode module with tape/rule-based entries."

## Friday Decision Framework

Decision date: Friday, March 13, 2026 (extend to Wed March 18 if sample size insufficient)

By Friday, use clean data to sort each setup into one of three buckets.

### Promote

Promote a setup only if:

- at least 10 trades
- positive clean expectancy
- acceptable anomaly count
- behavior matches the intended style

### Probation

Put a setup on probation if:

- sample size is too small, or
- behavior is mixed, or
- results are positive only because of anomalies

### Kill

Kill or hard-disable a setup if:

- clean expectancy is negative after meaningful sample
- it rarely goes green quickly enough for the intended style
- it depends on noisy/unreliable signal sources
- it repeatedly creates control problems

## Practical Success Criteria For Week 1

By end of week, success means:

1. No major state-sync or ghost-position failures.
2. No unexplained entries.
3. No outsized single-name exposure breaches.
4. Daily reports include both raw and clean P&L.
5. Each primary setup has measurable expectancy data.
6. We can name which setups are worth keeping.

## What Not To Do

- Do not count anomaly-driven wins as proof of edge.
- Do not add more signals just because today felt noisy.
- Do not confuse execution quality with alpha quality.
- Do not try to make one strategy engine satisfy two incompatible missions.
