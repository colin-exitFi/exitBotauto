# Velox - Autonomous Stock Trading Bot Architecture

## Executive Summary

**Mission:** Apply proven Kalshi trading strategies (capital velocity, sentiment-driven entries, tight stops) to STOCKS where the fundamentals work in our favor:
- ✅ Zero fees (Robinhood via SnapTrade)
- ✅ Start at 0% P&L (not underwater on entry)
- ✅ Momentum is real (runners run)
- ✅ Liquid markets (instant in/out)

**Core Strategy:** Stack 1-2% wins with tight stop losses, rapid capital redeployment, and sentiment-driven entry timing.

---

## Lessons from Kalshi Bot

### What Failed ❌
- **Binary options on crypto:** Can't beat the spread consistently
- **High entry cost:** Already underwater before trade starts
- **Inefficient capital:** Long hold times, can't redeploy

### What Worked ✅
- **Capital velocity:** Fast in/out, stack small wins
- **Sentiment analysis:** Twitter/X + news for timing
- **Tight risk management:** 1-2% stops, hard limits
- **Profit taking discipline:** Exit at target, no greed
- **Autonomous execution:** No emotional decisions

---

## Infrastructure We Can Leverage from exitFi

### 1. **SnapTrade Client & Agent** 📍
**Location:** `exitFi/packages/agents/src/snaptrade/`

**What We Get:**
- **snaptrade-client.ts:** Full SnapTrade SDK wrapper
  - `connectBroker()` - Robinhood authentication
  - `fetchPositions()` - Real-time position data
  - `fetchBalances()` - Account balances
  - `placeOrder()` - Market/limit order execution
  - `cancelOrder()` - Order management
  
- **snaptrade-agent.ts:** AI-powered broker integration agent
  - Position health monitoring
  - Connection status tracking
  - LLM-powered analysis of account state

**How We'll Use It:**
- Direct integration for Robinhood order execution
- Real-time position monitoring
- Autonomous order placement with zero fees

---

### 2. **Polygon Market Data** 📊
**Location:** `exitFi/apps/backend/integrations/polygon-client.ts`

**What We Get:**
- **Real-time stock quotes:** `getStockPrice(symbol)`
- **WebSocket support:** Live price streaming
- **Historical data:** Aggregates, candles, technical indicators
- **Premium API access:** Higher rate limits available

**How We'll Use It:**
- Scanner: Find momentum stocks with volume spikes
- Entry logic: Real-time price confirmation
- Exit logic: Tick-by-tick monitoring for stop-loss/take-profit

---

### 3. **X/Twitter Sentiment API** 🐦
**Location:** `exitFi/apps/backend/integrations/x-api.ts`

**What We Get:**
- **Tweet search:** `searchTweets(query, maxResults)`
- **Financial sentiment:** `getFinancialSentiment(symbols[])`
- **Trending topics:** `getTrends()`
- **Engagement metrics:** Likes, retweets, replies
- **Real-time monitoring:** Track cashtag volume

**Data Pipeline:** `exitFi/apps/backend/data-pipelines/twitter-collector.ts`
- `collectSentiment(symbol, days)` - Aggregate sentiment
- `monitorInfluencers(accounts)` - Track key voices
- `trackFinancialTrends()` - Identify viral stocks

**How We'll Use It:**
- **Entry catalyst:** Detect volume spikes + positive sentiment
- **Early warning:** Sentiment shift = instant exit
- **Hype detection:** Avoid pump-and-dump traps

---

### 4. **News Sentiment Engine** 📰
**Location:** `exitFi/apps/web/src/lib/exit-intelligence/news-sentiment-engine.ts`

**What We Get:**
- **Multi-source aggregation:** Perplexity (real-time) + News API
- **Sentiment scoring:** -100 to +100 scale
- **Exit-relevant detection:** Earnings misses, downgrades, lawsuits
- **Analyst tracking:** Upgrades/downgrades, price targets
- **Urgency classification:** Immediate/this_week/monitor

**How We'll Use It:**
- **Entry filter:** No positions on stocks with negative news
- **Exit trigger:** Breaking negative news = instant sell
- **Pre-earnings caution:** Exit 24h before earnings

---

### 5. **AI Agent Infrastructure** 🤖
**Location:** `exitFi/apps/backend/ai/`

**What We Get:**
- **Swarm Coordinator:** Multi-agent decision-making system
  - `swarm-service.ts` - Orchestrates 5 specialized agents
  - `coordinator.ts` - Meta-reasoning with GPT-4
  
- **Specialized Agents:**
  - Technical Analysis Agent
  - Fundamental Analysis Agent
  - Market Sentiment Agent
  - Risk Management Agent
  - Historical Performance Agent

- **OpenAI Integration:** `anthropic-client.ts` for Claude

**How We'll Use It:**
- **Entry validation:** Multi-agent consensus before trade
- **Exit strategy:** Coordinated decision on when to sell
- **Risk assessment:** Real-time risk scoring

---

### 6. **Available API Keys** (from `.env.example`)

✅ **Trading & Market Data:**
- `SNAPTRADE_CONSUMER_KEY`, `SNAPTRADE_CLIENT_ID`
- `POLYGON_API_KEY`, `POLYGON_PREMIUM_API_KEY`
- `FINNHUB_API_KEY`
- `ALPHA_VANTAGE_API_KEY`

✅ **Sentiment & News:**
- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`
- `TWITTER_BEARER_TOKEN`
- `NEWS_API_KEY`
- `MARKETAUX_API_KEY`

✅ **AI Models:**
- `ANTHROPIC_API_KEY` (Claude for reasoning)
- `OPENAI_API_KEY` (GPT-4 for coordination)
- `PERPLEXITY_API_KEY` (Real-time news)

✅ **Database:**
- `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (optional for logging)

---

## Proposed Bot Architecture (Python)

### Why Python?
- ✅ **Proven stack:** Kalshi bot was Python
- ✅ **Fast iteration:** Quick prototyping
- ✅ **Rich libraries:** pandas, numpy, asyncio
- ✅ **SnapTrade SDK:** Python SDK available
- ✅ **Easy deployment:** Simple systemd service

---

## Core Modules

### 1. **Scanner Module** 🔍
**File:** `src/scanner/scanner.py`

**Purpose:** Find high-momentum stocks with favorable conditions

**Data Sources:**
- Polygon: Price/volume data
- Twitter: Social volume spikes
- News API: Breaking catalysts

**Scan Criteria:**
1. **Price Range:** $5 - $500 (avoid penny stocks and mega-caps)
2. **Volume Spike:** >200% of 20-day average
3. **Momentum:** +2% in last hour
4. **Liquidity:** Average daily volume >1M shares
5. **Sentiment:** Positive Twitter sentiment (>60%)
6. **News:** No negative breaking news

**Output:** Ranked list of 5-10 candidate stocks every 5 minutes

---

### 2. **Sentiment Module** 📊
**File:** `src/sentiment/sentiment_analyzer.py`

**Purpose:** Real-time sentiment scoring for entry/exit decisions

**Inputs:**
- Twitter API: Cashtag mentions, engagement
- News API: Headlines, sentiment keywords
- X influencers: Track key financial accounts

**Sentiment Score:**
```python
sentiment_score = (
    twitter_sentiment * 0.5 +      # 50% weight
    news_sentiment * 0.3 +          # 30% weight
    influencer_sentiment * 0.2      # 20% weight
)
# Range: -1.0 (bearish) to +1.0 (bullish)
```

**Exit Triggers:**
- Sentiment drops below 0.3: Consider exit
- Sentiment drops below 0.0: Immediate exit
- Negative news spike: Instant sell

---

### 3. **Entry Logic Module** 🎯
**File:** `src/entry/entry_manager.py`

**Purpose:** Execute entries with perfect timing

**Entry Checklist:**
1. ✅ Scanner flags stock
2. ✅ Sentiment score >0.5
3. ✅ No negative news
4. ✅ Technical confirmation (price above VWAP)
5. ✅ Volume surge continues
6. ✅ Account has available buying power

**Position Sizing:**
- **Base position:** 1-2% of total portfolio
- **Max concurrent positions:** 5
- **Max capital deployed:** 10% of portfolio

**Order Execution:**
- Use **limit orders** at current ask price
- Cancel if not filled in 30 seconds
- Retry with adjusted price (up to 3 attempts)

---

### 4. **Exit Logic Module** 🚪
**File:** `src/exit/exit_manager.py`

**Purpose:** Lock in profits, cut losses fast

**Exit Scenarios:**

**A. Take Profit (Target: +1-2%)**
- Sell 50% at +1%
- Sell remaining 50% at +2%
- Or trailing stop: -0.5% from peak

**B. Stop Loss (Hard: -1%)**
- No exceptions
- Market order on breach
- Log reason for review

**C. Time-Based Exit**
- Max hold time: 4 hours
- If no movement after 2 hours: exit at break-even

**D. Sentiment-Based Exit**
- Sentiment drops below 0.3: Sell 50%
- Sentiment drops below 0.0: Sell 100%
- Breaking negative news: Instant sell

**E. End-of-Day Exit**
- Close all positions 30 min before market close
- Never hold overnight (avoid gap risk)

---

### 5. **Risk Management Module** ⚠️
**File:** `src/risk/risk_manager.py`

**Purpose:** Enforce discipline, prevent catastrophic loss

**Hard Limits:**
- **Max loss per trade:** -1%
- **Max daily loss:** -3%
- **Max portfolio deployed:** 10%
- **Max concurrent positions:** 5
- **Min liquidity:** 1M avg daily volume

**Circuit Breakers:**
- Daily loss hits -3%: **Stop trading for the day**
- 3 consecutive losses: **Reduce position size by 50%**
- Account drops 10% from peak: **Manual review required**

**Position Monitoring:**
- Check all positions every 5 seconds
- Update stop-loss levels in real-time
- Auto-exit on risk limit breach

---

### 6. **Dashboard Module** 📈
**File:** `src/dashboard/dashboard.py`

**Purpose:** Real-time monitoring and performance tracking

**Features:**
- **Live positions:** Entry price, current P&L, time held
- **Today's stats:** Win rate, total P&L, trades executed
- **Scanner feed:** Top candidates ranked by score
- **Recent exits:** Reason, profit/loss, hold time
- **Risk status:** Current exposure, limits remaining

**Tech Stack:**
- **Backend:** FastAPI for REST API
- **Frontend:** Simple HTML + htmx for real-time updates
- **Visualization:** Plotly for charts

**Endpoints:**
```
GET /status           # Overall bot status
GET /positions        # Current positions
GET /candidates       # Scanner results
GET /history          # Recent trades
GET /metrics          # Performance stats
POST /pause           # Pause trading
POST /resume          # Resume trading
```

---

## System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                         Velox                              │
│                                                                   │
│  ┌──────────────┐      ┌──────────────┐      ┌──────────────┐  │
│  │   Scanner    │─────→│  Sentiment   │─────→│Entry Manager │  │
│  │              │      │  Analyzer    │      │              │  │
│  │ - Polygon    │      │ - Twitter    │      │ - SnapTrade  │  │
│  │ - Twitter    │      │ - News API   │      │ - Order Exec │  │
│  │ - News       │      │ - Perplexity │      │              │  │
│  └──────────────┘      └──────────────┘      └──────┬───────┘  │
│         │                      │                      │          │
│         │                      │                      ▼          │
│         │                      │              ┌──────────────┐  │
│         │                      │              │     Exit     │  │
│         │                      └─────────────→│   Manager    │  │
│         │                                     │              │  │
│         │                                     │ - Stop Loss  │  │
│         │                                     │ - Take Profit│  │
│         │                                     │ - Sentiment  │  │
│         │                                     └──────┬───────┘  │
│         │                                            │          │
│         ▼                                            ▼          │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │              Risk Manager (Circuit Breakers)             │  │
│  │  - Max loss limits   - Position sizing   - Daily limits  │  │
│  └──────────────────────────────────────────────────────────┘  │
│         │                                            │          │
│         ▼                                            ▼          │
│  ┌──────────────┐                            ┌──────────────┐  │
│  │  Dashboard   │                            │   Logging    │  │
│  │  (FastAPI)   │                            │  (JSON/DB)   │  │
│  └──────────────┘                            └──────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              ▲
                              │
                    ┌─────────┴─────────┐
                    │   External APIs    │
                    │                    │
                    │ - SnapTrade        │
                    │ - Polygon.io       │
                    │ - Twitter/X        │
                    │ - News API         │
                    │ - Perplexity       │
                    └────────────────────┘
```

---

## Trading Strategy: Capital Velocity

### The Formula
```
Daily Profit = (Win Rate × Avg Win × Trades/Day) - (Loss Rate × Avg Loss × Trades/Day)

Target: 65% win rate, +1.5% avg win, -1% avg loss, 10 trades/day
Expected Daily Return: (0.65 × 1.5% × 10) - (0.35 × 1% × 10) = 9.75% - 3.5% = 6.25%
On $1,000 capital deployed: $62.50/day
```

### Why This Works on Stocks
1. **Zero fees** = Can trade frequently without bleeding
2. **Liquid markets** = Instant fills, no slippage
3. **Momentum predictability** = Runners continue for 1-4 hours
4. **Sentiment signals** = Twitter gives 5-10 min edge
5. **No overnight risk** = Close everything before 3:30pm

### Capital Deployment Strategy
- Start with **$1,000** deployed (10% of $10K account)
- Each position: **$200** (1-2% of account)
- 5 positions max at once
- If profitable, increase to **$2,000** deployed after 1 week

---

## SnapTrade Integration Plan

### Authentication Flow
1. **User registers** with SnapTrade (one-time)
2. **Connect Robinhood** via SnapTrade OAuth
3. **Store credentials** in `.env` file
4. **Bot authenticates** on startup

### Order Execution
```python
from snaptrade_client import SnapTrade

# Initialize client
snaptrade = SnapTrade(
    client_id=os.getenv("SNAPTRADE_CLIENT_ID"),
    consumer_key=os.getenv("SNAPTRADE_CONSUMER_KEY")
)

# Place order
order = snaptrade.place_order(
    user_id=USER_ID,
    order={
        "symbol": "AAPL",
        "quantity": 10,
        "type": "limit",
        "price": 150.50,
        "side": "buy",
        "account_id": ROBINHOOD_ACCOUNT_ID
    }
)
```

### Real-Time Position Monitoring
```python
# Fetch positions every 5 seconds
positions = snaptrade.fetch_positions(user_id=USER_ID)

for position in positions:
    current_price = polygon.get_stock_price(position.symbol)
    pnl = (current_price - position.entry_price) / position.entry_price
    
    if pnl >= 0.02:  # +2% take profit
        exit_manager.sell(position, reason="take_profit")
    elif pnl <= -0.01:  # -1% stop loss
        exit_manager.sell(position, reason="stop_loss")
```

---

## Deployment Strategy

### Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Set environment variables
cp .env.example .env
# Fill in API keys

# Run bot
python src/main.py
```

### Production Deployment
- **Server:** VPS (DigitalOcean, AWS, etc.)
- **Process manager:** systemd or supervisord
- **Logging:** JSON logs → file + optional Supabase
- **Monitoring:** Dashboard accessible via web UI
- **Alerts:** Telegram/SMS for critical events

### systemd Service
```ini
[Unit]
Description=Velox Trading Bot
After=network.target

[Service]
Type=simple
User=trader
WorkingDirectory=/home/trader/Velox
ExecStart=/usr/bin/python3 /home/trader/Velox/src/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

---

## What We Need from Colin

### 1. **API Keys** (Confirm availability)
- ✅ SnapTrade Consumer Key + Client ID
- ✅ Polygon Premium API Key (or use public)
- ✅ Twitter/X API credentials (full access)
- ✅ News API Key
- ✅ Perplexity API Key
- ✅ OpenAI/Anthropic API Key

### 2. **Robinhood Account**
- ✅ Active Robinhood account
- ✅ Link to SnapTrade (one-time OAuth flow)
- ✅ Minimum $1,000 buying power recommended

### 3. **Infrastructure Decisions**
- **Where to run?** Local machine or cloud VPS?
- **Database?** JSON files, SQLite, or Supabase?
- **Alerts?** Telegram, SMS, or email?

### 4. **Risk Preferences**
- **Starting capital:** How much to deploy? (Recommend $1K)
- **Max daily loss:** Comfortable with -3%?
- **Max positions:** 5 concurrent?
- **Trading hours:** Market open → 3:30pm ET?

---

## Risk Disclosure & Safety

⚠️ **This is an experimental trading bot. Risks include:**
- Algorithm errors leading to unintended trades
- API failures causing missed exits
- Market volatility exceeding stop-loss limits
- Slippage on fast-moving stocks
- Regulatory changes affecting Robinhood/SnapTrade

✅ **Safety Measures:**
- Hard stop-loss on every position (-1%)
- Daily loss limit (-3%)
- Real-time monitoring dashboard
- Manual kill switch (pause trading)
- Comprehensive logging for review

🔍 **Testing Plan:**
1. **Paper trading** for 1 week (simulated)
2. **Live with $100** for 1 week (minimal risk)
3. **Live with $1,000** after validation
4. Scale up only after consistent profitability

---

## Success Metrics

### Week 1 (Paper Trading)
- ✅ Bot runs without crashes
- ✅ Scanner finds 20+ candidates/day
- ✅ Sentiment analysis correlates with price moves
- ✅ Entry/exit logic triggers correctly

### Week 2 (Live $100)
- ✅ Win rate >55%
- ✅ No losses >-1%
- ✅ Average hold time <4 hours
- ✅ Zero overnight positions

### Week 3+ (Live $1,000)
- ✅ Win rate >60%
- ✅ Daily profit >+2%
- ✅ Max drawdown <-5%
- ✅ Sharpe ratio >1.5

---

## Next Steps

### Phase 1: Architecture (Complete) ✅
- [x] Audit exitFi codebase
- [x] Document available infrastructure
- [x] Design bot architecture

### Phase 2: Scaffolding (Now)
- [ ] Create Python project structure
- [ ] Write `requirements.txt`
- [ ] Create `.env.example`
- [ ] Write `README.md`

### Phase 3: Core Development (Next)
- [ ] Scanner module
- [ ] Sentiment analyzer
- [ ] SnapTrade integration
- [ ] Entry manager
- [ ] Exit manager
- [ ] Risk manager

### Phase 4: Testing & Deployment
- [ ] Paper trading validation
- [ ] Live testing ($100)
- [ ] Dashboard development
- [ ] Production deployment

---

## Conclusion

We have **EVERYTHING** we need from exitFi:
- ✅ SnapTrade client for zero-fee Robinhood trading
- ✅ Polygon for real-time market data
- ✅ Twitter API for sentiment analysis
- ✅ News sentiment engine for exit triggers
- ✅ AI agent infrastructure for decision-making
- ✅ All API keys available

The strategy is **proven** (from Kalshi bot). The infrastructure is **ready**. The only thing left is to **build the bot**.

Let's print money. 💸
