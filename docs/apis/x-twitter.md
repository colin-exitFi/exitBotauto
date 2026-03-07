# X / Twitter API Reference

Base URL: `https://api.twitter.com` (v2)
Docs: `https://docs.x.com/overview`

## Authentication

Bearer token:
```
Authorization: Bearer {X_BEARER_TOKEN}
```

OAuth 1.0a (for user-context endpoints):
```
X_CONSUMER_KEY
X_CONSUMER_SECRET
```

Settings in `.env`: `X_BEARER_TOKEN`, `X_CONSUMER_KEY`, `X_CONSUMER_SECRET`

## Velox Integration

Used by: `src/signals/twitter.py` (TwitterSentimentClient)

The Twitter client fetches recent tweets mentioning stock tickers to gauge retail sentiment. It supplements the Grok sentiment agent (which has native X data access and is the primary social signal source).

### Search Recent Tweets

```
GET /2/tweets/search/recent
Params: query (str), max_results (int), tweet.fields (str)
```

Used to search for cashtag mentions ($AAPL, $TSLA) and assess volume of discussion.

## Rate Limits

- App-rate-limited: 450 requests per 15-minute window (v2 Basic)
- Velox internal limit: configured in sentiment client
- Pay-per-usage pricing now available on X Developer Platform

## Production Gotchas

- X API has transitioned to pay-per-usage pricing (credit-based)
- The primary social sentiment signal in Velox comes from Grok (which has native X data), not direct Twitter API calls
- StockTwits (`src/signals/stocktwits.py`) is a more reliable social sentiment source for stock-specific discussion
