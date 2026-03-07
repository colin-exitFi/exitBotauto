# Coinbase Spot Price API Reference

Base URL: `https://api.coinbase.com/v2`

## Authentication

None required for public price endpoints.

## Endpoints Used by Velox

### Spot Price

```
GET /prices/{SYMBOL}-USD/spot
```

Returns current spot price for a cryptocurrency pair. Used as a fallback price source for crypto assets (BTC, ETH, SOL, etc.) when Polygon doesn't have the data.

## Velox Integration

Used by: `src/data/polygon_client.py` (`_get_crypto_price()`)
- Only called for crypto symbols (BTC, ETH, SOL, DOGE, etc.)
- Fallback when Polygon quote/snapshot returns no data for crypto
- Velox is primarily a stock/options bot -- crypto is a minor signal path

## Rate Limits

- 10,000 requests per hour (public, no auth)
- Velox calls this rarely (only for crypto price lookups)

## Production Gotchas

- Coinbase API returns prices as strings, not numbers -- parse with `float()`
- Response shape: `{"data": {"amount": "45123.45", "currency": "USD"}}`
