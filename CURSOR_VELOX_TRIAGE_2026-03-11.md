# Velox Triage Brief for Cursor — 2026-03-11

## Objective
Stabilize Velox’s execution + observability stack after the latest reset.

This is not one bug. It is a cluster of interacting issues:
1. **Broker/internal reconciliation is degraded**
2. **Entry decision-making is under-instrumented**
3. **Jury behavior is over-skipping / over-constrained**
4. **PDT restrictions are actively shaping behavior**
5. **Options trading is disabled even though Unusual Whales options signal is flowing in**
6. **Some positions appear to be reloaded/re-exited repeatedly, suggesting state drift**

The goal is to make the system both **correct** and **auditable**.

---

## What we observed from live VPS logs today

### 1) Reconciliation is still in critical mismatch
Repeated journal lines showed:

- `BROKER TRUTH:`
  - equity around **$24,913–$24,917**
  - small day P&L drift
  - open unrealized losses
  - overnight gap around **-189.58**
- `INTERNAL ANALYTICS:`
  - `pnl_state_realized=0.0`
  - `trade_history_realized=0`
  - `game_film_realized=0.0`
  - `trade_count=0`
- `RECONCILIATION:`
  - `status=critical_mismatch`
  - reasons:
    - `broker_symbols_missing_from_internal`
    - `broker_truth_canary_triggered`
    - `carryover_gap`
    - `internal_closed_trade_subset_only`
    - `residual_position_drift`

### 2) Decision-making exists, but mostly only as final verdicts
The bot **is** evaluating symbols. Logs show lines like:
- `Orchestrator: evaluating <ticker> @ <price> with 5 agents in parallel`
- `Jury verdict for <ticker>: SKIP/BUY ... votes={...}`
- `Jury SKIP for <ticker>: All jury models SKIPped / No consensus`

But for entry decisions we often only see the **final verdict**, not the full reasoning chain.

### 3) Jury is heavily abstaining
Observed many names being evaluated and skipped:
- NBIS
- SPY
- TQQQ
- IREN
- KOLD
- NVDU
- HOOD
- CRM
- NVS
- AAL
- PDD
- EWY
- AAPL
- TZA
- DOMO
- USO
- others

Typical patterns:
- unanimous `SKIP`
- no consensus
- confidence pinned at `0.0%`

This is suspiciously uniform and suggests over-filtering / bad prompt design / malformed inputs / confidence scaling issues.

### 4) PDT is real and active
A key log line:

- `⚠️ PDT count mismatch: Alpaca=168, internal=0`
- `⚠️ PDT SWING MODE: 168/3 day trades used, equity $24,905 < $25K (entries allowed; same-day exits may be blocked by broker)`

This means the bot is operating under active PDT constraints and its internal PDT accounting is also out of sync with Alpaca.

### 5) Options are disabled
Critical startup line from logs:

- `Options trading disabled (set OPTIONS_ENABLED=true to enable)`

This explains why no options positions have opened since reset **even though Unusual Whales options signal is part of the intelligence stack**.

### 6) Exit logic is much more visible than entry logic
Exit side is verbose and aggressive:
- repeated `EXIT_NOW`
- `ADVISOR_STRATEGIC_EXIT`
- strong reasoning text for CRCL / PLYX / RNAC / UPRO adjustments

### 7) State drift / repeated reload behavior likely persists
Observed repeated patterns where positions like **CRCL** and **PLYX** are:
- flagged for exit
- traded against
- then later reloaded from broker again
- then re-flagged for exit

This strongly suggests state reconciliation / persistence drift.

---

## High-confidence diagnosis

### A. Broker truth vs internal truth is still broken
This is not cosmetic. Top-line P&L and position state can still become misleading unless broker truth overrides internal analytics.

### B. Entry decisions are not explainable enough
We can see **that** the system skipped or bought something, but not a clean step-by-step trace of why.

### C. The jury stack is likely over-constrained
A sea of `0.0%` confidence and unanimous skips across many symbols does not look like healthy selective behavior. It looks like:
- thresholds too tight
- prompt framing too restrictive
- insufficient/malformed input context
- consensus logic collapsing to abstention

### D. PDT is reducing flexibility
The system is below $25K and acknowledging PDT swing mode. That likely contributes to risk aversion / execution constraints.

### E. Options intelligence is disconnected from options execution
Unusual Whales can be highly valuable for options flow, but the execution lane is currently disabled.

---

## Implementation plan

# 1) Make Alpaca the source of truth
Treat Alpaca as the authoritative ledger for:
- account equity
- cash
- open positions
- orders/fills
- PDT/day-trade count

Internal state is only a derived/cache layer.

## Required behavior
If broker and internal state disagree:
- **positions:** broker wins
- **cash/equity:** broker wins
- **PDT count:** broker wins
- **top-line P&L:** broker wins

Internal trade history should never be allowed to silently override broker truth.

---

# 2) Build a proper reconciliation engine
Create/strengthen a reconciliation layer that consumes:
- Alpaca account snapshot
- Alpaca positions
- Alpaca orders/fills
- internal positions
- internal trade journal
- previous reconciliation snapshot

It should output:
- reconciled positions
- reconciled realized P&L
- reconciled unrealized P&L
- mismatch severity
- structured mismatch reasons
- trust labels for the dashboard

## Severity tiers
Use at least:
- `healthy`
- `minor_mismatch`
- `critical_mismatch`

## Required behavior by severity
### Healthy
- dashboard normal
- internal analytics trusted

### Minor mismatch
- warning banner
- broker truth drives top-line values
- internal analytics shown but marked degraded

### Critical mismatch
- red banner
- broker truth only for top-line values
- internal analytics dimmed/suppressed where misleading
- structured alert logs emitted
- no false confidence in win rate / realized P&L / trade count

---

# 3) Fix carryover accounting
The logs explicitly show a carryover gap.

You need a clean separation of:
- prior-session unrealized carry
- today realized P&L
- current unrealized P&L

Do **not** let overnight exposure get mistaken for same-day trade performance.

Implement a start-of-day broker snapshot and use it to define:
- opening equity
- opening positions
- carryover basis
- overnight gap

---

# 4) Rebuild realized P&L from broker fills
Current logs imply internal closed-trade accounting is incomplete.

Rebuild realized P&L from actual Alpaca fills/orders, not dashboard event assumptions.

Must support:
- partial fills
- scale-ins
- scale-outs
- shorts
- covers
- cancels/replacements
- carryover positions closed today

---

# 5) Fix PDT handling and make broker PDT authoritative
Observed mismatch:
- `Alpaca=168, internal=0`

This is unacceptable as-is.

## Required changes
- Use Alpaca PDT/day-trade count as truth
- Log internal vs broker PDT only as a diagnostic
- Make the dashboard visibly show when the bot is in `PDT swing mode`
- Explicitly tag entry/exit decisions affected by PDT rules

## Every candidate evaluation should state whether PDT impacted it
Possible flags:
- `pdt_ok`
- `pdt_restricted_exit_risk`
- `pdt_blocks_intraday_plan`

---

# 6) Restore or intentionally redesign the options lane
Critical current state:
- `Options trading disabled (set OPTIONS_ENABLED=true to enable)`

## Immediate task
Determine whether options were intentionally disabled or accidentally disabled during reset.

## If options are supposed to be active
- enable them explicitly
- confirm end-to-end option candidate generation, risk checks, order construction, and execution
- ensure Unusual Whales options signal actually maps into actionable option candidates, not just context

## If options are intentionally off for now
- stop pretending the system is using options flow as an execution edge
- visually distinguish:
  - options signal ingestion
  - options execution disabled

Right now the architecture implies: **we have smart-money options insight but are not following it at all.**

---

# 7) Instrument the entry decision pipeline properly
This is one of the highest-priority usability fixes.

Right now the logs often only show the last line of the movie.

## For every evaluated ticker, log:
1. source of candidate
   - scanner / UW realtime / copy trader / dark pool / etc.
2. raw candidate context
   - symbol, direction, price, catalyst/source, sentiment
3. market regime context
   - trend, volatility, breadth, macro flags
4. risk filters applied
   - position limits, sector limits, PDT flags, liquidity guardrails
5. each model’s response
   - decision: BUY / SHORT / SKIP
   - confidence
   - short rationale snippet
6. consensus outcome
   - unanimous skip / majority buy / disagreement / short blocked / etc.
7. final action
   - placed order / skipped / vetoed / deferred
8. final reason code
   - `jury_unanimous_skip`
   - `jury_no_consensus`
   - `pdt_restricted`
   - `risk_veto`
   - `options_disabled`
   - `confidence_below_threshold`
   - `shorts_blocked`
   - etc.

## Important
Every skip should be explainable without reading code.

---

# 8) Investigate why jury confidence is collapsing to 0%
This is likely not just “bad market.”

Investigate:
- exact Claude / GPT / Grok prompts
- what context each model receives
- how confidence is normalized/scaled
- whether negative defaults are swallowing useful responses
- whether missing fields produce automatic `SKIP`
- whether consensus rules are too rigid
- whether short-side logic is structurally blocked

## Desired healthy behavior
Not constant buying.
But on a meaningful sample of names we should see:
- a mix of SKIPs and candidates
- occasional disagreement
- some medium-confidence setups
- not a near-monoculture of `0.0%` abstentions

---

# 9) Handle model rate limits explicitly
Observed:
- Grok rate limit backoffs
- Grok calls skipped after backoff

## Required behavior
When a model is rate-limited:
- log that clearly
- mark the verdict as degraded / reduced-panel decisioning
- do not silently present the same level of confidence as a full jury panel

Possible fields:
- `jury_panel_expected=3`
- `jury_panel_actual=2`
- `rate_limit_degraded=true`

---

# 10) Fix repeated exit/reload loops
CRCL and PLYX repeatedly appear to:
- be marked for exit
- execute/attempt exit
- then reappear as loaded positions from broker
- then get targeted again

Need to inspect:
- order fill confirmation timing
- broker sync refresh timing
- position close recognition
- stale state persistence
- conflict between disk restore and broker restore

Add logs for:
- why a just-exited position was reloaded
- whether broker still shows it open
- whether the close order partially filled / did not fill / was replaced / canceled

---

## Concrete tasks for Cursor

### Task 1 — Broker-first truth layer
- Make Alpaca the source of truth for equity, cash, positions, fills, PDT count
- Expose trust labels for dashboard components

### Task 2 — Reconciliation hardening
- Fix carryover gap
- Fix missing broker activity in internal history
- Fix closed-trade subset accounting
- Fix residual position drift

### Task 3 — Entry observability
- Add full decision trace logging for every candidate
- Ensure every skip has an explicit reason code

### Task 4 — Jury debugging
- Inspect prompts, thresholds, confidence mapping, consensus logic
- Explain why so many entries resolve to `SKIP conf=0.0%`

### Task 5 — PDT correctness
- Use broker PDT count as truth
- Log and surface PDT impact explicitly in decisions

### Task 6 — Options lane
- Re-enable if intended
- Confirm Unusual Whales options flow can create actual options trades
- If not intended, make disabled state explicit and stop pretending options are part of current execution edge

### Task 7 — Exit/reload loop cleanup
- Ensure exited positions do not get repeatedly reloaded and re-targeted unless broker truly still has them open

---

## Acceptance criteria
This work is done when:

1. **Dashboard top-line equity matches Alpaca**
2. **Open positions match Alpaca**
3. **PDT count shown by the bot matches Alpaca**
4. **Carryover positions no longer distort same-day P&L**
5. **Critical mismatch clearly downgrades trust**
6. **Every candidate evaluation is explainable from logs**
7. **Skipped entries show explicit reason codes**
8. **Model rate-limit degradation is visible**
9. **Options are either intentionally enabled and working, or explicitly disabled and surfaced as such**
10. **Repeated exit/reload loops are eliminated or fully explained by broker state**

---

## Recommended short directive to Cursor

> Treat this as a broker-truth + observability + execution-path audit, not a cosmetic logging patch.
>
> Alpaca must be the source of truth for equity, positions, fills, and PDT count. Internal analytics must downgrade themselves whenever reconciliation fails.
>
> Add full entry-decision tracing so every skip/buy/short is explainable from logs.
>
> Investigate why the jury is collapsing to near-universal `SKIP` with `0.0%` confidence.
>
> Fix the carryover/reconciliation drift, restore or intentionally disable the options lane with explicit visibility, and eliminate repeated exit/reload loops.

---

## Exact known smoking-gun log lines
These are important anchors from today’s VPS logs:

- `Options trading disabled (set OPTIONS_ENABLED=true to enable)`
- `⚠️ PDT count mismatch: Alpaca=168, internal=0`
- `⚠️ PDT SWING MODE: 168/3 day trades used, equity $24,905 < $25K (entries allowed; same-day exits may be blocked by broker)`
- `status=critical_mismatch`
- `reasons=broker_symbols_missing_from_internal,broker_truth_canary_triggered,carryover_gap,internal_closed_trade_subset_only,residual_position_drift`
- `Jury verdict for UPRO: BUY ... votes={'claude': 'SKIP', 'gpt': 'BUY', 'grok': 'BUY'}`
- many repeated lines like `Jury verdict for <ticker>: SKIP ... conf=0.0%`

---

## Summary in one sentence
Velox currently has real signal and real activity, but its **broker reconciliation, entry observability, PDT handling, and options execution path are out of alignment**, making the system hard to trust and harder to debug.
