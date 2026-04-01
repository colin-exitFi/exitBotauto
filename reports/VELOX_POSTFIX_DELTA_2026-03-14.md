# VELOX Post-Fix Delta - 2026-03-14

## What Changed Tonight

- Provider-plane outage is resolved (`OPENAI_MODEL` fixed + credits restored).
- Reconciliation no longer self-amplifies from dashboard API calls.
- Reconciler now syncs local positions from broker truth before mismatch classification.
- Critical parsing defect fixed in broker position mapping (`quantity` vs `qty`).
- Startup now forces a fresh reconciliation baseline instead of inheriting stale critical state.
- Broker-reconstructed closed trades are backfilled into `trade_history` with dedupe.

## Before vs After

- Before:
  - `critical_mismatch` persisted continuously.
  - `entry_pipeline_paused=true` continuously.
  - `consecutive_critical_mismatch` climbed without reset.
- After:
  - `status=minor_mismatch`, `severity=warning`.
  - `entry_pipeline_paused=false`.
  - `consecutive_critical_mismatch=0`.
  - Broker/internal live position parity restored (HIMX qty matched).

## Current Residual Warnings (Non-Blocking)

- `broker_fill_ledger_unresolved`
- `internal_closed_trade_subset_only`
- `internal_ledgers_diverge`
- `ledger_mismatch_no_live_exposure`
- `residual_position_drift`

These are ledger/history correctness issues, not live exposure integrity blockers.

## Monday Go / No-Go Thresholds

## GO if all are true

- Provider health remains stable for premarket window (`claude/gpt/grok` all `ok`).
- Reconciliation remains non-critical:
  - `reconciliation.status != critical_mismatch` for >= 80% of premarket samples.
  - `entry_pipeline_paused` clears and stays `false` for sustained windows.
- No sustained broker rate pressure:
  - `broker_api.recent_429_total < 5` over 5-minute windows.
- No live exposure drift:
  - Broker open position qtys match internal qtys (tolerance <= 0.001).
- No ghost state recurrence:
  - no repeated stale `pending_new` loops.

## NO-GO if any are true

- `entry_pipeline_paused=true` continuously through premarket/open.
- `critical_mismatch` persists as dominant state (>= 80% of samples).
- `consecutive_critical_mismatch` climbs without reset.
- Broker/internal live position quantities diverge again.
- Sustained Alpaca 429 bursts appear around open and trip degraded mode repeatedly.

## Operator Focus for Monday

- Prioritize exposure integrity over analytics perfection.
- If live exposure parity holds and guardrails stay non-paused, bot can operate while ledger cleanup continues.
- If exposure parity breaks, force broker-first rebuild and hold entries until parity is restored.
