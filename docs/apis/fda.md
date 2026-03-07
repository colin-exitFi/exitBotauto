# FDA Drug API Reference

Base URL: `https://api.fda.gov`
Docs: `https://open.fda.gov/apis/`

## Authentication

None required. Public API. No API key needed (but optional key increases rate limit).

## Endpoints Used by Velox

### Drug Approvals / PDUFA Dates

```
GET https://api.fda.gov/drug/drugsfda.json
Params: search (str), limit (int)
```

Used to find FDA drug approval decisions and PDUFA (Prescription Drug User Fee Act) action dates. These are binary catalyst events for biotech/pharma stocks -- a stock can move 50-100% on an FDA approval or rejection.

## Velox Integration

Used by: `src/signals/pharma_catalyst.py` (PharmaCatalystScanner)
- Refreshes PDUFA calendar every 6 hours during overnight session
- Pharma catalysts with upcoming dates are added to watchlist
- Also uses Perplexity for supplementary PDUFA date research when FDA API data is sparse

## Rate Limits

- Without API key: 240 requests per minute, 120,000 per day
- With API key: 240 requests per minute, unlimited daily
- Velox refreshes every 6 hours -- well within limits

## Production Gotchas

- FDA API data can lag actual approval announcements by hours
- PDUFA dates can be delayed by the FDA without notice
- The real signal is the PDUFA date proximity, not the API response -- stocks move in anticipation
- Perplexity supplementation (`_refresh_pdufa_calendar()`) often has more current dates than the FDA API itself
