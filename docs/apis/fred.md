# FRED API Reference

Base URL: `https://api.stlouisfed.org/fred`

## Authentication

- Free API key required
- Passed as `api_key` query parameter

## Endpoints Used by Velox

### Series Observations

```http
GET /series/observations
```

Params used by Velox:
- `series_id`
- `api_key`
- `file_type=json`
- `sort_order=desc`
- `limit`

Series currently used:
- `CPIAUCSL` - CPI index (used to derive YoY inflation)
- `FEDFUNDS` - Effective Fed Funds rate
- `UNRATE` - Unemployment rate
- `T10Y2Y` - 10Y minus 2Y Treasury spread

## Velox Integration

Used by: `src/signals/fred.py`
- Builds a compact macro snapshot for the macro agent
- Feeds inflation, rates, labor, and yield-curve context into jury-time regime analysis
- Cached for 1 hour because the underlying series move slowly

## Production Gotchas

- Missing observations can show up as `"."`
- CPI is an index, not a percent; Velox computes YoY change locally
- Macro series frequencies differ (monthly vs daily); treat them as regime context, not intraday timing triggers
