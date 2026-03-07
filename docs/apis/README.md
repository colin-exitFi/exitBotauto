# API Documentation Index

Reference documentation for all external APIs used by Velox. AI agents (Opus, GPT-5.4, Codex) should consult these before making API integration changes.

Last updated: 2026-03-07

## Machine-Readable (llms.txt)

These are fetched from the API providers and contain comprehensive endpoint documentation:

| File | Service | Size | Source |
|------|---------|------|--------|
| [alpaca.txt](alpaca.txt) | Alpaca Trading + Market Data + Options | ~100KB | `https://docs.alpaca.markets/llms.txt` |
| [polygon.txt](polygon.txt) | Polygon / Massive Market Data | ~37KB | `https://polygon.io/llms.txt` |

## Manual Reference Docs -- Core Trading

| File | Service | Auth Required | Used By |
|------|---------|---------------|---------|
| [anthropic.md](anthropic.md) | Anthropic / Claude | API key | Primary AI (technical, jury, observer, advisor, tuner, exit agent) |
| [openai.md](openai.md) | OpenAI / GPT | API key | Risk Agent, Macro Agent, jury fallback |
| [xai-grok.md](xai-grok.md) | xAI / Grok | API key | Sentiment Agent, jury fallback |
| [perplexity.md](perplexity.md) | Perplexity | API key | Catalyst Agent, overnight thesis, news scanning |
| [unusual-whales.md](unusual-whales.md) | Unusual Whales (flow, dark pool, tide, congress) | API key + client ID | Scanner enrichment, regime detection |

## Manual Reference Docs -- Signal Sources

| File | Service | Auth Required | Used By |
|------|---------|---------------|---------|
| [stocktwits.md](stocktwits.md) | StockTwits | None (public) | Scanner SOURCE 2, sentiment enrichment |
| [x-twitter.md](x-twitter.md) | X / Twitter | Bearer token | Twitter sentiment client |
| [sec-edgar.md](sec-edgar.md) | SEC EDGAR | None (public) | 8-K filing detection, overnight scan |
| [fred.md](fred.md) | FRED macro series | Free API key | Macro snapshot (inflation, rates, labor, curve) |
| [finnhub.md](finnhub.md) | Finnhub calendars | API key | Economic calendar + IPO watchlist |
| [fda.md](fda.md) | FDA Drug API | None (public) | Pharma PDUFA catalyst scanner |
| [nasdaq-earnings.md](nasdaq-earnings.md) | NASDAQ Earnings Calendar | None (public) | Earnings date tracking |
| [coinbase.md](coinbase.md) | Coinbase Spot Prices | None (public) | Crypto price fallback |
| [quiver-quant.md](quiver-quant.md) | Quiver Quant Congress Trades | May require key | Congressional trading signals |
| [barchart.md](barchart.md) | Barchart (web scrape) | None (scrape) | Unusual options fallback (deprecated) |
| [finviz.md](finviz.md) | FINVIZ (web scrape) | None (scrape) | Short interest / squeeze detection |
| [finra.md](finra.md) | FINRA short interest | None (public) | Structured short-interest primary with freshness guard |

## How Velox Uses These APIs

```
Brokerage:     Alpaca (trading, positions, orders, market data, options, websockets)
Market Data:   Polygon (bars, snapshots, gainers) + Alpaca (snapshots, extended hours)
AI Providers:  Claude (primary) -> GPT (fallback) -> Grok (fallback) -> Perplexity (research)
Inst. Signals: Unusual Whales (flow, dark pool, tide, congress)
Social:        StockTwits (trending + sentiment) + X/Twitter + Grok (native X data)
Events:        SEC EDGAR (8-K / Form 4) + FDA (PDUFA dates) + NASDAQ (earnings calendar) + Finnhub (economic / IPO)
Macro:         FRED (inflation, rates, labor, yield curve)
Alternatives:  Quiver Quant (congress) + Coinbase (crypto) + Barchart/FINVIZ (scrapes) + FINRA (short interest)
```

## Updating These Docs

To refresh the llms.txt files:
```bash
curl -o docs/apis/alpaca.txt https://docs.alpaca.markets/llms.txt
curl -o docs/apis/polygon.txt https://polygon.io/llms.txt
```

Manual reference docs should be updated when:
- A new API endpoint is integrated into Velox
- A production gotcha is discovered (error codes, auth changes, rate limits)
- An API is deprecated or replaced (e.g., Barchart -> Unusual Whales for options flow)
