# Velox V3 Backlog

## DONE (April 1)

- [x] Reconstructed trades tagged, dashboard uses clean analytics for win rate
- [x] Ratchet rebalanced: hard stop -0.75%, activation 0.3%, trail 1.0%, min hold 2min
- [x] Catalyst hold mode: new classifier mode for pharma/earnings/congress pre-positioning
- [x] Dust cleanup automated: dust_policy wired into monitor, blocks new entries on dusty symbols
- [x] EOD partial exit: swing/multiday/catalyst keep 40% overnight, intraday close fully
- [x] Daily review: triggers at 4:15 PM ET, saves to data/daily_reviews/
- [x] Reconciliation canaries deduplicated (no more 100-line log spam)
- [x] uw_flow_long enabled (was hardcoded disabled)
- [x] Position sizing: .env 8% respected, allocator advisory only
- [x] Position cap raised to 40
- [x] All 6 dust positions manually closed

## High Priority (Next Session)

### Dashboard Phantom P&L Fix
The AAPL +$154.33 ghost needs deeper investigation. The dashboard may be computing P&L from position entry prices on removed positions. Need to trace exactly where the dashboard builds the trade list for display and ensure it reads ONLY from trade_history, not from position state.

### Exhaustion Fade Short Triggers Too Strict
17 pending setups today, 0 entered. Trigger "volume needs to stop accelerating" never fires. Add alternative triggers: "price loses VWAP", "range contraction after extension". Or reduce volume decel threshold.

### Scale-In on Conviction
Watch live positions for "add more" signals. Use trigger engine to monitor live positions. Cap at 2x original. Pre-trade cost + concentration guard provide guardrails.

### Scale-Out on Profit (Intraday)
Partial profit-taking at milestones (e.g., take 50% at +3%, let rest ride). Different from EOD exit -- this is active profit management during the session.

### Options Pilot Whitelist
OPTIONS_PILOT_STRATEGY_TAGS needs UW_FLOW_LONG, UW_FLOW_SHORT added. OPTIONS_PILOT_MIN_CONFIDENCE at 93% is too restrictive -- lower to 60%. Currently blocking every options candidate.

## Medium Priority

### Wire Setup Funnel Into Dashboard
SetupFunnel records events to SQLite but dashboard doesn't display it. Add endpoints for conversion rates by mode.

### Replay Harness CLI
Add `python -m src.analytics.replay AAPL 2026-04-01` entry point.

### Analytics Trade Ledger Auto-Write
Wire persistence.py to auto-filter reconstructed trades into separate analytics ledger on every record_trade call.

### Provider Health Wiring
Wire provider_health.py into jury/council error handling.

## Ideas (Parking Lot)

### Real Money Transition Checklist
Re-enable: extended hours restriction, opening delay, allocator hard-blocking, appropriate position sizing, tighter ratchet settings.

### Multi-Day Position Management
Overnight positions need different ratchet profiles (wider stops during closed market).

### Pre-Trade Cost Gate on Every Entry (Not Just Auto-Enter)
Currently PreTradeCost runs on every candidate for data collection but only gates auto-enter path. Consider making it gate the jury path too -- skip jury eval entirely if spread is trash or liquidity is zero.
