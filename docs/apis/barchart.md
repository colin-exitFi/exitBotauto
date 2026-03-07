# Barchart Unusual Options Reference

URL: `https://www.barchart.com/options/unusual-activity/stocks`

## Authentication

None. This is a web scrape, not an API.

## How Velox Uses It

Used by: `src/signals/unusual_options.py` (UnusualOptionsScanner)
- Scrapes the Barchart unusual options activity page for large volume trades
- This is a SECONDARY source -- the primary source is now Unusual Whales API
- Falls back to Perplexity-based UOA lookup if Barchart scraping fails

## Important

- Barchart may block scrapers or change HTML structure without notice
- The Unusual Whales API (`src/signals/unusual_whales.py`) is now the preferred source for options flow data
- Barchart scraping should be considered deprecated but is kept as a fallback
