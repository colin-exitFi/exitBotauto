# Velox v2.2 — Strategy Book Separation

## Purpose

v2.2 is not a refactor. It is a **policy layer** on top of the working v2 infrastructure.

The data from 24 deduped trades over 2 days is clear:

| Strategy | P&L | Trades | Verdict |
|----------|-----|--------|---------|
| momentum_long | **+$211.04** | 8 | Profit center. Promote. |
| uw_flow_long | **-$125.42** | 5 | Loss engine. Disable. |
| uw_flow_short | -$3.31 | 1 | Insufficient sample. Constrain. |
| carryover | -$6.72 | 4 | Not a real strategy. Fix tagging. |
| broker_reconciled | +$13.52 | 6 | Not a real strategy. Fix tagging. |
| congress_follow | $0.00 | 0 | No trades yet. Keep alive, constrained. |

The highest-value move available right now is to **stop doing what's losing money** and **let what's working run harder.**

---

## Book Definitions

### Book A — momentum_long (PRIMARY)

**Status: FULL GO**

This is the profit center. +$211 on 8 trades. Best entries were tagged `pullback`. Best exits were ratchet.

| Parameter | Value |
|-----------|-------|
| Enabled | Yes |
| Sizing | Full tier size (5% COMPOUND) |
| Sizing boost | tier-1 unanimous: 6%, tier-1 majority: 5%, tier-2 majority: 4% |
| Exit profile | Standard ratchet: 1.5% activation, 4% trail, 15 min hold |
| Dead money | Tighten to -1.5% after 4 hours |
| Max concurrent positions | 8 |
| Max per sector/theme | 3 correlated positions per sector bucket |
| Re-entry cooldown after ratchet | 30 minutes |

**Entry quality sizing:**

| Entry Quality | Sizing |
|---------------|--------|
| `pullback` | 100% of allowed size |
| `neutral` | 85% of allowed size |
| `at_highs` | 50% of allowed size |

`at_highs` entries are never full-sized. The data shows pullback entries produce the best trades (ARTL +$85.70, +$41.65). Buying peaks is the most common source of avoidable losses.

### Book B — uw_flow_long (DISABLED)

**Status: OFF for 5 sessions. Re-evaluate after.**

-$125.42 on 5 trades. SOFI (-$60.99), MARA (-$65.18), CRCL (-$16.76). Every trade was a loser or marginal. The UW flow signal for longs is not producing edge in the current implementation.

| Parameter | Value |
|-----------|-------|
| Enabled | **No** — hard block in risk agent |
| Sizing | 0% (blocked) |
| Shadow mode | **Yes** — persist structured records for all hypothetical entries |
| Re-enable criteria | See dedicated section below |
| When re-enabled | Start at 25% of normal size as probe. Evaluate after 10 trades. |

**Why disable, not just de-risk:** At 40% size it still bleeds. The problem isn't sizing — it's that the UW flow long signal is currently identifying entries that are already extended. Reducing size just makes the losses smaller, not the strategy better. We need to fix the entry timing before re-enabling.

**Shadow mode requirements:** While disabled, the system must:
- Still surface `uw_flow_long` candidates through the scanner
- Still tag them with strategy_tag, signal_tier, entry_quality
- **Persist a structured shadow record** (not just log text) with:
  - symbol
  - strategy_tag
  - signal_tier
  - entry_quality
  - signal_price (hypothetical entry)
  - spread_pct
  - range_pct
  - timestamp
  - 1h / 4h / EOD follow-up prices (deferred fill via scheduled check)
  - hypothetical MFE / MAE (computed from follow-up prices)
- Shadow records must be **queryable via API or file** (`data/shadow_trades.json`), not just log text
- Do NOT execute any trades

This gives us paper-mode evaluation during the quarantine period. Without it, the disable period teaches us nothing.

### Book C — uw_flow_short (PROBE)

**Status: TINY PROBE**

Only 1 trade (-$3.31). Insufficient data to judge. Shorts have different dynamics. Keep alive at very small size to accumulate data.

| Parameter | Value |
|-----------|-------|
| Enabled | Yes |
| Sizing | 2% max (probe size) |
| Entry requirements | Jury 2-of-3 required (no single-model probe entries) |
| Exit profile | Standard ratchet (short-side): 1.5% activation, 4% trail, 15 min hold |
| Dead money | Tighten to -1.5% after 4 hours |
| Max concurrent | 2 positions |
| Review after | 10 trades |

### Book D — congress_follow / swing (CONSTRAINED)

**Status: ON, LOW COUNT**

0 trades so far. The infrastructure is there (tier-1 tagging, swing horizon, wider ratchet). Keep it alive to accumulate data, but don't let it consume capital.

| Parameter | Value |
|-----------|-------|
| Enabled | Yes |
| Sizing | 3% max |
| Exit profile | Swing ratchet: 3% activation, 6% trail, 4 hour min hold |
| Dead money | Tighten to -1.5% after 8 hours |
| Max concurrent | 3 positions |
| Holding horizon | swing (days, not hours) |
| Signal freshness | Only signals filed/surfaced within **7 calendar days** (calendar days, weekends count). Older signals skipped unless explicitly whitelisted. |
| Review after | 5 trades |

### Book E — carryover / broker_reconciled (CLEANUP)

**Status: NOT A REAL STRATEGY — EXCLUDED FROM ANALYTICS**

These are artifacts of restart-era fills and broker sync operations that didn't carry the original strategy tag. They are not valid strategy categories.

| Action | Detail |
|--------|--------|
| Fix tagging on entry | When broker sync creates a position, inherit the original strategy_tag from the entry order or candidate metadata |
| Fix tagging on reconciliation | When `broker_fill_reconstructed` records are created, copy strategy_tag from the position dict |
| Exclude from analytics | `carryover` and `broker_reconciled` must be excluded from forward-looking strategy expectancy reporting and per-book scoreboard comparisons — both retroactively where feasible and always going forward |
| Existing trades | Leave as-is in history. They'll age out as clean trades accumulate. New trades must never carry these tags. |

---

## Policy Ownership

Strategy book policy lives in specific places. Do not scatter it.

| Concern | Owner | Location |
|---------|-------|----------|
| Strategy enabled/disabled | Risk Agent | `src/agents/risk_agent.py` |
| Strategy size caps | Risk Agent | `src/agents/risk_agent.py` |
| Entry quality sizing haircut | Risk Agent | `src/agents/risk_agent.py` |
| Per-strategy concurrent position cap | Main loop | `src/main.py` `_process_candidates` |
| Per-sector concurrent cap | Main loop | `src/main.py` `_process_candidates` |
| Shadow mode persistence | Main loop | `src/main.py` `_process_candidates` → `data/shadow_trades.json` |
| Book scoreboard | Dashboard | `src/dashboard/dashboard.py` |
| Strategy analytics exclusion | Trade history | `src/ai/trade_history.py` |

---

## Implementation

### Strategy block in risk agent

In `src/agents/risk_agent.py`, add strategy-level policy constants and gates.

**Important:** Strategy disable check must happen **before** any size math so the output is unambiguous. A disabled strategy produces `can_trade=false` with no downstream sizing noise.

```python
DISABLED_STRATEGIES = {
    "uw_flow_long",
}

STRATEGY_SIZE_CAPS = {
    "uw_flow_short": 2.0,
    "congress_follow": 3.0,
}

STRATEGY_MAX_POSITIONS = {
    "uw_flow_short": 2,
    "congress_follow": 3,
    "momentum_long": 8,
}

ENTRY_QUALITY_SIZE_MULT = {
    "pullback": 1.0,
    "neutral": 0.85,
    "at_highs": 0.50,
}
```

In the `analyze()` function, **immediately after extracting strategy_tag** (before any sizing logic):

```python
strategy_tag = str((signals or {}).get("strategy_tag", "") or "").lower()

# Strategy-level hard block — check FIRST, before any sizing
if strategy_tag in DISABLED_STRATEGIES:
    can_trade = False
    constraint_flags.append(f"strategy_disabled_{strategy_tag}")
    # Return early with clean disabled output — no size math pollution
    return {
        "symbol": symbol,
        "can_trade": False,
        "size_cap_pct": 0.0,
        "reasoning": f"Strategy {strategy_tag} is disabled by book policy",
        "portfolio_heat": _heat_bucket(heat_pct),
        "constraint_flags": constraint_flags,
        "sector": sector,
        "sector_exposure_pct": round(sector_exposure, 2),
        "tier_size_pct": round(tier_size_pct, 3),
        "direction": direction,
        "error": False,
    }
```

After all other sizing logic (heat, streak, tier, spread):

```python
# Strategy-level size cap
if strategy_tag in STRATEGY_SIZE_CAPS:
    strategy_cap = STRATEGY_SIZE_CAPS[strategy_tag]
    size_cap_pct = min(size_cap_pct, strategy_cap)
    if size_cap_pct < tier_size_pct:
        constraint_flags.append(f"size_capped_{strategy_tag}")

# Entry quality sizing haircut
entry_quality = str((signals or {}).get("entry_quality", "neutral") or "neutral").lower()
eq_mult = ENTRY_QUALITY_SIZE_MULT.get(entry_quality, 1.0)
if eq_mult < 1.0:
    size_cap_pct *= eq_mult
    constraint_flags.append(f"size_reduced_entry_{entry_quality}")
```

Remove the previous `uw_flow_long` 40% de-risk (replaced by full disable with early return).

### Strategy position count + sector cap enforcement

In `src/main.py` `_process_candidates`, before jury evaluation:

```python
# Per-strategy position count cap
positions_by_strategy = {}
for pos in positions:
    st = str(pos.get("strategy_tag", "unknown") or "unknown").lower()
    positions_by_strategy[st] = positions_by_strategy.get(st, 0) + 1

strategy_tag = str(candidate.get("strategy_tag", "unknown") or "unknown").lower()
max_for_strategy = STRATEGY_MAX_POSITIONS.get(strategy_tag, 20)
current_count = positions_by_strategy.get(strategy_tag, 0)
if current_count >= max_for_strategy:
    logger.info(f"📊 BOOK CAP {symbol}: {strategy_tag} at {current_count}/{max_for_strategy}")
    continue

# Per-sector cap for momentum_long (with fallback for incomplete sector map)
if strategy_tag == "momentum_long":
    from src.risk.risk_manager import SECTOR_MAP
    candidate_sector = candidate.get("sector") or SECTOR_MAP.get(symbol, "unknown")
    sector_count = sum(
        1 for pos in positions
        if str(pos.get("strategy_tag", "") or "").lower() == "momentum_long"
        and (pos.get("sector") or SECTOR_MAP.get(str(pos.get("symbol", "") or "").upper(), "unknown")) == candidate_sector
    )
    if sector_count >= 3:
        logger.info(f"📊 SECTOR CAP {symbol}: {candidate_sector} already has {sector_count} momentum_long positions")
        continue
```

Note: sector lookup prefers stored `sector` field on the position/candidate, falls back to `SECTOR_MAP`, defaults to `"unknown"`. The `"unknown"` bucket is also capped at 3 to prevent unclassified names from clustering.

### Shadow mode for disabled strategies

In `src/main.py` `_process_candidates`, when a strategy is disabled, **persist a structured shadow record** instead of just logging:

```python
# After risk agent returns can_trade=False for disabled strategy
if "strategy_disabled" in str(constraint_flags):
    shadow_record = {
        "symbol": symbol,
        "strategy_tag": strategy_tag,
        "signal_tier": candidate.get("signal_tier", "tier_2"),
        "entry_quality": candidate.get("entry_quality", "neutral"),
        "signal_price": signal_price,
        "spread_pct": float(candidate.get("spread_pct", 0) or 0),
        "range_pct": float(candidate.get("range_pct", 0) or 0),
        "timestamp": time.time(),
        "price_1h": None,   # filled by deferred check
        "price_4h": None,   # filled by deferred check
        "price_eod": None,  # filled by deferred check
        "mfe": None,
        "mae": None,
    }
    _persist_shadow_record(shadow_record)
    logger.info(
        f"👻 SHADOW {symbol}: {strategy_tag} disabled — hypothetical entry @ ${signal_price:.2f} "
        f"entry_quality={candidate.get('entry_quality')} spread={candidate.get('spread_pct')} "
        f"range_pct={candidate.get('range_pct')}"
    )
    log_activity("shadow", f"👻 {symbol} {strategy_tag} hypothetical @ ${signal_price:.2f}")
    continue
```

Shadow records persisted to `data/shadow_trades.json`. Exposed via `/api/shadow-trades` endpoint for review.

### Congress signal freshness filter

In `src/scanner/scanner.py`, when tagging congress candidates. Uses calendar days (weekends count) as documented in Book D.

```python
if is_congress:
    filed_date = row.get("filed_date") or row.get("transaction_date") or ""
    if filed_date:
        try:
            from datetime import datetime, timedelta
            filed_dt = datetime.strptime(str(filed_date)[:10], "%Y-%m-%d")
            if (datetime.now() - filed_dt).days > 7:
                continue  # stale congress signal — 7 calendar days max
        except Exception:
            pass
```

---

## What This Does NOT Change

- Jury still decides entry (3-model consensus)
- Risk agent still sizes (can_trade / size_cap_pct / constraint_flags)
- Profit ratchet still manages all exits
- Hard stops still protect all positions
- Commentary layers still cannot execute
- Overnight context, entry quality, dead money — all stay as-is
- Scanner still surfaces all candidates (the block happens at risk/entry, not scanning)

The only change is: **some strategies are not allowed to trade, some have position count and sector caps, some have size limits, and entry quality affects sizing.**

---

## Files Changed

| File | Changes |
|------|---------|
| `src/agents/risk_agent.py` | DISABLED_STRATEGIES early return, STRATEGY_SIZE_CAPS, ENTRY_QUALITY_SIZE_MULT, remove old 40% uw_flow_long de-risk |
| `src/main.py` | Per-strategy position count enforcement, per-sector cap with fallback, shadow mode persistence, `_persist_shadow_record` helper |
| `src/scanner/scanner.py` | Congress signal freshness filter (7 calendar days max) |
| `src/ai/trade_history.py` | Exclude carryover/broker_reconciled from strategy analytics (retroactive + forward) |
| `src/dashboard/dashboard.py` | Book scoreboard API + UI (per-book metrics), `/api/shadow-trades` endpoint |

---

## Re-enable Criteria for uw_flow_long

Do NOT re-enable until ALL of the following:

1. **5 full trading sessions** have passed since disable
2. **Shadow mode expectancy is positive**: hypothetical entries tracked during disabled period must show positive expected value — paper-mode must outperform neutral baseline
3. **Entry timing is fixed**: UW flow candidates must pass `entry_quality != "at_highs"` check — entering on pullbacks or neutral, not buying extended moves
4. **Spread discipline verified**: UW flow candidates entering with spread < 0.8%
5. **Manual review** of the UW flow signal quality during the disabled period — are the signals directionally correct even if we're not trading them?
6. **Re-enable at 25% size** (probe mode) for the first 10 trades, then evaluate expectancy before scaling back up

---

## Exit Profile Summary

| Strategy | Activation | Trail | Min Hold | Dead Money |
|----------|-----------|-------|----------|------------|
| momentum_long | 1.5% | 4.0% | 15 min | -1.5% after 4h |
| uw_flow_long | N/A (disabled) | N/A | N/A | N/A |
| uw_flow_short | 1.5% | 4.0% | 15 min | -1.5% after 4h |
| congress_follow | 3.0% | 6.0% | 4 hours | -1.5% after 8h |

---

## Book Scoreboard Requirements

Dashboard or API must expose per-book (ship core metrics first, defer giveback/drawdown if they slow deployment):

**Core (ship now):**

| Metric | Description |
|--------|-------------|
| Realized P&L | Sum of closed trade P&L for this strategy |
| Unrealized P&L | Current open position P&L for this strategy |
| Open position count | Number of currently held positions in this book |
| Trade count | Number of completed trades |
| Win rate | % of trades with positive P&L |
| Avg win | Average P&L of winning trades |
| Avg loss | Average P&L of losing trades |
| Expectancy | `(win_rate * avg_win) - ((1 - win_rate) * abs(avg_loss))` |
| Ratchet activation rate | % of entries that reached ratchet activation threshold |

**Defer to next iteration:**

| Metric | Description |
|--------|-------------|
| Giveback % | Average `(MFE - realized) / MFE` on ratchet exits |
| Max drawdown | Largest peak-to-trough P&L within the book |

Without a book scoreboard, strategy separation exists in policy but not in management. You can't manage what you can't measure.

`carryover` and `broker_reconciled` are excluded from this scoreboard — both retroactively where feasible and always going forward. They are not strategies.

---

## Success Criteria

- [ ] No `uw_flow_long` entries for 5 sessions
- [ ] No `uw_flow_long` fills (zero fills, not just zero intentional entries — verify no leakage)
- [ ] Shadow mode records persisted to `data/shadow_trades.json` and queryable via API, not just log text
- [ ] New `momentum_long` entries reflect full allowed size with `at_highs` haircut to 50% when applicable
- [ ] `momentum_long` limited to 8 concurrent positions, max 3 per sector (including `unknown` sector bucket)
- [ ] `uw_flow_short` limited to 2 concurrent positions max
- [ ] `congress_follow` limited to 3 concurrent positions max with swing ratchet, signals < 7 calendar days old only
- [ ] No `carryover` or `broker_reconciled` strategy tags on new trades
- [ ] `carryover` and `broker_reconciled` excluded from strategy expectancy analytics (retroactive + forward)
- [ ] Dashboard or API exposes per-book: open position count, trade count, realized P&L, unrealized P&L, avg win, avg loss, expectancy, activation rate
- [ ] No regressions in ratchet, hard stop, or entry pipeline behavior

---

## Deployment

- Commit to git, push to main
- VPS: `cd /opt/velox-app && git pull && systemctl restart velox`
- Do NOT use SCP
- Do NOT modify git config
- Verify clean startup with existing positions (dust close orders should have executed by market open)
- Verify `uw_flow_long` produces shadow logs but zero fills on first scan cycle

---

## What v2.2 Is Preparing For

If this works over the next week:

- **v3 structured scoring** becomes feasible because we have clean per-strategy performance data
- **Portfolio allocator** becomes possible because we have defined books with caps
- **Options mirror** can be added as Book F with its own sizing, exit profile, and P&L tracking
- **Strategy-specific entry models** can replace the shared jury for individual books

v2.2's job: **ruthlessly prune what doesn't work, promote what does, and produce clean data for the next architectural step.**
