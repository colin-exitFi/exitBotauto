# SEC EDGAR API Reference

Full-text search: `https://efts.sec.gov/LATEST/search-index`
Company filings: `https://www.sec.gov/cgi-bin/browse-edgar`

## Authentication

None required. Public API. No API key needed.

## Endpoints Used by Velox

### Full-Text Search (primary)

```
GET https://efts.sec.gov/LATEST/search-index
Params: q (str), dateRange (str), startdt (YYYY-MM-DD), enddt (YYYY-MM-DD)
```

Searches SEC filings by keyword (ticker, company name). Used to detect material events (8-K filings).

### Company Filing Browser

```
GET https://www.sec.gov/cgi-bin/browse-edgar
Params: action=getcompany, type=8-K, count=40, output=atom
```

Returns recent 8-K filings as an Atom feed. Used as a secondary source for material events.

## Velox Integration

Used by: `src/signals/edgar.py` (EdgarScanner)
- Scans for recent 8-K filings every 30 minutes during overnight session
- 8-K filers are added to watchlist as potential catalyst plays
- No API key required -- completely free

## Rate Limits

- No documented rate limit, but SEC asks for reasonable usage
- Include `User-Agent` header with contact email per SEC policy
- Velox scans every 30 minutes (1,800s) which is well within reasonable use

## Production Gotchas

- SEC EDGAR search results are delayed ~10-15 minutes from filing time
- The `file_num` field is needed to construct the direct link to the filing
- 8-K filings are the most time-sensitive (material events); 10-K/10-Q are less actionable for momentum trading
