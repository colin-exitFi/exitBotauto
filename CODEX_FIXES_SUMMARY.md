# Codex Fixes Summary
**Date:** March 7, 2026

## Status
The Monday-critical production bugs are fixed. The major weekend strategy/plumbing items are also in place, including fade-runner wiring, SHORT-path unblocking, swing mode, Unusual Whales integration, rate-limit backoff, the options PDT retry cache, Alpaca trade-stream auth modernization, macro/calendar feeds, human-guided intel, and new watchlist-driven scanner sources.

## Fixed

### Monday-critical
- Jury veto now blocks fast-path entries for 60 minutes and clears on later BUY verdicts.
- PDT logic now uses Alpaca `daytrade_count`, skips PDT checks above `$25K`, and exposes swing-mode helpers.
- Conflicting sell orders are cancelled before new sell-side protection/exit orders.
- `run.sh` now enforces a single bot instance via lockfile + PID cleanup.
- Advisor save path is guarded against corrupted dict-shaped history files.
- Duplicate same-symbol entries are blocked, including the fast-path scout route.

### Weekend strategy / execution
- Phantom options P&L is gone: option exits finalize only after broker-confirmed reconciliation/fill.
- Trailing-stop placement retries after entry now cancel conflicts and mark `protection_failed` when exhausted.
- Risk-agent failures default to approve-with-reduced-size instead of hard block.
- Fade-runner candidates now preserve short-side metadata through scanner merge/scoring and jury context.
- SHORT verdicts now reach `enter_short()` without being killed by long-side sentiment gating, and blocked SHORTs are counted for telemetry.
- Swing mode now reduces size, widens trails, disables fast-path scouts, tags `swing_only` positions, and blocks same-day bot exits for those positions.
- `record_todays_runners()` is throttled to once after 3:55 PM ET instead of every scan cycle.
- Tuner unlock threshold is lowered from 20 trades to 10.

### Unusual Whales
- Added `src/signals/unusual_whales.py` client with flow alerts, dark pool, market tide, and congress endpoints.
- Scanner enrichment now uses UW flow and dark-pool context for score adjustments.
- Market tide now feeds regime biasing and jury context.
- Congress and unusual-options signals now prefer UW as the primary source when configured.

### Remaining QoL / ops items
- Base-agent rate limiting now uses bounded exponential backoff instead of immediate hard-skip.
- After-hours AI rate limits are reduced less aggressively.
- Options PDT rejections (`40310100`) are cached per contract for the rest of the process, stopping the 30-second retry spam.
- Alpaca trade-stream auth now uses the current `{"action":"auth","key":"...","secret":"..."}` payload. Market-stream auth handling was normalized to the same shape.

### API edge tranche
- Added Alpaca movers as an additional scanner source in [src/broker/alpaca_client.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/broker/alpaca_client.py) and [src/scanner/scanner.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/scanner/scanner.py).
- Market stream subscriptions now include `statuses` and `lulds`, with held-position halt/LULD handling wired through [src/streams/market_stream.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/streams/market_stream.py) and [src/main.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/main.py).
- Option contract selection now prefers Alpaca snapshot Greeks/quotes before falling back to the local delta approximation in [src/options/options_engine.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/options/options_engine.py).
- Added Unusual Whales gamma exposure and insider-trade methods in [src/signals/unusual_whales.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/unusual_whales.py); gamma levels now enrich scanner candidates and insider cluster buys now feed the overnight watchlist.
- Added a guarded FINRA consolidated short-interest path in [src/signals/short_interest.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/short_interest.py). Live probing showed the public no-auth partition was stale (`2020-04-15`), so the scanner discards stale FINRA data and falls back to Finviz/Perplexity instead of ingesting bad signals.
- Added placeholder config keys for FRED, Finnhub, and Alpha Vantage in [config/settings.py](/Users/colintracy/.openclaw/workspace/exitBotauto/config/settings.py) and [.env.example](/Users/colintracy/.openclaw/workspace/exitBotauto/.env.example).

## Verification
- Targeted regression suite after the latest pass:
  - `.venv/bin/python -m unittest tests.test_fast_path tests.test_scanner_adaptive tests.test_risk_manager_pdt tests.test_entry_sizing tests.test_exit_manager_short_logic tests.test_unusual_whales tests.test_options_engine tests.test_options_integration tests.test_base_agent tests.test_stream_auth`
  - Result: `59 tests`, all passing
- API edge tranche regression suite:
  - `.venv/bin/python -m unittest tests.test_options_engine tests.test_unusual_whales tests.test_short_interest tests.test_stream_auth tests.test_scanner_adaptive`
  - Result: `41 tests`, all passing
- Broader combined regression suite:
  - `.venv/bin/python -m unittest tests.test_fast_path tests.test_scanner_adaptive tests.test_risk_manager_pdt tests.test_entry_sizing tests.test_exit_manager_short_logic tests.test_unusual_whales tests.test_options_engine tests.test_options_integration tests.test_base_agent tests.test_stream_auth tests.test_short_interest`
  - Result: `70 tests`, all passing
- Compile check:
  - `.venv/bin/python -m py_compile src/broker/alpaca_client.py src/streams/market_stream.py src/scanner/scanner.py src/signals/unusual_whales.py src/signals/short_interest.py src/options/options_engine.py src/main.py config/settings.py`
  - Result: passed

### API edge tranche, continued
- Added Form 4 XML parsing in [src/signals/edgar.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/edgar.py); EDGAR insider activity now distinguishes open-market buys/sells from generic Form 4 presence and is fed into overnight watchlist building plus catalyst context.
- Added [src/signals/fred.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/fred.py) and [src/signals/finnhub.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/finnhub.py); FRED macro snapshots now enrich the macro agent and Finnhub economic/IPO calendars now feed jury context and overnight watchlist research.
- Added persisted human-in-the-loop context via [src/signals/human_intel.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/human_intel.py) and [src/dashboard/dashboard.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/dashboard/dashboard.py); operator notes now create guided scanner candidates, adjust scoring, enrich prompts, and can promote tickers directly into the watchlist.
- Added [src/signals/ark_trades.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/ark_trades.py); ARK daily buys/sells now flow into the overnight watchlist as explicit long/short ideas.
- Added a scoped V1 [src/signals/copy_trader.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/signals/copy_trader.py); Tier-1 trader posts are parsed into scanner candidates, jury context, and dashboard intelligence, with neutral starting weights and strict entry-only parsing.
- Copy-trader V1 now deduplicates previously seen tweets, persists per-trader outcome stats to `data/copy_trader_performance.json`, adjusts trader weights from realized results, and carries a conservative copy-trader size multiplier into share entries when jury-approved.
- Copy-trader exit signals are now parsed separately from entry tweets. On copy-trader-influenced positions, exit/trim chatter tightens protection trails, records exit context on the position, and supports an opt-in strong-convergence auto-exit mode.
- The dashboard now renders a dedicated Copy Trader Intelligence panel with active entry signals, exit signals, and tracked-trader weights/stats instead of exposing the data only via the raw API response.
- The overnight watchlist is now a real scanner source instead of a dead-end research artifact. Scanner merges watchlist, human-intel, ARK, IPO, insider, and congress ideas back into the live candidate queue with conviction-aware scoring.
- Hardened [src/main.py](/Users/colintracy/.openclaw/workspace/exitBotauto/src/main.py) for partial `TradingBot.__new__()` construction used by tests/replay tools, so new collaborators like human intel and EDGAR degrade cleanly when `initialize()` is bypassed.

- API edge continuation regression suite:
  - `.venv/bin/python -m unittest tests.test_edgar_form4 tests.test_macro_sources tests.test_human_intel tests.test_ark_trades tests.test_copy_trader tests.test_dashboard_security`
  - Result: `25 tests`, all passing
- Latest broad regression suite:
  - `.venv/bin/python -m unittest tests.test_fast_path tests.test_scanner_adaptive tests.test_risk_manager_pdt tests.test_entry_sizing tests.test_exit_manager_short_logic tests.test_unusual_whales tests.test_options_engine tests.test_options_integration tests.test_base_agent tests.test_stream_auth tests.test_short_interest tests.test_dashboard_security tests.test_edgar_form4 tests.test_macro_sources tests.test_human_intel tests.test_ark_trades tests.test_copy_trader tests.test_core_integrity tests.test_scan_cadence`
  - Result: `104 tests`, all passing
- Full discovery regression suite:
  - `.venv/bin/python -m unittest discover -s tests`
  - Result: `118 tests`, all passing

## Notes
- `BUGS_FOR_CODEX.md` remains the original forensic bug log and plan source.
- The system is materially safer than the Day 1 paper-trading build: the critical order-routing, PDT, duplicate-entry, and phantom-P&L bugs are addressed.
- Remaining work is mostly iterative tuning and live validation, not known blocker-class defects from the original report. The largest unshipped API-plan items are filtered-stream delivery for copy trader, richer exit-following policy beyond protective trail tightening, and any move toward live Unusual Whales websockets.
