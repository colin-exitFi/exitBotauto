# Unusual Whales API Reference

Base URL: `https://api.unusualwhales.com`
Full docs: `https://api.unusualwhales.com/docs#/`
Tier: Advanced ($375/mo) -- websockets, 50K req/day, 120 req/min

## Authentication

Two required headers on every request:

```
Authorization: Bearer {UW_API_TOKEN}
UW-CLIENT-API-ID: 100001
```

Setting: `UW_API_TOKEN` in `.env` and `config/settings.py`

## Endpoints Used by Velox

### Flow Alerts (whale options flow)

```
GET /api/option-trades/flow-alerts
Params: min_premium (int), ticker_symbol (str), limit (int)
```

Returns large options trades (sweeps, blocks). Primary institutional signal.
Velox caches for 60s. Used in scanner enrichment to boost/penalize candidate scores.

IMPORTANT: The correct path is `/api/option-trades/flow-alerts`, NOT `/api/options/flow`.

### Dark Pool

```
GET /api/darkpool/recent           -- all recent prints
GET /api/darkpool/{SYMBOL}         -- prints for specific ticker
Params: limit (int)
```

Institutional block trades that don't hit lit exchanges.
Velox caches for 60s.

### Market Tide

```
GET /api/market/market-tide
Params: interval_5m (bool, default false)
```

Aggregate put/call premium ratio. Used in regime detection:
- put_call_ratio >= 1.3 or puts >> calls -> risk_off
- put_call_ratio <= 0.8 or calls >> puts -> risk_on

Velox caches for 60s.

### Congress Trades

```
GET /api/congress/recent-trades
Params: limit (int)
```

Politician trading activity. Supplements existing congress.py scraping.
Velox caches for 900s (15 min).

### Gamma Exposure (not yet integrated)

```
GET /api/stock/{TICKER}/spot-exposures/strike
```

Dealer hedging levels that act as support/resistance. Future integration candidate.

## Rate Limits

- 120 requests/minute
- 50,000 requests/day
- Websocket access included (not yet used by Velox)

## Production Gotchas

- Response shape varies: `{"data": [...]}` wrapper on some endpoints, raw array on others. Client handles both.
- Field names are inconsistent across endpoints (e.g., `ticker` vs `ticker_symbol` vs `symbol`). Client normalizes.
- The `/api/option-trades/flow-alerts` endpoint is the correct one. `/api/options/flow` does NOT exist.
