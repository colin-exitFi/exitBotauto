# NASDAQ Earnings Calendar API Reference

Base URL: `https://api.nasdaq.com/api/calendar/earnings`

## Authentication

None required. Public API.

## Endpoints Used by Velox

### Earnings Calendar

```
GET https://api.nasdaq.com/api/calendar/earnings
Params: date (YYYY-MM-DD)
```

Returns companies reporting earnings on a given date. Used to identify earnings-driven momentum opportunities and avoid (or trade) earnings surprises.

## Velox Integration

Used by: `src/signals/earnings.py` (EarningsScanner)
- Refreshed on startup and during overnight session
- Today's earnings tickers are logged and added to watchlist
- Post-earnings reaction check: if a stock gaps up >3% after hours on earnings, conviction is boosted
- If it gaps down >2%, it's removed from long watchlist

## Rate Limits

- Undocumented, but NASDAQ API is public and lightly rate-limited
- Velox fetches once at startup and once during overnight -- minimal load

## Production Gotchas

- NASDAQ API occasionally returns HTML instead of JSON (parsing errors)
- Earnings dates can shift -- always cross-reference with the actual company IR page
- The `scan()` method returns actionable signals; `get_today()` returns today's full calendar
- After-hours earnings reactions are checked via Alpaca snapshots, not NASDAQ API
