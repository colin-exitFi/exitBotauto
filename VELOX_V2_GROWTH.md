# Velox v2.1 — Growth Plan

## Posture

Day 1 was green. The ratchet works. AI layers can't panic-sell. Positions breathe. That is the first time this system has behaved correctly in production.

Now we grow — but we grow **where the architecture can support it.** This is not "turn everything on." This is "accelerate in the places that preserve stability."

The machine just started working. The priority is to make it work harder, not to bolt on new instrument classes or force daily count targets. Earn the right to scale by proving clean attribution first.

**Current state:**
- Equity: $27,560.23 (up from $27,500)
- 5 open positions carrying overnight
- Ratchet proven (5/5 ratchet exits were winners on Day 1)
- Hard stops proven (capped losses at -3%)
- Pipeline unblocked (no playbook gate, no guardrail bugs)
- All 3 AI providers healthy

**Deployment process:**
- Commit to git, push to main
- VPS: `cd /opt/velox-app && git pull && systemctl restart velox`
- Do NOT use SCP
- Do NOT modify git config

---

## Priority 1: Overnight Index Context

### Problem

The bot is blind overnight and pre-market. Observer says "cannot assess market conditions." The jury has no directional context for early entries. Pre-market is when catalysts drop — earnings, FDA, geopolitics — and we can't read the tape.

### What to build

**Fetch overnight/pre-market direction from index ETFs using data sources we already pay for.**

This is not a futures feed with globex depth. It's **overnight index ETF context** — a directional regime hint from SPY/QQQ/DIA/IWM pre-market quotes.

Polygon and Alpaca both provide pre-market snapshots for these tickers. The scanner's `_get_alpaca_snapshot` already works in extended hours.

### Implementation

Create `src/signals/overnight_context.py`:

```python
class OvernightContext:
    """
    Pulls overnight/pre-market direction from index ETFs.
    Provides directional bias to Observer, jury, and overnight thesis.
    """
    TICKERS = ["SPY", "QQQ", "DIA", "IWM"]
    CACHE_TTL = 60  # refresh every 60 seconds

    def get_bias(self) -> dict:
        """
        Returns:
        {
            "direction": "bullish" | "bearish" | "flat",
            "spy_change_pct": float,
            "qqq_change_pct": float,
            "dia_change_pct": float,
            "iwm_change_pct": float,
            "avg_change_pct": float,
            "confidence": float,  # 0-1 based on magnitude
            "timestamp": float,
            "session": "pre_market" | "after_hours" | "overnight" | "regular",
        }
        """
```

Use Alpaca snapshot API first (works in extended hours), fall back to Polygon. Compare current price to previous close to compute change_pct. Cache for 60 seconds.

### Integration points

1. **Observer prompt** — add `OVERNIGHT INDEX CONTEXT: {bias}` so it stops saying "market closed, can't assess"
2. **Jury prompt** — add to existing macro section for directional context on early entries
3. **Overnight thesis** — feed direction into overnight research session
4. **Main loop** — initialize OvernightContext with alpaca_client and polygon_client, call `get_bias()` during overnight/pre-market/after-hours sessions, pass result through to Observer and jury via signals_data
5. **Dashboard** — show overnight bias in AI layers section

### Files to create
- `src/signals/overnight_context.py`

### Files to modify
- `src/main.py` — initialize and call OvernightContext, pass to Observer/jury
- `src/ai/observer.py` — add overnight context to prompt
- `src/agents/jury.py` — add overnight context to prompt
- `src/dashboard/dashboard.py` — display overnight bias

---

## Priority 2: Dead Money Detection and Stop Tightening

### Problem

CRCL sat for 11 hours at -1.83% with zero favorable movement. NBIS short sat for 14 hours going against us. That's capital trapped in losing positions instead of deployed into winners. The -3% hard stop is the right backstop, but positions that never show any life shouldn't get the full 3% runway.

### What to build

**Time-based dead money detection:**
- If a position has been held for 4+ hours AND has never achieved +1.5% peak P&L (never activated the ratchet) AND is currently negative → the position is "dead money"
- Dead money positions get their hard stop tightened from -3% to -1.5%
- This accelerates the exit of positions that aren't working without panic-selling positions that are still developing

### Implementation

In `src/exit/profit_ratchet.py`, add:

```python
DEAD_MONEY_HOURS = float(getattr(settings, "DEAD_MONEY_HOURS", 4.0) or 4.0)
DEAD_MONEY_TIGHT_STOP_PCT = float(getattr(settings, "DEAD_MONEY_TIGHT_STOP_PCT", -1.5) or -1.5)

@classmethod
def is_dead_money(cls, position: dict, current_price: float, now: float = None) -> bool:
    """Position held 4+ hours, never hit ratchet activation, currently red."""
    now_ts = float(now or time.time())
    hold_hours = (now_ts - float(position.get("entry_time", now_ts) or now_ts)) / 3600
    entry_price = float(position.get("entry_price", 0) or 0)
    side = str(position.get("side", "long") or "long").lower()
    peak_price = cls._compute_peak_price(position, current_price, side)
    peak_pnl = cls.calc_pnl_pct(entry_price, peak_price, side)
    current_pnl = cls.calc_pnl_pct(entry_price, current_price, side)
    return hold_hours >= cls.DEAD_MONEY_HOURS and peak_pnl < cls.RATCHET_ACTIVATION_PCT and current_pnl < 0
```

In `src/main.py` monitor loop, after the ratchet check:
- If `is_dead_money` returns true AND the position doesn't already have a tightened stop:
  - Cancel the existing hard stop order
  - Place a new hard stop at -1.5% from entry
  - Set `position["dead_money_tightened"] = True`
  - Log: `"💀 Dead money: {symbol} tightening stop -3% → -1.5%"`

Add settings to `config/settings.py`:
```python
DEAD_MONEY_HOURS = _env_float("DEAD_MONEY_HOURS", 4.0)
DEAD_MONEY_TIGHT_STOP_PCT = _env_float("DEAD_MONEY_TIGHT_STOP_PCT", -1.5)
```

### Files to modify
- `src/exit/profit_ratchet.py` — add `is_dead_money` method
- `src/main.py` — integrate dead money check in monitor loop, tighten hard stop
- `config/settings.py` — add DEAD_MONEY_* settings

---

## Priority 3: Entry Quality Tagging — Stop Buying Peaks

### Problem

We keep buying after the move already happened. EDSA at +31%, SMX at +33%. By the time we enter, the easy money is gone. We're buying other people's exits.

### What to build

**Pullback detection on candidates before jury evaluation:**

| Condition | Tag | Meaning |
|-----------|-----|---------|
| `range_pct > 95` | `at_highs` | Stock at top of day range — risky entry timing |
| `range_pct < 70` AND `change_pct > 3%` | `pullback` | Stock up on day but pulled back from highs — favorable entry |
| Everything else | `neutral` | Normal conditions |

Pass `entry_quality` to the jury prompt so it can factor in timing. **Do not block entries at highs** — just inform the jury so it can adjust confidence and sizing.

### Implementation

In `src/main.py` `_process_candidates`, before jury evaluation:
```python
range_pct = float(candidate.get("range_pct", 50) or 50)
change_pct = float(candidate.get("change_pct", 0) or 0)
if range_pct > 95:
    candidate["entry_quality"] = "at_highs"
elif range_pct < 70 and change_pct > 3:
    candidate["entry_quality"] = "pullback"
else:
    candidate["entry_quality"] = "neutral"
```

In `src/agents/jury.py` prompt template, add:
```
ENTRY TIMING: {entry_quality} — stock is at {range_pct}% of day range
```

Wire `entry_quality` and `range_pct` through `signals_data` to the jury prompt formatter.

### Files to modify
- `src/main.py` — add entry_quality to candidates before jury evaluation
- `src/agents/jury.py` — add entry timing to prompt template and format call

---

## Priority 4: Smarter Position Sizing — Moderated, Not Aggressive

### Problem

We're taking $700-$1300 positions. On $27,500, even a +5% winner is only +$50-65. Sizing should scale with signal quality, but we don't yet have enough clean data to justify full-send sizing.

### What to build

**Tiered sizing based on signal quality — conservative for now:**

| Signal Quality | Size % | Example ($27,500) |
|---------------|--------|-------------------|
| Tier-1 UW flow + 3-of-3 jury unanimous | **6%** | $1,650 |
| Tier-1 UW flow + 2-of-3 jury | **5%** | $1,375 |
| Tier-2 momentum + 2-of-3 jury | **4%** | $1,100 |
| Tier-1 probe (1 model + UW) | **2.5%** | $690 |
| Tier-3 social/trending | **skip or 1.5%** | $410 or skip |

These are moderate increases from current flat sizing. We earn the right to size harder after 50-100 clean trades with stable attribution.

### Implementation

In `src/main.py` where `consensus_size_modifier` is set after jury verdict, adjust based on:
- `signal_tier` (tier_1 gets a boost)
- Agreement level (unanimous gets a boost)
- Jury confidence (>70% gets a modest boost)

The risk agent's `size_cap_pct` already acts as the ceiling. We're adjusting the modifier within that ceiling.

### Also verify: equity-based sizing uses live equity

The `get_position_size` method in `src/risk/risk_manager.py` should use current live equity (synced from Alpaca), not starting equity or a cached number. Verify this is the case. If `_equity` is only set on init, it needs to update on every equity sync in the main loop.

### Files to modify
- `src/main.py` — size modifier logic after jury verdict
- `src/risk/risk_manager.py` — verify equity-based sizing uses live equity from Alpaca

---

## Priority 5: Congress Signal Isolation — Swing Positions

### Problem

Congress data streams in but gets buried in the scanner's 170-candidate pool alongside momentum plays and social trending. Congressional trades are **swing signals** — they hold for weeks or months, not minutes. Managing them with the same intraday ratchet params makes no sense.

### What to build

**Congressional trade signals get distinct treatment:**
- Tagged as `signal_tier: "tier_1"` for recent activity (< 7 days old)
- Tagged with `holding_horizon: "swing"`
- Tagged with `strategy_tag: "congress_follow"`
- Managed with wider ratchet params than intraday momentum

### Implementation

In `src/scanner/scanner.py`, when processing congress scanner candidates:
- Set `signal_tier = "tier_1"` for trades with recent filing dates
- Set `holding_horizon = "swing"`
- Set `strategy_tag = "congress_follow"`

In `src/exit/profit_ratchet.py`, add horizon-aware ratchet behavior. The `check_position` method should accept `holding_horizon` from the position dict and adjust params:

| Horizon | Activation | Trail | Min Hold |
|---------|-----------|-------|----------|
| `intraday` (default) | 1.5% | 4.0% | 15 min |
| `swing` | 3.0% | 6.0% | 4 hours |

This is the **beginning of v3 strategy-specific exit templates** — implemented as a simple horizon lookup, not a full book-isolation refactor.

### Files to modify
- `src/scanner/scanner.py` — congress candidate tagging
- `src/exit/profit_ratchet.py` — horizon-aware ratchet params
- `src/main.py` — pass holding_horizon through to position dict and ratchet
- `config/settings.py` — add SWING_RATCHET_* settings

---

## Deferred: Extended Hours Loosening

**Not yet.** Currently tier-1 only during extended hours.

Enable tier-2 pre-market entries only after ALL of the following are confirmed:
- Overnight index context feed is live and informing the jury
- Dead money logic is live and working
- Entry quality tagging is live
- Spread discipline is proven over 20+ extended-hours evaluations

Pre-market liquidity is thin. Getting this wrong means bad fills that corrupt our clean data.

---

## Deferred: Options Mirror Strategy

**Not yet.** This is a separate instrument class with spreads, Greeks, time decay, contract selection, and thin-liquidity issues. We just stabilized the equity flow.

Options should be a **later parallel book** activated after:
- 50+ clean equity trades with stable strategy attribution
- Ratchet giveback analysis completed (see measurement section below)
- Dead money logic proven
- Entry timing working
- Capital allocator concepts in place

When we do build it:
- UW flow $500K+ premium = trigger
- $500-$1000 fixed position size (not % of equity)
- Separate P&L tracking
- Premium trailing stop at 35%
- No daily minimum trade count — only trade when flow threshold AND contract liquidity are both met

---

## Required Measurements — Track These Starting Now

### Winner audit (start immediately)

For every ratchet exit over the next 20-30 trades, record:
- Peak unrealized gain before exit (MFE)
- Realized gain at exit
- Giveback percentage: `(MFE - realized) / MFE`
- Whether the stock continued running after our exit (check price 1h and 4h post-exit)

This tells us if the 1.5% activation / 4% trail config is actually better than the old 1.0% / 2.0%.

**Implementation:** The position dict already tracks `mfe_pct` and `ratchet_peak_pnl_pct`. On ratchet exit, compute giveback and log it. Also snapshot the price 1h later via a deferred check.

### Loser taxonomy (start immediately)

For every hard stop exit, tag the cause:
- `bad_timing` — entered at highs, stock reversed immediately
- `spread_liquidity` — entry fill was materially worse than signal price
- `wrong_signal` — signal source was incorrect about direction
- `macro_reversal` — broad market turned against position
- `extended_hours_fakeout` — entered in thin session, move wasn't real
- `dead_money` — never showed favorable movement, bled to stop
- `partial_favorable` — had some upside but reversed before ratchet activation

**Implementation:** Add `loss_category` field to trade record on hard stop exits. Can be inferred from metadata: if `entry_quality == "at_highs"` → bad_timing. If `mfe_pct < 0.3%` → dead_money. If `hold_seconds < 300 and session == "extended"` → extended_hours_fakeout.

### Files to modify for measurements
- `src/main.py` — `_record_realized_exit` to compute giveback and loss_category
- `src/exit/profit_ratchet.py` — expose giveback calculation
- Trade record schema to include `giveback_pct`, `loss_category`, `post_exit_1h_price`

---

## Operational KPIs — Replace Motivational Metrics

Do NOT measure "daily P&L trending up each day bigger than the last." Markets don't compound in a straight line. A good system can have 3 green days, 2 red days, and still have a bigger week.

### Measure these instead

| KPI | Target | Why |
|-----|--------|-----|
| Expectancy by strategy tag | Positive for each active strategy | Proves edge exists per strategy |
| Avg winner / avg loser ratio | > 1.5 | Proves asymmetry |
| % of entries that reach ratchet activation | > 40% | Proves entry quality |
| Dead money rate | < 25% of entries | Proves we're not sitting in losers |
| Time to first favorable excursion | < 30 minutes median | Proves entry timing |
| Realized P&L by signal tier | Tier-1 > Tier-2 > Tier-3 | Proves signal hierarchy works |
| Drawdown containment | Max -3% per position, max -5% daily | Proves risk discipline |
| Capital utilization | 30-60% of equity deployed | Proves we're trading, not sitting |
| Ratchet giveback % | < 40% of MFE | Proves ratchet trail is right |

---

## Capital Allocator Concept — Not Built Yet, But Needed for v3

Currently missing from the architecture: a layer that decides how much capital each **strategy type** gets.

Right now every jury BUY gets the same treatment. But we need:
- Max capital in momentum positions
- Max capital in UW flow positions
- Max capital in congress/swing positions
- Max capital in extended hours
- Max capital per symbol family (no more than 2 positions in same sector)
- Regime-based adjustment (risk-off → reduce momentum allocation, increase defensive)

This is a v3 feature. For now, the risk agent's `max_positions=20` and sector caps serve as rough allocator constraints. But as we add more strategy types (congress swing, options mirror), a real allocator becomes necessary.

---

## Execution Order for Codex

1. Overnight index context (Priority 1)
2. Dead money detection + stop tightening (Priority 2)
3. Entry quality tagging (Priority 3)
4. Smarter sizing — moderated (Priority 4)
5. Congress swing signal isolation + horizon-aware ratchet (Priority 5)
6. Winner audit + loser taxonomy measurements (measurements section)
7. Compile / lint / test locally
8. Commit all changes to git, push to main
9. On VPS: `cd /opt/velox-app && git pull && systemctl restart velox`
10. Verify clean startup with existing positions preserved

## Success Criteria

- [ ] Overnight index context visible in Observer and jury prompts during non-regular sessions
- [ ] Dead money detection logs "💀 Dead money" and tightens stops after 4 hours of no favorable movement
- [ ] Entry quality field present on all candidates before jury evaluation
- [ ] Sizing varies by signal tier and agreement level (not flat)
- [ ] Congress candidates tagged as tier_1 / swing / congress_follow
- [ ] Horizon-aware ratchet: swing positions get wider trail (6%) and higher activation (3%)
- [ ] Ratchet exits include giveback_pct in trade record
- [ ] Hard stop exits include loss_category in trade record
- [ ] Existing positions survive restart with hard stops intact
- [ ] No shutdown liquidation
- [ ] No PLAYBOOK GATE blocks
