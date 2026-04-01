# Velox v2 — Day 1 Fixes & Improvements

## Context

Velox v2 went live on March 17, 2026. The profit ratchet, stripped AI exit authority, and cleaned pipeline all worked. First day was 61% win rate on ratchet exits (+$60.05 from ratchet alone). But we ended the day at -$6.38 because of open position drawdown and execution issues we identified during live trading.

This document covers every fix needed based on the full Day 1 audit. Codex should implement ALL of these before market open tomorrow.

**Current state on VPS:**
- Git HEAD: `d455628` (portfolio total fix)
- Service: active, PID 241655
- 6 open positions: AMD, AVGO, CRCL, EDSA, LMND, NBIS (short)
- Hard stops in place on all positions
- Ratchet widened to 4% trail (deployed mid-day via .env)
- Shutdown liquidation removed (positions survive restarts)
- Playbook gate bypassed (v2_passthrough)
- Guardrail updated to recognize hard_stop_order_id

**Deployment process (CRITICAL):**
- All changes must be committed to git and pushed to `main`
- VPS deploys via `git pull` at `/opt/velox-app`
- Then `systemctl restart velox`
- **Do NOT use SCP** — it gets overwritten on restart
- **Do NOT modify git config**

---

## Fix 1: Eliminate Duplicate Trade Recording

### Problem

The trade history has 18 entries but only ~10 are unique trades. Every ratchet exit or hard stop exit gets a second `broker_fill_reconstructed` entry from the reconciler. This corrupts:
- Win/loss counts
- P&L totals
- Win rate percentages
- Game Film / retro feedback data
- Dashboard metrics

### Example

```
EDSA: +$12.40 reason=ratchet_exit    hold=5m   (real trade)
EDSA: +$7.44  reason=broker_fill_reconstructed hold=5m   (duplicate)
```

Same trade, recorded twice with different P&L calculations.

### Root Cause

When the profit ratchet or hard stop triggers an exit, `_record_realized_exit()` in `src/main.py` records the trade. Then separately, the monitor loop's broker reconciliation detects the fill on Alpaca and creates a second `broker_fill_reconstructed` entry via the same `_record_realized_exit()` path.

The dedup key (`_make_realized_trade_key`) uses `(symbol, entry_time, quantity, reason, exit_order_id)` — but the ratchet exit and the broker fill have different `reason` strings and sometimes different quantities (fractional vs whole), so they don't match.

### Fix

In `src/main.py`, in the `_record_realized_exit` method, add a dedup check that matches on `(symbol, exit_time within 30 seconds)` regardless of reason. If a trade was already recorded for the same symbol within 30 seconds, skip the second recording.

Also, in the monitor loop where broker fills are detected and `broker_fill_reconstructed` trades are created: before recording, check if a ratchet_exit or hard_stop trade was already recorded for this symbol in the last 60 seconds. If so, skip the broker_fill_reconstructed.

### Files to change
- `src/main.py` — `_record_realized_exit` method and the broker fill detection section in `_monitor_positions`

---

## Fix 2: Dashboard P&L Terminal — Use Alpaca as Source of Truth

### Problem

The P&L terminal computes metrics from a mix of internal state (`pnl_state.json`) and broker data, leading to discrepancies. The dashboard showed $27,970 portfolio total when Alpaca showed $27,515.

### Principle

**Alpaca is the source of truth for ALL financial metrics.** We have the API. We should never be calculating equity, P&L, portfolio value, drawdown, or unrealized P&L from internal state when the broker already has the authoritative number.

### What to change in `src/dashboard/dashboard.py`

The `_compute_pnl_terminal` function (around line 935) currently:
1. Tries to get equity from `broker_truth` (reconciliation cache)
2. Falls back to `alpaca_client.get_account()`
3. Falls back to `TOTAL_CAPITAL` setting
4. Computes `total_pnl = equity - starting` where `starting` comes from `_resolve_starting_equity` which looks at multiple internal sources

**Replace this with:**
1. Call `alpaca_client.get_account()` directly every time
2. Use `equity` from the response
3. Use `last_equity` from the response for day P&L: `day_pnl = equity - last_equity`
4. Use `equity - 27500` for total P&L (starting capital is fixed at $27,500)
5. Get unrealized from `sum(position.unrealized_pl)` from `alpaca_client.get_positions()`
6. Cache the result for 5 seconds to avoid hammering the API

### Specific fields that must come from Alpaca

| Dashboard field | Source | Alpaca field |
|----------------|--------|-------------|
| Equity | `GET /v2/account` | `equity` |
| Cash | `GET /v2/account` | `cash` |
| Buying power | `GET /v2/account` | `buying_power` |
| Day P&L | computed | `equity - last_equity` |
| Total P&L | computed | `equity - 27500` |
| Unrealized | `GET /v2/positions` | `sum(unrealized_pl)` |
| Drawdown | computed | `(peak - equity) / peak` where peak = max(equity seen) |
| Portfolio value | `GET /v2/account` | `equity` |
| ROI | computed | `(equity - 27500) / 27500` |

### Fields that come from internal trade history (these are OK)
- Win rate, W/L counts
- Best/worst trade
- Trade count
- Strategy breakdown
- Average hold time
- Signal latency

### Also fix
- `_resolve_starting_equity` — hardcode to 27500.0, remove the multi-source guessing
- The portfolio API endpoint `/api/portfolio` — already fixed to use `balances.equity` but verify it's working

### Files to change
- `src/dashboard/dashboard.py` — `_compute_pnl_terminal` function and `_resolve_starting_equity`

---

## Fix 3: Stop Re-entering Stocks We Just Ratcheted Out Of

### Problem

EDSA was traded 6+ times today. We kept re-entering after ratchet exits, catching small wins each time instead of one big win. The stock went +40% and we captured ~$38 across 6 round trips when one held position could have been +$250.

### Fix

After a ratchet exit, add a cooldown on that symbol. The symbol should not be re-evaluated by the jury for at least 30 minutes after a ratchet exit. This prevents the churn pattern of enter → ratchet out → re-enter → ratchet out.

### Implementation

In `src/main.py`, in `_record_realized_exit`:
- If reason is `ratchet_exit`, set `self._symbol_loss_cooldown_until[symbol] = time.time() + 1800` (reuse the existing cooldown mechanism but apply it to ratchet exits too, not just losses)
- Rename `_symbol_loss_cooldown_until` to `_symbol_reentry_cooldown_until` since it now covers both losses and ratchet exits
- Update `_symbol_loss_cooldown_remaining` → `_symbol_reentry_cooldown_remaining`
- Update all references

### Files to change
- `src/main.py` — `_record_realized_exit`, cooldown check in `_process_candidates` and `_passes_fast_path_deterministic_screen`

---

## Fix 4: UW Flow Candidates Should Still Check Spread and Extension

### Problem

CRCL was the worst trade of the day (-$16.76 hard stop). It entered via `uw_flow_long` strategy. The scanner filter bypass for UW flow candidates lets them through without any momentum/volume check, but it also skips the spread check. CRCL was already extended when we bought.

### Fix

In `src/scanner/scanner.py`, in `_passes_filter`, the UW flow bypass should still enforce:
- Price range ($5-$500) — already checked
- Ticker format (alpha, <=5 chars) — already checked  
- **Spread check**: if `spread_pct > 1.5`, reject even for UW flow
- **Extension check**: if `range_pct > 95` (stock already at day high), flag it but don't reject — let the jury see it with a warning

### Implementation

Change the UW flow bypass block from:
```python
if is_uw_flow or is_human or is_watchlist or is_copy_trader:
    s["signal_tier"] = "tier_1" if is_uw_flow else "tier_2"
    return True
```

To:
```python
if is_uw_flow or is_human or is_watchlist or is_copy_trader:
    s["signal_tier"] = "tier_1" if is_uw_flow else "tier_2"
    spread = float(s.get("spread_pct", 0) or 0)
    if spread > 1.5:
        return False
    return True
```

### Files to change
- `src/scanner/scanner.py` — `_passes_filter`

---

## Fix 5: Widen Ratchet Parameters in Code Defaults (Not Just .env)

### Problem

The ratchet params were widened in the VPS `.env` mid-day, but the code defaults still have the tight values. If `.env` vars are ever removed or a fresh deploy happens, it reverts to tight ratchet.

### Fix

Update the defaults in `src/exit/profit_ratchet.py` and `config/settings.py` to match the widened values:

```
PROFIT_RATCHET_HARD_STOP_PCT = -3.0      (unchanged)
PROFIT_RATCHET_ACTIVATION_PCT = 1.5      (was 1.0)
PROFIT_RATCHET_INITIAL_FLOOR_PCT = 0.25  (was 0.5)
PROFIT_RATCHET_TRAIL_PCT = 4.0           (was 2.0)
PROFIT_RATCHET_MIN_HOLD_SECONDS = 900    (was 1800)
```

### Files to change
- `config/settings.py` — default values for PROFIT_RATCHET_* settings
- `src/exit/profit_ratchet.py` — default values in class attributes (these read from settings, so just settings.py should be enough, but verify)

---

## Fix 6: Clean Up broker_fill_reconstructed Strategy Tags

### Problem

11 of 18 trades have `strategy_tag = "broker_reconciled"` instead of their real strategy. This happens because when the reconciler creates a `broker_fill_reconstructed` entry, it doesn't carry over the strategy_tag from the original position.

This corrupts the by-strategy analytics that v3 needs.

### Fix

When creating a `broker_fill_reconstructed` trade record, look up the position in `entry_manager.positions` (or recently removed positions) and copy its `strategy_tag`, `signal_tier`, and other metadata fields. If the position is not found, use `"unknown"` not `"broker_reconciled"`.

### Where this happens

Search for `broker_fill_reconstructed` in `src/main.py` — there are multiple places where these records are created in `_monitor_positions`. Each one needs to copy metadata from the position dict.

### Fields to copy from position
- `strategy_tag`
- `signal_tier`
- `entry_path`
- `signal_sources`
- `provider_used`
- `decision_confidence`

### Files to change
- `src/main.py` — all `broker_fill_reconstructed` trade record creation points in `_monitor_positions`

---

## Fix 7: P&L Terminal Total P&L Font Truncation

### Problem

The Total P&L number in the first box of the P&L terminal gets cut off when negative or when it has many digits. The font is too large for the container.

### Fix

In `src/dashboard/dashboard.py`, find the P&L terminal HTML section. The first stat box (`Total P&L`) needs:
- Smaller font size or auto-scaling
- `overflow: hidden; text-overflow: ellipsis` as a safety
- Or just reduce the font size of the value to match the other stat boxes

Search for the P&L terminal HTML in the dashboard template (around line 2000+). The first `summary-item` div has the total P&L value. Make its font size match the other boxes.

### Files to change
- `src/dashboard/dashboard.py` — P&L terminal HTML/CSS section

---

## Fix 8: Prevent Re-entry While Position Already Held

### Problem

The system entered EDSA multiple times while already holding EDSA. The `can_enter` check in `entry_manager.py` should prevent this, but the position may have been removed prematurely during ratchet exit processing.

### Fix

In `src/main.py` `_process_candidates`, before evaluating a candidate with the jury, check both:
1. `entry_manager.positions` (internal state)
2. The Alpaca positions list from the latest sync

If the symbol exists in either, skip it. Don't waste jury API calls on symbols we already hold.

### Files to change
- `src/main.py` — `_process_candidates`, add broker position check alongside internal position check

---

## Fix 9: Broker-Source-of-Truth Audit Sweep

### Problem

Multiple places in the codebase compute financial metrics internally when Alpaca already has the answer. This causes drift and confusion.

### Principle

Any metric that Alpaca computes from actual fills should come from Alpaca. Internal calculations should only be used for things Alpaca doesn't know (strategy tags, signal metadata, hold time by strategy, etc.).

### Audit these files

**`src/dashboard/dashboard.py`:**
- `_compute_pnl_terminal` — equity, P&L, drawdown, ROI should all come from Alpaca (Fix 2 above)
- `/api/portfolio` endpoint — already fixed but verify
- Equity curve data points — should use broker equity, not internal

**`src/reconciliation/reconciler.py`:**
- The reconciler computes `broker_vs_pnl_state_diff` — this is useful for detecting drift but should not be the P&L source
- `current_open_unrealized` should come from `sum(position.unrealized_pl)` from Alpaca, not internal calculation

**`src/risk/risk_manager.py`:**
- `_equity` should be synced from Alpaca's `equity` on every cycle, not just on init
- `get_status()` should report Alpaca's equity, not a cached internal number

**`src/main.py`:**
- The equity sync in the main loop (around line 555-567) already calls `get_balances()` — verify it's actually updating risk_manager._equity

### Files to change
- `src/dashboard/dashboard.py`
- `src/reconciliation/reconciler.py` (audit, may not need changes)
- `src/risk/risk_manager.py` (audit equity sync)
- `src/main.py` (audit equity sync in main loop)

---

## Fix 10: Hard Stop for Short Positions — Verify Direction

### Problem

NBIS is a short position. The hard stop for shorts should be a BUY stop (buy to cover) above the entry price. Verify that `_ensure_hard_stop` in `src/main.py` correctly handles short positions — it should place a buy stop at `entry * 1.03` (3% above entry for shorts), not a sell stop.

### Where to check
- `src/main.py` — `_ensure_hard_stop` method
- `src/exit/profit_ratchet.py` — `price_for_pnl` method (verify short-side math)
- `src/broker/alpaca_client.py` — `place_stop_loss_order` method (verify it flips to buy side for shorts)

The NBIS short has an open buy stop at $117.97 which is correct (~3% above $114.53 entry). So this may already be working. Just verify the code path is correct for all cases.

### Files to change
- Verify only, may not need changes

---

## Fix 11: After-Hours Position Protection

### Problem

After market close (4 PM ET), Alpaca stop orders don't execute. The ratchet system has a `software_managed` mode for extended hours, but we need to verify it's actually checking prices and triggering software exits during AH/pre-market.

### Where to check

In `src/main.py` `_apply_profit_ratchet_action`:
- When `extended_session` is true, it sets `order_state.hard_stop = "software_managed"`
- It calls `_submit_software_managed_exit` for hard_stop and ratchet_exit actions
- Verify this code path actually runs during AH (check `_entry_session_label()` returns "pre" or "after" correctly)

In `src/exit/extended_hours_guard.py`:
- The extended hours guard has its own HWM tracking and limit order logic
- Verify it doesn't conflict with the profit ratchet during AH

### Files to change
- Verify `_entry_session_label()` in `src/main.py`
- Verify `_apply_profit_ratchet_action` extended hours path
- May need to align `extended_hours_guard.py` with ratchet params

---

## Fix 12: Options Strategy Preparation

### Problem

No options trades have been taken despite having UW flow data, options engine code, and the infrastructure ready. The v2 spec blocked options on Phase 1 stability, but the ratchet is proven working (5/5 ratchet exits were winners).

### What to prepare (not deploy yet)

1. **Verify OPTIONS_ENABLED can be set to true without breaking anything**
   - Check `src/main.py` lines 354-367 where options engine initializes
   - Check `src/options/options_engine.py` contract selection logic
   - Check `src/options/options_monitor.py` exit rules

2. **Create a UW flow → options mirror pathway**
   - When UW stream receives a flow_alert with premium >= $500K
   - Determine direction (calls = bullish, puts = bearish)
   - Find nearest liquid contract matching direction (DTE 7-21, delta ~0.40)
   - Place $500-$1000 position
   - Exit via premium trailing stop at 35%

3. **Separate options P&L tracking**
   - Options wins/losses should not pollute equity trade history
   - Dashboard should show options P&L separately

### Files to audit
- `src/options/options_engine.py`
- `src/options/options_monitor.py`
- `src/main.py` — options initialization and UW flow handling
- `config/settings.py` — OPTIONS_* settings

### Do NOT enable options yet — just prepare and verify the code paths work

---

## Execution Order

1. Fix duplicate trade recording (Fix 1) — highest priority, corrupts all analytics
2. Fix dashboard P&L to use Alpaca source of truth (Fix 2)
3. Add ratchet re-entry cooldown (Fix 3)
4. Add spread check back to UW flow filter (Fix 4)
5. Update ratchet defaults in code (Fix 5)
6. Fix strategy tag propagation on broker_fill_reconstructed (Fix 6)
7. Fix P&L font truncation (Fix 7)
8. Prevent duplicate position entries (Fix 8)
9. Broker source of truth audit (Fix 9)
10. Verify short position hard stop (Fix 10)
11. Verify AH position protection (Fix 11)
12. Audit options code paths (Fix 12)
13. Compile / lint / test locally
14. Commit all changes to git, push to main
15. On VPS: `cd /opt/velox-app && git pull && systemctl restart velox`
16. Verify clean startup with 6 existing positions preserved

## Success Criteria

- [ ] No duplicate trades in trade_history.json (each fill recorded exactly once)
- [ ] Dashboard P&L matches Alpaca equity within $1
- [ ] Dashboard portfolio total matches Alpaca equity exactly
- [ ] All trades have real strategy_tag (no "broker_reconciled")
- [ ] Ratchet code defaults match .env values (4% trail, 1.5% activation)
- [ ] Re-entering a ratcheted symbol is blocked for 30 minutes
- [ ] UW flow candidates are spread-checked before reaching jury
- [ ] P&L terminal total P&L is not truncated
- [ ] 6 existing positions survive the restart with hard stops intact
- [ ] Short position (NBIS) hard stop is a BUY stop above entry
- [ ] No `close_all` on shutdown
- [ ] No `PLAYBOOK GATE` blocks in logs after restart

## Day 1 Performance Reference

For context on what the system did today:

```
Equity: $27,493.62 (from $27,500 start)
Day P&L (broker): -$6.38
Ratchet exits: 5 trades, +$60.05 (all winners)
Hard stop exits: 2 trades, -$29.00
Symbols traded: 11
Jury evaluations: 225
BUY/SHORT verdicts: 80 (36% conviction)
Entry signals: 18
Exit Agent recommendations (not executed): 303
PM recommendations (not executed): 232
Shutdown liquidations (before fix): 20
Guardrail blocks (before fix): 58
Playbook blocks (before fix): 11
```

Key takeaway: The ratchet works. The pipeline works when unblocked. The main issues are data hygiene (duplicates, wrong strategy tags), dashboard accuracy (not using Alpaca as source), and execution efficiency (re-entering the same stock instead of holding the runner).
