# Velox Relay Health Matrix - 2026-03-14

## Scope

Covers A+B+C relay surfaces:

- A: stream/signal pipelines (copy_trader, grok_x, stocktwits, unusual_whales)
- B: dashboard telemetry relay (`src/dashboard/dashboard.py`)
- C: VPS side-process state (`velox.service`, `velox-errors.service`, python workers)

## Matrix

| Surface | Source Files | Healthy Signal | Degraded Symptom | Current Status | Recommended Guardrail |
|---|---|---|---|---|---|
| Copy trader stream relay | `src/signals/copy_trader.py`, `src/main.py` | Fresh stream stats + actionable signals/exits | Stale stream, fallback churn, low-quality context | Mixed | Surface stream freshness + fallback mode in dashboard prominently |
| Grok X trending relay | `src/signals/grok_x_trending.py`, `src/scanner/scanner.py` | Non-empty `grok_x_reason/sentiment` for candidates | Empty trend context, weaker specialist confidence | Mixed | Add stale/error counters into intelligence panel |
| StockTwits relay | `src/signals/stocktwits.py`, `src/scanner/scanner.py` | Trending + sentiment arrays populated | Empty/flat sentiment coverage | Mixed | Persist fetch error rates and expose in `/api/intelligence` |
| Unusual Whales REST relay | `src/signals/unusual_whales.py` | Usage stats + flow/news summaries | Missing chain/news context | Mixed | Alert when request failures exceed threshold |
| Unusual Whales WS relay | `src/streams/unusual_whales_stream.py`, `src/main.py` | Stream connected + fresh events | Queue drops/stale stream | Mixed | Raise queue drop events above debug level when sustained |
| Specialist -> Orchestrator relay | `src/agents/orchestrator.py` | 5 briefs available (or explicit degraded metadata) | error/default briefs become "No data" silently | Degraded under provider pressure | Record per-brief quality score for each verdict |
| Orchestrator -> Jury relay | `src/agents/jury.py` | 3-model panel with clear votes/confidence | missing provider + SKIP overload | Degraded | Continue provider_health + panel-size visibility |
| Orchestrator/ExitAgent -> PM relay | `src/agents/exit_agent.py`, `src/ai/position_manager.py` | Specialist context supports/denies PM actions | PM emergency churn on weak/empty context | Improved | Keep throttling + add no-op reason logging |
| Dashboard API relay | `src/dashboard/dashboard.py` | `/api/*` reflects live trust + AI + stream state | Silent nulls/stale visuals can mask faults | Improved | Keep provider-health and trust flags visible on top cards |
| VPS service relay | systemd services | `velox.service` active + single main python process | duplicate runtimes, sidecar issues | Healthy now | Keep periodic checks in runbook |

## VPS Snapshot

- `velox.service`: active
- `velox-errors.service`: active
- Python app process: single `python -m src.main` instance observed

## Immediate Follow-ups

1. Add structured stream freshness fields into `/api/status` for one-glance health.
2. Add per-provider panel size metrics (`expected=3`, `actual=n`) to consensus summary.
3. Add alert threshold for sustained queue drops or stale stream windows.
