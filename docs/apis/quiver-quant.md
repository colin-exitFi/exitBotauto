# Quiver Quantitative Congressional Trading API Reference

Base URL: `https://api.quiverquant.com/beta`

## Authentication

May require API key depending on tier. Check current status in `src/signals/congress.py`.

## Endpoints Used by Velox

### Congressional Trading

```
GET /live/congresstrading
```

Returns recent congressional stock trades (buying and selling by members of Congress).

## Velox Integration

Used by: `src/signals/congress.py` (CongressScanner)
- Fetches congressional trades during overnight watchlist rebuild
- Multiple congress members buying the same stock = high-conviction signal
- Now SUPPLEMENTED by Unusual Whales congress trades (`/api/congress/recent-trades`)

## Important

- Quiver Quant may have moved to a paid tier or deprecated endpoints
- The Unusual Whales API (`src/signals/unusual_whales.py`) now provides congress trades as a more reliable source
- Both sources are used: Quiver Quant as primary REST, UW as supplement
- Congressional trade reporting has a 45-day lag -- this is not a real-time signal
- Falls back to Perplexity-based congress trade lookup if API fails
