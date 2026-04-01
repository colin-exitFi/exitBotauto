# Velox Prioritized Fix Backlog (P0-P3) - 2026-03-14

## P0 - Safety / Integrity (Must be true before Monday open)

1. Enforce risk deny at jury output
- Files: `src/agents/jury.py`
- Status: implemented
- Validation: zero new entries where risk reports `max_size_pct=0.0`.
- Rollback trigger: unexpected collapse of all entries despite healthy risk approvals.

2. Block fast-path outside regular hours
- Files: `src/main.py`
- Status: implemented
- Validation: no `FAST-PATH scout entered` logs outside 09:30-16:00 ET.
- Rollback trigger: none (safety invariant).

3. Startup provider health check
- Files: `src/main.py`, `src/dashboard/dashboard.py`
- Status: implemented
- Validation: `provider_health` visible in API/UI with explicit ok/fail.
- Rollback trigger: startup latency regression >30s repeatedly.

4. Clear stale pending ghosts
- Files: `src/reconciliation/reconciler.py`
- Status: implemented
- Validation: stale `pending_new` positions absent from broker are purged after threshold.
- Rollback trigger: false purges of legitimate delayed fills.

## P1 - Execution Stability

1. Wash-trade conflict handling on buy paths
- Files: `src/broker/alpaca_client.py`
- Status: implemented
- Validation: on wash-trade errors, logs show cancel+single retry and reduced repeat bursts.
- Rollback trigger: retry storms or unintended order cancellations.

2. PM emergency throttling
- Files: `src/ai/position_manager.py`
- Status: implemented
- Validation: repeated identical overnight emergency exits suppressed/backed off.
- Rollback trigger: genuine emergencies not acted on promptly.

3. Exit dedupe and pending-state handling
- Files: `src/exit/exit_manager.py`, `src/ai/position_manager.py`
- Status: partially implemented (dedupe existed, PM throttle added)
- Remaining: add explicit dedupe telemetry counters.

4. Reconciliation hardening under mismatch
- Files: `src/reconciliation/reconciler.py`, `src/dashboard/dashboard.py`
- Status: in progress (trust flags used, stale ghost cleanup added)
- Remaining: stricter broker-only mode for additional panels.

## P2 - Observability / Operability

1. Provider panel visibility
- Files: `src/dashboard/dashboard.py`
- Status: implemented
- Validation: AI panel shows provider status, latency, and errors.

2. Stream freshness / relay status cards
- Files: `src/dashboard/dashboard.py`, stream clients
- Status: pending
- Validation target: one-glance stale/healthy state per stream source.

3. Explicit reason-code rollups
- Files: `src/main.py`, `src/agents/jury.py`, dashboard endpoints
- Status: pending
- Validation target: skip/exit reason histograms in API.

4. Docs drift cleanup (runtime vs README)
- Files: `README.md`, runbook docs
- Status: pending
- Validation target: all operator commands reflect current ports/auth behavior.

## P3 - Strategy Architecture (Post-stability)

1. Regime-aware jury/risk policy tightening
- Files: `src/agents/jury.py`, risk configs
- Status: pending

2. Options lane activation policy and verification
- Files: options engine/monitor + settings
- Status: pending

3. Short strategy evidence-gating
- Files: scanner/jury/specialist prompts
- Status: pending

## Weekend Execution Order

1. Deploy implemented P0/P1 patches to VPS.
2. Run low-traffic weekend burn-in with real-time logs.
3. Confirm reduced emergency churn and stable reconciliation.
4. Add remaining P2 visibility patches.
5. Monday pre-open checklist run.
