# Velox — Completion Checklist

## ✅ DONE — Core Trading Engine (4,791 lines)
- [x] Alpaca client (paper + live, smart orders, fractional shares, extended hours)
- [x] Risk manager (6 tiers, dynamic sizing, circuit breakers daily/weekly, streak detection, heat tracking)
- [x] Scanner (Polygon gainers + StockTwits trending + volume spikes + composite scoring)
- [x] Entry manager (conviction sizing, chase prevention, smart order retry, position loading)
- [x] Exit manager (2-stage take profit, ATR stops, trailing stops, sentiment exit, time exit)
- [x] Multi-AI consensus engine (Claude + GPT jury, Perplexity tie-breaker)
- [x] AI Observer (market assessment every 10 min)
- [x] AI Advisor (strategy recommendations every 30 min)
- [x] AI Tuner (parameter adjustment with kill switch + hard bounds)
- [x] AI Game Film (trade review + pattern learning every 60 min)
- [x] AI Position Manager (veto power, portfolio health, emergency exits)
- [x] Trade history persistence (full metadata logging)
- [x] StockTwits signal integration
- [x] Twitter/X sentiment integration
- [x] Sentiment analyzer (VADER + composite)
- [x] Dashboard (real-time web UI on port 8421)
- [x] Settings/config (env-based, all params tunable)
- [x] Alpaca API keys connected + verified ($1,000 paper)
- [x] Polygon API connected + verified

## 🔨 TODO — Before Paper Trading Launch
- [ ] **End-to-end dry run** — Start bot, verify full scan→consensus→entry→monitor→exit cycle works
- [ ] **Perplexity news integration in scanner** — Scanner enriches candidates with Perplexity headlines
- [ ] **Dashboard: consensus panel** — Show jury votes, agreement rate, API costs
- [ ] **Dashboard: trade history panel** — Show completed trades with P&L
- [ ] **Auto-restart wrapper** — Restart bot on crash (systemd or screen + watchdog)
- [ ] **Logging verification** — Confirm logs write to logs/ directory
- [ ] **Data directory setup** — Ensure data/ directory exists for persistence files

## 🎯 TODO — Before Live Trading
- [ ] **2 weeks paper trading** — Prove system with real market data
- [ ] **Game film analysis** — Review paper trades, tune parameters
- [ ] **Alpaca live keys** — Create live API keys when ready
- [ ] **Capital: switch to live** — ALPACA_PAPER=false, update TOTAL_CAPITAL

## 💡 FUTURE ENHANCEMENTS
- [ ] Options support (covered calls for income)
- [ ] Alpaca WebSocket streaming (real-time vs polling)
- [ ] Backtesting module (test strategies on historical data)
- [ ] exitFi Alpaca Connect listing (OAuth app for users)
- [ ] Mobile alerts (Slack/push notifications for trades)
- [ ] Mean reversion strategy (Phase 2 at $5K)
- [ ] Sector rotation (Phase 3 at $25K)
