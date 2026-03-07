# FINVIZ Short Interest Reference

URL: `https://finviz.com/screener.ashx`

## Authentication

None. This is a web scrape, not an API.

## How Velox Uses It

Used by: `src/signals/short_interest.py` (ShortInterestScanner)
- Scrapes FINVIZ stock screener for high short interest stocks
- Identifies potential short squeeze candidates (high SI% + increasing volume)
- Falls back to Perplexity-based short interest lookup if scraping fails

## Important

- FINVIZ actively blocks automated scrapers -- may return 403 or captcha
- Short interest data is inherently delayed (reported bi-monthly by exchanges)
- The Unusual Whales API may provide more reliable short interest data in the future
- Consider this a best-effort signal source, not a reliable one
