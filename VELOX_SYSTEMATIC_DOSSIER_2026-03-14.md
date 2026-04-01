# Velox Systematic Dossier - 2026-03-14

## Scope and Data Reviewed

This dossier is based on direct forensic review of:

- VPS raw bot logs (not truncated journal output):
  - `/opt/velox-app/logs/bot_2026-03-12.log`
  - `/opt/velox-app/logs/bot_2026-03-13.log`
  - `/opt/velox-app/logs/bot_2026-03-14.log`
- Total volume reviewed: **342,125 lines**
- Live VPS runtime checks:
  - `/opt/velox-app/.env`
  - `systemctl restart velox && systemctl is-active velox`
  - direct provider smoke tests through runtime code path (`src.agents.base_agent`)
- Core codebase paths:
  - `src/agents/base_agent.py`
  - `src/agents/jury.py`
  - `src/entry/entry_manager.py`
  - `src/ai/position_manager.py`
  - `src/ai/consensus.py`
  - `src/ai/mission.py`
  - `src/ai/trade_history.py`
  - `src/data/strategy_controls.py`
  - `src/dashboard/dashboard.py`
  - `src/scanner/scanner.py`
  - `src/agents/orchestrator.py`

## Immediate Hotfix Applied Tonight

- Set `OPENAI_MODEL=gpt-5.4` in `/opt/velox-app/.env` (it was previously blank, so GPT requests sent an empty `model` field).
- Restarted service: `velox` is now `active`.
- Funded OpenAI billing after direct API checks returned `insufficient_quota`.

Important runtime result after restart:
- `call_claude()` returns valid JSON.
- `call_grok()` returns valid JSON.
- `call_gpt()` returns valid JSON on direct test with `gpt-5.4` after billing top-up.

## P0/P1 Patches Applied In This Session

- `src/agents/jury.py`
  - Fixed risk cap enforcement bug for `max_size_pct=0.0` (now uses explicit `is not None` handling).
  - Forces `SKIP` when risk max is zero even if jury voted action.
- `src/main.py`
  - Added regular-hours-only block in fast-path scout entry path to prevent after-hours fast-path entries.
  - Added startup provider health check (`claude`/`gpt`/`grok`) and persisted status in `ai_layers["provider_health"]`.
- `src/ai/position_manager.py`
  - Added per-symbol emergency exit throttling and suppression while market is closed with pending exit state.
- `src/broker/alpaca_client.py`
  - Added wash-trade/conflict retry handling for `place_market_buy()` and `place_limit_buy()`.
  - On conflict, broker now cancels related conflicting orders and retries once.
- `src/reconciliation/reconciler.py`
  - Added stale `pending_new` ghost cleanup pass before internal analytics snapshot.
  - Purges long-lived pending states absent from broker positions.
- `src/dashboard/dashboard.py`
  - Exposed `provider_health` in `/api/status` and `/api/ai-status`.
  - Added provider health display block in dashboard AI panel for immediate degraded-panel visibility.

## Executive Diagnosis

Velox is not failing from a single bug. It is failing from a **stacked failure cascade**:

1. **Jury degradation** (missing GPT for long windows, Grok backoff loops).
2. **Execution/state corruption** (pending exits, dust remainders, repeated reload/re-exit loops).
3. **Risk gate bypass behavior** (positions exist despite risk deny `max_size_pct=0.0`).
4. **After-hours path leakage** into strategies that should be regular-hours only.
5. **PM emergency loop** repeatedly forcing exits on "zero specialist data", causing token burn and churn.
6. **Reconciliation canaries firing continuously**, reducing trust in internal analytics.

Core insight: this is still primarily an **infrastructure/decision-pipeline integrity problem**, not a pure strategy-alpha problem.

## What the Raw Logs Prove

## 1) Jury was structurally degraded for large windows

From raw logs:

- `gpt_api_error`: **1079** (Mar 12), **1028** (Mar 13)
- `missing: gpt`: **468** (Mar 12), **490** (Mar 13)
- `single model insufficient`: **50** (Mar 12), **75** (Mar 13)
- `two models without unanimity`: **88** (Mar 12), **104** (Mar 13)

Observed verdict examples:
- `Degraded jury: all responding models SKIPped (missing: gpt)`
- `Two models responded without unanimous agreement - SKIP for safety`

Impact:
- When GPT disappears, jury runs as 2-model and often resolves to SKIP.
- Entry engine gets starved or receives low-quality/no-consensus signals.

## 2) PM emergency loop dominates behavior

`PM AI_EMERGENCY` counts:

- Mar 12: **341**
- Mar 13: **930**
- Mar 14: **70** (all HIMX)

Top symbols hit:
- Mar 12: `FLY`, `ONDS`, `ROMA`, `LWLG`
- Mar 13: `FLY`, `HIMX`, `ONDS`, `ROMA`, `LWLG`
- Mar 14: `HIMX` only (persistent ghost/corrupt state)

Impact:
- High noise + high token burn.
- Repeated exit attempts on already-pending exits.
- Operationally "fighting ghosts" instead of managing live risk.

## 3) Risk deny vs actual position entry is real

Logs repeatedly include:
- `approved=False`
- `max_size_pct=0.0`
- `RISK AGENT DENIED APPROVAL`
- yet position exists and triggers emergency exits.

This aligns with code risk-cap bug pattern in `src/agents/jury.py`:
- checking truthiness of `max_size_pct` fails for `0.0`.

## 4) Wash-trade / order-conflict behavior is severe

`wash trade` occurrences in raw logs:
- **815** (Mar 12)

Impact:
- Retry loops consume cycles and can block valid entries.
- Signals become stale while order pipeline churns.

## 5) Reconciliation trust remains degraded for long stretches

High-frequency canaries in logs:
- `position_qty_mismatch`, `realized_pnl_mismatch`, `broker_fill_ledger_unresolved`
- canary string hits:
  - **147,682** (Mar 12)
  - **21,345** (Mar 13)
  - **18,504** (Mar 14)

Impact:
- Dashboard/internal analytics trust is unstable.
- Bot behavior can drift from broker truth.

## 6) After-hours / impossible-state entries are present

Multiple logs describe entries that violate normal session assumptions (including the HIMX impossible-state pattern).

Impact:
- Increased probability of pending order limbo, thin liquidity, and stale state.
- Emergency loop gets amplified overnight when corrective actions cannot execute cleanly.

## Root-Cause Tree

1. Provider instability + config hygiene gaps
- Missing/invalid model usage state leads to jury panel collapse.
- GPT currently additionally constrained by 429 behavior.

2. Consensus policy brittleness under partial panel
- Current degraded logic defaults to SKIP frequently.
- No robust continuity mode for known provider degradation windows.

3. Entry safeguards are inconsistent across paths
- Fast path and edge paths can bypass assumptions from normal entry checks.

4. Exit and reconciliation coordination gaps
- Duplicate exit attempts on already pending orders.
- Reload-after-removal loops indicate state machine drift.

5. Emergency AI loops without strong throttles
- PM and Exit Agent repeatedly re-evaluate same unrecoverable state.

## Relay / Pipeline Audit Notes (A + B + C)

## A) Signals/streams pipeline

Critical files:
- `src/signals/copy_trader.py`
- `src/signals/grok_x_trending.py`
- `src/signals/stocktwits.py`
- `src/signals/unusual_whales.py`
- `src/streams/unusual_whales_stream.py`
- `src/scanner/scanner.py`
- `src/agents/orchestrator.py`

Failure mode:
- Source failures return empty/default payloads.
- Scanner and orchestrator continue with partial context.
- PM later sees weak/empty specialist context and triggers emergency exits.

## B) Command-center/telemetry path

In this checkout, effective telemetry surface is `src/dashboard/dashboard.py` (no local `command-center/` directory).

Risk points:
- API read path can return empty structures on upstream failures.
- Some stream-health visibility is not surfaced prominently enough.
- Sequential polling can mask transient backend faults.

## C) VPS side-process audit

Observed running services include:
- `velox.service` (main bot)
- `velox-errors.service` (error watcher sidecar)

Observed Python process set currently clean (single active `src.main` runtime).

Action:
- Keep explicit duplicate-process checks in runbook:
  - `systemctl list-units --type=service`
  - `ps aux | awk 'NR==1 || /python/'`

## AI Prompt and Control-Plane Findings

## `src/ai/mission.py`

- `MISSION_SHORT` is conservative ("quality setups only", "when uncertain SKIP"), while `jury.py` prompt text still contains aggressive action bias language.
- This philosophical conflict can produce unstable model behavior depending on which prompt framing dominates.

## `src/ai/trade_history.py`

- Retro feedback is available, but data quality depends on clean trade recording.
- If trade journal includes anomaly-heavy periods, retro can reinforce bad states unless anomaly weighting is stricter.

## `src/data/strategy_controls.py`

- Supports `hard_disabled`, `manual_disabled`, `soft_disabled`, `size_reductions`, `probation`.
- Existing `data/strategy_controls.json` is minimal and may not reflect full expected structure.
- Need strong visibility in dashboard/logs when controls are actively blocking strategy paths.

## Highest-Priority Fixes (P0/P1)

## P0-1: Provider health gate on startup + periodic checks

Add a startup and periodic health probe that validates all jury providers and logs explicit reason classes:
- auth failure
- quota/rate limit
- timeout
- invalid model

If 2/3 providers are unhealthy, system should switch to explicit degraded mode and annotate all decisions.

## P0-2: Fix risk cap application for `max_size_pct=0.0`

In `src/agents/jury.py`, change truthy check to `is not None`.

Example patch intent:

```python
risk_max = risk_brief.get("max_size_pct") if risk_brief else None
if risk_max is not None:
    verdict.size_pct = min(verdict.size_pct, float(risk_max))
    if float(risk_max) == 0.0 and verdict.decision in ("BUY", "SHORT"):
        verdict.decision = "SKIP"
        verdict.reasoning = f"Risk gate max_size_pct=0.0. {verdict.reasoning}"
```

## P0-3: Enforce regular-hours gate for fast-path entries

In `src/entry/entry_manager.py`, ensure fast-path cannot open positions when regular session is closed.

Example patch intent:

```python
if not self.is_market_open():
    logger.warning(f"fast_path blocked for {symbol}: market closed")
    self._fast_path_pending.discard(symbol)
    return
```

## P0-4: Ghost/pending-new cleanup state machine

In `src/ai/position_manager.py` and reconciliation path:
- detect `pending_new` age > threshold
- cancel conflicting orders
- mark stale state explicitly
- suppress repeated emergency attempts once quarantine is active

## P1-1: PM emergency throttling

Add per-symbol exponential backoff and "no-op when market closed + unrecoverable state unchanged."

## P1-2: Wash-trade conflict preflight

Before new entries, query/cancel conflicting opposite-side open orders for same symbol.

## P1-3: Exit dedupe hardening

Strengthen existing duplicate exit protection to avoid repeated broker calls when an exit is already pending.

## P1-4: Reconciliation severity-driven behavior

When canaries are active:
- broker truth must override top-line metrics
- internal analytics rendered as degraded
- AI layers get explicit trust flags in prompt/context

## Provider-Specific Notes for This Weekend

1. **OpenAI**
- Model now set to `gpt-5.4`.
- Confirmed root cause was dual-fault: blank `OPENAI_MODEL` in VPS `.env` plus depleted OpenAI credits (`insufficient_quota`).
- Direct post-fix API test now succeeds with `gpt-5.4` and valid JSON output.
- Monday guardrail: enable OpenAI auto-recharge so jury participation cannot silently degrade mid-session due to balance depletion.

2. **Grok**
- Runtime smoke test currently succeeds through Velox code path.
- Still observe heavy internal backoff windows in logs.
- If latency reappears in live market loop, consider moving jury path to faster Grok model for strict JSON classification tasks.

3. **Claude**
- Runtime smoke test succeeds through Velox code path.
- Keep as anchor provider for jury during GPT stabilization.

## Monday Execution Runbook (Codex/Opus)

## Step 0 (already done tonight)
- `OPENAI_MODEL=gpt-5.4` set in `/opt/velox-app/.env`
- OpenAI credits replenished after direct `insufficient_quota` verification
- `systemctl restart velox`

## Step 1 - Freeze and baseline capture
- Snapshot `.env` (redacted), service status, process list, and last 24h logs.
- Record provider health check results.

## Step 2 - Apply P0 code fixes first
- risk cap bug
- fast-path market-hours gate
- provider health visibility
- pending/ghost cleanup hooks

## Step 3 - Apply P1 stability fixes
- PM emergency throttles
- wash-trade preflight + cancel
- exit dedupe/reconciliation trust behavior

## Step 4 - Verify in paper mode
- Jury panel participation rates (3/3, 2/3, 1/3) by hour.
- Emergency exit frequency drops materially.
- No new after-hours unauthorized entries.
- Reconciliation canaries trend down.

## Step 5 - Only then strategy upgrades
- options lane enhancements
- regime-adaptive sizing
- short-playbook refinements

## SSH / Cursor Access for VPS Logs

Use Cursor Remote-SSH and read files directly (preferred) or copy with `scp`.

Target files:
- `/opt/velox-app/logs/bot_2026-03-12.log`
- `/opt/velox-app/logs/bot_2026-03-13.log`
- `/opt/velox-app/logs/bot_2026-03-14.log`

Recommended checks:
- `systemctl status velox --no-pager`
- `journalctl -u velox --since "24 hours ago" --no-pager`
- `ps aux | awk 'NR==1 || /python/'`

## Companion Artifacts Produced

- Event ledger: `reports/VELOX_EVENT_LEDGER_2026-03-14.md`
- Failure-path map: `reports/VELOX_FAILURE_PATH_MAP_2026-03-14.md`
- Relay health matrix: `reports/VELOX_RELAY_HEALTH_MATRIX_2026-03-14.md`
- Prioritized backlog: `reports/VELOX_FIX_BACKLOG_P0_P3_2026-03-14.md`
- Monday checklist runbook: `reports/VELOX_MONDAY_RUNBOOK_2026-03-16.md`

## Final Call

Velox is close, but still in a brittle state where infrastructure faults can dominate strategy logic.  
The fastest path to Monday readiness is:

1. keep GPT model config fixed and OpenAI billing auto-funded,
2. eliminate risk/entry safety bypasses,
3. stop emergency-loop churn,
4. force broker-truth behavior under reconciliation stress.

Do that first, and the rest of the stack (including strategy improvements) becomes testable and winnable.
