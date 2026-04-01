# Velox Monday Recovery Runbook - 2026-03-16

## Goal

Start Monday with stabilized infrastructure and transparent observability.

## Pre-Market (T-90 to T-30)

1. Connect to VPS
- Cursor Remote-SSH host: `velox-vps`
- Verify key access:
  - `ssh root@174.138.81.55`

2. Confirm service/process baseline
- `systemctl is-active velox`
- `systemctl status velox --no-pager`
- `systemctl status velox-errors --no-pager`
- `ps aux | awk 'NR==1 || /python/'`

3. Confirm model/env baseline
- `grep -E '^(OPENAI_MODEL|CLAUDE_MODEL|XAI_MODEL)=' /opt/velox-app/.env`
- Expected: `OPENAI_MODEL=gpt-5.4` (non-empty)

4. Provider smoke checks (runtime path)
- `cd /opt/velox-app && . .venv/bin/activate`
- Run minimal `call_claude/call_gpt/call_grok` probe script.
- Record pass/fail and latency.

5. Verify no ghost positions before open
- Check Alpaca positions and open orders.
- Ensure no stale `pending_new` remnants.

## Market Open Checklist (first 15 minutes)

1. Tail logs (raw + journal)
- `tail -f /opt/velox-app/logs/bot_$(date +%Y-%m-%d).log`
- `journalctl -u velox -f --no-pager`

2. Watch these indicators
- Jury panel health:
  - `missing: gpt` frequency
  - `missing: grok` frequency
  - single-model/two-model SKIP rates
- PM stability:
  - `PM AI_EMERGENCY` frequency
- Execution integrity:
  - wash trade/conflict retries
  - repeated pending exits
- Reconciliation:
  - canary bursts
  - mismatch status transitions

3. Dashboard quick checks
- AI panel shows provider health block with current status.
- Reconciliation/trust flags visible.
- Consensus panel reflects degraded/missing providers explicitly.

## Kill Switch Criteria (Immediate Pause)

Pause or halt trading if any condition is true:

1. Provider collapse
- 2+ jury providers failing for >5 minutes continuously.

2. Reconciliation critical persistence
- `critical_mismatch` with high canary volume sustained >10 minutes.

3. Emergency loop explosion
- `PM AI_EMERGENCY` cadence exceeds one per symbol per cycle and keeps repeating unresolved state.

4. Unauthorized/after-hours style entries in regular loop
- Any entry that violates configured session/risk gates.

## SSH / Remote Troubleshooting

If Cursor Remote-SSH fails:

1. Verify SSH alias and key permissions
- `~/.ssh/config` host block exists for `velox-vps`
- `chmod 600 ~/.ssh/id_ed25519`

2. Test direct shell
- `ssh -vvv root@174.138.81.55`

3. Known-host issues
- Remove stale host key entry if IP was reprovisioned:
  - `ssh-keygen -R 174.138.81.55`

4. If extension stalls
- Reload Cursor window
- Reconnect to host
- Open `/opt/velox-app` directly

## Post-Open (T+30 to T+120)

1. Capture first-hour incident snapshot
- provider health
- jury skip composition
- PM emergency count
- reconciliation status

2. Compare against weekend baseline
- Confirm reductions in:
  - after-hours/ghost patterns
  - wash-trade loops
  - repeated emergency re-exits

3. If stable
- Continue monitored operation with periodic health checks every 15-30 min.

## Canonical Artifacts

- Primary dossier: `VELOX_SYSTEMATIC_DOSSIER_2026-03-14.md`
- Event ledger: `reports/VELOX_EVENT_LEDGER_2026-03-14.md`
- Failure map: `reports/VELOX_FAILURE_PATH_MAP_2026-03-14.md`
- Relay matrix: `reports/VELOX_RELAY_HEALTH_MATRIX_2026-03-14.md`
- Fix backlog: `reports/VELOX_FIX_BACKLOG_P0_P3_2026-03-14.md`
