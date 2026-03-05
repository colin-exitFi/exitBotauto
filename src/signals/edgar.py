"""
EDGAR SEC Filing Scanner — Free, no auth required.
Monitors SEC EDGAR RSS feed for market-moving filings:
  - 8-K (material events: earnings, M&A, executive changes)
  - 4 (insider trading — buys are bullish signals)
  - 10-K/10-Q (annual/quarterly reports)
  - SC 13D/13G (activist investor stakes >5%)
  - S-1 (IPO filings)

API: https://efts.sec.gov/LATEST/search-index?q=...
RSS: https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&type=8-K&dateb=&owner=include&count=40&search_text=&action=getcompany&output=atom

Full-text search: https://efts.sec.gov/LATEST/search-index?q="ticker"&dateRange=custom&startdt=2026-03-04&enddt=2026-03-05
"""

import time
import asyncio
from typing import Dict, List, Optional
from loguru import logger

import httpx


# SEC requires a User-Agent with contact info
EDGAR_HEADERS = {
    "User-Agent": "Velox Trading Bot support@exitfi.ai",
    "Accept": "application/json",
}

# Filing types that move stocks
MATERIAL_TYPES = {"8-K", "4", "SC 13D", "SC 13G", "S-1", "10-K", "10-Q", "6-K"}

# Cache: ticker -> last filing timestamp
_cache: Dict[str, float] = {}
_filing_cache: Dict[str, List[Dict]] = {}
_last_bulk_scan: float = 0


class EdgarScanner:
    """Scan SEC EDGAR for recent filings that could move stock prices."""

    def __init__(self):
        self.base_url = "https://efts.sec.gov/LATEST/search-index"
        self.full_text_url = "https://efts.sec.gov/LATEST/search-index"
        self._recent_filings: List[Dict] = []
        self._last_scan = 0
        self._scan_interval = 300  # 5 min between scans (respect SEC rate limits)
        logger.info("EDGAR SEC filing scanner initialized")

    async def scan_recent_filings(self, form_types: List[str] = None, limit: int = 20) -> List[Dict]:
        """
        Scan recent SEC filings via EDGAR full-text search API.
        Returns list of {ticker, form_type, filed, description, url}.
        """
        now = time.time()
        if now - self._last_scan < self._scan_interval and self._recent_filings:
            return self._recent_filings

        if form_types is None:
            form_types = ["8-K", "4"]

        filings = []
        try:
            from datetime import datetime, timedelta
            today = datetime.utcnow().strftime("%Y-%m-%d")
            yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")

            async with httpx.AsyncClient(timeout=15, headers=EDGAR_HEADERS) as client:
                for form_type in form_types:
                    # Use EDGAR full-text search
                    resp = await client.get(
                        "https://efts.sec.gov/LATEST/search-index",
                        params={
                            "q": f'formType:"{form_type}"',
                            "dateRange": "custom",
                            "startdt": yesterday,
                            "enddt": today,
                            "forms": form_type,
                        },
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        hits = data.get("hits", {}).get("hits", [])
                        for hit in hits[:limit]:
                            src = hit.get("_source", {})
                            ticker = self._extract_ticker(src)
                            if ticker:
                                filings.append({
                                    "ticker": ticker,
                                    "form_type": src.get("forms", form_type),
                                    "filed": src.get("file_date", ""),
                                    "description": src.get("display_names", [src.get("entity_name", "")])[0] if src.get("display_names") else src.get("entity_name", ""),
                                    "url": f"https://www.sec.gov/Archives/edgar/data/{src.get('file_num', '')}/",
                                })
                    else:
                        logger.debug(f"EDGAR search returned {resp.status_code} for {form_type}")

                    await asyncio.sleep(0.2)  # Rate limit: 10 req/sec max

        except Exception as e:
            logger.debug(f"EDGAR scan failed: {e}")
            # Fallback: use RSS feed
            filings = await self._scan_rss_fallback()

        self._recent_filings = filings
        self._last_scan = now
        if filings:
            logger.info(f"📋 EDGAR: {len(filings)} recent filings found")
        return filings

    async def check_ticker(self, ticker: str) -> List[Dict]:
        """Check for recent SEC filings for a specific ticker."""
        cached = _filing_cache.get(ticker)
        if cached and time.time() - _cache.get(ticker, 0) < 600:
            return cached

        filings = []
        try:
            async with httpx.AsyncClient(timeout=10, headers=EDGAR_HEADERS) as client:
                resp = await client.get(
                    "https://efts.sec.gov/LATEST/search-index",
                    params={
                        "q": f'"{ticker}"',
                        "forms": "8-K,4,SC 13D,SC 13G",
                        "dateRange": "custom",
                        "startdt": self._days_ago(3),
                        "enddt": self._today(),
                    },
                )
                if resp.status_code == 200:
                    data = resp.json()
                    hits = data.get("hits", {}).get("hits", [])
                    for hit in hits[:5]:
                        src = hit.get("_source", {})
                        filings.append({
                            "form_type": src.get("forms", ""),
                            "filed": src.get("file_date", ""),
                            "entity": src.get("entity_name", ""),
                            "description": src.get("display_names", [""])[0] if src.get("display_names") else "",
                        })
        except Exception as e:
            logger.debug(f"EDGAR ticker check failed for {ticker}: {e}")

        _filing_cache[ticker] = filings
        _cache[ticker] = time.time()
        return filings

    async def get_insider_trades(self, ticker: str) -> Dict:
        """
        Check Form 4 filings for insider buying/selling.
        Insider BUYING is a strong bullish signal.
        Returns: {"buys": int, "sells": int, "net_signal": "bullish"|"bearish"|"neutral"}
        """
        filings = await self.check_ticker(ticker)
        form4s = [f for f in filings if "4" in f.get("form_type", "")]
        # Can't determine buy/sell direction from search alone,
        # but presence of Form 4s indicates insider activity
        return {
            "form4_count": len(form4s),
            "has_insider_activity": len(form4s) > 0,
            "signal": "watch" if form4s else "none",
        }

    async def _scan_rss_fallback(self) -> List[Dict]:
        """Fallback: scan EDGAR RSS feed for recent 8-K filings."""
        filings = []
        try:
            async with httpx.AsyncClient(timeout=10, headers=EDGAR_HEADERS) as client:
                resp = await client.get(
                    "https://www.sec.gov/cgi-bin/browse-edgar",
                    params={
                        "action": "getcompany",
                        "type": "8-K",
                        "dateb": "",
                        "owner": "include",
                        "count": "20",
                        "search_text": "",
                        "output": "atom",
                    },
                )
                if resp.status_code == 200:
                    # Parse Atom XML
                    import re
                    entries = re.findall(r'<entry>(.*?)</entry>', resp.text, re.DOTALL)
                    for entry in entries[:20]:
                        title = re.search(r'<title.*?>(.*?)</title>', entry)
                        link = re.search(r'<link.*?href="(.*?)"', entry)
                        updated = re.search(r'<updated>(.*?)</updated>', entry)
                        if title:
                            ticker = self._extract_ticker_from_title(title.group(1))
                            if ticker:
                                filings.append({
                                    "ticker": ticker,
                                    "form_type": "8-K",
                                    "filed": updated.group(1) if updated else "",
                                    "description": title.group(1),
                                    "url": link.group(1) if link else "",
                                })
        except Exception as e:
            logger.debug(f"EDGAR RSS fallback failed: {e}")
        return filings

    def _extract_ticker(self, source: dict) -> Optional[str]:
        """Try to extract ticker from EDGAR filing data."""
        tickers = source.get("tickers", "")
        if tickers and isinstance(tickers, str):
            parts = tickers.strip().split()
            for p in parts:
                if p.isalpha() and 1 <= len(p) <= 5:
                    return p.upper()
        return None

    def _extract_ticker_from_title(self, title: str) -> Optional[str]:
        """Extract ticker from EDGAR RSS title like '8-K - AAPL (0000320193)'."""
        import re
        match = re.search(r'- ([A-Z]{1,5}) \(', title)
        return match.group(1) if match else None

    def _today(self) -> str:
        from datetime import datetime
        return datetime.utcnow().strftime("%Y-%m-%d")

    def _days_ago(self, days: int) -> str:
        from datetime import datetime, timedelta
        return (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
