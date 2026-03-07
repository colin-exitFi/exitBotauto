# FINRA Short Interest Reference

Base URL: `https://api.finra.org/data/group/otcMarket/name`

## Authentication

- Velox currently uses the public no-auth partition

## Endpoint Used by Velox

### Consolidated Short Interest

```http
POST /consolidatedShortInterest
Content-Type: application/json
```

Payload used by Velox:
- `limit`
- `fields`

## Velox Integration

Used by: `src/signals/short_interest.py`
- Queried before Finviz for structured short-interest data
- Public data is accepted only if the latest `settlementDate` is recent
- If the public partition is stale, Velox discards it and falls back instead of ingesting bad data

## Production Gotchas

- The public unauthenticated partition can lag badly
- Velox live-probed stale data (`2020-04-15`) and now freshness-checks settlement dates before trusting the response
- Treat FINRA as authoritative only when the settlement date is current enough for the strategy horizon
