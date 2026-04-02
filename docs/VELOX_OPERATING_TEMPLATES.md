# VELOX Operating Templates

## How to use this pack

This file is the operating appendix for Velox. Use it to run weekly paper-trading reviews, graduate or demote desks, evaluate experiments cleanly, review setups/triggers/trades consistently, and decide what earns real capital later.

**Principle: Enabled in paper does not mean trusted in production.** Paper mode is for discovery. Production is for proven desks only.

---

# 1. Operating Doctrines

## Paper-Mode Doctrine

In Dev/Test, broad desk enablement is a feature, not a bug. The purpose of paper mode is to discover which desks, modes, triggers, and risk policies actually earn the right to live capital. Enabled does not mean trusted. All paper results must be desk-attributed, mode-attributed, and benchmarked independently. Promotion to production is evidence-driven, not excitement-driven.

## Production Doctrine

Production is not where Velox learns what works. Production is where Velox deploys only what has already proved it works in paper. Every desk in production must have positive expectancy, clean attribution, stable execution, explicit kill switches, and a bounded capital budget. Real capital is earned gradually.

## Options Doctrine

Options remain enabled in paper for discovery because they may provide the most asymmetric payoff opportunities in catalysts, hedging, and premium-selling regimes. But options are not presumed to have edge until they clear independent pilot thresholds with isolated scoreboards, clean execution statistics, and positive expectancy after spread, slippage, and decay.

---

# 2. Weekly Desk Review Template

```
Week of:
Environment: Dev / Test / Paper
Reviewed by:

PORTFOLIO SUMMARY
- Starting equity:
- Ending equity:
- Net P&L:
- Benchmark (SPY):
- Max drawdown:
- Win rate:
- Total trades:

DESK SCOREBOARD
| Desk | Stage | P&L | Trades | Win Rate | Avg Win | Avg Loss | Expectancy | Max DD | Verdict |
|------|-------|-----|--------|----------|---------|----------|------------|--------|---------|
| Momentum | | | | | | | | | |
| Catalyst | | | | | | | | | |
| Exhaustion | | | | | | | | | |
| Swing | | | | | | | | | |
| Options | | | | | | | | | |
| Core Index | | | | | | | | | |
| Hedging | | | | | | | | | |
| Cash Mgmt | | | | | | | | | |

PROMOTIONS / DEMOTIONS
- Promote:
- Demote:
- Shadow only:

WHAT WORKED:
WHAT FAILED:
BIGGEST MISSES:
BIGGEST SAVES:

ACTION ITEMS:
1.
2.
3.
```

---

# 3. Desk Graduation Scorecard

```
Desk:
Current Stage: Research / Shadow / Pilot / Production / Scaled
Review Period:

ELIGIBILITY METRICS
| Metric | Threshold | Actual | Pass/Fail |
|--------|-----------|--------|-----------|
| Trade count | >= 50 | | |
| Positive expectancy | > 0 | | |
| Max drawdown | within limit | | |
| Win rate | desk-specific | | |
| Broker-blocked rate | < 25% | | |
| Trigger miss rate | < 40% | | |
| Impl shortfall drift | < 2x est | | |
| Regime coverage | multiple | | |

DECISION: Promote / Hold / Demote / Shadow / Disable
```

---

# 4. Desk Charter Template

```
Desk Name:
Stage: Research / Shadow / Pilot / Production / Scaled

PURPOSE: What role does this desk play?
STRATEGY: What setups does it trade?
EDGE THESIS: Why should it outperform?
BEST REGIME:
WORST REGIME:
MODES ALLOWED:
ENTRY LOGIC:
EXIT LOGIC:
RISK BUDGET: Max capital / max positions / max sector / max drawdown
GRADUATION CRITERIA:
KILL SWITCH CRITERIA:
BENCHMARK:
```

---

# 5. Paper Experiment Card

```
Experiment Name:
Start Date:
End Date:
Desk:
Hypothesis:

CHANGE BEING TESTED:
WHY IT MATTERS:

METRICS TO WATCH:
- Trade count / Expectancy / Win rate / Avg win-loss / Trigger conversion / Impl shortfall

GUARDRAILS:
- Max capital / Modes included / Rollback condition

OUTCOME: Improved / Neutral / Worse
DECISION: Keep / Roll back / Extend / Promote
```

---

# 6. Trade Postmortem Template

```
Symbol:
Desk:
Mode:
Entry/Exit Time:

ENTRY: Decision price / Execution price / Entry quality / Trigger / Session context / Size
EXIT: Type (ratchet/stop/manual) / P&L / MFE / MAE / Giveback% / Hold time

WAS THE MODE CORRECT?
WAS THE TRIGGER CORRECT?
WAS SIZE CORRECT?
WAS EXIT CORRECT?

CATEGORIZATION:
[ ] textbook winner
[ ] right thesis bad timing
[ ] right trade bad execution
[ ] wrong mode
[ ] wrong trigger
[ ] dead money
[ ] avoidable loss
[ ] acceptable loss

LESSONS:
```

---

# 7. Trigger Miss Report

```
Symbol:
Setup ID:
Desk:
Mode:

WHAT WAS THE TRIGGER?
WHAT ACTUALLY HAPPENED?

WHY DID WE MISS?
[ ] Trigger too strict
[ ] Data lag
[ ] Broker-blocked
[ ] Capital-blocked
[ ] Expired too early
[ ] Mode reclassification
[ ] State machine bug

WHAT SHOULD CHANGE?
```

---

# 8. Production Promotion Checklist

```
Desk / Feature:
Date:

DATA READINESS
[ ] 50+ trades
[ ] Positive expectancy
[ ] Clean attribution
[ ] Stable trigger conversion
[ ] Acceptable impl shortfall
[ ] Replay validates trades

OPERATIONAL READINESS
[ ] Broker execution stable
[ ] Stops/protection verified
[ ] No dust leakage
[ ] State machine clean
[ ] Setup IDs end-to-end
[ ] Kill switch works
[ ] Scoreboards trustworthy

RISK READINESS
[ ] Desk capital cap defined
[ ] Drawdown limit defined
[ ] Sector cap defined
[ ] Live rollout size defined
[ ] Demotion criteria defined

DECISION: Promote / Stay Paper / Shadow / Disable
```

---

# 9. Live Rollout Stage Plan

```
Desk:
Stage: Week 1 / Week 2 / Week 3 / Full

CAPITAL: Live $ / Paper $ / Max position / Max desk budget / Max daily loss
MODES LIVE:
MODES PAPER ONLY:
SUCCESS CRITERIA FOR NEXT STAGE:
ROLLBACK TRIGGERS:
```

---

# 10. Core Index Allocation Policy

```
OBJECTIVE: Compound long-term wealth independent of active trading.

APPROVED HOLDINGS: VOO, VUG, QQQM, VTI, SCHD, VYM
TARGET WEIGHTS: (define per holding)

DCA RULES: Weekly schedule + threshold trigger + reserve floor
REBALANCE: Quarterly
DIVIDENDS: Reinvest per allocation weights
TAX HARVESTING: VOO<>IVV, VTI<>ITOT swaps

PROHIBITED:
- Core holdings cannot fund active trading losses
- Active desk logic cannot sell core holdings
- Emergency override requires human approval
```

---

# 11. Hedge Desk Policy

```
OBJECTIVE: Reduce portfolio drawdown during stress.

TRIGGERS: Risk-off context / VIX threshold / Term structure inversion / Delta threshold
INSTRUMENTS: SPY puts / VIX calls / Inverse ETFs / Sector hedges
BUDGET: Max spend / Max weekly / Max % of equity
SUCCESS METRIC: Drawdown reduction vs unhedged baseline
```

---

# 12. Competitor Bot Experiment

```
Bot A:
Bot B:
Review Window:

DIFFERENCES: Ratchet / Desks / Sizing / Hours / Triggers / Budgets

SHARED: Broker / Scanner / Session context / State machine / Attribution

COMPARISON: P&L / Drawdown / Sharpe / Trades / Expectancy / Conversion / Shortfall

WINNER: Bot A / Bot B / No clear winner
PROMOTE TO MAINLINE: Yes / Partial / No
```

---

# 13. Monthly IC Memo

```
Month:

EXECUTIVE SUMMARY: Return / Benchmark / Excess / Drawdown / Sharpe / Allocation mix

DESK COMMENTARY: (one paragraph per desk)

RISKS:
PROPOSED CHANGES:
DECISION: Approve / Approve with changes / Reject
```

---

# 14. "Should This Desk Exist?"

```
Desk:

THESIS: Why does it add portfolio value?
DIVERSIFICATION: Different return stream?
EDGE SOURCE: Speed / catalyst / pattern / carry / mean reversion / hedging / compounding / tax
EVIDENCE: Trade count / Expectancy / Drawdown / Regime dependence / Broker executability
COST OF KEEPING IT: Engineering / monitoring / capital / false positives / noise

DECISION: Keep / Shadow only / Merge / Kill
```
