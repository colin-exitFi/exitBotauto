# 🤖 exitBotauto - Autonomous Stock Trading Bot

> **Apply proven Kalshi strategies to stocks: Zero fees, instant liquidity, real momentum.**

---

## 🎯 Mission

Stack 1-2% wins with **capital velocity**, **sentiment-driven entries**, and **tight risk management**. No overnight holds. No emotional decisions. Just disciplined execution.

---

## 🏗️ Project Status

**Current Phase:** Architecture & Scaffolding ✅

- [x] Architecture documentation (see [ARCHITECTURE.md](./ARCHITECTURE.md))
- [x] Project structure created
- [x] Requirements defined
- [ ] Core modules implementation
- [ ] Paper trading validation
- [ ] Live deployment

---

## 📋 Prerequisites

### Required Accounts & API Keys

1. **SnapTrade Account** (Robinhood integration)
   - Sign up at [snaptrade.com](https://snaptrade.com)
   - Connect your Robinhood account
   - Get Consumer Key + Client ID

2. **Polygon.io** (Market data)
   - Free tier available: [polygon.io](https://polygon.io)
   - Premium recommended for higher rate limits

3. **Twitter/X API** (Sentiment analysis)
   - Apply for developer access: [developer.twitter.com](https://developer.twitter.com)
   - Elevated access required for search

4. **News API** (Breaking news)
   - Free tier: [newsapi.org](https://newsapi.org)

5. **AI APIs** (Optional but recommended)
   - OpenAI: [platform.openai.com](https://platform.openai.com)
   - Anthropic: [console.anthropic.com](https://console.anthropic.com)
   - Perplexity: [docs.perplexity.ai](https://docs.perplexity.ai)

### System Requirements

- **Python:** 3.10 or higher
- **OS:** Linux, macOS, or Windows
- **RAM:** 2GB minimum, 4GB recommended
- **Internet:** Stable connection (low latency preferred)

---

## 🚀 Quick Start

### 1. Clone & Setup

```bash
# Clone the repository
git clone <repo-url>
cd exitBotauto

# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
# Copy example env file
cp .env.example .env

# Edit .env and add your API keys
nano .env  # or use your preferred editor
```

**Required variables:**
- `SNAPTRADE_CLIENT_ID` + `SNAPTRADE_CONSUMER_KEY`
- `POLYGON_API_KEY`
- `X_API_KEY`, `X_API_SECRET`, `X_ACCESS_TOKEN`, `X_ACCESS_SECRET`
- `NEWS_API_KEY`

### 3. Run Paper Trading (Recommended First)

```bash
# Enable paper trading mode in .env
PAPER_TRADING_MODE=true

# Start the bot
python src/main.py
```

### 4. Monitor Dashboard

```bash
# Dashboard runs on http://localhost:8000
# Open in browser to see live status
```

---

## 📁 Project Structure

```
exitBotauto/
├── src/
│   ├── main.py                    # Bot entry point
│   ├── config/
│   │   ├── __init__.py
│   │   └── settings.py            # Configuration loader
│   ├── scanner/
│   │   ├── __init__.py
│   │   └── scanner.py             # Stock scanner (find momentum)
│   ├── sentiment/
│   │   ├── __init__.py
│   │   └── sentiment_analyzer.py  # Twitter + News sentiment
│   ├── entry/
│   │   ├── __init__.py
│   │   └── entry_manager.py       # Entry logic & order execution
│   ├── exit/
│   │   ├── __init__.py
│   │   └── exit_manager.py        # Exit logic (stop-loss, take-profit)
│   ├── risk/
│   │   ├── __init__.py
│   │   └── risk_manager.py        # Risk limits & circuit breakers
│   ├── dashboard/
│   │   ├── __init__.py
│   │   └── dashboard.py           # FastAPI dashboard
│   └── utils/
│       ├── __init__.py
│       ├── snaptrade_client.py    # SnapTrade wrapper
│       ├── polygon_client.py      # Polygon.io wrapper
│       └── logger.py              # Logging utilities
├── data/                          # Historical data & cache
├── logs/                          # Log files
├── config/                        # Config files (JSON)
├── tests/                         # Unit tests
├── .env.example                   # Environment variables template
├── requirements.txt               # Python dependencies
├── ARCHITECTURE.md                # Detailed architecture doc
└── README.md                      # This file
```

---

## 🎮 How It Works

### The Trading Loop

```
┌─────────────────────────────────────────────────────┐
│  1. SCANNER: Find high-momentum stocks              │
│     - Volume spike >200%                            │
│     - Price momentum >2%                            │
│     - Liquid markets (>1M volume)                   │
└──────────────┬──────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│  2. SENTIMENT: Analyze Twitter + News               │
│     - Twitter mentions & engagement                 │
│     - News headlines sentiment                      │
│     - Score: -1.0 (bearish) to +1.0 (bullish)      │
└──────────────┬──────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│  3. ENTRY: Execute if all checks pass               │
│     ✅ Sentiment >0.5                               │
│     ✅ No negative news                             │
│     ✅ Technical confirmation                       │
│     ✅ Risk limits OK                               │
└──────────────┬──────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│  4. MONITOR: Real-time position tracking            │
│     - Check every 5 seconds                         │
│     - Update stop-loss levels                       │
│     - Monitor sentiment shifts                      │
└──────────────┬──────────────────────────────────────┘
               │
               ▼
┌─────────────────────────────────────────────────────┐
│  5. EXIT: Sell when conditions met                  │
│     - Take profit at +1-2%                          │
│     - Stop loss at -1%                              │
│     - Sentiment drop <0.3                           │
│     - Max hold time 4 hours                         │
│     - End of day (3:30pm)                           │
└─────────────────────────────────────────────────────┘
```

### Risk Management

**Hard Limits (Circuit Breakers):**
- ⛔ **Stop loss:** -1% per position
- ⛔ **Daily loss:** -3% max, then stop trading
- ⛔ **Max positions:** 5 concurrent
- ⛔ **Max exposure:** 10% of portfolio
- ⛔ **No overnight holds:** Close all by 3:30pm

**Position Sizing:**
- Each position: 1-2% of account
- Start with $1,000 deployed
- Scale up after proven profitability

---

## 📊 Expected Performance

### Target Metrics

- **Win Rate:** 60-65%
- **Avg Win:** +1.5%
- **Avg Loss:** -1.0%
- **Trades/Day:** 8-12
- **Daily Return:** +3-5%

### Example Day

| Trade | Symbol | Entry | Exit | P&L | Hold Time | Reason |
|-------|--------|-------|------|-----|-----------|--------|
| 1     | AAPL   | $150  | $153 | +2% | 1h 15m    | Take profit |
| 2     | TSLA   | $220  | $218 | -0.9% | 45m      | Stop loss |
| 3     | NVDA   | $480  | $485 | +1% | 2h 30m    | Take profit |
| 4     | AMD    | $120  | $121 | +0.8% | 1h       | Sentiment drop |
| 5     | META   | $300  | $306 | +2% | 3h        | Take profit |

**Result:** 4 wins, 1 loss = **80% win rate, +4.9% daily return**

---

## 🛠️ Development Roadmap

### Phase 1: Core Modules (Current)
- [ ] Scanner implementation
- [ ] Sentiment analyzer
- [ ] SnapTrade integration
- [ ] Entry manager
- [ ] Exit manager
- [ ] Risk manager

### Phase 2: Testing
- [ ] Paper trading for 1 week
- [ ] Validate win rate >60%
- [ ] Verify risk limits working
- [ ] Dashboard functionality

### Phase 3: Live Deployment
- [ ] Start with $100 (minimal risk)
- [ ] Scale to $1,000 after validation
- [ ] Production monitoring
- [ ] Continuous optimization

---

## ⚠️ Risk Disclosure

**This is an experimental trading bot. You can lose money.**

**Risks:**
- Algorithm errors causing unintended trades
- API failures missing exit signals
- Market volatility exceeding stop-loss limits
- Slippage on fast-moving stocks
- Regulatory changes

**Mitigation:**
- Hard stop-loss on every position
- Daily loss circuit breaker
- Real-time monitoring dashboard
- Manual kill switch (pause trading)
- Comprehensive logging

**Start small. Test thoroughly. Never risk more than you can afford to lose.**

---

## 🔧 Configuration

### Key Settings in `.env`

**Risk Management:**
```bash
MAX_POSITION_LOSS_PCT=1.0       # -1% stop loss
TAKE_PROFIT_PCT=2.0             # +2% take profit
MAX_DAILY_LOSS_PCT=3.0          # -3% daily circuit breaker
MAX_CONCURRENT_POSITIONS=5      # Max 5 positions
```

**Capital Allocation:**
```bash
TOTAL_CAPITAL=10000             # Total account size
DEPLOYED_CAPITAL=1000           # Amount actively traded
POSITION_SIZE_PCT=2.0           # 2% per position
```

**Scanner Criteria:**
```bash
MIN_PRICE=5.0                   # Avoid penny stocks
MAX_PRICE=500.0                 # Avoid mega-caps
MIN_VOLUME=1000000              # Must be liquid
VOLUME_SPIKE_MULTIPLIER=2.0     # 200% volume spike
MIN_MOMENTUM_PCT=2.0            # +2% momentum required
```

**Sentiment Thresholds:**
```bash
MIN_ENTRY_SENTIMENT=0.5         # Only enter on positive sentiment
EXIT_SENTIMENT_WARNING=0.3      # Warning level
EXIT_SENTIMENT_CRITICAL=0.0     # Instant exit
```

---

## 📈 Dashboard

Access the live dashboard at `http://localhost:8000`

**Features:**
- 📊 Live positions with real-time P&L
- 🔍 Scanner results (top candidates)
- 📉 Recent trades & performance stats
- ⚠️ Risk status & circuit breakers
- 🎮 Manual controls (pause/resume)

---

## 🐛 Troubleshooting

### Bot won't start
```bash
# Check API keys in .env
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print(os.getenv('SNAPTRADE_CLIENT_ID'))"

# Check Python version (must be 3.10+)
python --version
```

### Orders not executing
- Verify SnapTrade connection: Check dashboard
- Check buying power: Ensure sufficient funds
- Review logs: `tail -f logs/bot.log`

### Sentiment data missing
- Verify Twitter API credentials
- Check rate limits: Twitter free tier has strict limits
- Use fallback mode: Bot can run without sentiment (higher risk)

---

## 🤝 Contributing

This is a personal trading bot. If you want to build your own:
1. Fork the repo
2. Read [ARCHITECTURE.md](./ARCHITECTURE.md) thoroughly
3. Test in paper trading mode first
4. Never trade with money you can't afford to lose

---

## 📝 License

MIT License - Use at your own risk. No warranties provided.

---

## 📞 Support

For questions about the architecture or strategy, see [ARCHITECTURE.md](./ARCHITECTURE.md).

**Remember:**
- Start with paper trading
- Test thoroughly
- Start small
- Monitor constantly
- Be disciplined

**Let's stack wins. 💸**
