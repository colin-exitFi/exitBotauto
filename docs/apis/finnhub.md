# Finnhub API Reference

Base URL: `https://finnhub.io/api/v1`

## Authentication

- API key required
- Passed as `token` query parameter

## Endpoints Used by Velox

### Economic Calendar

```http
GET /calendar/economic
```

Params used by Velox:
- `from` (`YYYY-MM-DD`)
- `to` (`YYYY-MM-DD`)
- `token`

Used for:
- Upcoming US macro events in the next 7 days
- Jury/macro-agent context for event risk

### IPO Calendar

```http
GET /calendar/ipo
```

Params used by Velox:
- `from` (`YYYY-MM-DD`)
- `to` (`YYYY-MM-DD`)
- `token`

Used for:
- Upcoming IPO listing dates
- Overnight watchlist enrichment

## Velox Integration

Used by: `src/signals/finnhub.py`
- Economic calendar summary feeds macro + catalyst context
- IPO calendar feeds the overnight watchlist as a low-conviction long-only discovery source
- Economic responses are cached for 1 hour; IPO responses for 6 hours

## Production Gotchas

- Response keys can vary slightly by endpoint wrapper (`economicCalendar`, `ipoCalendar`, `data`)
- Economic events should be filtered for US relevance before feeding the prompt
- IPO listings are watchlist signals, not auto-trade signals
