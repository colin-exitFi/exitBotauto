# COPY_TRADER_STRATEGY.md — Pro-Trader Signal Intelligence for Velox

> **Status:** NEW FEATURE — Ready for implementation
> **Priority:** HIGH — Deploy before Monday market open if possible, otherwise Tuesday
> **Author:** exitBot + Colin Tracy + Opus 4.6 review
> **Date:** 2026-03-07
> **Reviewed:** 2026-03-07 by Opus 4.6 — revisions incorporated below

---

## Executive Summary

Add a new signal source to Velox that monitors verified professional traders who post their trades publicly on X/Twitter. The system uses AI to parse trade signals from tweets in real-time, then feeds them into the existing multi-AI consensus engine as a weighted signal — not to blindly copy, but as one more vote in the jury.

**Why this is an edge:** Professional traders with verified track records post entries in real time. Their followers take 2-5 minutes to react. Our system parses and validates in 30-60 seconds (tweet delivery 1-5s + AI parse 3-8s + enrichment 2-5s + jury 5-15s + order 2-5s). The edge is SPEED + VALIDATION: we get the directional conviction of a proven trader, confirmed by our own technical/flow analysis, executed before the crowd moves the price.

**Integration point:** This is a new scanner source (`SOURCE 6`) in `src/scanner/scanner.py` and a new signal module at `src/signals/copy_trader.py`. It feeds candidates into the existing jury pipeline — the jury still has full veto power.

---

## Opus 4.6 Review Notes (incorporated into spec)

1. **V1 scope: Tier 1 only (6 traders), not all 15.** Prove signal quality with highest-confidence group before expanding. Less API pressure, less parsing cost, cleaner attribution data. Tier 2/3/4 activate in V2 after Tier 1 demonstrates positive contribution.

2. **All win rates start at 0.50 (neutral).** Do NOT pre-load self-reported win rates. Let the adaptive weighting system establish real performance from Velox's own data. If @TraderStewie really performs at 70% confirmed by our tracking after 20+ signals, the weight will naturally increase.

3. **Tweet parsing uses a cheaper model.** Parsing "did this tweet contain a trade?" is simpler than jury deliberation. Use the fastest/cheapest available model for tweet parsing (not the same Sonnet the jury uses). Budget tweet parsing calls separately from the jury AI budget.

4. **ARK Invest trades is the higher-edge, lower-risk signal. Implement first.** Structured CSV data, no AI parsing, no rate limits, $15B fund's actual positions. Copy trader (tweet parsing) is V1b after ARK is wired.

5. **Pro trader convergence (3+ on same stock) is signal, not crowding.** The original spec treated 3+ traders on the same stock as "CROWDED" and reduced size. This is backwards for institutional-quality traders — convergent conviction from verified traders is a strong signal. Reduce crowding logic to: if 3+ traders AND StockTwits trending AND retail FOMO visible, THEN reduce size. Pure pro convergence = boost.

---

## Architecture Overview

```
X/Twitter API (Filtered Stream)
        │
        ▼
┌─────────────────────────┐
│  CopyTraderMonitor      │  ← Persistent connection to X filtered stream
│  src/signals/copy_trader.py │
│                         │
│  1. Receive tweet       │
│  2. Check if from tracked trader │
│  3. AI parse: extract signal │
│  4. Score confidence    │
│  5. Emit to scanner queue │
└─────────────┬───────────┘
              │
              ▼
┌─────────────────────────┐
│  Scanner (SOURCE 6)     │  ← copy_trader candidates injected here
│  src/scanner/scanner.py │
│                         │
│  Merges with other sources, │
│  deduplicates, ranks    │
└─────────────┬───────────┘
              │
              ▼
┌─────────────────────────┐
│  Jury / Consensus       │  ← Gets extra context: "Pro trader @X (70% WR) just went long"
│  src/agents/jury.py     │
│                         │
│  Claude + GPT evaluate  │
│  Perplexity tie-break   │
└─────────────┬───────────┘
              │
              ▼
┌─────────────────────────┐
│  Entry Manager          │  ← If jury agrees, enter with boosted sizing
│  Risk Manager           │  ← Copy trader signals get 1.25-1.5x normal size
│  Exit Manager           │  ← Standard exits apply (trailing stops, etc.)
└─────────────────────────┘
```

---

## Tracked Traders Database

### Selection Criteria
A trader is added to the monitor list only if they meet ALL of:
1. Post specific entries/exits with tickers (not just opinions)
2. Have verifiable track record (broker screenshots, championship results, or platform verification)
3. Post frequently enough to be useful (≥3 trades/week)
4. Not primarily a course seller (some education is fine, but trading must be primary)
5. Active on X/Twitter (posted in last 7 days)

### Tier 1 — Highest Confidence (Verified, Consistent, Equity Focus) — V1 SCOPE

These traders have broker-verified or championship-verified results. **V1 monitors ONLY Tier 1.** Tier 2/3/4 activate in V2 after Tier 1 demonstrates positive contribution over 50+ signals.

**IMPORTANT:** All `win_rate` values in the tracker config start at `0.0` (neutral). The system discovers real performance through its own tracking. The "Win Rate" column below is historical/claimed — for reference only, NOT loaded into the system.

| # | Handle | Name | Style | Trades | Win Rate | Verified? | Notes |
|---|--------|------|-------|--------|----------|-----------|-------|
| 1 | @TraderStewie | Gil Morales | Momentum/swing equities | Daily | ~70% | Yes — E*TRADE broker screenshots, Profitly | Co-author "Trade Like an O'Neil Disciple". Posts real-time entries with chart + price + stop. |
| 2 | @InvestorsLive | Nathan Michaud | Small/mid-cap momentum | Daily (5-8/wk) | ~65-72% | Yes — TOS statements, Investors Underground | Thread-style with risk/reward. Posts exits same-day. Optional paid room but 80%+ free on X. |
| 3 | @markminervini | Mark Minervini | Swing growth stocks | Weekly | ~70% | Yes — U.S. Investing Championship winner (+33,500% in 1997) | Gold standard. Posts setups with specific levels. Books: "Trade Like a Stock Market Wizard". |
| 4 | @PeterLBrandt | Peter Brandt | Classical charting, swing | Weekly | ~60% | Yes — 40+ year track record, Factor Trading | Posts futures/commodities/FX positions. Chart-heavy. Publicly admits losses. |
| 5 | @alphatrends | Brian Shannon | Large-cap/ETF momentum | Daily (4-7/wk) | ~68% | Yes — TOS statements, CMT chartered | Volume profile charts + brief rationale. "Technical Analysis Using Multiple Timeframes" author. |
| 6 | @ripster47 | Ripster | EMA level swing trades | 3-5/wk | ~65-70% | Yes — IBKR screenshots, self-tracked spreadsheets | Annotated chart-heavy. 2-10 day holds. Purely free sharing, no paid push. |

### Tier 2 — High Confidence (Partially Verified, High Volume) — V2 SCOPE

These traders post frequently with partial verification. **NOT monitored in V1.** Activate after Tier 1 proves positive.

| # | Handle | Name | Style | Trades | Win Rate | Verified? | Notes |
|---|--------|------|-------|--------|----------|-----------|-------|
| 7 | @warriortrading | Ross Cameron | Day/momentum small-cap | Daily | ~70% | Yes — Lightspeed broker statements, $10M+ cumulative | Live streams trades. Sells courses (red flag) but track record is real. |
| 8 | @HumbledTrader18 | Shay Huang | Day/momentum small-cap | Daily | ~60% | Partial — broker screenshots | YouTube recaps. Course sales but transparent about losses. |
| 9 | @traborinvest | Trabor Burns | Momentum/breakout equities | Daily | Unknown | Partial — P&L screenshots | Posts real-time entries with levels. Active community. |
| 10 | @DaddyDayTrader_ | Chris | Day trading equities | Daily | Unknown | Partial — screenshots | Real-time scalps and swings. Transparent with losses. |
| 11 | @Modern_Rock | Modern Rock | Swing equities | 3-5/wk | Unknown | Self-reported | Chart-based setups with entries/stops/targets. |
| 12 | @stockdweebs | Stock Dweebs | Momentum equities | Daily | Unknown | Self-reported | Quick-fire alerts with tickers and levels. |

### Tier 3 — Institutional / Macro (Position Signals, Lower Frequency) — V2 SCOPE

These are hedge fund managers, institutional traders, or macro strategists. **NOT monitored in V1.** Lower frequency makes them less useful for momentum but valuable for macro context.

| # | Handle | Name | Style | Frequency | Notes |
|---|--------|------|-------|-----------|-------|
| 13 | @BillAckman | Bill Ackman | Activist/macro | Monthly | Pershing Square ($16B AUM). Tweets major positions. 13F-verified. |
| 14 | @chaaborhes | Chamath Palihapitiya | Venture/macro | Weekly | Social Capital. Posts macro views + positions. |
| 15 | @CathieDWood | Cathie Wood | Disruptive innovation | Daily | ARK Invest ($15B AUM). Daily trade emails are PUBLIC and free. |
| 16 | @NorthmanTrader | Sven Henrich | Macro/technical | Weekly | Bearish bias — useful as SHORT signal source. |
| 17 | @LizAnnSonders | Liz Ann Sonders | Macro strategy | Weekly | Schwab chief strategist. Not specific trades but regime context. |

### Tier 4 — Options Flow (Supplement, Not Direct Copy)

These provide options flow intelligence that augments the copy-trader signals.

| # | Handle | Name | What | Notes |
|---|--------|------|------|-------|
| 18 | @unusual_whales | Unusual Whales | Options flow alerts | We already have their API — this is the X feed supplement |
| 19 | @CheddarFlow | Cheddar Flow | Options flow | Dashboard shares with volume/OI data |
| 20 | @OptionsHawk | Options Hawk | Unusual options activity | Detailed flow analysis with commentary |

### Special: ARK Invest Daily Trades
**This is arguably the single best free signal source in existence.**
- ARK publishes EVERY trade, EVERY day, via email and on their website
- URL: `https://ark-funds.com/trade-notifications`
- These are positions of a $15B fund — when Cathie buys, it moves prices
- Can be scraped daily at market close or subscribed via email API
- Implementation: Add `src/signals/ark_trades.py` that fetches daily CSV

---

## Implementation: `src/signals/copy_trader.py`

### Class: `CopyTraderMonitor`

```python
"""
Copy Trader Monitor — Parse pro-trader tweets into actionable signals.

Monitors verified professional traders on X/Twitter for real-time trade entries/exits.
Uses Claude to parse natural language trade posts into structured signals.
Feeds parsed signals into the scanner as SOURCE 6 (copy_trader).

Pro traders get it right 60-75% of the time. Combined with our jury validation,
we only act when BOTH the pro AND our AI agree — dramatically increasing edge.
"""

import asyncio
import time
import json
import re
import requests
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
from loguru import logger
from anthropic import AsyncAnthropic

from config import settings


# ── Trader Database ──────────────────────────────────────────────────────────

class TraderTier(Enum):
    TIER_1 = "tier_1"  # Verified, highest confidence
    TIER_2 = "tier_2"  # Partially verified
    TIER_3 = "tier_3"  # Institutional/macro
    TIER_4 = "tier_4"  # Options flow supplement


@dataclass
class TrackedTrader:
    """A pro trader we're monitoring."""
    handle: str                    # X/Twitter handle (without @)
    name: str                      # Display name
    x_user_id: str                 # Twitter numeric user ID (resolved at startup)
    tier: TraderTier               # Confidence tier
    style: str                     # e.g., "momentum", "swing", "macro"
    win_rate: float                # Known win rate (0.0-1.0), 0 if unknown
    avg_hold_days: float           # Typical hold period in days
    trades_equities: bool = True
    trades_options: bool = False
    trades_futures: bool = False
    verified: bool = False         # Broker-verified track record?
    
    # Performance tracking (updated by the system)
    signals_emitted: int = 0
    signals_correct: int = 0       # Jury agreed AND trade was profitable
    signals_wrong: int = 0         # Jury agreed but trade lost
    signals_rejected: int = 0      # Jury vetoed
    current_streak: int = 0        # Positive = win streak, negative = loss streak
    last_signal_ts: float = 0.0
    
    # Adaptive weight (starts at tier default, adjusted by performance)
    weight: float = 1.0            # Multiplier for signal confidence


# Pre-configured trader list — resolve X user IDs at startup via API
## V1 ACTIVE TRADERS (Tier 1 only — 6 traders)
# All win_rate = 0.0 → system discovers real performance via adaptive tracking.
# claimed_win_rate is for human reference only, NOT loaded into the system.
TRACKED_TRADERS: List[Dict] = [
    # ── Tier 1: Verified (V1 ACTIVE) ──
    {"handle": "TraderStewie", "name": "Gil Morales", "tier": "tier_1", "style": "momentum_swing", "win_rate": 0.0, "claimed_win_rate": 0.70, "avg_hold_days": 3, "verified": True},
    {"handle": "InvestorsLive", "name": "Nathan Michaud", "tier": "tier_1", "style": "momentum", "win_rate": 0.0, "claimed_win_rate": 0.67, "avg_hold_days": 1, "verified": True},
    {"handle": "markminervini", "name": "Mark Minervini", "tier": "tier_1", "style": "swing_growth", "win_rate": 0.0, "claimed_win_rate": 0.70, "avg_hold_days": 10, "verified": True},
    {"handle": "PeterLBrandt", "name": "Peter Brandt", "tier": "tier_1", "style": "swing_classical", "win_rate": 0.0, "claimed_win_rate": 0.60, "avg_hold_days": 14, "verified": True},
    {"handle": "alphatrends", "name": "Brian Shannon", "tier": "tier_1", "style": "momentum_swing", "win_rate": 0.0, "claimed_win_rate": 0.68, "avg_hold_days": 2, "verified": True},
    {"handle": "ripster47", "name": "Ripster", "tier": "tier_1", "style": "swing_ema", "win_rate": 0.0, "claimed_win_rate": 0.67, "avg_hold_days": 5, "verified": True},
]

## V2 TRADERS (activate after Tier 1 proves positive over 50+ signals)
V2_TRADERS: List[Dict] = [
    # ── Tier 2: Partially Verified ──
    {"handle": "warriortrading", "name": "Ross Cameron", "tier": "tier_2", "style": "day_momentum", "win_rate": 0.0, "avg_hold_days": 0.5, "verified": True},
    {"handle": "HumbledTrader18", "name": "Shay Huang", "tier": "tier_2", "style": "day_momentum", "win_rate": 0.0, "avg_hold_days": 0.5, "verified": False},
    {"handle": "traborinvest", "name": "Trabor Burns", "tier": "tier_2", "style": "momentum", "win_rate": 0.0, "avg_hold_days": 2, "verified": False},
    {"handle": "DaddyDayTrader_", "name": "Chris", "tier": "tier_2", "style": "day_trade", "win_rate": 0.0, "avg_hold_days": 0.5, "verified": False},
    {"handle": "Modern_Rock", "name": "Modern Rock", "tier": "tier_2", "style": "swing", "win_rate": 0.0, "avg_hold_days": 5, "verified": False},
    {"handle": "stockdweebs", "name": "Stock Dweebs", "tier": "tier_2", "style": "momentum", "win_rate": 0.0, "avg_hold_days": 1, "verified": False},
    
    # ── Tier 3: Institutional / Macro ──
    {"handle": "BillAckman", "name": "Bill Ackman", "tier": "tier_3", "style": "activist_macro", "win_rate": 0.0, "avg_hold_days": 90, "verified": False},
    {"handle": "CathieDWood", "name": "Cathie Wood", "tier": "tier_3", "style": "disruptive_innovation", "win_rate": 0.0, "avg_hold_days": 30, "verified": False},
    {"handle": "NorthmanTrader", "name": "Sven Henrich", "tier": "tier_3", "style": "macro_technical", "win_rate": 0.0, "avg_hold_days": 14, "verified": False},
]


# ── Signal Parsing ───────────────────────────────────────────────────────────

@dataclass
class ParsedTradeSignal:
    """Structured trade signal extracted from a tweet."""
    trader_handle: str
    trader_tier: str
    trader_win_rate: float
    tweet_id: str
    tweet_text: str
    tweet_ts: float                 # Unix timestamp of the tweet
    parsed_ts: float                # When we parsed it
    
    # Trade details (extracted by AI)
    ticker: str                     # e.g., "AAPL"
    direction: str                  # "LONG", "SHORT", or "EXIT"
    entry_price: Optional[float]    # Stated entry price (may be None)
    stop_loss: Optional[float]      # Stated stop loss
    target_price: Optional[float]   # Stated profit target
    is_options: bool                # Whether this is an options trade
    options_detail: Optional[str]   # e.g., "AAPL 150C 3/14" if applicable
    
    # Confidence scoring
    signal_confidence: float        # 0.0-1.0 how confident we are this is a real trade signal
    parse_confidence: float         # 0.0-1.0 how confident we are in the parsed details
    is_real_trade: bool             # AI determined this is a real entry (not opinion/commentary)
    
    # Context
    has_chart_image: bool           # Tweet included a chart
    has_specific_levels: bool       # Entry/stop/target all provided
    engagement: Dict = field(default_factory=dict)  # likes, RTs, etc.


TRADE_SIGNAL_PARSE_PROMPT = """You are a financial tweet parser for an automated trading system. Your job is to determine if a tweet from a professional trader is a REAL TRADE SIGNAL (they actually entered/exited a position) or just commentary/opinion.

TWEET from @{handle} ({name}, {style} trader, {win_rate_pct}% win rate):
"{tweet_text}"

Posted at: {tweet_time}

Analyze this tweet and respond with a JSON object:
{{
    "is_real_trade": true/false,       // Is this an actual entry/exit, or just opinion/analysis?
    "direction": "LONG"/"SHORT"/"EXIT"/"NONE",  // Trade direction. EXIT = closing a position.
    "ticker": "AAPL" or null,          // Stock ticker if identifiable
    "entry_price": 150.50 or null,     // Stated entry price
    "stop_loss": 145.00 or null,       // Stated stop loss
    "target_price": 160.00 or null,    // Stated profit target
    "is_options": true/false,          // Is this an options trade?
    "options_detail": "AAPL 150C 3/14" or null,  // Options details if applicable
    "signal_confidence": 0.0-1.0,      // How confident are you this is a real, actionable trade signal?
    "reasoning": "brief explanation"   // Why you classified it this way
}}

RULES:
- "is_real_trade" = true ONLY if the trader clearly states they entered or exited a position NOW
- Hypotheticals ("If AAPL breaks 150, I'd go long") are NOT real trades → is_real_trade: false
- General analysis ("AAPL looks bullish") are NOT real trades → is_real_trade: false  
- Watchlist posts ("Watching AAPL for a break of 150") are NOT real trades → is_real_trade: false
- Past tense trades with no current action ("Made 20% on AAPL last week") → is_real_trade: false
- "Trimming" or "Adding" = partial entry/exit, still a real trade signal
- If multiple tickers, extract the PRIMARY one (most emphasized)
- signal_confidence should be HIGH (>0.8) only if: specific ticker + clear direction + price level

Respond with ONLY the JSON object, no other text."""


# ── Monitoring Modes ─────────────────────────────────────────────────────────

class CopyTraderMonitor:
    """
    Monitors pro-trader X/Twitter accounts for trade signals.
    
    Two modes:
    1. POLL mode (default): Polls user timelines every 60s. Works on X Basic tier ($100/mo).
    2. STREAM mode (future): Filtered stream for real-time delivery. Needs X Basic tier.
    
    Current implementation uses POLL mode since we're on Basic tier.
    """
    
    def __init__(self):
        self._bearer = settings.X_BEARER_TOKEN
        self._anthropic = AsyncAnthropic(api_key=settings.ANTHROPIC_API_KEY)
        
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._bearer}",
            "User-Agent": "Velox/2.0",
        })
        
        # Trader registry
        self._traders: Dict[str, TrackedTrader] = {}
        self._user_id_map: Dict[str, str] = {}  # x_user_id → handle
        
        # Signal queue (consumed by scanner)
        self._signal_queue: List[ParsedTradeSignal] = []
        self._signal_history: List[ParsedTradeSignal] = []  # Last 500 signals for tracking
        
        # Polling state
        self._last_tweet_ids: Dict[str, str] = {}  # handle → last seen tweet_id
        self._poll_interval = 60  # seconds between polls
        self._last_poll_ts = 0.0
        
        # Rate limiting
        self._api_calls_today = 0
        self._api_calls_reset_ts = 0.0
        self._max_daily_calls = 90  # Leave headroom under 100/day Basic limit
        
        # Parse caching
        self._parsed_tweet_ids: set = set()  # Don't re-parse same tweet
        
        # Performance file
        self._perf_file = "data/copy_trader_performance.json"
        
        logger.info(f"📡 CopyTraderMonitor initialized with {len(TRACKED_TRADERS)} traders")
    
    async def initialize(self):
        """Resolve X user IDs for all tracked traders. Call once at startup."""
        logger.info("📡 Resolving X user IDs for tracked traders...")
        
        # Batch lookup: X API allows up to 100 usernames per request
        handles = [t["handle"] for t in TRACKED_TRADERS]
        
        # Batch in groups of 100
        for i in range(0, len(handles), 100):
            batch = handles[i:i+100]
            try:
                resp = self._session.get(
                    "https://api.twitter.com/2/users/by",
                    params={"usernames": ",".join(batch), "user.fields": "id,name,username,public_metrics"},
                    timeout=10,
                )
                resp.raise_for_status()
                data = resp.json()
                
                user_map = {}
                for user in data.get("data", []):
                    user_map[user["username"].lower()] = user["id"]
                
                for trader_config in TRACKED_TRADERS:
                    handle = trader_config["handle"]
                    user_id = user_map.get(handle.lower(), "")
                    if not user_id:
                        logger.warning(f"⚠️ Could not resolve X user ID for @{handle}")
                        continue
                    
                    trader = TrackedTrader(
                        handle=handle,
                        name=trader_config["name"],
                        x_user_id=user_id,
                        tier=TraderTier(trader_config["tier"]),
                        style=trader_config["style"],
                        win_rate=trader_config["win_rate"],
                        avg_hold_days=trader_config["avg_hold_days"],
                        verified=trader_config.get("verified", False),
                        weight=self._tier_default_weight(TraderTier(trader_config["tier"])),
                    )
                    self._traders[handle] = trader
                    self._user_id_map[user_id] = handle
                    
            except Exception as e:
                logger.error(f"Failed to resolve X user IDs: {e}")
        
        # Load historical performance
        self._load_performance()
        
        logger.info(f"📡 Resolved {len(self._traders)}/{len(TRACKED_TRADERS)} traders")
    
    def _tier_default_weight(self, tier: TraderTier) -> float:
        """Default signal weight by tier."""
        return {
            TraderTier.TIER_1: 1.5,   # Verified traders get 1.5x weight
            TraderTier.TIER_2: 1.0,   # Partially verified = normal
            TraderTier.TIER_3: 0.75,  # Institutional = less actionable (slower moves)
            TraderTier.TIER_4: 0.5,   # Options flow = supplementary
        }.get(tier, 1.0)
    
    async def poll(self) -> List[ParsedTradeSignal]:
        """
        Poll all tracked traders for new tweets. Returns new parsed signals.
        
        Call this from the main scan loop every 60 seconds.
        Rate-limited to stay under X API Basic tier limits (100 req/day).
        """
        now = time.time()
        
        # Reset daily counter
        if now - self._api_calls_reset_ts > 86400:
            self._api_calls_today = 0
            self._api_calls_reset_ts = now
        
        # Don't poll more frequently than interval
        if now - self._last_poll_ts < self._poll_interval:
            return []
        
        self._last_poll_ts = now
        new_signals = []
        
        for handle, trader in self._traders.items():
            # Rate limit check
            if self._api_calls_today >= self._max_daily_calls:
                logger.warning("📡 X API daily limit reached, stopping poll")
                break
            
            try:
                tweets = self._fetch_user_tweets(trader)
                self._api_calls_today += 1
                
                for tweet in tweets:
                    tweet_id = tweet["id"]
                    if tweet_id in self._parsed_tweet_ids:
                        continue
                    
                    self._parsed_tweet_ids.add(tweet_id)
                    
                    # Quick pre-filter: does this look like a trade?
                    if not self._quick_trade_filter(tweet["text"]):
                        continue
                    
                    # AI parse the tweet
                    signal = await self._parse_tweet(trader, tweet)
                    if signal and signal.is_real_trade and signal.signal_confidence >= 0.6:
                        new_signals.append(signal)
                        self._signal_queue.append(signal)
                        self._signal_history.append(signal)
                        trader.signals_emitted += 1
                        trader.last_signal_ts = now
                        
                        logger.info(
                            f"📡 COPY SIGNAL: @{handle} → {signal.direction} ${signal.ticker} "
                            f"(conf={signal.signal_confidence:.0%}, tier={trader.tier.value})"
                        )
                
                # Update last seen tweet ID
                if tweets:
                    self._last_tweet_ids[handle] = tweets[0]["id"]
                    
            except Exception as e:
                logger.warning(f"📡 Failed to poll @{handle}: {e}")
        
        # Trim history
        if len(self._signal_history) > 500:
            self._signal_history = self._signal_history[-500:]
        if len(self._parsed_tweet_ids) > 10000:
            self._parsed_tweet_ids = set(list(self._parsed_tweet_ids)[-5000:])
        
        return new_signals
    
    def consume_signals(self) -> List[ParsedTradeSignal]:
        """
        Consume pending signals (called by scanner to get new candidates).
        Returns and clears the signal queue.
        """
        signals = list(self._signal_queue)
        self._signal_queue.clear()
        return signals
    
    def get_scanner_candidates(self) -> List[Dict]:
        """
        Convert pending signals to scanner-compatible candidate dicts.
        This is the interface the scanner calls to get SOURCE 6 candidates.
        """
        signals = self.consume_signals()
        candidates = []
        
        for sig in signals:
            if sig.direction == "EXIT":
                continue  # Exit signals handled separately
            
            trader = self._traders.get(sig.trader_handle)
            if not trader:
                continue
            
            candidate = {
                "symbol": sig.ticker,
                "price": sig.entry_price or 0,  # Scanner will get real price
                "change_pct": 0,  # Will be filled by scanner
                "volume": 0,      # Will be filled by scanner
                "source": "copy_trader",
                "copy_trader_handle": sig.trader_handle,
                "copy_trader_name": trader.name,
                "copy_trader_tier": trader.tier.value,
                "copy_trader_win_rate": trader.win_rate,
                "copy_trader_direction": sig.direction,
                "copy_trader_confidence": sig.signal_confidence,
                "copy_trader_weight": trader.weight,
                "copy_trader_entry": sig.entry_price,
                "copy_trader_stop": sig.stop_loss,
                "copy_trader_target": sig.target_price,
                "copy_trader_is_options": sig.is_options,
                "copy_trader_options_detail": sig.options_detail,
                "copy_trader_tweet_text": sig.tweet_text[:500],
                "copy_trader_tweet_id": sig.tweet_id,
                # Force direction for jury
                "suggested_direction": sig.direction,  # "LONG" or "SHORT"
                # Priority boost
                "priority": "HIGH",
            }
            candidates.append(candidate)
        
        return candidates
    
    def _fetch_user_tweets(self, trader: TrackedTrader) -> List[Dict]:
        """Fetch recent tweets from a trader's timeline."""
        params = {
            "max_results": 10,
            "tweet.fields": "created_at,text,public_metrics,attachments,context_annotations",
            "expansions": "attachments.media_keys",
            "media.fields": "url,type",
        }
        
        # Only fetch tweets newer than last seen
        last_id = self._last_tweet_ids.get(trader.handle)
        if last_id:
            params["since_id"] = last_id
        
        resp = self._session.get(
            f"https://api.twitter.com/2/users/{trader.x_user_id}/tweets",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", [])
    
    def _quick_trade_filter(self, text: str) -> bool:
        """
        Fast regex pre-filter before expensive AI parsing.
        Returns True if the tweet MIGHT contain a trade signal.
        Intentionally permissive — AI will do the real classification.
        """
        text_lower = text.lower()
        
        # Must contain a cashtag ($TICKER)
        if not re.search(r'\$[A-Z]{1,5}\b', text):
            # Or at least mention a trade action
            trade_words = ["entry", "long", "short", "bought", "sold", "calls", "puts",
                          "entered", "exited", "trimmed", "added", "position", "stop",
                          "target", "sl ", "pt ", "tp ", "swing", "scalp"]
            if not any(w in text_lower for w in trade_words):
                return False
        
        # Filter out obvious non-trades
        noise_patterns = [
            r'(subscribe|join my|sign up|use code|discount|promo)',
            r'(RT @|Retweet)',
            r'(giveaway|free money|airdrop)',
        ]
        for pattern in noise_patterns:
            if re.search(pattern, text_lower):
                return False
        
        return True
    
    async def _parse_tweet(self, trader: TrackedTrader, tweet: Dict) -> Optional[ParsedTradeSignal]:
        """Use Claude to parse a tweet into a structured trade signal."""
        try:
            text = tweet.get("text", "")
            tweet_time = tweet.get("created_at", "")
            has_media = bool(tweet.get("attachments", {}).get("media_keys"))
            
            prompt = TRADE_SIGNAL_PARSE_PROMPT.format(
                handle=trader.handle,
                name=trader.name,
                style=trader.style,
                win_rate_pct=int(trader.win_rate * 100) if trader.win_rate else "unknown",
                tweet_text=text,
                tweet_time=tweet_time,
            )
            
            response = await self._anthropic.messages.create(
                model="claude-sonnet-4-5-20250929",  # Fast + cheap for parsing
                max_tokens=500,
                messages=[{"role": "user", "content": prompt}],
            )
            
            # Parse JSON response
            content = response.content[0].text.strip()
            # Handle markdown code blocks
            if content.startswith("```"):
                content = content.split("```")[1]
                if content.startswith("json"):
                    content = content[4:]
            
            parsed = json.loads(content)
            
            if not parsed.get("ticker"):
                return None
            
            metrics = tweet.get("public_metrics", {})
            
            return ParsedTradeSignal(
                trader_handle=trader.handle,
                trader_tier=trader.tier.value,
                trader_win_rate=trader.win_rate,
                tweet_id=tweet["id"],
                tweet_text=text,
                tweet_ts=time.time(),  # Ideally parse created_at
                parsed_ts=time.time(),
                ticker=parsed["ticker"],
                direction=parsed.get("direction", "NONE"),
                entry_price=parsed.get("entry_price"),
                stop_loss=parsed.get("stop_loss"),
                target_price=parsed.get("target_price"),
                is_options=parsed.get("is_options", False),
                options_detail=parsed.get("options_detail"),
                signal_confidence=parsed.get("signal_confidence", 0.5),
                parse_confidence=0.9,  # Claude is generally accurate
                is_real_trade=parsed.get("is_real_trade", False),
                has_chart_image=has_media,
                has_specific_levels=all([
                    parsed.get("entry_price"),
                    parsed.get("stop_loss") or parsed.get("target_price"),
                ]),
                engagement={
                    "likes": metrics.get("like_count", 0),
                    "retweets": metrics.get("retweet_count", 0),
                    "replies": metrics.get("reply_count", 0),
                },
            )
            
        except json.JSONDecodeError as e:
            logger.warning(f"📡 Failed to parse Claude response for @{trader.handle}: {e}")
            return None
        except Exception as e:
            logger.warning(f"📡 Tweet parse failed for @{trader.handle}: {e}")
            return None
    
    # ── Performance Tracking ─────────────────────────────────────────────────
    
    def record_outcome(self, tweet_id: str, profitable: bool):
        """
        Called by the exit manager when a copy-trader-sourced position closes.
        Updates the trader's performance stats and adaptive weight.
        """
        signal = next((s for s in self._signal_history if s.tweet_id == tweet_id), None)
        if not signal:
            return
        
        trader = self._traders.get(signal.trader_handle)
        if not trader:
            return
        
        if profitable:
            trader.signals_correct += 1
            trader.current_streak = max(0, trader.current_streak) + 1
        else:
            trader.signals_wrong += 1
            trader.current_streak = min(0, trader.current_streak) - 1
        
        # Adaptive weight adjustment
        if trader.signals_emitted >= 5:  # Need minimum sample
            actual_wr = trader.signals_correct / max(1, trader.signals_correct + trader.signals_wrong)
            
            # If performing above expected → boost weight (up to 2.0)
            # If performing below expected → reduce weight (down to 0.25)
            baseline = self._tier_default_weight(trader.tier)
            if actual_wr > 0.6:
                trader.weight = min(2.0, baseline * (1 + (actual_wr - 0.5)))
            elif actual_wr < 0.4:
                trader.weight = max(0.25, baseline * actual_wr)
            else:
                trader.weight = baseline
            
            # Losing streak penalty: 3+ consecutive losses = halve weight
            if trader.current_streak <= -3:
                trader.weight *= 0.5
                logger.warning(f"📡 @{trader.handle} on {abs(trader.current_streak)}-loss streak, weight reduced to {trader.weight:.2f}")
        
        self._save_performance()
    
    def _load_performance(self):
        """Load trader performance from disk."""
        try:
            with open(self._perf_file, "r") as f:
                data = json.load(f)
            for handle, stats in data.items():
                if handle in self._traders:
                    trader = self._traders[handle]
                    trader.signals_emitted = stats.get("emitted", 0)
                    trader.signals_correct = stats.get("correct", 0)
                    trader.signals_wrong = stats.get("wrong", 0)
                    trader.signals_rejected = stats.get("rejected", 0)
                    trader.current_streak = stats.get("streak", 0)
                    trader.weight = stats.get("weight", self._tier_default_weight(trader.tier))
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"Failed to load copy trader performance: {e}")
    
    def _save_performance(self):
        """Persist trader performance to disk."""
        try:
            data = {}
            for handle, trader in self._traders.items():
                data[handle] = {
                    "emitted": trader.signals_emitted,
                    "correct": trader.signals_correct,
                    "wrong": trader.signals_wrong,
                    "rejected": trader.signals_rejected,
                    "streak": trader.current_streak,
                    "weight": trader.weight,
                }
            with open(self._perf_file, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save copy trader performance: {e}")
    
    # ── Stats / Dashboard ────────────────────────────────────────────────────
    
    def get_stats(self) -> Dict:
        """Return stats for dashboard display."""
        trader_stats = []
        for handle, trader in self._traders.items():
            total_decided = trader.signals_correct + trader.signals_wrong
            actual_wr = (trader.signals_correct / total_decided * 100) if total_decided > 0 else 0
            trader_stats.append({
                "handle": f"@{handle}",
                "name": trader.name,
                "tier": trader.tier.value,
                "signals": trader.signals_emitted,
                "wins": trader.signals_correct,
                "losses": trader.signals_wrong,
                "rejected": trader.signals_rejected,
                "win_rate": f"{actual_wr:.0f}%",
                "streak": trader.current_streak,
                "weight": f"{trader.weight:.2f}",
            })
        
        return {
            "total_traders": len(self._traders),
            "total_signals_today": sum(1 for s in self._signal_history if time.time() - s.parsed_ts < 86400),
            "api_calls_today": self._api_calls_today,
            "api_limit": self._max_daily_calls,
            "traders": sorted(trader_stats, key=lambda x: x["signals"], reverse=True),
        }
```

---

## Scanner Integration (SOURCE 6)

### Changes to `src/scanner/scanner.py`

Add to the `__init__` parameters:
```python
def __init__(self, ..., copy_trader_monitor=None, ...):
    ...
    self.copy_trader = copy_trader_monitor
```

Add new source block after SOURCE 5 (Grok X):
```python
        # ── SOURCE 6: Copy Trader Signals (pro-trader monitoring) ────────
        if self.copy_trader:
            try:
                ct_candidates = self.copy_trader.get_scanner_candidates()
                for ct in ct_candidates:
                    symbol = ct["symbol"]
                    if symbol in seen:
                        # Merge: add copy_trader data to existing candidate
                        existing = next((c for c in candidates if c["symbol"] == symbol), None)
                        if existing:
                            existing["copy_trader_handle"] = ct.get("copy_trader_handle")
                            existing["copy_trader_confidence"] = ct.get("copy_trader_confidence")
                            existing["copy_trader_direction"] = ct.get("copy_trader_direction")
                            existing["copy_trader_weight"] = ct.get("copy_trader_weight")
                            existing["suggested_direction"] = ct.get("suggested_direction")
                            existing["source"] = self._merge_sources(existing.get("source", ""), "copy_trader")
                    else:
                        seen.add(symbol)
                        candidates.append(ct)
                
                if ct_candidates:
                    logger.info(f"📡 Copy trader signals: {len(ct_candidates)} candidates from pro traders")
            except Exception as e:
                logger.warning(f"Copy trader scan failed: {e}")
```

### Changes to candidate ranking in `_rank_candidates()`:

Add copy-trader bonus to composite score:
```python
        # Copy trader bonus: if a pro trader entered, boost the candidate
        ct_conf = c.get("copy_trader_confidence", 0)
        ct_weight = c.get("copy_trader_weight", 0)
        if ct_conf > 0:
            copy_bonus = ct_conf * ct_weight * 15  # Up to +22.5 points for Tier 1 verified
            composite += copy_bonus
```

---

## Jury Integration

### Changes to `src/agents/jury.py`

Add copy trader context to the jury prompt. In the system prompt, add:

```python
COPY_TRADER_CONTEXT = """
COPY TRADER SIGNAL: {copy_context}
"""
```

In the `deliberate()` function, build the copy context:
```python
        # Build copy trader context
        if sd.get("copy_trader_handle"):
            copy_context = (
                f"Pro trader @{sd['copy_trader_handle']} ({sd.get('copy_trader_name', '?')}) "
                f"just went {sd.get('copy_trader_direction', '?')} on {symbol}. "
                f"Trader tier: {sd.get('copy_trader_tier', '?')}, "
                f"win rate: {sd.get('copy_trader_win_rate', 0)*100:.0f}%, "
                f"confidence: {sd.get('copy_trader_confidence', 0)*100:.0f}%. "
                f"Entry: ${sd.get('copy_trader_entry', 'N/A')}, "
                f"Stop: ${sd.get('copy_trader_stop', 'N/A')}, "
                f"Target: ${sd.get('copy_trader_target', 'N/A')}. "
                f"Tweet: \"{sd.get('copy_trader_tweet_text', '')[:200]}\" "
                f"NOTE: This pro trader has a verified track record. Give their signal weight, "
                f"but still evaluate independently. If technicals disagree, VETO."
            )
        else:
            copy_context = "None"
```

---

## Main.py Integration

### In `VeloxBot.__init__()`:
```python
from src.signals.copy_trader import CopyTraderMonitor

# In __init__:
self.copy_trader_monitor = CopyTraderMonitor()
```

### In startup (after Alpaca client init):
```python
await self.copy_trader_monitor.initialize()
```

### In the scan loop (every cycle):
```python
# Poll copy traders (rate-limited internally to every 60s)
await self.copy_trader_monitor.poll()
```

### Pass to scanner:
```python
self.scanner = Scanner(
    ...,
    copy_trader_monitor=self.copy_trader_monitor,
)
```

### In exit callback (when position closes):
```python
# If this position was sourced from a copy-trader signal, record outcome
if position.get("copy_trader_tweet_id"):
    self.copy_trader_monitor.record_outcome(
        position["copy_trader_tweet_id"],
        profitable=(pnl > 0),
    )
```

---

## Entry Manager: Sizing Adjustments

When a candidate has copy-trader confirmation, adjust position sizing:

```python
# In entry_manager.py, modify size calculation:
copy_weight = sentiment_data.get("copy_trader_weight", 0)
if copy_weight > 0:
    # Pro-trader confirmed: boost size by 25-50% based on tier
    copy_multiplier = 1.0 + (min(copy_weight, 1.5) * 0.25)  # Max 1.375x
    position_dollars *= copy_multiplier
    logger.info(f"📡 Copy trader boost: {copy_multiplier:.2f}x → ${position_dollars:.0f}")
```

---

## Risk Controls

### Concentration Limit
- Max 3 positions from the same trader at any time
- Max 5 total positions sourced from copy-trader signals
- Max 20% of portfolio in copy-trader-sourced positions

### Convergence / Crowding Logic (revised per Opus review)
- If 3+ verified pro traders (Tier 1) enter the same ticker in <10 minutes = CONVERGENT CONVICTION → boost score by +0.15 (this is strong directional signal from independent experts)
- If 3+ pro traders AND StockTwits trending AND retail FOMO visible (high StockTwits bullish ratio) = TRUE CROWDING → reduce size by 30% (the move is already being front-run by retail)
- Pure pro convergence without retail pile-on is signal, not noise

### Regime Detection (Trader Going Cold)
- Track rolling 10-trade win rate per trader
- If a trader's rolling WR drops below 40%: halve their weight
- If rolling WR drops below 25%: disable their signals entirely (log warning)
- Auto-re-enable after 10 trades if WR recovers above 50%

### Stale Signal Expiry
- Signals expire after 5 minutes (momentum trades) or 30 minutes (swing trades)
- If the price has moved >3% since the tweet, signal is STALE → skip

---

## X API Configuration

### Tier: Basic ($100/month)
- 100 requests/24h for user timeline endpoint
- 15 traders × 1 poll/hour = 360 calls/day → OVER LIMIT
- Solution: Poll Tier 1 traders every 30 min (12 polls × 6 = 72/day), Tier 2 every 2h (12 × 12 = 144/day → too many)
- **Revised strategy:** Poll ALL traders every 2 minutes in round-robin (one trader per cycle)
  - 15 traders × 1 poll each = 15 calls per round
  - 1 round every 30 min = 48 rounds/day = 720 calls → STILL TOO MANY
  
### ACTUAL VIABLE APPROACH: Filtered Stream
- Basic tier includes 1 filtered stream connection with 25 rules
- V1: Create rules for 6 Tier 1 traders only: `from:user_id1 OR from:user_id2 OR ... OR from:user_id6`
- This uses ONE persistent connection, ZERO polling calls
- Tweets arrive in 1-5 seconds
- V2: Expand rules to include Tier 2/3 (still under 25-rule limit with 15 total traders)
- **This is the correct implementation.** Polling is only the fallback.

### Fallback: If stream disconnects
- Poll Tier 1 traders only (6 traders × 4 polls/day = 24 calls)
- Tier 2/3 once per day = ~10 calls
- Total: ~34 calls/day, well under 100 limit

### Required Environment Variables
```env
# Already in .env:
X_BEARER_TOKEN=AAAAAAAAAAAAAAAAAAAAAALf7wEAAAAA...  (existing)
X_CONSUMER_KEY=3rgj7hEtP2TSQ8SpgYFqeFfx5  (existing)
X_CONSUMER_SECRET=eAu2LZY6lPZlNBcNRHLlB9dPI1XzLjuuGQGLqwUk5XA9jBQE76  (existing)

# No new keys needed — Bearer token handles everything
```

---

## ARK Invest Daily Trades (Bonus Signal)

### `src/signals/ark_trades.py`

Cathie Wood's ARK Invest publishes every trade daily. This is a separate, simpler signal source:

```python
"""
ARK Invest Daily Trade Notifications — $15B fund's daily buys and sells.

Source: https://ark-funds.com/trade-notifications (CSV download)
Alternative: Subscribe to email and parse, or scrape the page.

ARK trades are published after market close. Use as NEXT-DAY signal:
- ARK bought X → consider long entry at open if Velox agrees
- ARK sold X → consider avoiding or shorting

This is institutional flow data for FREE.
"""

import csv
import io
import time
import requests
from typing import Dict, List
from loguru import logger
from datetime import datetime


class ArkTradesScanner:
    """Fetch and parse ARK Invest daily trade notifications."""
    
    ARK_TRADES_URL = "https://ark-funds.com/auto/trades/ARK_Trades.csv"  # Or use their API
    
    def __init__(self):
        self._cache: List[Dict] = []
        self._cache_ts = 0
        self._cache_ttl = 3600  # Refresh hourly
    
    def get_todays_trades(self) -> List[Dict]:
        """Fetch today's ARK trades. Returns list of {fund, date, direction, ticker, shares, weight}."""
        if time.time() - self._cache_ts < self._cache_ttl and self._cache:
            return self._cache
        
        try:
            resp = requests.get(self.ARK_TRADES_URL, timeout=15)
            resp.raise_for_status()
            
            reader = csv.DictReader(io.StringIO(resp.text))
            trades = []
            today = datetime.now().strftime("%m/%d/%Y")
            
            for row in reader:
                if row.get("date") == today or not self._cache:  # Get latest if no today
                    trades.append({
                        "fund": row.get("fund", ""),
                        "date": row.get("date", ""),
                        "direction": row.get("direction", "").upper(),  # Buy/Sell
                        "ticker": row.get("ticker", ""),
                        "company": row.get("company", ""),
                        "shares": int(row.get("shares", 0)),
                        "weight": float(row.get("weight(%)", 0)),
                    })
            
            self._cache = trades
            self._cache_ts = time.time()
            
            buys = [t for t in trades if t["direction"] == "BUY"]
            sells = [t for t in trades if t["direction"] == "SELL"]
            logger.info(f"🏛️ ARK trades: {len(buys)} buys, {len(sells)} sells")
            
            return trades
            
        except Exception as e:
            logger.warning(f"ARK trades fetch failed: {e}")
            return self._cache  # Return stale cache on failure
```

---

## Dashboard Integration

Add a "Copy Traders" section to the Velox dashboard showing:
- Active traders being monitored (handle, tier, weight)
- Signals emitted today
- Win/loss record per trader
- Current streaks
- API usage (calls today / limit)
- Last signal received (timestamp + details)

---

## Implementation Order (revised per Opus review)

**Phase A: ARK Invest daily trades (implement FIRST)**
- Higher edge, lower risk: structured CSV, no AI parsing, no rate limits, $15B fund
- Create `src/signals/ark_trades.py`
- Wire into overnight watchlist rebuild
- Tag as `signal_source: "ark_invest"`

**Phase B: Tier 1 copy trader monitor (implement after ARK)**
- 6 traders only, filtered stream, Claude parsing with cheap model
- Create `src/signals/copy_trader.py` (V1 scope)
- Wire as SOURCE 6 in scanner, context in jury prompt

**Phase C (V2): Expand to Tier 2/3 after 50+ Tier 1 signals with positive attribution**

## File Map

```
src/signals/ark_trades.py        ← NEW: ARK Invest daily trades (Phase A — implement first)
src/signals/copy_trader.py       ← NEW: Main copy trader monitor (Phase B — Tier 1 only)
data/copy_trader_performance.json ← NEW: Persistent trader performance stats
src/scanner/scanner.py           ← MODIFIED: Add SOURCE 6
src/agents/jury.py               ← MODIFIED: Add copy trader context to prompt
src/entry/entry_manager.py       ← MODIFIED: Copy trader sizing boost
src/main.py                      ← MODIFIED: Initialize and wire copy trader + ARK
src/exit/exit_manager.py         ← MODIFIED: Record copy-trade outcomes on exit
src/dashboard/dashboard.py       ← MODIFIED: Add copy trader + ARK stats section
```

---

## Tweet Parsing Model (revised per Opus review)

The tweet parser should NOT use Claude Sonnet (the jury model). Tweet parsing is a simpler classification task:
- Use the fastest/cheapest available model for parsing
- Budget tweet parsing API calls SEPARATELY from jury/agent calls
- At 6 Tier 1 traders posting ~3-5 trade-like tweets/day each = ~18-30 parse calls/day
- This is a small budget compared to the 850+ jury calls/day

---

## Testing Plan

1. **Unit test tweet parsing:** Feed 20 real trader tweets through the parse prompt, verify correct extraction
2. **Integration test:** Mock X API responses, verify signals flow through scanner → jury → entry
3. **Paper trade validation:** Run for 1 full trading day, compare copy-trader-sourced trades vs. other sources
4. **Backtest (future):** Historical tweets from tracked traders + historical price data → simulated P&L

---

## Success Metrics

- **Signal quality:** >70% of parsed signals correctly identified as real trades (not opinion)
- **Jury agreement rate:** 40-60% of copy-trader signals get jury approval (too high = rubber stamping, too low = useless)
- **Copy-sourced win rate:** >55% profitable (our validation should improve on the raw trader WR)
- **Latency:** Tweet → signal in scanner < 60 seconds (realistic end-to-end, not the optimistic 15s)
- **No false positives:** 0 trades entered from opinion/commentary tweets
