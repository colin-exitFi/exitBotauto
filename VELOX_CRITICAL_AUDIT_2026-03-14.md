# Velox Critical System Audit — March 14, 2026

**Prepared by:** exitBot  
**Period covered:** March 12–13, 2026 (full 48-hour VPS log dump + complete codebase audit)  
**Status:** 🔴 SYSTEM CRITICALLY BROKEN — Do not trade live until all P0 issues are resolved  
**Target:** Fix everything before Monday open (March 16, 09:30 ET)

---

## Executive Summary

Velox is in a catastrophic multi-system failure state. The past 2 days of trading confirm:

- The **AI jury is non-functional** — GPT and Grok are returning null on nearly every call. Claude alone cannot trade (requires 2/3 agreement). Result: 100% SKIP rate during market hours.
- The **fast_path breakout system bypasses all quality gates** and entered a real position (HIMX) 4 hours after market close at 20:00 ET Friday with a corrupted fill (0.1 shares vs 20.77 intended). This ghost position is currently stuck in `pending_new` with an unresolved exit — creating gap risk Monday.
- The **wash trade error loop** ran for 3+ hours on LWLG and SOC, retrying the same doomed buy orders every 2–3 minutes because the system doesn't detect lingering opposite-side exit orders.
- The **broker reconciliation is permanently mismatched** at -$54.68, with `position_qty_mismatch` and `realized_pnl_mismatch` canaries firing on every heartbeat.
- The **Position Manager AI loop** (PM AI_EMERGENCY) is firing every 2 minutes, screaming about HIMX, but has no mechanism to force-clear a ghost position when the market is closed.
- The **Advisor called EMERGENCY HALT** (34.8% win rate) which cascaded into panic exits across all positions.

Net result: -1% on a -2% market day. Would have been +0.5-1% if the jury was working.

---

## 🔴 P0 — MUST FIX BEFORE MONDAY OPEN

### P0-1: Ghost Position — HIMX (Manual Action Required NOW)

**Symptom:**  
HIMX entered at 20:00 ET Friday (4 hours after close) with 0.1 shares filled vs 20.77 intended. Order status `pending_new` persisting for 200+ minutes. Exit order also stuck. Reconciliation shows -$54.68 mismatch. PM AI is looping every 2 minutes, screaming but can't exit.

**Log evidence:**
```
23:51:44 | WARNING | 🤖 PM AI_EMERGENCY: HIMX — INFRASTRUCTURE FAILURE: 0.1 shares vs intended 20.77,
after-hours entry 4 hours post-close, 'pending_new' status after 232 minutes, zero specialist data,
exit already pending but unconfirmed.

status=minor_mismatch broker_vs_pnl_state=-54.68 broker_vs_trade_history=-54.68
reasons=broker_fill_ledger_unresolved,internal_closed_trade_subset_only,internal_symbols_missing_from_broker_day_bundle
canaries=position_qty_mismatch,realized_pnl_mismatch,broker_fill_ledger_unresolved
```

**Manual action required (you, Colin, right now):**
1. Log into Alpaca paper account
2. Verify if HIMX position exists — if 0.1 shares shows, cancel any pending orders and place a market sell
3. Also cancel any open exit orders for HIMX that may be stuck
4. On Monday morning, verify position is gone before the bot starts

**Code fix (entry_manager.py) — Prevents after-hours fast_path entries:**

The `_execute_fast_path_scout_entry` method does NOT check `is_market_open()`. It only checks `is_extended_hours()` to reduce size. It needs to BLOCK entries entirely after regular hours:

```python
# In _execute_fast_path_scout_entry, add at the top of the method:
async def _execute_fast_path_scout_entry(self, candidate: dict):
    symbol = candidate.get("symbol", "")
    
    # CRITICAL: Block fast_path during after-hours and pre-market
    # The regular can_enter() checks is_market_open(), but fast_path bypasses it
    if not self.is_market_open():
        logger.warning(
            f"⛔ fast_path blocked for {symbol}: market closed "
            f"(hour={datetime.now(ET_TZ).hour}:{datetime.now(ET_TZ).minute})"
        )
        self._fast_path_pending.discard(symbol)
        return
    
    # ... rest of existing code
```

**Code fix — Ghost position force-clear mechanism:**

When `pending_new` persists for more than 30 minutes, the bot should force-remove it from local tracking and cancel the order at Alpaca:

```python
# In position_manager.py or wherever positions are monitored:
MAX_PENDING_AGE_MINUTES = 30

async def _cleanup_stale_pending_positions(self):
    """Force-clear positions stuck in pending_new for too long."""
    now = time.time()
    for symbol, pos in list(self.positions.items()):
        if pos.get("status") == "pending_new":
            entry_time = pos.get("entry_time", now)
            age_minutes = (now - entry_time) / 60
            if age_minutes > MAX_PENDING_AGE_MINUTES:
                logger.warning(
                    f"🧹 Force-clearing ghost position {symbol}: "
                    f"pending_new for {age_minutes:.0f} minutes. "
                    f"Cancelling orders and removing from tracking."
                )
                # Cancel any open orders for this symbol
                await self.broker.cancel_all_orders_for_symbol(symbol)
                # Remove from local tracking
                del self.positions[symbol]
                # Log as ghost
                await self._record_ghost_exit(symbol, pos)
```

---

### P0-2: AI Jury — GPT and Grok Returning Null (System Can't Trade)

**Symptom:**  
Screenshot shows every single symbol returning `missing: gpt` and `missing: grok`. Only Claude is responding. Since the jury requires 2/3 agreement, and 1 model = automatic SKIP, the bot cannot trade at all. The jury voted SKIP on 100% of opportunities during market hours on March 13.

**Log/screenshot evidence:**
```
HIMS:  claude:SKIP · grok:SKIP | missing: gpt
ELPW:  claude:SKIP · grok:BUY  | missing: gpt  (two models, split = SKIP for safety)
SVCO:  claude:SKIP              | missing: gpt, grok
PLYX:  claude:SKIP              | missing: gpt, grok
ORKA:  claude:SKIP              | missing: gpt, grok
NVDA:  claude:SKIP · grok:SKIP  | missing: gpt
```

Every symbol shows "Single model response is insufficient" or "Two models responded without unanimous agreement."

**Root cause analysis:**

1. **Most likely: Model name is wrong on VPS.** The `.env` has `XAI_MODEL=REDACTED`. If it's an old model name (e.g., `grok-2`, `grok-2-vision`, `grok-3-beta`) that's been sunset or renamed, every call will fail with a 404 or "model does not exist" error — which the `_safe_call` wrapper silently swallows and returns `None`. Same issue could affect GPT if `OPENAI_MODEL` points to an unavailable model (e.g., `o3-mini`, `gpt-5-preview`, a fine-tuned model with an expired ID).

2. **Second likely: API keys have billing issues.** If the OpenAI or xAI keys hit their billing limit, the calls fail with 429/402 and get absorbed into the rate-limiter backoff — permanently. The system then marks the provider as "backing off" and never retries cleanly.

3. **Third: Rate limiting cascade.** The `provider_is_backing_off()` function uses exponential backoff. If a provider hit a rate limit once, the backoff can be hours long with no reset mechanism on restart.

**Immediate diagnostic — run this on VPS:**
```bash
# Test GPT directly
curl -s -X POST https://api.openai.com/v1/chat/completions \
  -H "Authorization: Bearer $OPENAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"'$OPENAI_MODEL'","messages":[{"role":"user","content":"say hi"}],"max_tokens":10}' \
  | python3 -m json.tool

# Test Grok/xAI directly  
curl -s -X POST https://api.x.ai/v1/chat/completions \
  -H "Authorization: Bearer $XAI_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"'$XAI_MODEL'","messages":[{"role":"user","content":"say hi"}],"max_tokens":10}' \
  | python3 -m json.tool
```

**Code fix — Add explicit error logging for model failures:**

In `base_agent.py`, the current code swallows errors. Add real error logging:

```python
# In _safe_call in jury.py, improve error visibility:
async def _safe_call(provider_name: str, caller, prompt: str) -> Dict:
    last_error = ""
    for attempt in range(1, 3):
        try:
            result = await caller(prompt, max_tokens=400)
            rate_limited = result is None and provider_is_backing_off(provider_name)
            if result is None and not rate_limited:
                # Log the actual API response error so we can see it
                logger.error(
                    f"🚨 Jury {provider_name} returned None (attempt {attempt}/2) — "
                    f"check API key, model name, billing. "
                    f"backing_off={provider_is_backing_off(provider_name)}"
                )
            # ... rest of existing code
```

**Code fix — Add provider health check on startup:**

In `main.py` startup sequence, add:

```python
async def _startup_provider_health_check():
    """Verify all AI providers are reachable before entering market loop."""
    from src.agents.base_agent import call_claude, call_gpt, call_grok
    
    providers = [
        ("claude", call_claude),
        ("gpt", call_gpt),
        ("grok", call_grok),
    ]
    
    test_prompt = '{"decision":"BUY","size_pct":1.0,"trail_pct":3.0,"reasoning":"test","confidence":75}'
    failures = []
    
    for name, caller in providers:
        try:
            result = await asyncio.wait_for(caller(f"Return exactly this JSON: {test_prompt}", max_tokens=100), timeout=30)
            if result:
                logger.info(f"✅ Provider {name}: healthy")
            else:
                logger.error(f"❌ Provider {name}: returned None — CHECK API KEY AND MODEL NAME")
                failures.append(name)
        except Exception as e:
            logger.error(f"❌ Provider {name}: {e}")
            failures.append(name)
    
    if len(failures) >= 2:
        logger.critical(
            f"🚨 CRITICAL: {failures} providers failed health check. "
            f"Jury cannot operate with fewer than 2 models. "
            f"Bot will SKIP all trades until providers are restored."
        )
    
    return failures
```

**Code fix — Jury graceful degradation (1 model should trade at reduced size):**

The current logic: 1 model responding = SKIP. This is too conservative when GPT/Grok are systemically broken. When only Claude responds, it should still trade at 50% size:

```python
# In jury.py _apply_consensus, modify the single-model case:
if total == 1:
    single_vote = votes[0]
    if single_vote["decision"] in ("BUY", "SHORT") and single_vote["confidence"] >= 75:
        # Allow high-confidence single model trade at reduced size (50%)
        logger.warning(
            f"⚠️ Jury single-model fallback for {symbol}: "
            f"{single_vote['provider']}={single_vote['decision']} conf={single_vote['confidence']:.0f}% "
            f"— trading at 50% size due to degraded jury"
        )
        return _decision_verdict(
            symbol, single_vote["decision"], [single_vote], [],
            providers_used, vote_map, briefs,
            "single_model_high_confidence", 0.50, 0.80,
            degraded=True,
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
        )
    return _skip_verdict(
        symbol, briefs, providers_used, vote_map, total,
        "single_model_insufficient",
        "Single model response is insufficient for action — SKIP for safety",
        # ...
    )
```

---

### P0-3: Risk Agent Denying But Positions Entering Anyway (Safety Bypass)

**Symptom:**  
The exit feed shows multiple entries with this message:
```
ai_emergency: CRITICAL: Risk agent explicitly DENIED entry (approved=False, max_size_pct=0.0)
but position exists anyway — systemic safety bypass.
```

And:
```
ai_emergency: RISK AGENT DENIED APPROVAL — position entered WITHOUT risk clearance
(approved=False, max_size_pct=0.0). After-hours entry at 19:51 ET violates trading protocol.
Infrastructure critically broken per Advisor. Position is UNAUTHORIZED.
```

This means the risk gate is being bypassed. A position with `approved=False` and `max_size_pct=0.0` should result in zero notional, but the position enters anyway.

**Root cause:**  
In `entry_manager.py`, the jury returns a `JuryVerdict` with `size_pct` set based on jury votes. The risk brief's `max_size_pct` is applied AFTER the fact:

```python
# In jury.py (current code — BUG):
# Cap size_pct by risk agent's max
if risk_brief and risk_brief.get("max_size_pct"):
    verdict.size_pct = min(verdict.size_pct, float(risk_brief["max_size_pct"]))
```

The bug: `if risk_brief.get("max_size_pct"):` — this is falsy when `max_size_pct == 0.0`! Zero is falsy in Python. So when risk denies with `max_size_pct=0.0`, the size cap is NEVER applied.

**Code fix:**

```python
# In jury.py — fix the falsy zero bug:
risk_max = risk_brief.get("max_size_pct") if risk_brief else None
if risk_max is not None:  # Use 'is not None', not truthy check — 0.0 must be respected!
    verdict.size_pct = min(verdict.size_pct, float(risk_max))
    if float(risk_max) == 0.0 and verdict.decision in ("BUY", "SHORT"):
        logger.warning(
            f"🛡️ Risk agent set max_size_pct=0.0 for {symbol} — "
            f"forcing SKIP (was {verdict.decision})"
        )
        verdict.decision = "SKIP"
        verdict.reasoning = f"Risk gate: max_size_pct=0.0. {verdict.reasoning}"
```

Also verify in `entry_manager.py` that `can_enter()` is called for all paths including fast_path:

```python
# In _execute_fast_path_scout_entry — add risk check:
risk_brief = await self._get_risk_brief(symbol)
if risk_brief and not risk_brief.get("approved", True):
    logger.warning(f"⛔ fast_path blocked for {symbol}: risk agent denied")
    self._fast_path_pending.discard(symbol)
    return
```

---

## 🟠 P1 — High Priority (Fix This Weekend)

### P1-1: Wash Trade Loop — Retrying Dead Orders for Hours

**Symptom:**  
LWLG and SOC repeatedly tried to buy but got `potential wash trade detected` (code 40310000) because there were still pending sell orders open on the broker side. The system retried every 2–3 minutes for 3+ hours:

```
00:14:25 | ERROR | Market buy failed (LWLG): potential wash trade detected.
  use complex orders — opposite side market/stop order exists
[repeats every 3 minutes from 00:14 to 01:27]
```

This is ~25+ identical failed buy attempts, burning scan cycles and obscuring real signals.

**Code fix — Detect and handle wash trade errors:**

```python
# In alpaca_client.py or entry_manager.py:
WASH_TRADE_CODE = 40310000

async def place_order(self, symbol, qty, side, ...):
    try:
        # ... existing order placement code
    except AlpacaAPIError as e:
        if e.code == WASH_TRADE_CODE or "wash trade" in str(e).lower():
            # Cancel the conflicting order, then retry once
            logger.warning(
                f"⚠️ Wash trade detected for {symbol} — "
                f"cancelling opposite-side order {e.existing_order_id} and retrying"
            )
            await self.cancel_order(e.existing_order_id)
            await asyncio.sleep(1.0)  # Let cancel propagate
            # Retry once
            return await self._place_order_raw(symbol, qty, side, ...)
        raise

# Also: Before placing any buy order, check for open sell orders on the same symbol:
async def _has_conflicting_order(self, symbol: str, side: str) -> bool:
    """Check if there's a pending order on the opposite side."""
    open_orders = await self.get_open_orders(symbol)
    opposite = "sell" if side == "buy" else "buy"
    return any(o.get("side") == opposite for o in open_orders)
```

### P1-2: After-Hours Market Hours Logic

**Symptom:**  
HIMX entered at 20:00 ET, SVCO at 19:51 ET, APEI at 19:49 ET — all after regular market close.

The `is_market_open()` method correctly returns False. BUT `breakout_fast_path` calls `_execute_fast_path_scout_entry()` which skips the `can_enter()` check. It only uses `is_extended_hours()` to reduce size, not to block.

Additionally, the `is_extended_hours()` check is returning `True` at 20:00 ET, which means Velox's definition of "extended hours" extends to 8 PM. This should NOT be a valid entry window for fast_path breakouts.

**Code fix:**

```python
# In entry_manager.py:
def is_extended_hours(self) -> bool:
    """Returns True for pre-market (4-9:30 AM ET) and after-hours (4-8 PM ET)."""
    # ...existing logic...

def is_regular_hours(self) -> bool:
    """True only during regular market hours 9:30 AM - 4:00 PM ET."""
    now_et = datetime.now(ET_TZ)
    market_open = now_et.replace(hour=9, minute=30, second=0)
    market_close = now_et.replace(hour=16, minute=0, second=0)
    return market_open <= now_et <= market_close

# In _handle_fast_path_breakout:
def _handle_fast_path_breakout(self, symbol, price, pct_change, volume_spike):
    # BLOCK fast_path during extended hours — it's a breakout strategy,
    # breakouts need real market liquidity and real volume confirmation
    if not self.is_regular_hours():
        logger.debug(f"fast_path: skipping {symbol} — not regular market hours")
        return
    # ...rest of existing code
```

### P1-3: Reconciliation Mismatch — Stuck at -$54.68

**Symptom:**  
Every heartbeat shows:
```
status=minor_mismatch broker_vs_pnl_state=-54.68 broker_vs_trade_history=-54.68
canaries=position_qty_mismatch,realized_pnl_mismatch,broker_fill_ledger_unresolved
```

This has been stuck at exactly -$54.68 for hours. It's the HIMX ghost position (22 shares × ~$9.85 ≈ exactly that amount being counted as open unrealized but broker shows it differently).

**Code fix — Reconciler needs a "ghost position" recovery path:**

```python
# In reconciliation/reconciler.py:
async def _reconcile_ghost_positions(self):
    """
    If a position exists in local tracking but broker shows zero or pending_new
    for > MAX_PENDING_AGE, force-remove it and mark reconciliation as resolved.
    """
    broker_positions = await self.broker.get_positions()
    broker_symbols = {p["symbol"] for p in broker_positions}
    
    for symbol in list(self.position_tracker.positions.keys()):
        pos = self.position_tracker.positions[symbol]
        
        # Case 1: We track it but broker doesn't know about it
        if symbol not in broker_symbols:
            age = (time.time() - pos.get("entry_time", time.time())) / 60
            if age > 30:  # 30-minute grace period for fills to propagate
                logger.warning(
                    f"🧹 Ghost position detected: {symbol} not in broker "
                    f"after {age:.0f}m — removing from local tracking"
                )
                self.position_tracker.positions.pop(symbol, None)
                self._record_reconciliation_removal(symbol, pos, "ghost_not_in_broker")
        
        # Case 2: pending_new for too long
        elif pos.get("status") == "pending_new":
            age = (time.time() - pos.get("entry_time", time.time())) / 60
            if age > 30:
                logger.warning(f"🧹 Stale pending_new: {symbol} after {age:.0f}m — cancelling and removing")
                await self.broker.cancel_all_orders_for_symbol(symbol)
                self.position_tracker.positions.pop(symbol, None)
```

### P1-4: PM AI Emergency Loop — No Escalation / Backoff

**Symptom:**  
The PM AI_EMERGENCY is called every ~2 minutes about HIMX for 5+ hours straight. Each call costs tokens (Claude API calls) and does nothing because the market is closed. There's no escalation mechanism — it just loops forever.

**Code fix — Add emergency loop backoff and market-hours gate:**

```python
# In position_manager.py (wherever PM AI is called):
_emergency_call_count: Dict[str, int] = {}
_emergency_last_call: Dict[str, float] = {}

async def _call_pm_ai_emergency(self, symbol: str, pos: dict, context: str):
    """Rate-limited PM AI emergency call with market-hours gate."""
    
    # Don't call if market is closed — AI can't do anything anyway
    if not self.entry_manager.is_market_open():
        # Just log locally, don't burn API tokens
        logger.warning(f"[AFTER HOURS] PM emergency for {symbol}: {context[:100]}")
        return
    
    # Exponential backoff per symbol
    count = _emergency_call_count.get(symbol, 0)
    last = _emergency_last_call.get(symbol, 0)
    backoff = min(300, 30 * (2 ** count))  # 30s, 60s, 120s, 240s, 300s max
    
    if time.time() - last < backoff:
        return  # Still in backoff window
    
    _emergency_call_count[symbol] = count + 1
    _emergency_last_call[symbol] = time.time()
    
    # ... existing PM AI call code
```

### P1-5: Position Advisor → Position Manager Disconnect

**From the logs and Week 1 plan:**  
The Advisor screams "exit" with HIGH urgency. The Position Manager says "healthy." They're not connected. The Advisor's exit recommendations are being ignored.

**Code fix:**

```python
# The Advisor should write to a shared state that PM reads:
# In advisor.py:
class AdvisorRecommendation(TypedDict):
    symbol: str
    action: str  # "exit", "reduce", "hold", "add"
    urgency: str  # "critical", "high", "medium", "low"
    reasoning: str
    timestamp: float

# advisor maintains this dict:
_advisor_recommendations: Dict[str, AdvisorRecommendation] = {}

def get_recommendation(symbol: str) -> Optional[AdvisorRecommendation]:
    return _advisor_recommendations.get(symbol)

# In position_manager.py _evaluate_position():
from src.ai import advisor as advisor_module

advisor_rec = advisor_module.get_recommendation(symbol)
if advisor_rec and advisor_rec["urgency"] in ("critical", "high"):
    age = time.time() - advisor_rec["timestamp"]
    if age < 300:  # Fresh recommendation (< 5 minutes old)
        logger.warning(
            f"🎯 PM overriding AI assessment for {symbol}: "
            f"Advisor says {advisor_rec['action']} (urgency={advisor_rec['urgency']})"
        )
        if advisor_rec["action"] == "exit":
            await self._exit_position(symbol, reason=f"advisor_override:{advisor_rec['urgency']}")
            return
```

---

## 🟡 P2 — Important (This Weekend)

### P2-1: 34.8% Win Rate Root Cause

Based on the exit feed, nearly every position exited in the past 2 days was triggered by `ai_emergency` with "ZERO specialist data." This means:

1. The specialist agents (technical, sentiment, catalyst) are running but returning `{}` or `{"error": ...}` for many symbols
2. The PM AI treats "zero specialist data" as an emergency and exits immediately
3. This creates a pattern: enter → no specialist data → immediate exit = guaranteed small loss

**Fix: Require specialist data before entry is allowed:**

```python
# In entry_manager.py can_enter():
async def can_enter(self, symbol, sentiment_score, current_positions):
    # ... existing checks ...
    
    # NEW: Require at least 2/5 specialist agents to have real data
    specialist_coverage = await self._check_specialist_coverage(symbol)
    if specialist_coverage < 2:
        logger.info(
            f"⛔ {symbol} blocked: only {specialist_coverage}/5 specialists have data "
            f"— need at least 2 for entry confidence"
        )
        return self._set_gate(symbol, False, "insufficient_specialist_coverage")
    
    return self._set_gate(symbol, True, "ok")

async def _check_specialist_coverage(self, symbol: str) -> int:
    """Count how many specialist agents have real (non-error) data for a symbol."""
    from src.ai.specialist_cache import get_cached_brief
    count = 0
    for agent in ["technical", "sentiment", "catalyst", "risk", "macro"]:
        brief = get_cached_brief(symbol, agent)
        if brief and not brief.get("error") and len(brief) > 2:
            count += 1
    return count
```

### P2-2: Strategy Controls Not Killing breakout_fast_path After-Hours

The `breakout_fast_path` strategy is explicitly designed for intraday breakouts. It should be disabled after 3:30 PM ET to prevent late-day garbage entries with no follow-through.

```python
# In strategy_controls.py or as a time-based gate in entry_manager.py:
FAST_PATH_CUTOFF_HOUR = 15  # 3:00 PM ET
FAST_PATH_CUTOFF_MINUTE = 30  # 3:30 PM ET cutoff

def _is_fast_path_allowed(self) -> bool:
    now = datetime.now(ET_TZ)
    cutoff = now.replace(hour=FAST_PATH_CUTOFF_HOUR, minute=FAST_PATH_CUTOFF_MINUTE)
    market_open = now.replace(hour=9, minute=30)
    
    if now < market_open:
        return False  # Pre-market: no fast_path
    if now > cutoff:
        return False  # Late day: no fast_path (gap risk, no follow-through)
    return True
```

### P2-3: Jury Prompt — "BIAS TOWARD ACTION" is Too Aggressive

The jury prompt currently says:
```
BIAS TOWARD ACTION. Dead capital is the enemy.
```

And:
```
We have trailing stops at 3%. Maximum downside per trade is 3%.
The cost of a wrong entry is small. The cost of missing a runner is infinite.
```

This framing is causing the jury to recommend BUY on setups with poor quality when specialist data is incomplete. In a -2% market day, this philosophy is wrong. The jury should be regime-aware.

**Fix — Add market regime context to jury prompt:**

```python
# In jury.py, add to signals_data:
market_regime = await _detect_market_regime()
# e.g., "bull_trend", "bear_trend", "choppy", "crash_day"

# In PROMPT_TEMPLATE, add:
MARKET_REGIME: {market_regime}
SPY_TODAY: {spy_change_pct:+.1f}%

# And update the decision framework:
"""
- In BEAR/CRASH regime (SPY < -1.5%): Require higher conviction (75%+ conf).
  Only trade stocks with STRONG idiosyncratic catalyst or SHORT setups.
  Do NOT go long on momentum — most moves are relief bounces that fail.
- In BULL regime (SPY > +0.5%): Standard bias toward action applies.
- In CHOPPY regime: Require 2/3 unanimous BUY, no split verdicts.
"""
```

### P2-4: Trailing Stop Confirmation — PM and Order State Out of Sync

From the exits feed:
```
ai_emergency: ZERO specialist data = blind position, entered 1min ago with IMMEDIATE reversal,
systematic data integrity failure confirmed by Advisor, no intelligence to justify holding
```

Multiple positions were entered and exited within 1 minute. The trailing stop system appears to be working mechanically, but the PM AI is also triggering independent exits. This creates double-exit attempts and broker order conflicts.

**Fix — Add exit deduplication:**

```python
# In position_manager.py:
_exit_initiated: Dict[str, float] = {}  # symbol → timestamp

async def _exit_position(self, symbol: str, reason: str):
    # Prevent double-exit within 30 seconds
    last_exit = _exit_initiated.get(symbol, 0)
    if time.time() - last_exit < 30:
        logger.debug(f"Exit deduplicated for {symbol}: already exiting")
        return
    _exit_initiated[symbol] = time.time()
    
    # ... existing exit logic
```

### P2-5: Cursor/Codex SSH Access to VPS

For Codex to inspect VPS logs:

**Add to `~/.ssh/config`:**
```
Host velox-vps
    HostName 174.138.81.55
    User root
    IdentityFile ~/.ssh/id_ed25519  # or whatever key you have set up
    StrictHostKeyChecking no
    ServerAliveInterval 60
```

Then Cursor can use the Remote SSH extension to directly browse `/opt/velox-app/` and `/var/log/` on the VPS.

---

## 🟢 P3 — Strategy & Architecture Improvements

### P3-1: Options Strategy — Stop Fearing Options

The options engine exists (`src/options/options_engine.py`, `src/options/options_monitor.py`) but is essentially disabled or not integrated into the main trade loop.

Options provide:
- **Defined risk**: max loss = premium paid
- **Leverage**: 10-20x notional exposure per dollar
- **Short-side vehicle**: buy puts instead of shorting (no locate, no short interest issues)
- **Regime alpha**: in high-VIX environments, options ARE the trade

**Integration plan:**
```python
# In main.py scan loop, after jury verdict:
if verdict.decision in ("BUY", "SHORT") and signals_data.get("uw_chain_summary"):
    # Check if options make more sense than shares
    options_eval = await options_engine.evaluate(
        symbol=symbol,
        direction=verdict.decision,
        price=price,
        expiry="weekly",  # Friday expiry
        budget=equity * verdict.size_pct / 100
    )
    if options_eval.edge_score > 0.6 and options_eval.implied_move > signals_data.get("atr_pct", 0):
        # Use options instead of shares
        sentiment_data["use_options"] = True
        sentiment_data["options_contract"] = options_eval.best_contract
        sentiment_data["share_notional_multiplier"] = 0.0  # No shares
```

### P3-2: Regime-Adaptive Strategy Weights

The bot needs to know what kind of market it's in and adjust:

| Regime | Strategy | Position Size | Stop Width |
|--------|----------|---------------|------------|
| Bull trend (SPY > 20-day MA, up >0.5% today) | Momentum long, hold longer | 2-3% | 4-5% trail |
| Bear trend (SPY < 20-day MA, down >1%) | Short setups, fade runners | 1% | 2-3% tight |
| Crash day (SPY < -2%) | Cash + puts only | 0.5% | 1.5% very tight |
| Choppy (range-bound, low volume) | Fade the moves | 0.5% | 2% |
| High VIX (>25) | Options over shares | 0.5% notional | N/A (defined risk) |

```python
# Add to config.py or a new market_regime.py:
class MarketRegime(Enum):
    BULL = "bull"
    BEAR = "bear"
    CRASH = "crash"
    CHOPPY = "choppy"
    HIGH_VOL = "high_vol"

async def detect_regime(spy_change_pct: float, vix: float, spy_vs_20ma: float) -> MarketRegime:
    if spy_change_pct < -2.0:
        return MarketRegime.CRASH
    if vix > 25:
        return MarketRegime.HIGH_VOL
    if spy_change_pct < -1.0 or spy_vs_20ma < -0.02:
        return MarketRegime.BEAR
    if spy_change_pct > 0.5 and spy_vs_20ma > 0.01:
        return MarketRegime.BULL
    return MarketRegime.CHOPPY
```

### P3-3: Short Strategy — Real Short Thesis Required

Currently, the jury can vote SHORT but the specialist agents and prompt aren't tuned for short setups. The short thesis needs:

1. **Technical**: RSI > 70 (overbought), price at or above Bollinger upper, volume declining
2. **Catalyst**: Earnings miss, guidance cut, sector headwind, FDA rejection
3. **Fade signal**: Yesterday's runner that's now stalling (this exists in `fade_runner.py`)
4. **Options confirmation**: Put flow > call flow from UW

Add to the jury prompt for SHORT-specific validation:
```
SHORT validation — ALL must be true:
1. Technical agent says SELL or RSI > 65
2. Price is DOWN from yesterday's close OR this is a fade setup
3. Unusual Whales shows bearish flow OR neutral
4. Risk agent approves (portfolio not already net-short)
Do NOT go short just because SPY is down.
```

### P3-4: Position Hold Time Distribution

Looking at the exit feed, nearly every position exited in 0–8 minutes. This is too short for any real edge to manifest. Momentum trades need time.

**Minimum hold time by strategy:**
```python
MIN_HOLD_MINUTES = {
    "breakout_fast_path": 3,     # Scout positions: exit fast if thesis fails
    "momentum_long": 15,          # Must hold at least 15 min for momentum to develop
    "uw_flow_long": 30,           # Options flow usually plays out over 30-60 min
    "fade_runner": 10,            # Give the fade room to develop
    "carryover": 60,              # Overnight thesis = hold until day plays out
}

# In position_manager.py:
async def _should_exit_early(self, symbol: str, pos: dict, reason: str) -> bool:
    strategy = pos.get("strategy_tag", "unknown")
    min_hold = MIN_HOLD_MINUTES.get(strategy, 5)
    hold_minutes = (time.time() - pos.get("entry_time", time.time())) / 60
    
    if hold_minutes < min_hold:
        # Only override for hard stops (actual price loss), not AI panic
        if "ai_emergency" in reason or "zero_specialist" in reason:
            logger.info(
                f"⏱️ Holding {symbol}: {hold_minutes:.1f}m < {min_hold}m minimum "
                f"for {strategy}. AI emergency suppressed."
            )
            return False
    return True
```

---

## 🔵 P4 — Monday Readiness Checklist

Before Monday 9:00 AM ET, run through this checklist:

### Manual Actions (Colin):
- [ ] **Verify HIMX position cleared** in Alpaca paper account — cancel any stuck orders
- [ ] **Test GPT API key**: `curl -s https://api.openai.com/v1/models -H "Authorization: Bearer $OPENAI_API_KEY" | python3 -m json.tool | grep -i "gpt\|o3\|o4"`
- [ ] **Test xAI/Grok API key**: `curl -s https://api.x.ai/v1/models -H "Authorization: Bearer $XAI_API_KEY" | python3 -m json.tool`
- [ ] **Verify model names** in `.env` match available models
- [ ] **Check billing**: Log into OpenAI and xAI dashboards — verify both accounts have positive balance and active billing

### Code Changes (Codex/Opus):
- [ ] P0-1: Add market-hours gate to `_execute_fast_path_scout_entry`
- [ ] P0-1: Add ghost position force-clear mechanism (30-minute timeout)
- [ ] P0-2: Fix jury single-model logging (surface actual errors)
- [ ] P0-2: Add startup provider health check
- [ ] P0-2: Add single-model 50%-size fallback
- [ ] P0-3: Fix `if risk_brief.get("max_size_pct"):` → `is not None` check
- [ ] P1-1: Add wash trade detection and conflicting order pre-check
- [ ] P1-2: Add `is_regular_hours()` and block fast_path in extended hours
- [ ] P1-4: Add PM AI emergency loop backoff + market-hours gate
- [ ] P1-5: Wire Advisor recommendations into PM decision loop
- [ ] P2-2: Add 3:30 PM fast_path cutoff
- [ ] P2-4: Add exit deduplication (30-second window)

### Deployment:
- [ ] `git pull` on VPS after all changes are pushed
- [ ] `systemctl restart velox`
- [ ] Watch first 15 minutes of jury scan output
- [ ] Verify GPT and Grok show as `voting` (not `missing`) in dashboard

---

## Appendix: Raw Log Patterns Summary

| Pattern | Frequency | Impact |
|---------|-----------|--------|
| `missing: gpt` in jury | Every symbol, all day | Jury can't trade |
| `missing: grok` in jury | Most symbols | Jury can't trade |
| `pm_ai_emergency: HIMX INFRASTRUCTURE FAILURE` | Every 2 minutes, 5 hours | Token burn, noise |
| `status=minor_mismatch broker_vs_pnl_state=-54.68` | Every 30 seconds | Reconciliation broken |
| `Market buy failed: potential wash trade detected (LWLG/SOC)` | Every 2-3 min, 3 hours | Capital locked, buy blocked |
| `TRADING BLIND - zero specialist data` | Multiple exits | 34% win rate driver |
| `RISK AGENT DENIED APPROVAL but position exists` | 2 entries | Safety bypass |
| `breakout_fast_path` + `after-hours` entries | 3 symbols | Ghost positions |
| `Advisor: EMERGENCY HALT CONFIRMED - 34.8% win rate` | Once | Cascading exits |

---

## The Core Diagnosis

Colin, here's the honest read:

**This isn't a strategy problem. It's an infrastructure problem.**

The jury — which is supposed to be the entry gate — has been effectively disabled for 2 days because GPT and Grok are returning null. The bot has been running on vibes (fast_path breakouts without jury validation) and panic exits (PM AI triggered by the same missing-data conditions that should have blocked entry).

The 34.8% win rate isn't because the edge doesn't work. It's because we're entering positions with zero intelligence and then exiting when the intelligence-checking logic says "wait, I have no data for this position." That's not a bad edge — that's a broken pipeline creating guaranteed losers.

Fix the models (P0-2), fix the safety bypass (P0-3), fix the after-hours gate (P0-1/P1-2), and the jury architecture that won Velox paper Day 1 (+0.68%) will work again.

The market being down 2% and us only losing 1% is actually evidence the core is sound — the trailing stops and risk sizing did their job. The bot just needs its brain back.

**This weekend: fix the infrastructure. Monday: let the jury run.**
