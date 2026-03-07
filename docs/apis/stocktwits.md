# StockTwits API Reference

Base URL: `https://api.stocktwits.com/api/2`
Docs: `https://api.stocktwits.com/developers`

## Authentication

None required for public endpoints. Rate limits are IP-based.

## Endpoints Used by Velox

### Trending Symbols

```
GET /trending/symbols.json
```

Returns currently trending stock symbols on StockTwits. Used as a social momentum signal source.

### Symbol Sentiment

```
GET /streams/symbol/{SYMBOL}.json
```

Returns recent messages and sentiment data for a specific ticker. Velox extracts bullish/bearish message counts and computes a sentiment score.

## Velox Integration

Used by: `src/signals/stocktwits.py` (StockTwitsClient)
- `get_trending()` feeds into scanner as SOURCE 2 (alongside Polygon gainers)
- `get_sentiment(symbol)` provides per-ticker bullish/bearish ratio during enrichment
- Trending symbols with high bullish sentiment get score boosts
- StockTwits data also feeds the overnight watchlist rebuild

## Rate Limits

- 200 requests per hour per IP (public, no auth)
- Velox fetches trending once per scan cycle + sentiment for top candidates
- At 5-min scan cadence with 10 sentiment lookups: ~130 requests/hour (within limit)

## Production Gotchas

- StockTwits API can be slow (2-5 second response times)
- Trending list changes frequently -- symbols can appear/disappear between scans
- Sentiment data is noisy -- StockTwits users are predominantly retail and can be wrong
- The API occasionally returns empty responses during high-traffic periods
- StockTwits is one of the strongest social momentum signals in the scanner -- it often front-runs moves that appear on X/Twitter later
