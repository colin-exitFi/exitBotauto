# Velox Research Notes

## Proven Algorithmic Trading Strategies (for our momentum/velocity approach)

### 1. Intraday Momentum (Our Primary Strategy)
- Buy stocks showing strong upward momentum (price + volume)
- Technical confirmation: price above VWAP, RSI not overbought
- Take profit at 1-2%, hard stop at -1.5%
- Academic evidence: Jegadeesh & Titman (1993) — momentum effect persists for 3-12 months
- **Intraday version**: momentum in first 30 min of trading predicts rest of day
- **Opening range breakout**: stock breaks above first 15-min high → ride momentum

### 2. News/Sentiment-Driven Momentum
- QuantConnect research: "Using News Sentiment to Predict Price Direction" — 
  analyzes drug manufacturer news, places intraday trades on positive news
- Our edge: AI-powered sentiment analysis on MULTIPLE sources simultaneously
  (X/Twitter, StockTwits, Perplexity news) vs. most bots that use one source
- **Key finding**: sentiment signals are most valuable in the first 30 minutes after news breaks
- After that, the market has priced it in

### 3. Volume-Weighted Momentum
- Volume spike (>200% of average) + price momentum = strongest signal
- High volume validates price movement (it's real, not noise)
- Low volume + price movement = likely to reverse (don't trade it)
- **Our implementation**: Polygon gainers API + StockTwits trending = volume-confirmed momentum

### 4. Anti-Martingale / Streak-Aware Sizing
- Increase position size after wins, decrease after losses
- Mathematical basis: if your strategy has edge, consecutive wins suggest conditions favor you
- Consecutive losses suggest adverse conditions → reduce exposure
- **Kelly Criterion**: optimal bet = (win_prob * payoff - loss_prob) / payoff
- **Half-Kelly** is standard practice (full Kelly is too volatile)

### 5. Mean Reversion (Future Strategy)
- After large moves (+3-5%), stocks tend to partially reverse
- Could be used as exit trigger OR as separate strategy
- RSI overbought (>70) or oversold (<30) signals
- Not our primary strategy but game film may discover this edge

## Key Insights from Open Source Projects

### From TradingGoose (Multi-Agent LLM Trading)
- **Two-layer architecture**: Analysis agents recommend, Portfolio manager decides
- Portfolio manager can OVERRIDE analysis agents based on risk constraints
- Event-driven: news event → analysis → trading signal → risk check → execute
- Uses Perplexity for real-time news analysis (we have this!)

### From trading-buddy (Alpaca Integration)
- **10+ data providers with automatic failover** — critical for reliability
- TWAP/VWAP execution for larger orders (reduces slippage)
- Circuit breakers at multiple levels: per-trade, daily, weekly
- WebSocket streaming for real-time price updates

### From Freqtrade (34K stars, Most Popular)
- **Hyperparameter optimization** via backtesting
- Dry-run → Live pipeline (exactly our paper → live approach)
- Strategy optimization via machine learning
- Telegram alerts for trades (we use Slack)

### From Lumibot (Backtesting Framework)
- Same code for backtesting AND live trading
- **Backtest first, then go live** — validate strategy on historical data

### From Microsoft Qlib (AI Quant Platform)
- RD-Agent: LLM-based autonomous factor discovery
- Our game film analyzer does this — discovers what entry criteria actually work

## Critical Success Factors

### What kills most algo trading bots:
1. **Overfitting** — strategy works on historical data but fails live
2. **Ignoring transaction costs** — even small fees compound
3. **No risk management** — one bad trade wipes out months of gains
4. **Emotional override** — human intervenes and makes it worse
5. **Technology failures** — API goes down, bot crashes, no recovery
6. **Regime changes** — market conditions shift, strategy stops working

### How we address each:
1. **Paper trading first** — prove on live data before real money
2. **Zero commission** — Alpaca has no fees
3. **Dynamic risk tiers** — position sizing scales with account size
4. **Full autonomy** — no human emotion in the loop
5. **Auto-restart wrapper** — bot restarts on crash, data provider failover
6. **Self-improving AI** — game film + auto-tuner adapt to changing conditions

## Strategy Combinations (Future)

### Phase 1 (Current): Pure Momentum
- Volume spikes + price momentum + sentiment confirmation
- 1-4 hour holds, 1-2% targets

### Phase 2 (>$5K): Add Mean Reversion
- After momentum exit, look for reversion trades on the pullback
- Doubles the number of trading opportunities

### Phase 3 (>$25K): Add Sector Rotation
- Track which sectors are in favor (tech, energy, healthcare, etc.)
- Weight positions toward hot sectors
- Use sector ETFs as hedge

### Phase 4 (>$100K): Add Options
- Sell covered calls on positions for income
- Use puts as insurance on large positions
- Options flow as signal source (Unusual Whales API)

## Bulletproof Risk Management Spec

### 1. ATR-Based Dynamic Stops (NOT fixed percentages)
Average True Range measures actual volatility per stock. A volatile stock ($TSLA) needs a wider stop than a stable one ($JNJ).
- **Entry stop**: 1.5x ATR below entry price
- **Trailing stop**: 2x ATR below highest price since entry
- **Why**: Fixed % stops (like -1.5%) get whipsawed on volatile stocks but leave money on table on calm ones
- **ATR calculation**: 14-period ATR on 5-min bars for intraday trades
- **Implementation**: Alpaca supports trailing_stop order type natively! Use `trail_percent` or `trail_price`

### 2. Three-Layer Circuit Breakers
Inspired by NYSE market-wide circuit breakers but for our bot:
- **Per-trade**: Max loss = 1.5x ATR or 2% of position, whichever is tighter
- **Daily**: If cumulative daily P&L drops below -3% of portfolio → exit-only mode for rest of day
- **Weekly**: If cumulative weekly P&L drops below -5% → reduce position sizes by 50% for next week
- **Each layer is independent** — any one triggers protection

### 3. Kelly Criterion Position Sizing
`Kelly% = W - [(1-W) / R]` where W = win rate, R = win/loss ratio
- Use HALF-Kelly (divide by 2) — full Kelly is too aggressive
- Requires ≥30 trades of history to be statistically meaningful
- During paper trading, use fixed 5% positions until we have enough data
- Update Kelly calculation daily from game film data

### 4. The One-Percent Rule (for small accounts)
Never risk more than 1% of portfolio on a single trade.
- $1,000 portfolio → max $10 loss per trade
- This means position size = $10 / (stop distance in $)
- Example: stock at $50, ATR = $1, stop at $48.50 → max 6.6 shares ($10 / $1.50)
- As portfolio grows, this scales naturally

### 5. Correlation Risk
Don't hold 5 tech stocks that all move together — that's really ONE position.
- **Max sector exposure**: 40% of portfolio in any one sector
- **Max correlated positions**: 3 stocks in same industry
- Game film should track correlation between holdings

### 6. Extended Hours Safety Rules (from Alpaca docs)
- Extended hours REQUIRE limit orders (no market orders)
- `time_in_force = "day"` required
- `extended_hours = True` flag
- **Wider stops** in extended hours (2x normal) due to low liquidity
- **Smaller positions** in extended hours (50% of normal size)
- Pre-market 4AM-9:30AM ET, After-hours 4PM-8PM ET, Overnight 8PM-4AM ET

### 7. Order Execution Quality
- **Market hours**: Market orders are fine for small positions (<$500)
- **Larger positions**: Use limit orders at current ask (buys) or bid (sells)
- **Never chase**: If price moves >0.5% from signal → skip entry
- **Partial fills**: If only partially filled after 30 sec → cancel remainder

### 8. Cash Account Settlement (T+1)
- Sold stock cash settles NEXT business day
- Track "settled cash" vs "unsettled cash"
- Don't count unsettled cash as buying power for new positions
- Alpaca API provides `cash`, `buying_power`, `non_marginable_buying_power` — use the right one

## PDT Rule Awareness
- Pattern Day Trader: 4+ day trades in 5 business days on margin account with <$25K
- **Our mitigation**: Start with cash account (no PDT) OR keep under 3 day trades/week
- Alpaca offers both cash and margin accounts
- Cash account: unlimited day trades but must wait for settlement (T+1)
- At $25K+ this restriction lifts entirely

## Extended Hours Opportunities
- Pre-market (4:00-9:30 AM ET): Earnings reactions, overnight news
- After-hours (4:00-8:00 PM ET): Earnings releases, merger announcements
- **Wider spreads** — need to account for higher slippage
- **Lower volume** — position sizes should be smaller
- **Bigger moves** — earnings can move stocks 10-30% in minutes
- Our edge: most retail bots don't trade extended hours
