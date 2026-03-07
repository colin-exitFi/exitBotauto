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
from xml.etree import ElementTree as ET
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
        self._form4_cache: Dict[str, Dict] = {}
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
                                issuer_cik = self._extract_issuer_cik(src, ticker)
                                filings.append({
                                    "ticker": ticker,
                                    "form_type": src.get("forms", form_type),
                                    "filed": src.get("file_date", ""),
                                    "description": src.get("display_names", [src.get("entity_name", "")])[0] if src.get("display_names") else src.get("entity_name", ""),
                                    "url": self._build_filing_directory_url(issuer_cik, src.get("adsh", "")),
                                    "adsh": src.get("adsh", ""),
                                    "issuer_cik": issuer_cik,
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
                        form_type = src.get("forms") or src.get("form") or ""
                        ticker = self._extract_ticker(src) or ticker
                        issuer_cik = self._extract_issuer_cik(src, ticker)
                        filings.append({
                            "ticker": ticker,
                            "form_type": form_type,
                            "filed": src.get("file_date", ""),
                            "entity": src.get("entity_name", ""),
                            "description": src.get("display_names", [""])[0] if src.get("display_names") else "",
                            "adsh": src.get("adsh", ""),
                            "issuer_cik": issuer_cik,
                            "xsl": src.get("xsl", ""),
                            "url": self._build_filing_directory_url(issuer_cik, src.get("adsh", "")),
                        })
        except Exception as e:
            logger.debug(f"EDGAR ticker check failed for {ticker}: {e}")

        _filing_cache[ticker] = filings
        _cache[ticker] = time.time()
        return filings

    async def get_insider_trades(self, ticker: str, filings: Optional[List[Dict]] = None) -> Dict:
        """
        Check Form 4 filings for insider buying/selling.
        Insider BUYING is a strong bullish signal.
        Returns: {"buys": int, "sells": int, "net_signal": "bullish"|"bearish"|"neutral"}
        """
        filings = filings if filings is not None else await self.check_ticker(ticker)
        form4s = [f for f in filings if "4" in f.get("form_type", "")]
        if not form4s:
            return {
                "form4_count": 0,
                "has_insider_activity": False,
                "signal": "none",
                "summary": "No recent Form 4 filings",
                "transactions": [],
                "open_market_buys": 0,
                "open_market_sells": 0,
                "buy_shares": 0.0,
                "sell_shares": 0.0,
            }

        transactions: List[Dict] = []
        for filing in form4s[:3]:
            parsed = await self._fetch_and_parse_form4(filing)
            transactions.extend(parsed.get("transactions", []))

        open_market_buys = [t for t in transactions if t.get("transaction_code") == "P"]
        open_market_sells = [t for t in transactions if t.get("transaction_code") == "S"]
        buy_like = [t for t in transactions if t.get("direction") in ("buy", "acquire")]
        sell_like = [t for t in transactions if t.get("direction") in ("sell", "dispose")]
        buy_shares = sum(float(t.get("shares", 0) or 0) for t in buy_like)
        sell_shares = sum(float(t.get("shares", 0) or 0) for t in sell_like)
        buy_value = sum(float(t.get("value", 0) or 0) for t in buy_like)
        sell_value = sum(float(t.get("value", 0) or 0) for t in sell_like)

        signal = "watch"
        if open_market_buys and not open_market_sells:
            signal = "bullish"
        elif open_market_sells and not open_market_buys:
            signal = "bearish"
        elif buy_value > (sell_value * 1.25) and buy_value > 0:
            signal = "bullish"
        elif sell_value > (buy_value * 1.25) and sell_value > 0:
            signal = "bearish"
        elif buy_shares > sell_shares * 1.25 and buy_shares > 0:
            signal = "bullish"
        elif sell_shares > buy_shares * 1.25 and sell_shares > 0:
            signal = "bearish"

        summary_parts = [f"{len(form4s)} Form 4s"]
        if open_market_buys:
            summary_parts.append(f"{len(open_market_buys)} open-market buys")
        if open_market_sells:
            summary_parts.append(f"{len(open_market_sells)} open-market sells")
        if buy_shares or sell_shares:
            summary_parts.append(f"shares B/S {buy_shares:,.0f}/{sell_shares:,.0f}")
        return {
            "form4_count": len(form4s),
            "has_insider_activity": len(form4s) > 0,
            "signal": signal,
            "summary": "; ".join(summary_parts),
            "transactions": transactions[:12],
            "open_market_buys": len(open_market_buys),
            "open_market_sells": len(open_market_sells),
            "buy_shares": round(buy_shares, 2),
            "sell_shares": round(sell_shares, 2),
            "buy_value": round(buy_value, 2),
            "sell_value": round(sell_value, 2),
        }

    async def _fetch_and_parse_form4(self, filing: Dict) -> Dict:
        adsh = str(filing.get("adsh", "") or "").strip()
        cache_key = f"{filing.get('issuer_cik', '')}:{adsh}"
        cached = self._form4_cache.get(cache_key)
        if cached and (time.time() - cached.get("fetched_at", 0)) < 3600:
            return cached.get("payload", {})

        xml_url = await self._resolve_form4_xml_url(filing)
        if not xml_url:
            return {"transactions": []}
        try:
            async with httpx.AsyncClient(timeout=15, headers=EDGAR_HEADERS) as client:
                resp = await client.get(xml_url)
                if resp.status_code != 200:
                    return {"transactions": []}
                payload = self._parse_form4_xml(resp.text)
                self._form4_cache[cache_key] = {"fetched_at": time.time(), "payload": payload}
                return payload
        except Exception as e:
            logger.debug(f"Form 4 XML fetch failed for {filing.get('ticker', '?')}: {e}")
            return {"transactions": []}

    async def _resolve_form4_xml_url(self, filing: Dict) -> Optional[str]:
        issuer_cik = str(filing.get("issuer_cik", "") or "").strip()
        adsh = str(filing.get("adsh", "") or "").strip()
        if not issuer_cik or not adsh:
            return None
        accession = adsh.replace("-", "")
        cik_dir = str(int(issuer_cik)) if issuer_cik.isdigit() else issuer_cik.lstrip("0")
        index_url = f"https://www.sec.gov/Archives/edgar/data/{cik_dir}/{accession}/index.json"
        try:
            async with httpx.AsyncClient(timeout=15, headers=EDGAR_HEADERS) as client:
                resp = await client.get(index_url)
                if resp.status_code != 200:
                    return None
                payload = resp.json()
        except Exception as e:
            logger.debug(f"Form 4 index lookup failed for {filing.get('ticker', '?')}: {e}")
            return None

        items = ((payload or {}).get("directory", {}) or {}).get("item", []) or []
        for item in items:
            name = str((item or {}).get("name", "") or "")
            lower = name.lower()
            if not lower.endswith(".xml"):
                continue
            if "index" in lower:
                continue
            if "form4" in lower or "ownership" in lower:
                return f"https://www.sec.gov/Archives/edgar/data/{cik_dir}/{accession}/{name}"
        for item in items:
            name = str((item or {}).get("name", "") or "")
            lower = name.lower()
            if lower.endswith(".xml") and "index" not in lower:
                return f"https://www.sec.gov/Archives/edgar/data/{cik_dir}/{accession}/{name}"
        return None

    @classmethod
    def _parse_form4_xml(cls, xml_text: str) -> Dict:
        transactions: List[Dict] = []
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            return {"transactions": []}

        issuer = root.find("issuer")
        ticker = cls._xml_text(issuer, "issuerTradingSymbol") if issuer is not None else ""
        owner_name = cls._xml_text(root.find("reportingOwner/reportingOwnerId"), "rptOwnerName")

        for tag_name, security_kind in (("nonDerivativeTransaction", "non_derivative"), ("derivativeTransaction", "derivative")):
            for node in root.findall(f".//{tag_name}"):
                transaction_code = cls._xml_text(node.find("transactionCoding"), "transactionCode").upper()
                acquired_disposed = cls._xml_text(
                    node.find("transactionAmounts"), "transactionAcquiredDisposedCode/value"
                ).upper()
                direction = cls._classify_transaction_direction(transaction_code, acquired_disposed)
                shares = cls._xml_float(node.find("transactionAmounts"), "transactionShares/value")
                price = cls._xml_float(node.find("transactionAmounts"), "transactionPricePerShare/value")
                transactions.append(
                    {
                        "ticker": ticker,
                        "owner_name": owner_name,
                        "security_kind": security_kind,
                        "security_title": cls._xml_text(node.find("securityTitle"), "value"),
                        "transaction_date": cls._xml_text(node.find("transactionDate"), "value"),
                        "transaction_code": transaction_code,
                        "acquired_disposed": acquired_disposed,
                        "direction": direction,
                        "shares": shares,
                        "price": price,
                        "value": round(shares * price, 2),
                    }
                )
        return {"ticker": ticker, "owner_name": owner_name, "transactions": transactions}

    @staticmethod
    def _xml_text(node, path: str) -> str:
        if node is None:
            return ""
        child = node.find(path)
        if child is None or child.text is None:
            return ""
        return str(child.text).strip()

    @classmethod
    def _xml_float(cls, node, path: str) -> float:
        text = cls._xml_text(node, path)
        try:
            return float(text)
        except Exception:
            return 0.0

    @staticmethod
    def _classify_transaction_direction(transaction_code: str, acquired_disposed: str) -> str:
        code = str(transaction_code or "").upper()
        acquired_disposed = str(acquired_disposed or "").upper()
        if code == "P":
            return "buy"
        if code == "S":
            return "sell"
        if acquired_disposed == "A":
            return "acquire"
        if acquired_disposed == "D":
            return "dispose"
        return "neutral"

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

    @staticmethod
    def _extract_issuer_cik(source: dict, ticker: Optional[str] = None) -> str:
        ciks = source.get("ciks", []) or []
        display_names = source.get("display_names", []) or []
        ticker = str(ticker or "").upper().strip()
        if ticker and isinstance(ciks, list) and isinstance(display_names, list):
            for idx, display_name in enumerate(display_names):
                if ticker and ticker in str(display_name).upper():
                    if idx < len(ciks):
                        return str(ciks[idx])
        if isinstance(ciks, list) and ciks:
            return str(ciks[-1])
        return ""

    @staticmethod
    def _build_filing_directory_url(issuer_cik: str, adsh: str) -> str:
        cik_text = str(issuer_cik or "").strip()
        adsh_text = str(adsh or "").strip()
        if not cik_text or not adsh_text:
            return ""
        cik_dir = str(int(cik_text)) if cik_text.isdigit() else cik_text.lstrip("0")
        accession = adsh_text.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{cik_dir}/{accession}/"

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
