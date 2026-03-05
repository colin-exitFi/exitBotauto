# Velox v2 Architecture — The Wealth Engine

## Philosophy
"I want to feel comfortable putting ALL my capital into this."

That means:
1. **No single point of failure** — not one model, not one data source, not one strategy
2. **Multi-AI consensus** — trades only happen when multiple AI models AGREE
3. **Self-improving** — learns from every trade, gets smarter every day
4. **Capital preservation FIRST** — the bot's #1 job is to not lose money. #2 is to make money.

---

## Multi-AI Consensus Engine

### The Problem with Single-Model Trading
Every AI model has blind spots. Claude is great at reasoning but can hallucinate confidence. GPT is creative but can be overoptimistic. Using one model = one set of biases.

### The Solution: Jury System
Like a court of law — no single juror decides. The jury must reach consensus.

```
                    ┌─────────────┐
                    │   SCANNER   │
                    │  (Polygon + │
                    │  StockTwits │
                    │  + Alpaca)  │
                    └──────┬──────┘
                           │ Candidates
                           ▼
              ┌────────────────────────┐
              │   SIGNAL AGGREGATOR    │
              │                        │
              │  Polygon momentum ─────┤
              │  StockTwits trending ──┤
              │  X/Twitter sentiment ──┤
              │  Perplexity news ──────┤
              │  Technical indicators ─┤
              └────────────┬───────────┘
                           │ Enriched candidates
                           ▼
        ┌──────────────────────────────────────┐
        │         AI JURY (Consensus)          │
        │                                      │
        │  ┌──────────┐  ┌──────────┐          │
        │  │ Claude    │  │ GPT 5.2  │          │
        │  │ Sonnet    │  │ (OpenAI) │          │
        │  │           │  │          │          │
        │  │ Analysis: │  │ Analysis:│          │
        │  │ BUY/HOLD  │  │ BUY/HOLD │         │
        │  │ /SKIP     │  │ /SKIP    │         │
        │  │ + conf %  │  │ + conf % │         │
        │  └─────┬─────┘  └────┬─────┘         │
        │        │             │                │
        │        └──────┬──────┘                │
        │               ▼                       │
        │    ┌─────────────────────┐            │
        │    │  CONSENSUS CHECK    │            │
        │    │                     │            │
        │    │  Both BUY + avg     │            │
        │    │  confidence > 70%   │            │
        │    │  → EXECUTE          │            │
        │    │                     │            │
        │    │  One BUY, one SKIP  │            │
        │    │  → TIE-BREAKER:     │            │
        │    │  Perplexity deep    │            │
        │    │  research decides   │            │
        │    │                     │            │
        │    │  Both SKIP          │            │
        │    │  → NO TRADE         │            │
        │    └─────────────────────┘            │
        └──────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────┐
        │         RISK MANAGER (VETO)          │
        │                                      │
        │  Even if jury says BUY:              │
        │  - Portfolio heat too high? → BLOCK  │
        │  - Daily loss limit hit? → BLOCK     │
        │  - Sector overweight? → BLOCK        │
        │  - Losing streak? → REDUCE SIZE      │
        │  - Extended hours? → HALF SIZE       │
        └──────────────────────────────────────┘
                           │
                           ▼
        ┌──────────────────────────────────────┐
        │         EXECUTION ENGINE             │
        │                                      │
        │  Smart order routing:                │
        │  - < $500: market order              │
        │  - > $500: limit at mid, fallback    │
        │  - Extended hours: limit only        │
        │  - ATR-based stop placement          │
        └──────────────────────────────────────┘
```

### Consensus Rules
| Claude | GPT | Perplexity | Action |
|--------|-----|------------|--------|
| BUY    | BUY | -          | **EXECUTE** (avg confidence) |
| BUY    | SKIP| BUY        | **EXECUTE** (reduced size, 75%) |
| BUY    | SKIP| SKIP       | **NO TRADE** |
| SKIP   | BUY | BUY        | **EXECUTE** (reduced size, 75%) |
| SKIP   | BUY | SKIP       | **NO TRADE** |
| SKIP   | SKIP| -          | **NO TRADE** |

### Why This Works
- Claude catches logical inconsistencies in the bull case
- GPT catches pattern recognition from its broader training data
- Perplexity provides REAL-TIME news context neither model has
- When they all agree, that's a HIGH CONVICTION signal
- When they disagree, we stay out — disagreement = uncertainty = risk

---

## The 7 Layers of Intelligence

### Layer 1: Market Scanner (every 30 seconds)
- Polygon: gainers, volume spikes, momentum
- StockTwits: trending tickers, sentiment
- Alpaca: real-time quotes and bars
- Output: ranked candidate list

### Layer 2: Signal Aggregation (per candidate)
- Technical: RSI, VWAP, ATR, volume profile
- Social: StockTwits sentiment, X/Twitter cashtag volume
- News: Perplexity real-time search for each candidate
- Output: enriched candidate with multi-source signals

### Layer 3: AI Jury (per trade decision)
- Claude Sonnet: analysis + recommendation
- GPT 5.2: independent analysis + recommendation
- Consensus required for entry
- Output: BUY/SKIP with confidence and reasoning

### Layer 4: Risk Manager (per trade + portfolio level)
- ATR-based position sizing
- Sector exposure limits
- Portfolio heat tracking
- Three-layer circuit breakers (per-trade, daily, weekly)
- Cash settlement awareness
- Output: approved size or VETO

### Layer 5: Execution Engine
- Smart order routing (market vs limit)
- Extended hours safety
- Partial fill handling
- Slippage monitoring
- Output: filled order

### Layer 6: Position Manager (every 2 minutes)
- Monitor all positions with real-time pricing
- ATR-based trailing stops
- Sentiment degradation detection
- Correlation risk monitoring
- Emergency exit triggers
- Output: hold/exit decisions

### Layer 7: Self-Improvement Loop
- **Observer** (10 min): What's happening in the market?
- **Advisor** (30 min): What should our strategy be?
- **Auto-Tuner** (30 min): What parameters need adjustment?
- **Game Film** (60 min): What worked, what didn't, and why?
- Output: strategy adaptations within hard bounds

---

## Data Sources

| Source | What We Get | Cost |
|--------|-------------|------|
| Alpaca | Trading, quotes, bars, account | Free |
| Polygon | Gainers/losers, snapshots, historical bars, avg volume | Paid (have key) |
| StockTwits | Trending tickers, social sentiment | Free |
| X/Twitter | Cashtag mentions, engagement volume | Paid (have key) |
| Perplexity | Real-time news, company analysis | Paid (have key) |
| Claude Sonnet | Trade analysis, strategy reasoning | Paid (have key) |
| GPT 5.2 | Independent trade analysis | Paid (have key) |
| Coinbase | Crypto spot prices | Free |

**Failover chain for price data:**
1. Alpaca real-time → 2. Polygon snapshot → 3. Coinbase (crypto only)

---

## Capital Preservation Rules (Non-Negotiable)

These rules CANNOT be overridden by any AI layer:

1. **Max 1% risk per trade** — (portfolio * 0.01) / (entry - stop) = max shares
2. **Max 3% daily drawdown** → exit-only mode
3. **Max 5% weekly drawdown** → 50% size reduction
4. **Max 40% in any sector** — diversification enforced
5. **Max positions per tier** — scales from 3 (tiny) to 15 (large)
6. **ATR-based stops on every position** — no position without a stop
7. **Consensus required for entry** — no single-model trades
8. **Extended hours = half size** — always
9. **Chase prevention** — skip if price moved >0.5% since signal
10. **Graceful shutdown** — close all on crash/restart

---

## Performance Targets

| Metric | Target | Rationale |
|--------|--------|-----------|
| Win rate | >55% | Consensus filtering should push above 50% |
| Avg win / Avg loss | >1.5:1 | Let winners run (trailing stops), cut losers fast |
| Max daily drawdown | <3% | Circuit breaker enforced |
| Max weekly drawdown | <5% | Circuit breaker enforced |
| Daily return target | 1-3% | Aggressive but achievable with momentum |
| Sharpe ratio | >2.0 | Risk-adjusted returns matter |
| Time to $10K | ~60 trading days | 2% daily compound from $1K |
| Time to $100K | ~120 trading days | Slower tiers, but compounding |
| Time to $1M | ~200 trading days | PRESERVE tier, very conservative |

---

## Why This Will Work

1. **Zero fees** — Alpaca charges nothing. Every dollar of edge is profit.
2. **Multi-model consensus** — Two AI brains are better than one. Three is a jury.
3. **Battle-tested architecture** — Kalshi bot lessons burned into the code.
4. **Self-improving** — Game film learns from every trade. Strategy evolves.
5. **Risk-first** — Capital preservation is literally coded as non-negotiable.
6. **Extended hours edge** — Most retail bots sleep. Ours catches earnings moves.
7. **Sentiment edge** — StockTwits + Twitter + Perplexity = social alpha.
8. **Dynamic sizing** — Small when conditions are bad, big when conditions are good.
9. **No human emotion** — The bot doesn't panic sell or FOMO buy.
10. **Compounding machine** — 2% daily compounds to $1M in ~200 trading days.

---

## Implementation Priority

### Phase 1: Foundation (DONE ✅)
- Alpaca client, scanner, entry/exit, risk tiers, dashboard, AI layers

### Phase 2: Hardening (IN PROGRESS 🔨)
- ATR stops, circuit breakers, extended hours safety, chase prevention

### Phase 3: Multi-AI Consensus (NEXT)
- Add GPT 5.2 as second opinion
- Perplexity as tie-breaker
- Consensus logic in entry manager

### Phase 4: Paper Trading Validation
- Run 2 weeks on Alpaca paper
- Collect game film data
- Tune parameters based on real results

### Phase 5: Live with $1,000
- Switch to live keys
- Monitor closely first 48 hours
- Full autonomy after validation

### Phase 6: Scale
- Let compounding work
- Reduce human involvement
- Add new strategies as portfolio grows
