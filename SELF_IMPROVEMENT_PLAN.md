# Velox Self-Improvement Hardening — Codex Build Plan

## Objective

Close the gaps in Velox's self-improvement pipeline so that:
1. The Advisor's position-level recommendations actually get executed (not just logged)
2. The Tuner measures before/after impact of every parameter change
3. Halt/LULD handling protects positions during trading halts
4. Game Film auto-disable activates earlier with smaller sample sizes
5. The full Observer → Advisor → Tuner → Execution loop is closed-loop, not advisory-only

## Current State

The self-improvement engine has three layers:
- **Game Film** (`src/ai/game_film.py`) — hourly data analysis, persists to `data/game_film.json`
- **Advisor** (`src/ai/advisor.py`) — 30-min Claude call, persists to `data/advisor.json`
- **Tuner** (`src/ai/tuner.py`) — 30-min Claude call, modifies `settings.*` params, persists to `data/config_state.json`

### Problems

1. **Advisor output is a dead letter.** It recommends position-level actions ("exit NVDA", "trim TSLA", "add to AAPL") but nothing reads `data/advisor.json` or routes those recommendations to the Exit Agent or Entry Manager. The Claude API call costs money and produces zero impact on trading behavior.

2. **Tuner has no before/after tracking.** When it changes `STOP_LOSS_PCT` from 1.5 to 2.0, there's no snapshot of performance at time of change and no follow-up measurement. The change history shows what changed but not whether it helped. Claude sees raw trade data and has to eyeball it.

3. **Halt/LULD handling is informational only.** `main.py` sets `pos["halted"] = True` and `pos["luld_state"]` on WebSocket events, but:
   - The exit monitor loop doesn't check `pos.get("halted")` before attempting exits
   - The entry manager doesn't check halt status before entering
   - Halted positions will generate repeated Alpaca 400/422 rejections until the halt lifts
   - LULD bands aren't used to adjust trailing stop placement

4. **Game Film auto-disable threshold is too high.** Requires 30 trades per strategy tag before evaluating. In early trading with <200 total trades spread across 5-10 strategy tags, no single tag will hit 30. A losing strategy can bleed for weeks before accumulating enough data.

5. **No strategy re-enable path.** Once Game Film disables a strategy, there's no automated mechanism to re-test it after market conditions change. Manual enable exists in `strategy_controls.py` but no one calls it programmatically.

## Changes

### 1. Wire Advisor Position Recommendations to Exit Agent

**File: `src/ai/advisor.py`**

Add a method to extract actionable position recommendations:

```python
def get_position_actions(self) -> List[Dict]:
    """
    Extract position-level actions from the last Advisor output.
    Returns list of:
    [{"symbol": "NVDA", "action": "exit|trim|hold|add", "reason": "...", "urgency": "high|medium|low"}]
    
    Only return actions with urgency "high" or explicit exit/trim.
    Filter out "hold" and "add" — those are informational.
    """
```

**File: `src/agents/exit_agent.py`**

Add advisor consumption to the exit agent's monitoring loop:

```python
async def _check_advisor_recommendations(self, positions: List[Dict], advisor: 'Advisor'):
    """
    Called every monitoring cycle. Checks if the Advisor has exit/trim
    recommendations for any currently held position.
    
    Logic:
    - If Advisor says "exit" with high urgency → trigger EXIT_NOW
    - If Advisor says "trim" → tighten trailing stop to 1.5% (force quick exit on any pullback)
    - If Advisor says "exit" with medium urgency → tighten trailing stop to 2.0%
    - Only act on each recommendation ONCE (track processed recommendation timestamps)
    - Never override emergency exits or halt-related exits
    
    Safety:
    - Advisor recommendations are SUGGESTIONS, not orders
    - If risk manager says the exit would violate swing-mode rules, defer
    - Log every advisor-driven action to trade history with reason="advisor_recommendation"
    """
```

**File: `src/main.py`**

In the main monitoring loop (inside the scan cycle), after the existing exit monitoring:

```python
# ── ADVISOR-DRIVEN EXITS ────────────────────────────
if self.advisor and self.exit_agent:
    try:
        await self.exit_agent._check_advisor_recommendations(
            self.entry_manager.get_positions(),
            self.advisor,
        )
    except Exception as e:
        logger.debug(f"Advisor exit check failed: {e}")
```

### 2. Tuner Before/After Measurement

**File: `src/ai/tuner.py`**

Add performance snapshots when parameters change:

```python
# New data structure for tracking change impact
@dataclass
class ParameterChange:
    param: str
    old_value: Any
    new_value: Any
    reason: str
    timestamp: float
    # Snapshot at time of change
    snapshot_win_rate: float
    snapshot_pnl: float
    snapshot_sharpe: float
    snapshot_trade_count: int
    # Filled in later by measure_impact()
    post_win_rate: Optional[float] = None
    post_pnl: Optional[float] = None
    post_sharpe: Optional[float] = None
    post_trade_count: Optional[int] = None
    impact_measured_at: Optional[float] = None
    verdict: Optional[str] = None  # "helped", "hurt", "neutral", "insufficient_data"
```

Add snapshot capture when applying changes:

```python
def _snapshot_performance(self) -> Dict:
    """Capture current performance metrics for before/after comparison."""
    analytics = get_analytics()
    recent = analytics.get("recent_20", {})
    return {
        "win_rate": float(analytics.get("win_rate", 0) or 0),
        "total_pnl": float(analytics.get("total_pnl", 0) or 0),
        "sharpe": float(analytics.get("sharpe_ratio", 0) or 0),
        "trade_count": int(analytics.get("total_trades", 0) or 0),
        "recent_20_win_rate": float(recent.get("win_rate_pct", 0) or 0),
        "recent_20_pnl": float(recent.get("pnl", 0) or 0),
    }
```

Add impact measurement (runs every Tuner cycle):

```python
async def measure_impact(self) -> List[Dict]:
    """
    For each previous parameter change, measure whether it helped.
    
    Logic:
    - Wait until at least 15 NEW trades have completed since the change
    - Compare post-change metrics to pre-change snapshot
    - Verdict:
      - "helped": post win_rate > pre AND post P&L trajectory positive
      - "hurt": post win_rate < pre by >5% OR post P&L significantly worse
      - "neutral": within noise margin
      - "insufficient_data": < 15 trades since change
    
    Feed verdicts back into the Tuner's prompt so it learns:
    "Last time you widened stop loss, win rate dropped 8%. Don't do that again."
    
    Persist to data/tuner_impact.json
    """
```

Update the Tuner prompt to include impact history:

```python
IMPACT_HISTORY (what worked and what didn't):
{json.dumps(self._impact_history[-10:], indent=2, default=str)}

RULE: If a previous change was measured as "hurt", do NOT make the same
change again unless market conditions are fundamentally different.
Cite the impact data when explaining your reasoning.
```

**New file: `data/tuner_impact.json`**
```json
[
    {
        "param": "STOP_LOSS_PCT",
        "old": 1.5,
        "new": 2.0,
        "changed_at": 1772900000,
        "pre_snapshot": {"win_rate": 0.45, "total_pnl": -23.50, "trade_count": 42},
        "post_snapshot": {"win_rate": 0.52, "total_pnl": 15.20, "trade_count": 58},
        "trades_since_change": 16,
        "verdict": "helped",
        "measured_at": 1772986400
    }
]
```

### 3. Halt and LULD Protection

**File: `src/main.py` — `_monitor_positions()` method**

Add halt check at the top of the per-position monitoring loop:

```python
# Inside the position monitoring loop, before any exit logic:
if position.get("halted"):
    logger.debug(f"{symbol}: HALTED — skipping exit checks until trading resumes")
    continue
```

**File: `src/entry/entry_manager.py` — `can_enter()` method**

Add halt check:

```python
# After market_open check, before duplicate check:
# Check if the symbol is currently halted
if hasattr(self, '_halted_symbols') and symbol in self._halted_symbols:
    logger.info(f"⛔ {symbol} is halted — blocking entry")
    return self._set_gate(symbol, False, "halted")
```

**File: `src/main.py` — `_on_halt_status()` callback**

Extend to maintain a halted symbols set that the entry manager can check:

```python
def _on_halt_status(self, symbol: str, status_code: str, reason: str, halted: bool):
    """Pause monitoring + block entries on halted symbols."""
    # Existing position tracking (keep as-is)
    if self.entry_manager:
        pos = self.entry_manager.positions.get(symbol)
        if pos:
            pos["halted"] = bool(halted)
            pos["market_status_code"] = status_code
            pos["market_status_reason"] = reason
            pos["market_status_updated_at"] = time.time()
    
    # NEW: Maintain global halted set for entry blocking
    if not hasattr(self.entry_manager, '_halted_symbols'):
        self.entry_manager._halted_symbols = set()
    
    if halted:
        self.entry_manager._halted_symbols.add(symbol)
        log_activity("alert", f"🚨 {symbol} HALTED ({reason or status_code})")
    else:
        self.entry_manager._halted_symbols.discard(symbol)
        log_activity("alert", f"✅ {symbol} RESUMED")
```

**File: `src/main.py` — LULD-aware trailing stop adjustment**

When LULD bands are active and tightening, adjust protection:

```python
def _on_luld_status(self, symbol: str, band_data: dict):
    """Track LULD bands + adjust protection for held positions."""
    if not self.entry_manager:
        return
    pos = self.entry_manager.positions.get(symbol)
    if not pos:
        return
    
    # Existing tracking (keep as-is)
    pos["luld_state"] = band_data.get("band_state") or band_data.get("indicator") or "active"
    pos["luld_upper_band"] = band_data.get("upper_band")
    pos["luld_lower_band"] = band_data.get("lower_band")
    pos["luld_updated_at"] = time.time()
    
    # NEW: If LULD lower band is tightening toward our entry price,
    # flag position as at-risk so exit agent can tighten protection
    side = pos.get("side", "long")
    entry_price = float(pos.get("entry_price", 0) or 0)
    lower = float(band_data.get("lower_band", 0) or 0)
    upper = float(band_data.get("upper_band", 0) or 0)
    
    if side == "long" and lower > 0 and entry_price > 0:
        distance_pct = ((entry_price - lower) / entry_price) * 100
        if distance_pct < 3.0:  # LULD lower band within 3% of our entry
            pos["luld_at_risk"] = True
            logger.warning(f"⚠️ {symbol} LULD lower band ${lower:.2f} is {distance_pct:.1f}% from entry ${entry_price:.2f}")
        else:
            pos["luld_at_risk"] = False
    elif side == "short" and upper > 0 and entry_price > 0:
        distance_pct = ((upper - entry_price) / entry_price) * 100
        if distance_pct < 3.0:
            pos["luld_at_risk"] = True
            logger.warning(f"⚠️ {symbol} LULD upper band ${upper:.2f} is {distance_pct:.1f}% from short entry ${entry_price:.2f}")
        else:
            pos["luld_at_risk"] = False
```

### 4. Lower Game Film Auto-Disable Threshold + Graduated Response

**File: `src/ai/game_film.py` — `_generate_recommendations()` method**

Replace the current hard 30-trade threshold with a graduated system:

```python
def _generate_recommendations(self, insights: Dict) -> Dict:
    """
    Graduated strategy evaluation:
    
    Tier 1: WARN (10+ trades, <35% WR, negative P&L)
      → Log warning, reduce position size for this strategy by 50%
      → Add to game_film output as "watch_list"
    
    Tier 2: SOFT DISABLE (20+ trades, <33% WR, negative P&L, AND losing in both halves)
      → Disable strategy but allow manual override
      → Add to "soft_disabled" list
    
    Tier 3: HARD DISABLE (30+ trades, <30% WR, negative P&L, losing in both halves)
      → Hard disable, log reason
      → Same as current behavior but with stricter criteria
    
    The key change: strategies start getting throttled at 10 trades instead of 
    running unchecked until 30. This limits bleed while still collecting data.
    """
    
    recs = {}
    by_strategy = insights.get("by_strategy_tag", {}) or {}
    
    watch_list = []
    soft_disable = []
    hard_disable = []
    size_reductions = []  # NEW: per-strategy size multipliers
    
    for tag, bucket in by_strategy.items():
        trades = int(bucket.get("trades", 0) or 0)
        win_rate = float(bucket.get("win_rate_pct", 0) or 0)
        pnl = float(bucket.get("pnl", 0) or 0)
        
        if trades < 10 or pnl >= 0:
            continue
        
        # Check edge stability (need by_strategy to include first/second half)
        # This requires splitting trades by strategy AND by time half
        
        if trades >= 30 and win_rate < 30.0:
            hard_disable.append({
                "strategy_tag": tag, "trades": trades,
                "win_rate_pct": round(win_rate, 2), "pnl": round(pnl, 2),
                "action": "hard_disable",
                "reason": f"Hard disable: {win_rate:.0f}% WR over {trades} trades, ${pnl:.2f} P&L",
            })
        elif trades >= 20 and win_rate < 33.0:
            soft_disable.append({
                "strategy_tag": tag, "trades": trades,
                "win_rate_pct": round(win_rate, 2), "pnl": round(pnl, 2),
                "action": "soft_disable",
                "reason": f"Soft disable: {win_rate:.0f}% WR over {trades} trades, ${pnl:.2f} P&L",
            })
        elif trades >= 10 and win_rate < 35.0:
            watch_list.append({
                "strategy_tag": tag, "trades": trades,
                "win_rate_pct": round(win_rate, 2), "pnl": round(pnl, 2),
                "action": "warn_reduce_size",
                "reason": f"Warning: {win_rate:.0f}% WR over {trades} trades — reducing size 50%",
            })
            size_reductions.append({
                "strategy_tag": tag,
                "size_multiplier": 0.5,
                "reason": f"Game film: {win_rate:.0f}% WR, ${pnl:.2f} P&L over {trades} trades",
            })
    
    if hard_disable:
        recs["disable_strategies"] = hard_disable
    if soft_disable:
        recs["soft_disable_strategies"] = soft_disable
    if watch_list:
        recs["watch_list_strategies"] = watch_list
    if size_reductions:
        recs["size_reductions"] = size_reductions
    
    # ... keep existing best_symbols, worst_symbols, best_hours, etc.
```

**File: `src/data/strategy_controls.py`**

Add soft disable and size reduction support:

```python
_DEFAULT_CONTROLS = {
    "hard_disabled": {},
    "manual_enabled": {},
    "manual_disabled": {},
    "soft_disabled": {},       # NEW
    "size_reductions": {},     # NEW: {"strategy_tag": {"multiplier": 0.5, "reason": "..."}}
}

def get_size_multiplier(tag: str, controls: Dict) -> float:
    """
    Return the size multiplier for a strategy tag.
    1.0 = normal, 0.5 = half size, etc.
    Returns 1.0 if no reduction is active.
    """
    normalized = _normalize_controls(controls)
    reductions = normalized.get("size_reductions", {})
    entry = reductions.get(tag)
    if not entry:
        return 1.0
    return max(0.1, min(1.0, float(entry.get("multiplier", 1.0) or 1.0)))
```

**File: `src/entry/entry_manager.py`**

Apply strategy-level size reductions:

```python
# In enter_position() and enter_short(), after computing notional:
from src.data import strategy_controls
controls = strategy_controls.load_controls()
strategy_tag = sentiment_data.get("strategy_tag", "unknown")

# Check if strategy is disabled
disabled_set = strategy_controls.get_effective_disabled(controls)
if strategy_tag in disabled_set:
    logger.warning(f"⛔ Strategy '{strategy_tag}' is disabled — blocking entry for {symbol}")
    self.last_order_error = "strategy_disabled"
    return None

# Apply size reduction if game film flagged this strategy
size_mult = strategy_controls.get_size_multiplier(strategy_tag, controls)
if size_mult < 1.0:
    logger.info(f"📉 Strategy '{strategy_tag}' size reduced to {size_mult:.0%} by game film")
    notional *= size_mult
```

### 5. Automated Strategy Re-Enable (Probation System)

**File: `src/ai/game_film.py`**

Add a probation check for previously disabled strategies:

```python
def check_probation_candidates(self, controls: Dict) -> List[Dict]:
    """
    Check if any disabled strategies should be re-tested.
    
    A strategy is eligible for probation if:
    1. It was disabled at least 5 trading days ago
    2. Market regime has changed since disabling
       (e.g., was disabled in "risk_off", now we're in "momentum")
    3. The strategy's worst losing pattern doesn't match current conditions
    
    Probation means:
    - Re-enable with 25% normal size for 10 trades
    - If 10-trade probation shows >45% WR and positive P&L → fully re-enable
    - If probation fails → re-disable for another 10 days
    
    Returns list of strategies to put on probation:
    [{"strategy_tag": "fade_runner", "reason": "...", "probation_size_mult": 0.25}]
    """
```

**File: `src/data/strategy_controls.py`**

Add probation state:

```python
_DEFAULT_CONTROLS = {
    "hard_disabled": {},
    "manual_enabled": {},
    "manual_disabled": {},
    "soft_disabled": {},
    "size_reductions": {},
    "probation": {},  # NEW: {"strategy_tag": {"started_at": ..., "trades_completed": 0, "size_mult": 0.25}}
}
```

### 6. Sharpe Ratio Calculation in Trade Analytics

**File: `src/ai/trade_history.py` — `get_analytics()` method**

The Tuner needs Sharpe ratio for before/after measurement, but `get_analytics()` doesn't currently compute it. Add:

```python
def _compute_sharpe(trades: List[Dict], risk_free_rate: float = 0.05) -> float:
    """
    Compute annualized Sharpe ratio from trade P&L series.
    
    - Convert each trade's P&L to a return percentage
    - Calculate mean return and std deviation
    - Annualize based on average trades per day × 252 trading days
    - Sharpe = (annualized_return - risk_free_rate) / annualized_std
    
    Returns 0.0 if insufficient data or zero variance.
    """
```

Add to `get_analytics()` output:
```python
analytics["sharpe_ratio"] = _compute_sharpe(history)
analytics["sharpe_ratio_recent_50"] = _compute_sharpe(history[-50:])
```

## Testing

### `tests/test_advisor_actions.py`
- Test `get_position_actions()` extracts exit/trim recommendations correctly
- Test exit agent processes advisor "exit" with high urgency → triggers EXIT_NOW
- Test exit agent processes advisor "trim" → tightens trailing stop
- Test duplicate recommendation is not processed twice
- Test advisor recommendation respects swing-mode rules

### `tests/test_tuner_impact.py`
- Test `_snapshot_performance()` captures correct metrics
- Test `measure_impact()` waits for 15 trades before measuring
- Test verdict logic: "helped" when win rate improves, "hurt" when it drops
- Test impact history is included in Tuner prompt
- Test "hurt" verdicts prevent repeat changes

### `tests/test_halt_handling.py`
- Test halted position skips exit monitoring
- Test halted symbol blocks new entry via `can_enter()`
- Test resume clears halted state
- Test LULD at-risk flag triggers when band approaches entry price
- Test LULD at-risk clears when band widens

### `tests/test_game_film_graduated.py`
- Test 10-trade strategy with <35% WR → watch_list + 50% size reduction
- Test 20-trade strategy with <33% WR → soft_disable
- Test 30-trade strategy with <30% WR → hard_disable
- Test profitable strategy (any trade count) → no action
- Test size_multiplier is applied in entry_manager
- Test disabled strategy blocks entry

### `tests/test_probation.py`
- Test disabled strategy becomes eligible after 5 trading days
- Test probation re-enables with 25% size
- Test probation success (>45% WR, +P&L) → full re-enable
- Test probation failure → re-disable for 10 days

### `tests/test_sharpe_calculation.py`
- Test Sharpe = 0 when no trades
- Test Sharpe calculation with known P&L series
- Test annualization factor is correct
- Test recent-50 Sharpe is independent of full-history Sharpe

## Execution Order

1. `trade_history.py` — Add Sharpe ratio calculation (Tuner needs this for snapshots)
2. `tuner.py` — Add before/after snapshots + impact measurement + prompt update
3. `game_film.py` — Graduated thresholds (warn at 10, soft at 20, hard at 30)
4. `strategy_controls.py` — Add soft_disabled, size_reductions, probation support
5. `entry_manager.py` — Apply strategy disable checks and size multipliers
6. `main.py` — Halt/LULD protection in monitor loop + entry blocking
7. `advisor.py` — Add `get_position_actions()` method
8. `exit_agent.py` — Add `_check_advisor_recommendations()` method
9. `main.py` — Wire advisor recommendations to exit agent in scan loop
10. `game_film.py` — Add probation candidate checking
11. All tests

## File Map

Files to modify:
- `src/ai/trade_history.py` — Sharpe ratio
- `src/ai/tuner.py` — Before/after snapshots, impact measurement
- `src/ai/game_film.py` — Graduated thresholds, probation candidates
- `src/ai/advisor.py` — Position action extraction
- `src/agents/exit_agent.py` — Advisor recommendation consumption
- `src/entry/entry_manager.py` — Strategy disable + size reduction
- `src/data/strategy_controls.py` — Soft disable, size reductions, probation
- `src/main.py` — Halt protection, LULD handling, advisor→exit wiring
- `src/risk/risk_manager.py` — No changes needed

New files:
- `data/tuner_impact.json` — Parameter change impact tracking

Test files:
- `tests/test_advisor_actions.py`
- `tests/test_tuner_impact.py`
- `tests/test_halt_handling.py`
- `tests/test_game_film_graduated.py`
- `tests/test_probation.py`
- `tests/test_sharpe_calculation.py`

## Important Constraints

- **Advisor actions are suggestions, not commands.** The exit agent should NEVER override emergency exits, circuit breakers, or halt protection based on advisor recommendations.
- **Tuner impact measurement requires patience.** Don't measure impact until 15 trades post-change. Premature measurement is noise.
- **Halt handling must be instant.** When a halt WebSocket event arrives, the halted set must update synchronously. No async delays.
- **Size reductions compound with existing modifiers.** If swing mode already reduces to 70% and game film reduces to 50%, the result is 35%. This is correct — both risk signals apply.
- **Probation is conservative.** 25% size, 10 trades, strict pass criteria. Better to miss re-enabling a good strategy than to re-enable a bad one at full size.
- **All existing tests must still pass.** Run `python -m unittest discover -s tests` after all changes and verify 118+ tests passing.
