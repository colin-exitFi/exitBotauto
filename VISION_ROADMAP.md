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

## Where We Are Today (April 7, 2026)

### Working & Proven
- Multi-source scanner (8+ data feeds: Alpaca, Polygon, StockTwits, UW, Congress, EDGAR, Pharma, Grok X)
- AI jury/council for trade decisions (Claude + GPT + Grok)
- Broker integration (Alpaca paper account)
- Session context (VIX, rates, yield curve, SKEW, sector rotation, overnight bias)
- Pre-trade cost estimation (Almgren-Chriss market impact, slippage, edge-vs-cost)
- Symbol state machine + SQLite persistence (32k+ transitions, 84k+ funnel events)
- Trigger engine for pending setups (206 pending setups tracked)
- Dashboard with live positions and analytics
- Daily operating review (auto-generates at 4:15 PM ET, 5 days of reviews saved)
- Setup funnel tracking full pipeline: scan → classify → trigger → cost → enter → exit

### Active Desks (Paper, Data Collection Mode)
- **Desk 1: Momentum** — Production Candidate. continuation_long/short. Profit factory ratchet (-0.75% stop, 0.3% activation, 1% trail, 2min hold). 124 trades in 5 days.
- **Desk 2: Catalyst** — Pilot. catalyst_hold mode for pharma FDA, earnings, congress. Wider -5% stop, dead money disabled.
- **Desk 3: Exhaustion Fades** — Experimental. Loosened triggers (VWAP loss, range contraction). 3 trades so far.
- **Desk 4: Swing** — Early Pilot. EOD partial exit keeps 40% overnight. Multi-day holds enabled.
- **Desk 5: Options** — Research/Pilot. All books whitelisted, 55% min confidence. 2 trades.
- **UW Whale Flow** — Enabled. 83 trades via uw_flow_long book.
- **Sector Rotation** — Candidates tagged hot/cold from SectorRotationModel.

### Critical Bugs Fixed (April 7)
- Hard stops were NOT being placed on broker-synced positions (entry state stuck at "open"). 44% of losses ($126/week) from unprotected positions. **FIXED: force entry confirmation for all broker-confirmed positions.**
- Reconciler creating phantom losing trades from broker activity. 31% of losses ($89/week). **FIXED: reconstructed trades with P&L < -$5 blocked from trade history.**
- Exit agent killing positions within the hard stop range. 8% of losses ($22/week). **FIXED: exit agent backs off when position is losing but above stop level.**
- Dashboard showing phantom P&L (AAPL +$154 ghost). **FIXED: dashboard reads clean analytics.**
- VIX reading stale FRED data (30.6 vs actual 24.5). **FIXED: Polygon live data first, FRED fallback.**
- Position sizing crushed by allocator and risk agent stacking reductions. **FIXED: .env 8% respected, allocator advisory only.**

### 5-Day Performance (Paper)
- Starting equity: $26,863
- Current equity: $26,533
- Week change: -$330 (-1.2%)
- Of that: ~$237 (69%) was from bugs now fixed, ~$104 (31%) was real trading friction
- 124 trades, 37% win rate, avg winner +$2.48, avg loser -$5.91
- Only profitable book: social_momentum_long (+$20, 77.8% win rate, 9 trades)

### Operational Infrastructure
- SQLite StateStore: pending setups, state transitions, funnel events, session snapshots
- Shadow trade tracking for all blocked candidates
- Dust auto-cleanup via dust_policy
- Reconciliation canary deduplication
- Book-to-mode mapping
- Provider health tracking
- Latency budget monitoring
- Replay harness for post-trade diagnostics

### Not Yet Built
- Index fund accumulation layer (Desk 6: VOO, VUG, QQQM, VTI, SCHD, VYM)
- Portfolio rebalancing / DCA logic
- Dividend capture/reinvestment
- Options strategies beyond single-leg (covered calls, spreads, hedges)
- Hedging desk (SPY puts, VIX calls, inverse ETFs)
- Cash allocation logic (capital waterfall between desks)
- Scale-in on conviction / Scale-out on profit milestones
- 13F follower book (Berkshire, Bridgewater, Renaissance position tracking)
- Competitor bot (A/B testing)
- Real money transition controls
- Tax-loss harvesting
- Performance reporting vs SPY benchmark

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

---

## The Full Hedge Fund Playbook

A real hedge fund runs multiple desks. Each desk has a strategy, a risk budget, and its own performance metrics. Velox needs the same structure.

### Capital Hierarchy (Order of Priority)

1. Maintain emergency cash reserve (minimum 10% of equity)
2. Maintain hedge minimum if session context shows risk_off
3. Fund proven active desks up to their risk budget
4. Sweep excess realized gains to core index book (Desk 6)
5. Experimental desks receive capped risk capital only
6. No desk may pull capital from core holdings without explicit human approval

### Desk Graduation Framework

Every desk must earn its budget. Labels:

| Stage | Meaning | Capital Access |
|-------|---------|---------------|
| **Research** | Idea only, no trades | None |
| **Shadow** | Paper tracking, no real orders | None |
| **Pilot** | Live trades, capped at 5% of active capital | Minimal |
| **Production** | Proven positive expectancy over 50+ trades | Full budget |
| **Scaled** | Sustained production, eligible for budget increase | Up to 2x budget |

Graduation rules: positive expectancy over 50 trades, max drawdown < desk limit, win rate above desk-specific threshold. Demotion rules: negative expectancy over 30 trades, max drawdown breached, or kill switch triggered.

### Desk Kill Switches

Each desk auto-downgrades if:
- Expectancy negative over rolling 30 trades → production → pilot
- Max drawdown exceeds desk limit → pilot → shadow
- Implementation shortfall > 2x estimated for 20+ trades → pilot → shadow
- Broker-blocked rate > 50% of setups → shadow → off
- Trigger miss rate > 80% over 50 setups → shadow → off

### Desk 1: Momentum / Intraday — PRODUCTION CANDIDATE
**Strategy:** Buy strength, sell weakness. Tight ratchet, quick in and out.
**Edge:** Speed of signal processing across 8+ data sources.
**Best regime:** Risk-on trending days.
**Risk budget:** 30% of active capital.
**Books:** momentum_long, momentum_short, social_momentum_long/short
**Infrastructure:** Scanner, classifier, jury/council, profit ratchet.

### Desk 2: Event-Driven / Catalyst — PILOT
**Strategy:** Position ahead of known catalysts. Hold through event. Asymmetric payoff.
**Edge:** Pharma FDA dates, earnings calendar, congress filings, 13F changes.
**Best regime:** Any -- catalysts are regime-independent.
**Risk budget:** 20% of active capital.
**Books:** pharma_catalyst, congress_follow, catalyst_hold
**Infrastructure:** Pharma scanner, congress scanner, EDGAR 13F scanner, earnings scanner.
**Signal sources already built:**
- `src/signals/edgar.py` -- SEC EDGAR filings (8-K, Form 4 insider, SC 13D activist stakes). Already scanning and feeding insider signals to watchlist. Extend with 13F quarterly filing parser for Berkshire, Bridgewater, Renaissance, Citadel, Pershing Square position changes.
- `src/signals/congress.py` -- Congress trades from Unusual Whales + Quiver. 87 cached trades. Already wired into scanner.
- Pharma scanner -- FDA NDA/BLA dates with scores. Already finding BIIB, DNLI, ORCA.
- `src/signals/earnings.py` -- Earnings calendar. Already initialized.
- `src/signals/finnhub.py` -- Economic calendar for macro catalysts.

**13F Follower Book (to build):**
- Parse quarterly 13F-HR filings from SEC EDGAR for top funds
- Detect new positions (fund didn't hold last quarter, holds now)
- Detect significant increases (>25% position size increase)
- Detect significant decreases (>25% reduction or full exit)
- Map to catalyst_hold mode with quarterly rebalance horizon
- Buffett buys XYZ → we buy XYZ as a swing_catalyst_long
- When multiple top funds converge on the same name → higher conviction
- Funds to track: Berkshire Hathaway, Bridgewater, Renaissance Technologies, Citadel, Pershing Square, Appaloosa, Third Point, Greenlight Capital, Soros Fund Management, Viking Global

### Desk 3: Exhaustion / Mean Reversion — EXPERIMENTAL
**Strategy:** Short overextended runners after they lose momentum. Fade the euphoria.
**Edge:** Pattern recognition on exhaustion signals (VWAP loss, range contraction, volume decel).
**Best regime:** Volatile days with big movers. Natural hedge against momentum desk.
**Risk budget:** 10% of active capital.
**Books:** fade_runner, exhaustion_fade_short
**Infrastructure:** Mode classifier, loosened triggers (just shipped).

### Desk 4: Swing / Multi-Day — EARLY PILOT
**Strategy:** Enter quality setups, hold 2-10 days. Wider stops, bigger targets.
**Edge:** Patience. Most bots can't hold overnight. We can with EOD partial exit.
**Best regime:** Trending markets with sector rotation.
**Risk budget:** 15% of active capital.
**Books:** watchlist_long/short, swing plays
**Infrastructure:** EOD partial exit (just shipped), sector rotation model, overnight context.

### Desk 5: Options — RESEARCH/PILOT (HIGH CAUTION)
**Strategy:** Defined-risk bets for leverage and hedging.
**Edge:** Asymmetric payoff on catalysts. Income generation on core holdings.
**Best regime:** Any -- different option strategies for different regimes.
**Risk budget:** 10% of active capital.
**Plays:**
- Catalyst calls: buy calls ahead of FDA/earnings for leveraged upside, capped downside
- Covered calls: sell calls against core index holdings for income
- Protective puts: buy puts on portfolio during high-VIX for crash protection
- Bull/bear spreads: defined-risk directional bets on high-conviction setups
- Iron condors: sell premium on range-bound names during choppy regime
**Infrastructure:** Options engine (live, pilot mode), Unusual Whales options flow, Polygon options data (upgrade needed for full chain).

### Desk 6: Core Holdings / Index — FUTURE BUILD
**Strategy:** Long-term compounding through low-cost index funds and dividend growth.
**Edge:** Discipline. Systematic DCA. Rebalancing. Tax efficiency.
**Best regime:** All -- this is the permanent allocation.
**Risk budget:** Separate from active trading. Target 30-50% of total portfolio.
**Holdings:**
- QQQM (Nasdaq 100 -- growth)
- VOO (S&P 500 -- broad market)
- VUG (Growth index)
- VTI (Total market)
- SCHD (Dividend growth)
- VYM (High dividend yield)
**Rules:**
- Weekly DCA when cash exceeds threshold
- Quarterly rebalance to target weights
- Never sold by active trading logic
- Dividends reinvested per allocation weights
- Tax-loss harvest near year-end (swap VOO↔IVV, VTI↔ITOT)

### Desk 7: Risk / Hedging — FUTURE BUILD
**Strategy:** Portfolio protection during regime transitions and tail events.
**Edge:** Session context detects risk-off before positions bleed.
**Risk budget:** 5% of active capital (insurance cost).
**Plays:**
- VIX calls when term structure inverts (backwardation = stress)
- SPY puts when portfolio delta exceeds threshold
- Reduce all position sizes automatically during risk_off regime
- Inverse ETFs (SQQQ, SH) as temporary hedges during fast selloffs
**Infrastructure:** Session context (VIX, term structure, SKEW already built). Concentration guard (beta/delta monitoring).

### Desk 8: Cash Management — FUTURE BUILD
**Strategy:** Optimal allocation of capital across all desks.
**Edge:** Never over-deployed, never under-deployed. Dynamic based on regime and opportunity.
**Rules:**
- Active trading desks (1-5): size based on regime confidence
- Risk-on: deploy up to 80% across active desks
- Neutral: deploy up to 60%
- Risk-off: deploy up to 30%, rest in cash or hedges
- Core holdings (Desk 6): steady accumulation regardless of regime
- Cash reserve: minimum 10% always available for opportunities
- When a catalyst fires (FDA approval, earnings beat): immediate cash available for scale-in
- When drawdown exceeds -3% weekly: reduce active allocation, increase cash

---

## The Competitor Bot

Two identical Velox instances, different strategies, full visibility into each other:

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

Evolutionary optimization applied to trading. Instead of guessing, let them fight it out.

---

## What This Becomes

**Velox Capital:** A self-managed portfolio system that runs every play a real hedge fund runs.

1. **Generates cash** from 5 active trading desks (momentum, catalyst, fades, swing, options)
2. **Compounds wealth** through systematic index accumulation (VOO, VUG, SCHD, VYM)
3. **Protects capital** through hedging desk and regime-aware cash management
4. **Discovers edge** by following smart money (13F filings, insider trades, congress, whale flow)
5. **Improves continuously** through competitor bot A/B testing
6. **Manages taxes** through loss harvesting and strategic rebalancing
7. **Replaces a financial advisor** completely -- one account, fully automated

No financial advisor. No 17 brokerages. No guessing. Data-driven portfolio management that gets better every week.

The infrastructure exists today to support all of this. The scanner feeds, the classifier, the state machine, the SQLite store, the session context, the book system, the ratchet -- they're all designed for multi-strategy portfolio management. What remains is connecting the pieces and proving each desk one at a time.

### Benchmark Framework

Different desks need different benchmarks. "Beat SPY" is the headline, not the internal metric.

| Desk | Benchmark | How Measured |
|------|-----------|-------------|
| Momentum | Cash + implementation shortfall adjusted expectancy | Net P&L after slippage vs zero |
| Catalyst | Event basket baseline (avg catalyst move) | Did we capture more than the avg FDA/earnings move? |
| Exhaustion | Win rate on fades vs random short timing | Better timing than entering at arbitrary points? |
| Swing | Buy-and-hold of the same names over same period | Did active management add alpha over just holding? |
| Options | Premium collected vs premium lost + hedging cost | Net options P&L after theta/gamma |
| Core Index | SPY / blended ETF benchmark | Tracking error, are we keeping up? |
| Hedging | Portfolio drawdown reduction | Did hedges reduce drawdowns vs unhedged portfolio? |
| Cash Management | T-bill yield / sweep baseline | Is cash earning more than sitting idle? |

### Index Layer Operating Policy

When Desk 6 goes live, these rules govern it:

- **What counts as "profits to sweep":** Realized gains only, after all desk P&L is settled for the day
- **New cash deposits:** Split per target allocation immediately, don't wait for sweep
- **DCA trigger:** Both -- weekly schedule AND threshold-based (cash > $500 unallocated)
- **If Tier 1 is down but new cash arrives:** New cash still gets DCA'd into index. Active losses don't eat index capital.
- **Rebalancing:** Quarterly, sells overweight to buy underweight. Creates taxable events -- track and offset with harvesting.
- **Core holdings NEVER fund active trading losses.** Active desks have their own budget. If they lose it, they get smaller, not funded by index sales.
- **Minimum reserve:** DCA pauses if cash reserve drops below 10% of total equity

### Tax Awareness (Phase 4+)

High-turnover Desk 1 will produce short-term capital gains. The system needs to:
- Track realized gains by holding period (short-term vs long-term)
- Core holdings held >1 year get favorable long-term treatment
- Tax-loss harvesting: swap VOO↔IVV, VTI↔ITOT to realize losses without losing exposure
- Wash sale tracking: don't rebuy within 30 days of a loss harvest
- Year-end report: total tax liability estimate for planning

This is complex and deserves its own implementation phase. Not hand-waved.

### Paper Account Mode vs Production

**Current state:** Everything enabled for data collection. All desks active. No capital constraints. This is correct for paper -- the goal is to learn which desks earn their budget.

**When transitioning to real money:** Every desk that hasn't graduated to at least Pilot status gets turned off. Only desks with proven positive expectancy over 50+ paper trades get real capital. The vision stays the same; the deployment is staged by evidence.

---

**The path:** Prove Desk 1 (momentum) → Graduate Desk 2 (catalyst) → Build Desk 6 (index core) → Graduate Desks 3-5 from data → Layer in Desks 7-8 → Launch competitor bot → Deploy real capital → Velox Capital.

Every desk must earn capital. Not attention. Not excitement. Capital.
