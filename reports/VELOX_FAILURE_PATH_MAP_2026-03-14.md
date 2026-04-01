# Velox Failure Path Map - 2026-03-14

## End-to-End Failure Chains

## Chain A: Provider Failure -> Jury Degrade -> Entry Starvation

1. Provider calls fail/back off in `src/agents/base_agent.py` (`call_gpt`, `_await_rate_limit_slot`).
2. Jury wraps missing provider results in `src/agents/jury.py` (`_safe_call`, `_apply_consensus`).
3. Consensus ends in SKIP for:
   - single model response,
   - two-model split,
   - degraded unanimous skip.
4. `src/main.py` candidate processing records jury vetoes and skips entries.

Observed symptom in logs:
- frequent `missing: gpt`
- `Single model response is insufficient`
- `Two models responded without unanimous agreement`

## Chain B: Entry Allowed Under Weak/Conflicted State -> PM Emergency Exit

1. Entry path in `src/main.py` and `src/entry/entry_manager.py` accepts candidate.
2. Position context reaches PM every 2 minutes in `src/ai/position_manager.py`.
3. PM prompt sees weak specialist context from orchestrator/exit-agent brief cache.
4. PM emits `emergency_exits` and calls `_execute_market_exit(..., source="ai_emergency")`.
5. Exit manager attempts close; repeated pending/duplicate conditions create loop pressure.

Observed symptom in logs:
- `PM AI_EMERGENCY`
- `TRADING BLIND` / `zero specialist data`

## Chain C: Order Conflict/Wash Trade -> Retry Churn -> Position Instability

1. Broker rejects entry/exit with wash-trade/conflict in `src/broker/alpaca_client.py`.
2. Prior behavior retried incompletely for buy paths.
3. Open opposite-side orders persist and block subsequent actions.
4. PM/Exit layers repeatedly attempt resolution, increasing noise/churn.

Patch applied:
- buy paths now call `cancel_related_orders_from_error(...)` and retry once.

## Chain D: After-Hours Fast Path -> Pending/Ghost State -> Overnight Emergency Loop

1. Fast-path scout callback in `src/main.py` enqueues scout entry.
2. Without strict regular-hours gate, extended-hours execution can proceed.
3. Broker state can remain `pending_new` or partial/dust.
4. PM repeatedly emergency-exits symbol while market is closed.

Patch applied:
- regular-hours guard in `_execute_fast_path_scout_entry`.
- PM emergency throttling for closed-market + pending exit scenarios.

## Chain E: Reconciliation Drift -> Trust Degradation -> Analytics Mismatch

1. Broker truth assembled in `src/reconciliation/reconciler.py`.
2. Internal ledgers from `trade_history`, `pnl_state`, game film diverge.
3. Canaries fire (`position_qty_mismatch`, `realized_pnl_mismatch`, `broker_fill_ledger_unresolved`).
4. Dashboard trust flags degrade internal analytics visibility.

Patch applied:
- stale `pending_new` ghost cleanup before internal snapshot generation.

## Relay/Telemetry Integration Points

- Stream/signal ingestion:
  - `src/signals/copy_trader.py`
  - `src/signals/grok_x_trending.py`
  - `src/signals/stocktwits.py`
  - `src/signals/unusual_whales.py`
  - `src/streams/unusual_whales_stream.py`
- Candidate merge and routing:
  - `src/scanner/scanner.py`
  - `src/agents/orchestrator.py`
- UI/API telemetry plane:
  - `src/dashboard/dashboard.py`

## Current Runtime Guardrails Added

- Startup provider health check written into `ai_layers["provider_health"]`.
- Dashboard now exposes provider health in `/api/status` and `/api/ai-status`.
- AI panel renders provider health state (ok/fail, latency, error).
