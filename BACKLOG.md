# Velox V3 Backlog

## High Priority (Next Session)

### EOD Partial Exit (Scale-Out at Close)
Instead of binary close/hold at EOD, scale out 60-70% and let 30-40% ride overnight for multi-day runners. Requires: partial exit by percentage in exit manager, ratchet tracking on reduced position, hold-style awareness (intraday = close all, swing = scale out). Directly addresses "monsters we liquidated before close that ran another 7-8% the next day."

### Scale-In on Conviction
Watch live positions for "add more" signals: holding VWAP, volume reacceleration, sector confirming. Use trigger engine to monitor live positions (not just pending setups). Pre-trade cost + concentration guard provide the guardrails that were missing when scale-in went crazy before. Cap at 2x original position size.

### Scale-Out on Profit
Partial profit-taking at configurable thresholds (e.g., take 50% at +5%, let rest ride with tightened ratchet). Different from EOD exit -- this is intraday profit scaling. The ratchet already knows peak P&L; extend it to trigger partial exits at milestones.

### Dust Cleanup Automation
Wire dust_policy.py into _monitor_positions loop. Auto-liquidate positions below $5 notional. Block new entries on symbols with existing dust residuals. Current dust positions (SOXL $10, APLD $18, NIO $5) are fragments from partial ratchet exits that should be cleaned up.

## Medium Priority

### Wire Setup Funnel Into Dashboard
The SetupFunnel is recording events to SQLite but the dashboard doesn't display conversion rates yet. Add endpoints: /api/funnel/summary, /api/funnel/by_mode, /api/funnel/block_reasons. This is how you answer "why didn't we trade X?"

### Wire Daily Review Into Scheduled Task
build_daily_review() exists but isn't called anywhere. Schedule it at 4:15 PM ET in the main loop or as a cron. Output to data/daily_reviews/ as JSON for historical comparison.

### Replay Harness CLI
replay.py exists but has no CLI entry point. Add a simple script: `python -m src.analytics.replay AAPL 2026-04-01` that prints the pipeline journey for a symbol on a given day.

### Reconciliation Log Noise
The broker_activity_missing_internal_history canary repeats 100+ times per reconciliation cycle, flooding logs. Cap the canary list or deduplicate it. The reconciliation is working correctly; the logging is just too verbose.

## Lower Priority

### Analytics Trade Ledger Write Path
persistence.py needs a write path for analytics_trade_ledger.json that filters out reconstructed/dust trades automatically on every trade record. Currently the ledger was manually created on the VPS.

### Provider Health Wiring
provider_health.py exists but isn't wired into the jury/council error handling. Track success/failure per provider and feed degradation policy into auto-enter decisions.

### Book Allocator Hard-Block Mode
Re-enable allocator hard-blocking once ConcentrationGuard V1 has proven itself. Currently in advisory-only mode (logs but doesn't block). When going live with real money, this needs to actually constrain capital.

## Ideas (Parking Lot)

### Real Money Transition Checklist
Before switching from paper to real: re-enable extended hours auto-entry restriction, re-enable 9:30-9:45 opening delay, re-enable allocator hard-blocking, set POSITION_SIZE_PCT to live-appropriate value, verify all ratchet/stop protections work on real fills.

### Multi-Day Position Management
Positions that survive EOD need different ratchet profiles for overnight (wider stops, no trailing during closed market). The profit ratchet already has swing profiles but they're not automatically assigned based on hold-style.
