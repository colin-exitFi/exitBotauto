# Alpaca API — Comprehensive Reference for exitBot

> Compiled 2026-03-04 from docs.alpaca.markets. This is a practical reference for building our trading bot — not a copy of their docs, but what WE need to know.

---

## Table of Contents

1. [Base URLs & Authentication](#1-base-urls--authentication)
2. [Trading API — Account](#2-trading-api--account)
3. [Trading API — Orders](#3-trading-api--orders)
4. [Order Types Deep Dive](#4-order-types-deep-dive)
5. [Advanced Orders (Bracket, OCO, OTO)](#5-advanced-orders-bracket-oco-oto)
6. [Trailing Stop Orders](#6-trailing-stop-orders)
7. [Time-in-Force (TIF)](#7-time-in-force-tif)
8. [Order Lifecycle & Statuses](#8-order-lifecycle--statuses)
9. [Fractional Trading](#9-fractional-trading)
10. [Extended Hours & Overnight (24/5)](#10-extended-hours--overnight-245)
11. [Positions](#11-positions)
12. [Assets](#12-assets)
13. [Market Data API — Historical](#13-market-data-api--historical)
14. [Market Data API — Real-time WebSocket](#14-market-data-api--real-time-websocket)
15. [Trade Updates WebSocket (Order Streaming)](#15-trade-updates-websocket-order-streaming)
16. [Crypto Trading](#16-crypto-trading)
17. [Paper Trading](#17-paper-trading)
18. [Buying Power & Margin](#18-buying-power--margin)
19. [Pattern Day Trader (PDT) Rules](#19-pattern-day-trader-pdt-rules)
20. [User Protections (DTMC, Wash Trades)](#20-user-protections-dtmc-wash-trades)
21. [Rate Limits](#21-rate-limits)
22. [Error Handling](#22-error-handling)
23. [SDKs & Libraries](#23-sdks--libraries)
24. [Complete API Endpoint Reference](#24-complete-api-endpoint-reference)
25. [Bot-Specific Gotchas & Notes](#25-bot-specific-gotchas--notes)

---

## 1. Base URLs & Authentication

### Base URLs

| Environment | Trading API | Market Data API |
|---|---|---|
| **Paper** | `https://paper-api.alpaca.markets` | `https://data.alpaca.markets` |
| **Live** | `https://api.alpaca.markets` | `https://data.alpaca.markets` |
| **Sandbox Data** | — | `https://data.sandbox.alpaca.markets` |

- All Trading API endpoints use `/v2/` prefix (e.g., `/v2/orders`, `/v2/account`)
- Market Data endpoints use `/v2/stocks/` or `/v1beta3/crypto/`
- Paper and live use the **same** Market Data URL — only trading URL differs

### Authentication Headers

Every request must include:
```
APCA-API-KEY-ID: {your_key_id}
APCA-API-SECRET-KEY: {your_secret_key}
```

Alternative: Basic Auth header `Authorization: Basic base64(KEY:SECRET)`

### Environment Variable Convention
```bash
APCA_API_BASE_URL=https://paper-api.alpaca.markets   # or https://api.alpaca.markets for live
APCA_API_KEY_ID=your_key
APCA_API_SECRET_KEY=your_secret
```

### Request IDs
Every response includes `X-Request-ID` header — save these for debugging/support tickets.

---

## 2. Trading API — Account

### Get Account
```
GET /v2/account
```

**Key Response Fields:**

| Field | Type | What It Means For Us |
|---|---|---|
| `buying_power` | string<number> | How much we can spend RIGHT NOW |
| `cash` | string<number> | Cash balance (can be negative with margin) |
| `equity` | string<number> | cash + long_market_value + short_market_value |
| `portfolio_value` | string<number> | Same as equity |
| `pattern_day_trader` | boolean | **CRITICAL** — if true, different rules apply |
| `daytrade_count` | int | Day trades in last 5 business days |
| `daytrading_buying_power` | string<number> | Only meaningful if PDT flagged |
| `regt_buying_power` | string<number> | Reg T buying power (2x for margin) |
| `non_marginable_buying_power` | string<number> | For crypto/non-marginable assets |
| `multiplier` | string<number> | 1 = cash, 2 = margin, 4 = PDT |
| `trading_blocked` | boolean | Can't trade if true |
| `shorting_enabled` | boolean | Whether short selling is allowed |
| `long_market_value` | string<number> | Total value of long positions |
| `short_market_value` | string<number> | Total value of short positions |
| `last_equity` | string<number> | Previous day's equity at 4pm ET |
| `initial_margin` | string<number> | Reg T initial margin |
| `maintenance_margin` | string<number> | Maintenance margin requirement |
| `crypto_status` | string | ACTIVE if crypto enabled |

**Account Statuses:** ONBOARDING → SUBMITTED → APPROVAL_PENDING → ACTIVE (also: REJECTED, ACCOUNT_UPDATED)

---

## 3. Trading API — Orders

### Create Order
```
POST /v2/orders
Content-Type: application/json
```

**Request Body:**
```json
{
  "symbol": "AAPL",
  "qty": "10",              // OR "notional": "500.00" — NOT both
  "side": "buy",            // buy | sell
  "type": "market",         // market | limit | stop | stop_limit | trailing_stop
  "time_in_force": "day",   // day | gtc | opg | cls | ioc | fok
  "limit_price": "150.00",  // required for limit/stop_limit
  "stop_price": "145.00",   // required for stop/stop_limit
  "extended_hours": false,   // true for extended hours (limit+day only!)
  "client_order_id": "my-unique-id",  // optional, we should set this
  "order_class": "",         // simple | bracket | oco | oto
  "take_profit": {},         // for bracket/oco
  "stop_loss": {},           // for bracket/oco/oto
  "trail_price": "2.00",    // for trailing_stop
  "trail_percent": "1.0"    // for trailing_stop (alternative to trail_price)
}
```

**⚠️ CRITICAL:** `qty` and `notional` are mutually exclusive. Sending both = 400 error.

### Get All Orders
```
GET /v2/orders?status=open&limit=100&direction=desc&nested=true
```
- `status`: open | closed | all (default: open)
- `limit`: max 500
- `after` / `until`: RFC-3339 timestamps
- `direction`: asc | desc
- `nested`: true to include child orders (bracket legs)
- `symbols`: comma-separated filter

### Get Order by ID
```
GET /v2/orders/{order_id}
```

### Get Order by Client Order ID
```
GET /v2/orders:by_client_order_id?client_order_id={id}
```

### Replace (Modify) Order
```
PATCH /v2/orders/{order_id}
```
Body can include: `qty`, `time_in_force`, `limit_price`, `stop_price`, `trail` (for trailing stops)

**Note:** Replacement creates a NEW order (new ID). The old order gets status `replaced`, the new order's `replaces` field points to the old ID.

### Cancel Order
```
DELETE /v2/orders/{order_id}
```

### Cancel ALL Orders
```
DELETE /v2/orders
```

---

## 4. Order Types Deep Dive

### Market Order
- Fills nearly instantly at current NBBO
- Risk of slippage in volatile markets
- Use for: getting in/out quickly when price isn't critical

### Limit Order
- Specify `limit_price` — buy at that price or lower, sell at that price or higher
- May not fill if price never reaches your limit
- **Price precision rules:**
  - Price >= $1.00: max 2 decimal places
  - Price < $1.00: max 4 decimal places
  - Violating this → rejection with code `42210000`

### Stop Order
- Triggers when price hits `stop_price`, then becomes a market order
- Same price precision rules as limit orders
- **⚠️ Sell stop orders are NOT converted to stop-limit — they become market orders**

### Stop-Limit Order
- Triggers at `stop_price`, then becomes a limit order at `limit_price`
- Safer than stop but might not fill if price gaps through
- Requires both `stop_price` AND `limit_price`

### IPO Stocks
- Only limit orders accepted before first trade
- Check asset's `ipo` attribute via Assets API

---

## 5. Advanced Orders (Bracket, OCO, OTO)

### Bracket Order (OTOCO)
Entry + take-profit + stop-loss in one submission:
```json
{
  "side": "buy",
  "symbol": "SPY",
  "type": "market",
  "qty": "100",
  "time_in_force": "gtc",
  "order_class": "bracket",
  "take_profit": { "limit_price": "301" },
  "stop_loss": { "stop_price": "299", "limit_price": "298.5" }
}
```

**Rules:**
- Creates 3 orders: entry + take-profit (limit) + stop-loss (stop or stop-limit)
- Exit legs activate only after entry fills completely
- If one exit fills, the other cancels
- `take_profit.limit_price` must be > `stop_loss.stop_price` for buys (vice versa for sells)
- **No extended hours** — `extended_hours` must be false
- TIF must be `day` or `gtc`
- Partial fill on take-profit adjusts stop-loss quantity
- Use `nested=true` on GET to see legs
- Order replacement (PATCH) supported for price updates

### OCO (One-Cancels-Other)
For existing positions — submit take-profit + stop-loss:
```json
{
  "side": "sell",
  "symbol": "SPY",
  "type": "limit",
  "qty": "100",
  "time_in_force": "gtc",
  "order_class": "oco",
  "take_profit": { "limit_price": "301" },
  "stop_loss": { "stop_price": "299", "limit_price": "298.5" }
}
```
- `type` must be "limit" (this is the take-profit leg type)

### OTO (One-Triggers-Other)
Entry + one exit leg (either take-profit OR stop-loss):
```json
{
  "side": "buy",
  "symbol": "SPY",
  "type": "market",
  "qty": "100",
  "time_in_force": "gtc",
  "order_class": "oto",
  "stop_loss": { "stop_price": "299", "limit_price": "298.5" }
}
```

### Stop-Loss Restrictions on Advanced Orders
The `stop_price` must be at least $0.01 away from the "base price":
- OCO: base = take-profit limit price
- Bracket/OTO with limit entry: base = entry limit price
- Also checked against current market price

---

## 6. Trailing Stop Orders

```json
{
  "side": "sell",
  "symbol": "SPY",
  "type": "trailing_stop",
  "qty": "100",
  "time_in_force": "day",
  "trail_price": "6.15"       // OR "trail_percent": "1.0"
}
```

- Tracks high water mark (HWM) — highest price since submission (for sell)
- Stop price = HWM - trail_price (or HWM × (1 - trail_percent/100))
- **Does NOT trigger outside regular market hours**
- Valid TIF: `day` or `gtc`
- Can update trail via PATCH with `trail` parameter
- Response includes `hwm` and derived `stop_price` fields
- Currently single orders only (not as bracket leg)

---

## 7. Time-in-Force (TIF)

| TIF | Description | Key Notes |
|---|---|---|
| `day` | Valid for current trading day | Default. If submitted after close, queued for next day. With `extended_hours=true`, valid 4am-8pm ET |
| `gtc` | Good til canceled | Auto-canceled after **90 days** (at 4:15pm ET on expiry date). Subject to price adjustments for corporate actions |
| `opg` | Market/Limit on Open | Executes in opening auction only. Rejected if submitted 9:28am-7pm ET |
| `cls` | Market/Limit on Close | Executes in closing auction only. Rejected if submitted 3:50pm-7pm ET |
| `ioc` | Immediate or Cancel | Fill what you can immediately, cancel the rest |
| `fok` | Fill or Kill | Fill entire quantity or cancel — no partial fills |

### TIF × Order Type Compatibility (Regular Hours Equities)

| TIF | Market | Limit | Stop | Stop-Limit |
|---|---|---|---|---|
| day | ✅ | ✅ | ✅ | ✅ |
| gtc | ✅ | ✅ | ✅ | ✅ |
| ioc | ✅* | ✅* | ❌ | ❌ |
| fok | ✅* | ✅* | ❌ | ❌ |
| opg | ✅* | ✅* | ❌ | ❌ |
| cls | ✅* | ✅* | ❌ | ❌ |

\* = Contact sales for some TIF combinations

### Extended Hours TIF
- **Only `day` + `limit` + `extended_hours=true`** — everything else rejected

### Crypto TIF
- **Only `gtc` and `ioc`** — day, opg, cls, fok NOT supported

---

## 8. Order Lifecycle & Statuses

### Common Statuses
| Status | Meaning |
|---|---|
| `new` | Received and routed to exchange |
| `partially_filled` | Some shares filled |
| `filled` | Completely filled — terminal state |
| `done_for_day` | Won't get more fills today |
| `canceled` | Canceled — terminal state |
| `expired` | Time expired — terminal state |
| `replaced` | Replaced by another order |
| `pending_cancel` | Cancel in progress |
| `pending_replace` | Replace in progress (cancel requests rejected while here) |

### Rare Statuses
| Status | Meaning |
|---|---|
| `accepted` | Received but not yet routed (common outside market hours) |
| `pending_new` | Routed but not yet accepted by exchange |
| `rejected` | Rejected — terminal state |
| `suspended` | Not eligible for trading |
| `calculated` | Filled/done but settlement calcs pending |

**⚠️ Can cancel orders until they reach: `filled`, `canceled`, or `expired`**

---

## 9. Fractional Trading

### Key Facts
- Available for 2,000+ US equities (check `fractionable=true` on asset)
- Buy as little as **$1 worth** of shares
- Both `qty` (up to 9 decimal places) and `notional` (dollar amount) supported
- **All Alpaca accounts have fractional trading enabled by default**

### Supported Order Types for Fractional
- Market, limit, stop, stop-limit with `time_in_force=day`
- Extended hours supported with limit orders
- Overnight session supported

### Fractional Gotchas
- ❌ **No short selling** with fractional orders — all fractional sells marked long
- ❌ `qty` and `notional` are mutually exclusive (400 error if both provided)
- Fill price for fractional portion = NBBO at submission time (or whole share fill price if mixed)
- Day trading fractional shares **counts toward PDT day trade count**
- Dividends paid proportionally (e.g., $0.10/share × 0.5 shares = $0.05, rounded to penny)
- Non-fractionable asset → rejection: "requested asset is not fractionable"
- **Outside market hours:** Cannot send 2 sell orders (notional+qty mix) for same security → "unable to open new notional orders while having open closing position orders"

---

## 10. Extended Hours & Overnight (24/5)

### Trading Sessions (All times ET)
| Session | Hours | Days |
|---|---|---|
| **Overnight** | 8:00 PM – 4:00 AM | Sunday–Friday |
| **Pre-market** | 4:00 AM – 9:30 AM | Monday–Friday |
| **Regular** | 9:30 AM – 4:00 PM | Monday–Friday |
| **After-hours** | 4:00 PM – 8:00 PM | Monday–Friday |

### Extended Hours Rules
- **Must set `extended_hours=true`** on the order
- **Only limit orders** accepted
- **Only `time_in_force=day`** accepted
- All other types/TIFs rejected

### Overnight Session (BOATS — Blue Ocean ATS)
- Only limit orders, TIF=day
- Day orders placed overnight carry through to pre-market → regular → after-hours
- Assets eligible: check `overnight_tradable` attribute via Assets API
- `overnight_halted`: check if asset currently halted overnight
- Fractional shares supported overnight
- **DTBP does NOT apply** — max 2x margin buying power overnight
- Trade dates: 8pm-midnight = T+1, midnight-4am = T
- No GTC yet for overnight (planned)
- Sync tradable asset list between 7:45-8:00 PM ET (ideal: 7:55 PM)

### Overnight Market Data
- Feed: `boats` (real data) or `overnight` (15-min delayed, cheaper)
- Stream URL: `wss://stream.data.alpaca.markets/v1beta1/boats` or `/v1beta1/overnight`

---

## 11. Positions

### Get All Open Positions
```
GET /v2/positions
```

### Get Position by Symbol
```
GET /v2/positions/{symbol_or_asset_id}
```

### Close a Position
```
DELETE /v2/positions/{symbol_or_asset_id}
```
Query params:
- `qty`: close specific quantity
- `percentage`: close a percentage (e.g., "50" for 50%)

### Close ALL Positions
```
DELETE /v2/positions?cancel_orders=true
```
- `cancel_orders=true` cancels all open orders before closing

---

## 12. Assets

### Get All Assets
```
GET /v2/assets?status=active&asset_class=us_equity
```
- `asset_class`: `us_equity` | `crypto`
- `exchange`: AMEX, ARCA, BATS, NYSE, NASDAQ, NYSEARCA, OTC

### Get Single Asset
```
GET /v2/assets/{symbol_or_asset_id}
```

### Key Asset Fields
```json
{
  "id": "uuid",
  "symbol": "AAPL",
  "name": "Apple Inc.",
  "status": "active",
  "tradable": true,
  "marginable": true,
  "shortable": true,
  "easy_to_borrow": true,
  "fractionable": true,
  "ipo": false,
  "overnight_tradable": true,
  "overnight_halted": false,
  "min_order_size": "1",         // for crypto
  "min_trade_increment": "1",    // for crypto
  "price_increment": "0.01"      // for crypto
}
```

---

## 13. Market Data API — Historical

### Data Feeds
| Feed | Description | Subscription Needed? |
|---|---|---|
| `iex` | IEX exchange only (~2.5% market volume) | **Free** — no subscription needed |
| `sip` | All US exchanges (100% volume), ultra-low latency | Yes (Algo Trader Plus) |
| `boats` | Blue Ocean ATS overnight data | Yes |
| `overnight` | Alpaca's derived overnight (15-min delayed) | Yes (cheaper than boats) |
| `delayed_sip` | 15-minute delayed SIP | ? |

### Historical Bars (Multi-symbol)
```
GET https://data.alpaca.markets/v2/stocks/bars?symbols=AAPL,MSFT&timeframe=1Day&start=2024-01-01&end=2024-12-31&feed=iex&limit=1000
```
- `timeframe`: 1Min, 5Min, 15Min, 30Min, 1Hour, 4Hour, 1Day, 1Week, 1Month
- `start`/`end`: RFC-3339 timestamps
- `feed`: iex | sip | boats | overnight
- `limit`: max 10000
- `next_page_token`: for pagination (results sorted by symbol, then timestamp)

### Historical Bars (Single symbol)
```
GET https://data.alpaca.markets/v2/stocks/{symbol}/bars?timeframe=1Day&start=...
```

### Latest Bars
```
GET https://data.alpaca.markets/v2/stocks/bars/latest?symbols=AAPL,MSFT&feed=iex
```

### Historical Quotes
```
GET https://data.alpaca.markets/v2/stocks/quotes?symbols=AAPL&start=...&end=...
```

### Latest Quotes
```
GET https://data.alpaca.markets/v2/stocks/quotes/latest?symbols=AAPL&feed=iex
```

### Historical Trades
```
GET https://data.alpaca.markets/v2/stocks/trades?symbols=AAPL&start=...&end=...
```

### Latest Trades
```
GET https://data.alpaca.markets/v2/stocks/trades/latest?symbols=AAPL&feed=iex
```

### Snapshots (Current state: latest trade + quote + minute bar + daily bar + prev daily bar)
```
GET https://data.alpaca.markets/v2/stocks/snapshots?symbols=AAPL,MSFT&feed=iex
```

### Single Symbol Snapshot
```
GET https://data.alpaca.markets/v2/stocks/{symbol}/snapshot?feed=iex
```

### Bar Response Format
```json
{
  "t": "2024-01-02T05:00:00Z",  // timestamp
  "o": 185.5,                    // open
  "h": 186.2,                    // high
  "l": 184.8,                    // low
  "c": 185.9,                    // close
  "v": 1234567,                  // volume
  "n": 5432,                     // number of trades
  "vw": 185.75                   // VWAP
}
```

### Other Market Data Endpoints
- **Historical Auctions:** `GET /v2/stocks/auctions`
- **Condition Codes:** `GET /v2/stocks/meta/conditions/{ticktype}`
- **Exchange Codes:** `GET /v2/stocks/meta/exchanges`
- **Most Active:** `GET /v1beta1/screener/stocks/most-actives`
- **Top Movers:** `GET /v1beta1/screener/stocks/movers`
- **News:** `GET /v1beta1/news?symbols=AAPL&limit=10`
- **Logos:** `GET /v1beta1/logos/{symbol}`
- **Corporate Actions:** `GET /v1/corporate-actions`

---

## 14. Market Data API — Real-time WebSocket

### Stream URLs
| Feed | URL |
|---|---|
| SIP | `wss://stream.data.alpaca.markets/v2/sip` |
| IEX | `wss://stream.data.alpaca.markets/v2/iex` |
| Delayed SIP | `wss://stream.data.alpaca.markets/v2/delayed_sip` |
| BOATS | `wss://stream.data.alpaca.markets/v1beta1/boats` |
| Overnight | `wss://stream.data.alpaca.markets/v1beta1/overnight` |
| Crypto US | `wss://stream.data.alpaca.markets/v1beta3/crypto/us` |
| Test | `wss://stream.data.alpaca.markets/v2/test` (symbol: FAKEPACA) |
| Sandbox | `wss://stream.data.sandbox.alpaca.markets/v2/{feed}` |

### Connection Flow
```
1. Connect → receive: [{"T":"success","msg":"connected"}]
2. Auth (within 10 seconds!):
   {"action":"auth","key":"...","secret":"..."}
   → receive: [{"T":"success","msg":"authenticated"}]
3. Subscribe:
   {"action":"subscribe","trades":["AAPL"],"quotes":["AMD"],"bars":["*"]}
   → receive: [{"T":"subscription",...}]
```

### Auth Alternatives
- Header auth: `APCA-API-KEY-ID` + `APCA-API-SECRET-KEY`
- Basic Auth header
- Message auth (shown above)

### Available Channels
- `trades` — individual trades
- `quotes` — bid/ask quotes (NBBO)
- `bars` — 1-minute aggregated bars
- `dailyBars` — daily bars (updated every minute after open)
- `updatedBars` — corrected bars for late trades (every 30 seconds)
- `statuses` — trading halt/resume messages
- `lulds` — Limit Up/Limit Down bands
- `corrections` — trade corrections (auto-subscribed with trades)
- `cancelErrors` — trade cancellations (auto-subscribed with trades)

Use `"*"` to subscribe to all symbols for a channel.

### Trade Message Format
```json
{"T":"t","S":"AAPL","i":96921,"x":"D","p":126.55,"s":1,"t":"2021-02-22T15:51:44.208Z","c":["@","I"],"z":"C"}
```
Fields: T=type, S=symbol, i=tradeID, x=exchange, p=price, s=size, t=timestamp, c=conditions, z=tape

### Quote Message Format
```json
{"T":"q","S":"AMD","bx":"U","bp":87.66,"bs":1,"ax":"Q","ap":87.68,"as":4,"t":"...","c":["R"],"z":"C"}
```
Fields: bp/bs=bid price/size, ap/as=ask price/size, bx/ax=bid/ask exchange

### Bar Message Format
```json
{"T":"b","S":"SPY","o":388.985,"h":389.13,"l":388.975,"c":389.12,"v":49378,"n":461,"vw":389.062,"t":"..."}
```

### ⚠️ Connection Limits
- **Most subscriptions (including Algo Trader Plus): 1 connection per endpoint**
- Second connection → error 406 "connection limit exceeded"
- Slow clients → error 407 and disconnection

### Compression
- Supports RFC-7692 (permessage-deflate) — SDKs handle this
- Also supports MessagePack: `Content-Type: application/msgpack`

### WebSocket Error Codes
| Code | Message | Meaning |
|---|---|---|
| 400 | invalid syntax | Bad message format or invalid symbol |
| 401 | not authenticated | Subscribe before auth |
| 402 | auth failed | Bad credentials |
| 403 | already authenticated | Double auth |
| 404 | auth timeout | Took too long to auth |
| 405 | symbol limit exceeded | Too many symbols |
| 406 | connection limit exceeded | Too many connections |
| 407 | slow client | Can't keep up |
| 409 | insufficient subscription | Feed not in your plan |
| 500 | internal error | Their bug |

---

## 15. Trade Updates WebSocket (Order Streaming)

### Connection
```
wss://paper-api.alpaca.markets/stream    # paper
wss://api.alpaca.markets/stream          # live
```

**⚠️ Uses binary frames** (not text like market data stream)

### Auth & Subscribe
```json
{"action":"auth","key":"...","secret":"..."}
// → {"stream":"authorization","data":{"status":"authorized","action":"authenticate"}}

{"action":"listen","data":{"streams":["trade_updates"]}}
// → {"stream":"listening","data":{"streams":["trade_updates"]}}
```

### Trade Update Events

**Common Events:**
| Event | When | Extra Fields |
|---|---|---|
| `new` | Order routed to exchange | — |
| `fill` | Completely filled | `timestamp`, `price`, `qty`, `position_qty` |
| `partial_fill` | Partially filled | `timestamp`, `price`, `qty`, `position_qty` |
| `canceled` | Canceled | `timestamp` |
| `expired` | TIF expired | `timestamp` |
| `done_for_day` | No more fills today | — |
| `replaced` | Order replaced | `timestamp` |

**Rare Events:** `accepted`, `rejected`, `pending_new`, `stopped`, `pending_cancel`, `pending_replace`, `calculated`, `suspended`, `order_replace_rejected`, `order_cancel_rejected`

### Example Fill Event
```json
{
  "stream": "trade_updates",
  "data": {
    "event": "fill",
    "execution_id": "uuid",
    "order": { /* full order object */ },
    "position_qty": "100",
    "price": "150.25",
    "qty": "100",
    "timestamp": "2024-01-15T15:30:00Z"
  }
}
```

**⚠️ `price` is per-fill price (may differ from order's `filled_avg_price` if partial fills)**

---

## 16. Crypto Trading

### Key Differences from Stocks
- Symbol format: `BTC/USD`, `ETH/BTC` (legacy `BTCUSD` still works)
- **24/7 trading** — orders execute any time
- **Cannot use margin** — evaluated against `non_marginable_buying_power`
- **Cannot short sell**
- **Max order size: $200k notional per order**
- Asset class: `crypto`

### Supported Order Types (Crypto)
- Market, Limit, Stop-Limit
- **Stop (market) NOT supported for crypto**
- TIF: `gtc` and `ioc` only

### Crypto Fees (Volume-tiered)
| 30D Volume | Maker | Taker |
|---|---|---|
| $0-100K | 15 bps | 25 bps |
| $100K-500K | 12 bps | 22 bps |
| $500K-1M | 10 bps | 20 bps |
| $1M-10M | 8 bps | 18 bps |
| $10M-25M | 5 bps | 15 bps |
| $25M+ | 0-2 bps | 10-13 bps |

Fees charged on the received asset. Posted end-of-day (not real-time yet).

### Crypto Market Data
```
GET https://data.alpaca.markets/v1beta3/crypto/us/latest/orderbooks?symbols=BTC/USD,ETH/USD
GET https://data.alpaca.markets/v1beta3/crypto/us/bars?symbols=BTC/USD&timeframe=1Hour
```

### Crypto WebSocket
```
wss://stream.data.alpaca.markets/v1beta3/crypto/us
```
Channels: trades, quotes, bars, updatedBars, dailyBars, orderbooks

### Crypto & PDT
- **PDT rules do NOT apply to crypto**
- Crypto day trades do NOT count toward day trade count
- Crypto NOT eligible as margin collateral

---

## 17. Paper Trading

### Key Facts
- **Free for all users** (paper-only accounts available globally)
- Base URL: `https://paper-api.alpaca.markets`
- **Different API keys** from live account
- Default balance: **$100,000** (configurable on reset)
- Same API spec as live — just different base URL + keys
- IEX data only for paper-only accounts

### How Paper Fills Work
- Orders fill when marketable against real-time NBBO
- **Order quantity NOT checked against actual NBBO size** — you can fill huge orders
- **10% of the time: random partial fills** — remainder re-evaluated
- Non-marketable limit orders wait until price reaches limit
- Market data API works identically to live

### What Paper Does NOT Simulate
- ❌ Market impact of your orders
- ❌ Information leakage
- ❌ Price slippage from latency
- ❌ Order queue position (for non-marketable limits)
- ❌ Price improvement
- ❌ Regulatory fees
- ❌ Dividends
- ❌ Borrow fees (coming soon)
- ❌ Order fill emails

### What Paper DOES Simulate
- ✅ PDT checks (will reject 4th day trade if equity < $25K)
- ✅ DTMC protection
- ✅ Wash trade protection
- ✅ Margin trading & short selling
- ✅ Pre-market / after-hours
- ✅ MFA

### Managing Paper Accounts
- Create new: Dashboard → paper account dropdown → "Open New Paper Account"
- Delete: Dashboard → Account Settings → Delete Account
- **Generate new API keys for each new paper account**
- Cannot change balance after creation without resetting

---

## 18. Buying Power & Margin

### Account Types
| Multiplier | Type | Buying Power |
|---|---|---|
| 1 | Cash / Limited Margin | buying_power = cash |
| 2 | Reg T Margin (default for equity ≥ $2K) | buying_power = max(equity - initial_margin, 0) × 2 |
| 4 | PDT (pattern day trader) | daytrading BP = (last_equity - last_maintenance_margin) × 4, overnight BP = 2× |

### Buying Power Checks on Order Entry
| When Submitted | Price Used |
|---|---|
| Core session open | Far side of NBBO |
| Extended hours open | Midpoint of inside market |
| All sessions closed | Latest trade from cache |

### Key Rules
- Open (unfilled) orders **reduce** your available buying power
- Sell/cover orders do NOT restore buying power until executed
- Short sell value = MAX(limit price, ask + 3%) × quantity
- Crypto: uses `non_marginable_buying_power` only — no margin
- Accounts ≥ $2,000 equity get margin (2× multiplier)

---

## 19. Pattern Day Trader (PDT) Rules

### Definition
- **4+ day trades within 5 business days**, AND those day trades represent >6% of total trades in that window
- A day trade = buy + sell (or short + cover) same security on same calendar day
- Multiple trades count as ONE day trade if they close the same position opened that day

### PDT Protection (Alpaca Implementation)
- Alpaca **pre-checks** every order submission
- If order would trigger 4th day trade AND equity < $25,000 → **order rejected (HTTP 403)**
- Pending (unfilled) buy + sell pair for same security counts as potential day trade
- Check `daytrade_count` on account to see current count

### If Flagged as PDT
- Need $25,000+ equity to continue day trading
- Account restricted from new day trades until:
  - PDT restriction lift granted, OR
  - One-time PDT removal (lifetime, one per account), OR
  - Equity ≥ $25,000 by end of trading day
- After restriction lift + day trade → **90-day liquidation-only** (or meet $25K)

### ⚠️ Paper Trading Simulates PDT
- Test with realistic balance to catch PDT issues before going live

### Crypto & PDT
- **Crypto exempt** — crypto trades don't count toward day trade count

---

## 20. User Protections (DTMC, Wash Trades)

### Day Trade Margin Call (DTMC) Protection
Only applies to PDT accounts (multiplier=4):
- Day trading BP = 4 × (last_equity - last_maintenance_margin) at start of day
- Entering positions reduces DTBP; exiting same-day restores it
- DTBP **cannot increase** beyond start-of-day value (closing overnight positions doesn't add DTBP)
- If max day trade exposure exceeds starting DTBP → DTMC issued next day

**Two protection modes (one must be active):**
1. **Entry protection (default):** Blocks orders that would exceed DTBP at entry
2. **Exit protection:** Blocks exits that would cause DTMC (may prevent liquidation until next day)

### Wash Trade Prevention
- Alpaca rejects orders that could interact with your own existing orders (HTTP 403)
- Market buy + any sell for same security = always rejected
- Limit buy + limit sell where buy price ≥ sell price = rejected
- **Exceptions:** Bracket orders, OCO orders, trailing stops

### Concentrated Position Protection
- Account restricted to closing-only if position > 600% of equity

---

## 21. Rate Limits

Alpaca's rate limit documentation has moved/is sparse, but known limits:

| API | Rate Limit |
|---|---|
| **Trading API** | ~200 requests/minute per account (documented in older versions) |
| **Market Data REST** | Varies by subscription. Free tier: ~200/min. Paid: higher |
| **WebSocket connections** | Usually **1 per stream endpoint** per account |
| **Order submissions** | No explicit published limit beyond the API rate limit |

**Best practices for our bot:**
- Use WebSocket streaming for real-time data instead of polling REST
- Use trade_updates WebSocket for order status instead of polling GET /orders
- Batch symbol requests where possible (multi-symbol endpoints)
- Implement exponential backoff on 429 responses
- Cache asset info (doesn't change often)

---

## 22. Error Handling

### HTTP Status Codes
| Code | Meaning |
|---|---|
| 200 | Success |
| 207 | Multi-status (some succeeded, some failed) |
| 400 | Bad request / validation error |
| 401 | Unauthorized (bad API keys) |
| 403 | Forbidden (PDT, wash trade, insufficient buying power) |
| 404 | Not found |
| 422 | Unprocessable (e.g., invalid order params) |
| 429 | Rate limited |
| 500 | Internal server error |

### Common Order Rejection Errors
| Error | Cause | Fix |
|---|---|---|
| `insufficient buying power` | Not enough cash/margin | Check buying_power before ordering |
| `pattern day trader` (403) | Would trigger PDT | Check daytrade_count, ensure equity > $25K |
| `wash trade` (403) | Order would interact with existing order | Use bracket/OCO instead |
| `invalid limit_price ... sub-penny` (42210000) | Too many decimals | Round to 2 decimals (≥$1) or 4 (<$1) |
| `requested asset is not fractionable` | Fractional order on non-fractional asset | Check `fractionable` field |
| `unable to open new notional orders...` | Conflicting fractional sells outside hours | Avoid mixed notional/qty sells |

---

## 23. SDKs & Libraries

### Official (Alpaca-maintained)
| Language | Package | Install |
|---|---|---|
| **Python** | `alpaca-py` | `pip install alpaca-py` |
| .NET/C# | `Alpaca.Markets` | NuGet |
| Node.js | `@alpacahq/alpaca-trade-api` | npm |
| Go | `alpaca-trade-api-go` | GitHub |
| Python (legacy) | `alpaca-trade-api` | `pip install alpaca-trade-api` |

### alpaca-py (Recommended for Our Bot)
- Modern Python SDK covering Trading, Market Data, and Broker APIs
- Handles WebSocket streaming, auth, pagination
- Docs: https://alpaca.markets/sdks/python/
- PyPI: https://pypi.org/project/alpaca-py/

### Community SDKs
Java, Rust, Ruby, Elixir, Scala, R, C++, TypeScript — see docs for links.

---

## 24. Complete API Endpoint Reference

### Trading API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/v2/account` | Get account info |
| PATCH | `/v2/account/configurations` | Update account config |
| GET | `/v2/account/configurations` | Get account config |
| GET | `/v2/account/activities` | Get account activities |
| GET | `/v2/account/activities/{type}` | Activities by type |
| GET | `/v2/account/portfolio/history` | Portfolio history |
| POST | `/v2/orders` | Create order |
| GET | `/v2/orders` | List orders |
| DELETE | `/v2/orders` | Cancel all orders |
| GET | `/v2/orders/{id}` | Get order by ID |
| PATCH | `/v2/orders/{id}` | Replace order |
| DELETE | `/v2/orders/{id}` | Cancel order |
| GET | `/v2/orders:by_client_order_id` | Get by client ID |
| GET | `/v2/positions` | List positions |
| DELETE | `/v2/positions` | Close all positions |
| GET | `/v2/positions/{symbol}` | Get position |
| DELETE | `/v2/positions/{symbol}` | Close position |
| GET | `/v2/assets` | List assets |
| GET | `/v2/assets/{symbol}` | Get asset |
| GET | `/v2/calendar` | Market calendar |
| GET | `/v2/clock` | Market clock |
| GET | `/v2/watchlists` | List watchlists |
| POST | `/v2/watchlists` | Create watchlist |
| GET/PUT/DELETE | `/v2/watchlists/{id}` | CRUD watchlist |
| GET | `/v2/corporate_actions/announcements` | Corp actions |

### Market Data Endpoints (Stock)

| Method | Endpoint | Description |
|---|---|---|
| GET | `/v2/stocks/bars` | Historical bars (multi) |
| GET | `/v2/stocks/{symbol}/bars` | Historical bars (single) |
| GET | `/v2/stocks/bars/latest` | Latest bars |
| GET | `/v2/stocks/{symbol}/bars/latest` | Latest bar (single) |
| GET | `/v2/stocks/quotes` | Historical quotes (multi) |
| GET | `/v2/stocks/{symbol}/quotes` | Historical quotes (single) |
| GET | `/v2/stocks/quotes/latest` | Latest quotes |
| GET | `/v2/stocks/{symbol}/quotes/latest` | Latest quote (single) |
| GET | `/v2/stocks/trades` | Historical trades (multi) |
| GET | `/v2/stocks/{symbol}/trades` | Historical trades (single) |
| GET | `/v2/stocks/trades/latest` | Latest trades |
| GET | `/v2/stocks/snapshots` | Snapshots (multi) |
| GET | `/v2/stocks/{symbol}/snapshot` | Snapshot (single) |
| GET | `/v2/stocks/auctions` | Historical auctions |

### Market Data Endpoints (Crypto)

| Method | Endpoint | Description |
|---|---|---|
| GET | `/v1beta3/crypto/us/bars` | Historical bars |
| GET | `/v1beta3/crypto/us/latest/bars` | Latest bars |
| GET | `/v1beta3/crypto/us/quotes` | Historical quotes |
| GET | `/v1beta3/crypto/us/latest/quotes` | Latest quotes |
| GET | `/v1beta3/crypto/us/trades` | Historical trades |
| GET | `/v1beta3/crypto/us/latest/trades` | Latest trades |
| GET | `/v1beta3/crypto/us/snapshots` | Snapshots |
| GET | `/v1beta3/crypto/us/latest/orderbooks` | Latest orderbook |

### Other Data Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/v1beta1/screener/stocks/most-actives` | Most active stocks |
| GET | `/v1beta1/screener/stocks/movers` | Top movers |
| GET | `/v1beta1/news` | News articles |
| GET | `/v1beta1/logos/{symbol}` | Company logos |
| GET | `/v1/corporate-actions` | Corporate actions |

---

## 25. Bot-Specific Gotchas & Notes

### Things That Will Bite Us

1. **Paper fills are optimistic.** Orders fill against NBBO with no size check. A $1M market order fills instantly in paper but would move the market in live. Test with realistic sizes.

2. **PDT in paper mode.** Paper simulates PDT! If we're testing with <$25K balance and making day trades, orders WILL get rejected on the 4th.

3. **Extended hours = limit + day ONLY.** Forget to set both? Rejected. Set market order with extended_hours=true? Rejected.

4. **GTC orders expire after 90 days.** If we set a GTC limit order and forget about it, it auto-cancels at day 90 (4:15pm ET).

5. **Order replacement creates new order.** PATCH doesn't modify in-place — it creates a new order with a new ID. The old one shows as `replaced`.

6. **WebSocket: 1 connection per endpoint.** Can't have two processes listening to the same stream. Use a single connection and fan out internally.

7. **10-second auth timeout on WebSocket.** Must send auth message within 10 seconds of connecting.

8. **Fractional + notional are mutually exclusive.** Sending both = instant 400.

9. **Crypto TIF: only gtc and ioc.** No day orders for crypto!

10. **Wash trade protection blocks legitimate strategies.** Can't have opposing limit orders — use bracket/OCO instead.

11. **Price precision matters.** ≥$1.00 → 2 decimals max. <$1.00 → 4 decimals max. Violate this → rejection.

12. **Buying power includes open orders.** An unfilled limit order still locks up buying power.

13. **Trailing stops don't work in extended hours.** They track prices during regular session only.

14. **Overnight trade dates are tricky.** 8pm-midnight = next day's date. Midnight-4am = current day. Affects PDT counting.

### Our Bot Should

- ✅ Always use `client_order_id` for idempotency
- ✅ Use WebSocket `trade_updates` for order status (not polling)
- ✅ Use WebSocket market data stream (not polling REST)
- ✅ Check `fractionable` before sending fractional orders
- ✅ Check `daytrade_count` before day trades (if equity < $25K)
- ✅ Round prices to correct decimal places
- ✅ Store `X-Request-ID` from every API response
- ✅ Implement exponential backoff on 429s
- ✅ Use `nested=true` when querying bracket orders
- ✅ Handle partial fills gracefully (especially in paper — 10% random partials)
- ✅ Check `trading_blocked` and `account_blocked` on startup
- ✅ Cache asset data (refresh daily)
- ✅ Set `cancel_orders=true` when closing all positions (emergency exit)

---

*Last updated: 2026-03-04. Source: https://docs.alpaca.markets*
