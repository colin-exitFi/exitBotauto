# Velox v2 — Stabilization Architecture Rebuild

## Purpose

Velox v2 is a **stabilization rebuild**, not the final-state architecture.

The goal is to stop self-sabotage, restore clean control flow, remove discretionary AI exits, reset contaminated production state, and create the foundation for strategy isolation in v3.

The current system has strong raw signal inputs but an unstable live decision architecture: multi-layer veto logic, contaminated feedback loops, and exit behavior that suppresses winners.

---

## Core Principles

1. **One layer decides entries.** Jury is the v2 live entry decision-maker. Other layers inform or constrain portfolio safety but do not override entries for subjective reasons.
2. **Exits are mechanical.** No Advisor, PM, Exit Agent, or commentary-layer discretionary liquidation. Exits are governed by hard stop, profit ratchet, and portfolio circuit breaker.
3. **Missing data degrades confidence, not stability.** Agent brief failures return neutral output. Missing data must never produce "system integrity failure" behavior.
4. **Production is boring.** Stable thresholds, deterministic order handling, clean authority boundaries, minimal live improvisation.
5. **Research and adaptation are bounded.** Tuner and Game Film remain active only in constrained, observational, low-authority modes until production behavior is stable.

---

## Non-Negotiable Safety Rails

- No commentary-layer execution authority
- One active exit order per position max
- Explicit cancel-confirm-replace flow for ratchet order updates
- No options mirroring until core equity flow is stable
- No aggressive extended-hours expansion during stabilization
- No high-authority adaptive tuning until trade definitions and metadata are stable
- All live trades must persist structured metadata

---

## Agreed Design Decisions

### A. Profit Ratchet replaces trailing stops and AI exits

| Situation | Mechanism | Behavior |
|-----------|-----------|----------|
| **Underwater** | Hard stop | -3% from entry → exit, no AI input. Always active, even during min hold. Regular hours: stop-loss order on Alpaca. Extended hours: software-managed check with limit sell (spread/liquidity aware). |
| **Profitable > +1%** | Ratcheting limit sell | Floor set at (peak PnL - 2%). Only moves UP. Never below breakeven once activated. |
| **Between 0% and +1%** | Hold zone | No exit trigger except hard stop. Minimum 30 min hold. |
| **Portfolio circuit breaker** | Daily loss cap | -5% portfolio equity → halt new entries for the day |

**Ratchet activation logic (precise):**
- Upon first activation at +1% peak PnL, set initial protected floor to **+0.5%** (breakeven-plus buffer).
- After that, floor becomes **max(+0.5%, peak_pnl - 2.0%)**.
- Floor only moves UP, never down.

Example:
- Enter at $100, hard stop at $97 (-3%)
- Climbs to $101 (+1%) → ratchet activates, floor = +0.5%, limit at ~$100.50
- Climbs to $105 (+5%) → floor = max(0.5%, 5.0-2.0) = +3.0%, limit at ~$103
- Climbs to $127 (+27%) → floor = max(0.5%, 27.0-2.0) = +25.0%, limit at ~$125
- Pulls back to $125 → limit fills. Realized +25%.

If peak never exceeds +2.5%, the floor stays at +0.5% (the initial breakeven-plus buffer).

**These are v2 stabilization defaults, not assumed long-term optimal.** v3 should split exit templates by strategy family.

### B. Advisor + PM lose execution authority

**Allowed:** dashboard notes, monitoring, recommendations, ratchet-tighten suggestions, tuner recommendations.

**Not allowed:** market exits, emergency liquidation commands, fast-path discretionary exits, direct live order submission.

### C. Jury is the v2 entry decision layer

- 2-of-3 models agree = execute
- 1 BUY + strong UW confirmation ($500K+) = reduced-size execute
- Unanimous SKIP = skip
- Scout override removed

**This is a transition architecture.** Long-term direction should move toward structured scoring and threshold logic instead of narrative voting.

### D. Risk Agent = portfolio safety + sizing, not second-thesis veto

Risk Agent output is restructured from a single `approved` boolean to explicit fields:

```
can_trade: true/false      (hard portfolio constraints only)
size_cap_pct: 0.0-5.0      (sizing guidance)
constraint_flags: [...]     (wash_sale, max_positions, sector_cap, halted, etc.)
```

**`can_trade=false` only for hard constraints:** wash sale, trading halt, max positions reached, gross heat above circuit breaker, execution safety failure (spread/liquidity).

**`can_trade=true` with reduced `size_cap_pct` for:** consecutive losses, elevated portfolio heat, sector concentration approaching cap, lower-tier signal.

**Risk may NOT block for:** subjective disagreement with thesis, narrative discomfort, "I wouldn't take this trade" logic.

**Framing:** Risk cannot override the signal thesis for discretionary reasons. Risk CAN enforce portfolio and execution constraints. The output semantics must be unambiguous — `can_trade` is a hard gate, `size_cap_pct` is a dial.

### E. UW flow becomes primary tier-1 signal

- **Tier 1:** UW flow with $500K+ premium → priority evaluation, direct to jury
- **Tier 2:** momentum / breakout
- **Tier 3:** social / trending / weaker scanner candidates

Currently only `uw_news_summary` and `uw_chain_summary` reach jury prompt. Need to add: flow premium, direction, sentiment, net premium, dark pool bias.

### F. Contaminated production state is reset

Current live state is considered contaminated. Mixed trade outcomes, panic exits, and unstable control flow have polluted priors and tuning behavior.

- Archive once for postmortem
- Wipe production state
- Never feed archive back into live logic

### G. Agent failures become neutral

"No data available" means: neutral stance, reduced confidence, no panic language, no system-compromised implications in production logic.

---

## Extended-Hours Policy for v2

**Chosen: Option B** — Extended-hours entries allowed only for tier-1 signals at reduced size.

- Research / monitor 24/7
- Queue / score 24/7
- Regular-hours: full trading
- Pre-market / after-hours: tier-1 signals only, reduced size, limit orders only
- Overnight (8PM-4AM): no new entries

---

## Metadata Requirements

**At candidate stage** (before jury evaluation):

- `strategy_tag` (momentum_breakout / uw_followthrough / catalyst / fade / copy_trader / etc.)
- `signal_tier` (tier_1 / tier_2 / tier_3)
- `holding_horizon` (intraday / swing / multiday)
- `market_regime` (risk_on / risk_off / mixed / volatile)

This enables funnel analysis: which strategies are surfaced, which reach jury, which get executed, where candidates drop off.

**At trade execution** (persisted on position and trade record):

All candidate fields above, plus:
- `entry_reason_code`
- `order_state`
- `entry_model_votes` (e.g. `{claude: "BUY", gpt: "SKIP", grok: "BUY"}`)
- `risk_constraints_applied` (e.g. `["size_reduced_heat", "sector_near_cap"]`)
- `ratchet_peak_pnl_pct` (highest profit seen)
- `ratchet_floor_pct` (current protection floor)
- `ratchet_limit_order_id`
- `hard_stop_order_id`

Without clean metadata at both stages, v2 will stabilize execution but fail to create learning inputs for v3.

---

## Tuner / Game Film Restrictions

For v2 stabilization:

- Tuner runs in bounded / low-authority mode
- Game Film records outcomes but does NOT aggressively reshape production parameters
- No major adaptive changes until trade definitions and metadata quality are validated
- **Record first, trust later**

---

## Backtest Integration

`data/backtest_results/` is preserved. A follow-up implementation must define:

- How validated indicators affect candidate ranking
- Whether backtest results influence score weighting
- How regime sensitivity is incorporated
- Which strategies may use backtest priors

Preserved backtest data without a live integration path is passive storage, not production value.

---

# Phase 1 — Stop the Bleeding

## 1a. Snapshot, then wipe production state

**Archive once** (for postmortem only):
- All data/*.json files
- VPS .env config
- Recent journalctl output

**Then delete production files in `data/`:**

1. trade_history.json
2. game_film.json
3. config_state.json
4. tuner_impact.json
5. tuner.json
6. advisor.json
7. risk_state.json
8. pnl_state.json
9. ai_state.json
10. entry_controls.json
11. strategy_controls.json
12. reconciliation_state.json
13. copy_trader_performance.json
14. observations.json
15. positions.json
16. options_positions.json
17. trades.json
18. bot_state.json
19. overnight_state.json
20. tomorrow_thesis.json
21. yesterdays_runners.json

**Keep:** `data/backtest_results/`, `data/watchlist.json`, `data/human_intel.json`

## 1b. Strip Advisor, PM, and Exit Agent execution authority

**`src/ai/position_manager.py`**
- Remove `_execute_market_exit` calls from emergency_exits and strategic_exits loops
- Replace with: log recommendation to dashboard, do NOT execute
- Keep ratchet-tighten suggestion capability (PM can request tighter ratchet, cannot remove it)
- Keep `can_enter` portfolio safety checks (heat, max positions)

**`src/main.py`**
- Remove all paths where PM or Advisor output triggers live exits
- Remove `_exit_fast_path_scout` entirely
- Remove fast-path scout evaluation loop

**`src/agents/exit_agent.py`**
- Remove live `EXIT_NOW` execution behavior
- Recommendation-only: logs what it would do, cannot submit orders

## 1c. Build Profit Ratchet Engine

**New file: `src/exit/profit_ratchet.py`**

Core module returns explicit actions only: `hold`, `update_limit`, `hard_stop`, `ratchet_exit`. No narrative ambiguity.

```python
class ProfitRatchet:
    HARD_STOP_PCT = -3.0
    RATCHET_ACTIVATION_PCT = 1.0
    RATCHET_TRAIL_PCT = 2.0
    MIN_HOLD_SECONDS = 1800
    DAILY_CIRCUIT_BREAKER_PCT = -5.0

    def check_position(self, position, current_price, now=None):
        # Returns: {"action": str, "reason": str, ...}
        # Actions: hold, update_limit, hard_stop, ratchet_exit
```

**Broker-side implementation:**
- On entry: place stop-loss order at entry * 0.97 (hard stop)
- On ratchet activation (+1%): place GTC limit sell at (peak - 2%) price
- On peak update: cancel-confirm-replace old limit with new higher limit
- One active ratchet order max per position
- Idempotent order keys
- Partial-fill reconciliation
- Restart recovery: on boot, verify/recreate ratchet orders for all positions

## 1d. Replace trailing stop system

**Remove from `src/main.py` `_monitor_positions`:**
- Trailing stop verification and placement logic
- Replace with: ProfitRatchet.check_position() call per position, every 5 seconds

**Remove from `src/entry/entry_manager.py`:**
- Trailing stop placement on entry
- Replace with: hard stop order placement

**Update `src/exit/extended_hours_guard.py`:**
- Align with ratchet params (the existing software-managed limit approach already works like a ratchet)
- During extended hours: software-managed ratchet checks (no broker trailing stops)
- On transition to regular hours: ensure broker-side orders are in place

## 1e. Ratchet order operational safeguards

Required for production stability:

- One active ratchet order max per position (hard stop + ratchet limit = 2 orders max)
- Explicit cancel-confirm-replace sequencing (never place new before old confirmed cancelled)
- Idempotent `client_order_id` scheme
- Partial-fill reconciliation
- Restart recovery: scan open orders on boot, match to positions, fill gaps
- Stale-order cleanup: cancel orphaned orders not matching any position
- Order-state audit logging

## 1f. Risk Agent becomes size + portfolio safety only

**`src/agents/risk_agent.py`**
- Restructure output from `{approved: bool, max_size_pct: float}` to `{can_trade: bool, size_cap_pct: float, constraint_flags: list}`
- `can_trade=false` ONLY for hard constraints: wash sale, trading halt, max positions, gross heat above breaker
- `can_trade=true` with varied `size_cap_pct` for everything else (consecutive losses → smaller size, not denial)
- Remove all discretionary thesis denial from prompt and hard-override logic
- Preserve portfolio constraints: max positions, sector concentration, gross heat

**`src/agents/jury.py`**
- Replace risk-override block: `can_trade=false` blocks entry (hard constraint). `can_trade=true` never blocks entry.
- Risk brief visible to jury for context. `size_cap_pct` caps position sizing. No forced SKIP from risk opinion.
- Jury decides IF. Risk decides HOW MUCH (and enforces hard portfolio gates).

---

# Phase 2 — Clean Up the Decision Path

## 2a. UW flow priority in candidate routing

**`src/main.py` `_process_candidates`**
- Add `signal_tier` field to all candidates
- Tier-1 candidates evaluated first
- Build UW flow summary for jury prompt

**`src/agents/jury.py`**
- Add `UW FLOW: {uw_flow_summary}` to prompt with premium, sentiment, direction, net premium
- Tier-1 + 1 model BUY = reduced-size execute
- Tier-1 + 2 models BUY = full-size execute

## 2b. Neutral fallback for all agent briefs

All agents (technical, sentiment, catalyst, risk, macro):
- Failed call returns neutral structured output with `error: false`
- No "SYSTEM INTEGRITY FAILURE" language anywhere in production path
- Missing data = lower confidence, not panic

## 2c. Simplify jury consensus

- 2-of-3 agree = execute (keep)
- 1 BUY + tier-1 UW = reduced-size execute (new)
- Unanimous SKIP = skip
- Scout override removed entirely

## 2d. Dashboard fixes

**`src/dashboard/dashboard.py`**
- Fix total P&L truncation / font sizing in first stat box
- Surface `strategy_tag`, `signal_tier`, `ratchet_floor_pct` on all trades

---

# Phase 3 — Controlled Expansion (Blocked on Phase 1+2 Stability)

## 3a. Options mirror — explicitly blocked until:

- Clean Phase 1 behavior confirmed
- Ratchet orders stable across 20+ trades
- Restart recovery verified
- No commentary-layer exits
- Trade metadata complete

**Then:**
- Enable OPTIONS_ENABLED=true
- UW $500K+ directional flow → $500-$1000 mirror position
- Same strike/expiry family
- Premium trailing stop at 35%
- Separate P&L tracking
- Treat as separate strategy family

---

# Files Changed Summary

| File | Changes |
|------|---------|
| `src/exit/profit_ratchet.py` | **NEW:** profit ratchet engine |
| `src/ai/position_manager.py` | Strip execution, keep recommendation/monitor role |
| `src/agents/exit_agent.py` | Recommendation-only, no live orders |
| `src/agents/risk_agent.py` | Size + portfolio safety only, no thesis denial |
| `src/agents/jury.py` | UW flow prompt, simplified consensus, remove scout override, remove risk-forced-SKIP |
| `src/agents/technical_agent.py` | Neutral fallback on failure |
| `src/agents/sentiment_agent.py` | Neutral fallback on failure |
| `src/agents/catalyst_agent.py` | Neutral fallback on failure |
| `src/agents/macro_agent.py` | Neutral fallback on failure |
| `src/exit/exit_manager.py` | Integrate ratchet, remove trailing-stop exit logic |
| `src/exit/extended_hours_guard.py` | Align with ratchet params and v2 extended-hours policy |
| `src/entry/entry_manager.py` | Hard stop on entry, remove trailing stop placement |
| `src/main.py` | Replace AI exit paths with ratchet, remove fast-path scout exits, add UW priority routing, add metadata fields |
| `src/dashboard/dashboard.py` | P&L font fix, metadata display |
| `config/settings.py` | New ratchet settings, deprecate trailing-stop settings, extended-hours policy settings |
| `data/*.json` | Archive once, then wipe 21 production files |

---

# Execution Order

1. Archive current production state (local + VPS) for postmortem
2. Write `src/exit/profit_ratchet.py` with operational safeguards
3. Strip PM / Advisor / Exit Agent execution authority
4. Replace trailing-stop system with ratchet in monitor loop
5. Update entry flow: hard stop on entry, no trailing stop
6. Ratchet order safeguards: cancel-confirm-replace, restart recovery, stale cleanup
7. Risk agent → size + portfolio safety only
8. Fix all agent brief fallbacks to neutral
9. Add UW flow priority routing and richer jury prompt context
10. Simplify jury consensus logic
11. Add required trade metadata fields to all entry/exit paths
12. Define extended-hours policy in settings
13. Dashboard P&L fix + metadata display
14. Compile / lint / boot validation locally
15. Wipe production data (local)
16. Deploy to VPS + wipe VPS data
17. Restart and verify clean state
18. Observe first 20 trades: no adaptive tuning, validate control flow

---

# Success Criteria for v2 Phase 1

Phase 1 is successful only if ALL of the following are true:

- [ ] No AI discretionary exits occur
- [ ] No duplicate ratchet orders exist per position
- [ ] Hard stops place correctly on entry
- [ ] Ratchet orders update correctly as peak changes
- [ ] No stale exit orders survive restart
- [ ] No "system integrity failure" semantics in live execution path
- [ ] All trades persist required metadata (strategy_tag, signal_tier, etc.)
- [ ] Extended-hours behavior matches chosen policy
- [ ] Tuner does not materially reshape production parameters during early stabilization
- [ ] First 20 live trades complete with clean control flow and auditable order state
- [ ] Cancel-confirm-replace sequencing verified (no orphaned orders)
- [ ] At least one restart / recovery scenario validated in live-like conditions before scaling beyond observation window
- [ ] `strategy_tag` and `signal_tier` present on all candidates at routing time (not just executed trades)

---

# What v2 Is Preparing For

v2 is not the final answer. It is the bridge to v3:

- **Strategy isolation:** separate books for momentum, flow, catalyst, fade, options
- **Structured scoring:** replace narrative voting with numeric feature → probability → threshold
- **Strategy-specific exits:** ratchet templates tuned per strategy family
- **Portfolio allocator:** capital budgets by strategy, regime-based enable/disable
- **Cleaner learning:** post-trade evaluation by strategy family, not mixed blob

v2's job is to make production sane enough to get there.
