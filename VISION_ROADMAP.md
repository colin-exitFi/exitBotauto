# Velox Vision: Self-Managed Portfolio System

## The Goal

Replace a financial advisor. One brokerage account where all money goes. Fully automated portfolio management across every asset class and strategy a real hedge fund would run. Continuously improving by competing against itself. Beat the S&P 500 meaningfully and consistently.

## What a Real Hedge Fund Does (That We Need To Do)

A real hedge fund managing $200k doesn't just momentum scalp. They run:

1. **Active Trading (Alpha Generation)** -- short-term trades that generate cash
2. **Macro Positioning** -- positioning based on rate environment, sector rotation, geopolitical
3. **Event-Driven** -- catalysts, earnings, FDA, mergers, activist positions
4. **Market-Neutral / Hedging** -- shorts and options that protect during drawdowns
5. **Core Holdings** -- long-term compounding positions (index funds, dividend growth)
6. **Risk Management** -- portfolio-level VaR, correlation monitoring, drawdown limits
7. **Cash Management** -- when to deploy, when to hold cash, rebalancing schedule

## Where We Are Today (April 2026)

### Working
- Momentum scalping (continuation_long/short)
- Profit ratchet exit system
- Multi-source scanner (8+ data feeds)
- AI jury/council for trade decisions
- Broker integration (Alpaca)
- Session context (macro regime awareness)
- Pre-trade cost estimation
- Symbol state machine + SQLite persistence
- Trigger engine for pending setups
- Dashboard

### Just Shipped (Tonight)
- Catalyst hold mode (pharma/earnings/congress)
- Exhaustion fade shorts (loosened triggers)
- EOD partial exit (swing positions keep 40% overnight)
- Options pilot (all books whitelisted)
- Sector rotation signal integration
- Dust automation
- Daily operating review
- UW whale flow enabled
- Profit factory ratchet settings

### Not Yet Built
- Index fund accumulation layer
- Dividend capture/reinvestment
- Portfolio rebalancing
- Multi-day position management (proper swing trading)
- Options strategies beyond single-leg
- Hedging / portfolio protection
- Cash allocation logic
- Competitor bot (A/B testing)
- Real money transition controls
- Tax-loss harvesting
- Performance reporting (vs S&P benchmark)

---

## Roadmap: From Trading Bot to Hedge Fund

### Phase 1: Prove the Profit Factory (NOW -- April 2026)
**Goal:** Consistent daily green P&L from active trading.

- [x] V3 institutional workflow engine
- [x] Profit factory ratchet settings
- [x] All strategy books enabled
- [x] Catalyst hold mode
- [x] Exhaustion fades
- [x] Options pilot
- [ ] Tune ratchet from live data (first week)
- [ ] Validate which modes/books actually make money
- [ ] Achieve 3 consecutive green weeks on paper
- [ ] Scale-in on conviction
- [ ] Scale-out on profit milestones
- [ ] Pre-trade cost gate on all entries (not just auto-enter)

**Success metric:** Net positive P&L over a rolling 5-day period.

### Phase 2: Multi-Strategy Portfolio (May 2026)
**Goal:** Active trading + core index holdings + swing positions running simultaneously.

#### Index Fund Accumulation Layer
- Add index fund tickers to a "core holdings" book: VOO, VUG, QQQM, VTI, SCHD, VYM
- Weekly DCA schedule: when cash balance exceeds threshold, buy next allocation
- Allocation weights configurable (e.g., 30% VOO, 20% VUG, 15% QQQM, 15% VTI, 10% SCHD, 10% VYM)
- Never sell core holdings from active trading logic -- separate book with own rules
- Rebalance quarterly (sell overweight, buy underweight)
- Track performance vs SPY benchmark

#### Dividend Management
- Detect dividend payments from broker activity
- DRIP (dividend reinvestment) into the same holding or redistribute per allocation weights
- Track dividend income separately in scoreboard

#### Cash Allocation Logic
- Target: 60% deployed in active trading, 25% in core holdings, 15% cash reserve
- When active trading P&L exceeds weekly target, sweep excess into core holdings
- When drawdown exceeds threshold, reduce active trading allocation and increase cash
- Monthly rebalance between tiers

#### Swing Trading (Proper)
- Multi-day holds with daily re-evaluation
- Different ratchet profile: wider stops, daily trailing instead of intraday
- Earnings positioning: enter 2-5 days before, exit day after
- Sector rotation swings: long hot sector ETF, short cold sector ETF, hold 1-2 weeks

**Success metric:** Portfolio Sharpe ratio > 1.0 over 30 days. Beating SPY on a rolling 30-day basis.

### Phase 3: Options & Hedging (June 2026)
**Goal:** Defined-risk bets + portfolio protection.

#### Options Strategies
- Single-leg calls/puts on high-conviction catalyst plays (already started)
- Covered calls on core holdings for income generation
- Protective puts on large active positions during high-VIX
- Spreads: bull call spreads on momentum, bear put spreads on fades
- Iron condors on range-bound names during choppy regime

#### Portfolio Hedging
- When session context shows risk_off + VIX elevated: auto-buy SPY puts or VIX calls
- Size hedge based on total portfolio delta exposure
- Hedge cost tracked as insurance premium, not a loss

#### Greeks-Aware Position Management
- Track portfolio delta, gamma, theta, vega
- Options positions managed by time decay rules (don't hold into expiry unless catalyst imminent)
- Roll options that are profitable but expiring soon

**Success metric:** Maximum drawdown < 5% in any single week. Options P&L net positive.

### Phase 4: Intelligence & Self-Improvement (July 2026)
**Goal:** The system learns from its own data and improves autonomously.

#### Competitor Bot
- Clone the bot to a second paper account
- Bot A: current strategy (profit factory + catalyst + swing)
- Bot B: experimental strategy (different ratchet, different modes, different sizing)
- Both have visibility into each other's P&L and positions
- Weekly comparison: winning strategy's settings get promoted
- Continuous A/B testing without risking capital

#### Adaptive Strategy Weighting
- Daily review generates observations (already built)
- Monthly review adjusts book budgets based on realized expectancy
- Modes with negative expectancy over 50+ trades get reduced allocation
- Modes with positive expectancy get increased allocation
- Human approval required for changes (governance committee)

#### Market Regime Adaptation
- Risk-on regime: increase momentum allocation, reduce hedging
- Risk-off regime: increase fade/short allocation, increase hedging, reduce momentum sizing
- Choppy regime: increase options (premium selling), reduce directional bets
- Transition detection: when regime changes, preemptively adjust before losses hit

#### Tax-Loss Harvesting
- Track unrealized losses across all positions
- Near year-end: harvest losses to offset gains
- Maintain market exposure by swapping correlated ETFs (sell VOO at a loss, buy IVV)
- Track wash sale rules automatically

**Success metric:** Year-over-year improvement in Sharpe ratio. Tax alpha from harvesting.

### Phase 5: Real Money Deployment (When Ready)
**Goal:** Transition from paper to live with full safety controls.

#### Pre-Launch Checklist
- 30 consecutive trading days net positive on paper
- Maximum drawdown < 3% in any week on paper
- All ratchet/stop protections verified on real fill latency
- Position sizing reduced to 3-5% for live (from 8% paper)
- Extended hours auto-entry restricted to pre-market only
- Opening delay re-enabled (first 10 min)
- Allocator hard-blocking re-enabled
- Daily loss circuit breaker active (-2% max daily loss)
- Manual kill switch tested and working

#### Staged Rollout
- Week 1: $5,000 live, rest stays paper. Only momentum_long book active.
- Week 2: If green, add momentum_short and swing_catalyst.
- Week 3: If green, increase to $15,000. Add remaining books.
- Week 4: If green, full deployment. Begin index accumulation.
- Month 2: Add options layer on live.
- Month 3: Full portfolio management active.

#### The $200k Deployment
- Once 3 months profitable on live with staged rollout
- Deploy full capital across all tiers
- Target allocation: 50% active trading, 30% core index, 20% cash/options reserve
- Monthly performance review vs SPY
- Quarterly rebalancing

**Success metric:** Annualized return > 25% (SPY averages ~10%). Sharpe > 1.5.

---

## The Competitor Bot Architecture

Two identical Velox instances, different strategies:

```
Bot A (Aggressor)                    Bot B (Conservative)
- Tight ratchet (0.3% activation)   - Wider ratchet (1% activation)
- All modes active                   - Only continuation + catalyst
- 8% position sizes                  - 5% position sizes
- Extended hours trading             - Regular hours only
- High trade count target            - Low trade count, high conviction

Both see each other's:
- Daily P&L
- Win rate
- Sharpe ratio
- Max drawdown
- Position count
- Best/worst trades

Weekly: winning strategy's edge gets adopted by the loser
Monthly: full strategy comparison report
```

This is evolutionary optimization applied to trading. Instead of guessing which parameters work, let two bots figure it out by competing.

---

## What This Becomes

A self-managed portfolio system that:

1. **Generates cash** from active trading (momentum, fades, catalysts, options)
2. **Compounds wealth** through index fund accumulation (VOO, VUG, SCHD)
3. **Protects capital** through hedging and risk management (puts, VIX, correlation)
4. **Improves continuously** through competitor bot testing and data-driven tuning
5. **Manages taxes** through loss harvesting and strategic rebalancing
6. **Replaces a financial advisor** completely -- one account, fully automated

No financial advisor. No 17 brokerages. No guessing. Just data-driven portfolio management that gets better every week.

That's the hedge fund in a box.
