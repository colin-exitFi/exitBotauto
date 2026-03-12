# Velox Trading Doctrine
*Written by Colin Tracy — March 12, 2026, 3:38 AM*

---

## The Core Problem: Missing Trading Doctrine

Velox still does not have a tight, human-quality answer to:
- What exact setups are we paid to trade?
- In what market regimes do those setups work?
- What invalidates them?
- When do we use shares vs options?
- When do we do nothing?

A good human does not say: "Here are 14 weak reasons to maybe trade."  
A good human says: "This is a day-2 exhaustion short." or "This is a genuine trend continuation long." or "This tape is garbage, we sit out."

---

## The Mission Is an Outcome Constraint, Not a Behavior Driver

`$25K → $5M` is motivation. It becomes dangerous if it shapes behavior directly — making the bot think:
- dead capital is failure
- every minute out of the market is missed opportunity
- missing runners is worse than bad trades

A good trader maximizes:
- **expectancy**
- **asymmetry**
- **capital preservation**
- **repeatability**
- **regime fit**

The system should be designed around: positive expectancy per strategy, bounded downside per mistake, selective aggression only when setup quality is real.

---

## Consensus vs. Thesis

The 5-agent / jury model is useful for: context synthesis, risk vetoes, management overlays, post-trade explanation.

It is **not** an edge generator.

Edge comes from: faster recognition of a real pattern, better filtering of bad patterns, consistent execution, intelligent management of good vs. bad trades.

A good human trader would not ask five analysts whether to enter every mediocre name. They would:
1. Recognize a specific setup
2. Confirm it fast
3. Size it correctly
4. Manage it actively

**Velox must become more thesis-driven and less consensus-driven at entry.**

---

## Strategy Surface: Too Wide

Current Velox has ingredients for momentum continuation, social momentum, fade-the-runner, UW flow-following, dark pool interpretation, copy-trader following, pharma catalysts, macro-sensitive behavior, and options expression.

That is too much surface area with one shared mental model.

### Core Live Strategies (allowed tomorrow and beyond)
- `breakout_long`
- `fade_short`
- `uw_flow_long`
- `uw_flow_short`

### Research/Probation Only (not live, data collection only)
- copy-trader
- pharma/catalyst
- broad social momentum
- anything not yet proven with real segmented expectancy

---

## UW Is a Native Strategy Family, Not a Feature

Right now Velox mostly treats UW as confidence seasoning, scanner enrichment, optional leverage toggle. That is too weak.

UW gives:
- strike-specific information
- expiry-specific information
- premium concentration
- dark-pool alignment
- tide/gamma context
- fast smart-money positioning hints

**UW needs to drive a first-class options strategy, not just boost confidence scores.**

---

## Regime Specialization

| Regime | Behavior |
|--------|----------|
| `risk_on` | Favor continuation / breakout / constructive flow |
| `risk_off` | Favor fade / short / put-pressure / defensive plays |
| `choppy` | Mostly do nothing. Only very high-quality options-flow dislocations. |
| `mixed` | Size down. Be selective. |

Requires: explicit regime gating, strategy allowlists per regime, different position management by regime, different options usage by regime.

---

## Execution Doctrine

- **Shares** for plain directional / liquidity-friendly setups
- **Options** when the signal is options-native or convexity is the actual edge — not sprinkled on "high confidence" equity trades
- **No trade** in low-quality regime (flat/chop should produce inactivity, not boredom trades)
- Hard per-symbol and per-strategy churn limits
- Broker truth always wins
- If protection is compromised, entries stop

---

## What a Human Trader Would Do

1. **Cut the playbook down aggressively.** Only keep setups with clear logic.
2. **Make each setup explicit.** For each one: trigger, regime, invalidation, ideal holding period, share vs option expression, exit logic.
3. **Stop treating all signals equally.** UW flow ≠ StockTwits buzz. Day-2 fade ≠ breakout continuation.
4. **Accept that "no trade" is a skill.** Flat/chop should produce inactivity.
5. **Use AI as an assistant to a thesis, not as the thesis.** AI scores, vetoes, and manages. It does not create conviction from noise.
6. **Make options intentional.** Use them where signal is options-native or convexity is the real edge.
7. **Judge everything by segmented expectancy.** Not total P&L. By: strategy / regime / source / side / options vs shares / hold profile / timing quality.

---

## Strategic Conclusion

Velox will only become "reliably good" if it evolves from:

> an autonomous signal blender

into:

> a small set of regime-aware trading playbooks with disciplined execution and broker-truth control

The real challenge is not "can the bot process a lot of information?" but "can it behave like a disciplined trader with a coherent edge?"

---

## Next Phase Priorities

1. Define exact playbook for each of the 4 core strategies (trigger, regime, invalidation, share/option routing, exit)
2. Disable or probation-gate everything outside the core 4
3. Build UW as a native first-class strategy family
4. Hard-code regime behavior model
5. Measure segmented expectancy — not aggregate P&L — as the primary success metric
