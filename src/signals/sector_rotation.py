"""
Sector Rotation Model — Follow the money flow between sectors.

Money rotates predictably between sectors based on macro conditions:
  - Rising rates → banks up, tech down, REITs down
  - Oil spikes → energy up, transports down, consumer discretionary down
  - Risk-on → tech, growth, crypto stocks up
  - Risk-off → utilities, healthcare, consumer staples up
  - Dollar strength → exporters down, domestic up

Uses sector ETFs as proxies:
  XLK (Tech), XLF (Financials), XLE (Energy), XLV (Healthcare),
  XLY (Consumer Disc), XLP (Consumer Staples), XLI (Industrials),
  XLB (Materials), XLRE (Real Estate), XLU (Utilities), XLC (Communication)

Signal: Rotate INTO sectors showing momentum, AVOID sectors weakening.
"""

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from loguru import logger

import httpx

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CACHE_FILE = DATA_DIR / "sector_rotation.json"

SECTOR_ETFS = {
    "XLK": "Technology",
    "XLF": "Financials",
    "XLE": "Energy",
    "XLV": "Healthcare",
    "XLY": "Consumer Discretionary",
    "XLP": "Consumer Staples",
    "XLI": "Industrials",
    "XLB": "Materials",
    "XLRE": "Real Estate",
    "XLU": "Utilities",
    "XLC": "Communication",
    "SPY": "S&P 500 (benchmark)",
}

# Which sectors tend to have which stocks
SECTOR_STOCKS = {
    "Technology": ["AAPL", "MSFT", "NVDA", "GOOGL", "META", "AVGO", "AMD", "CRM", "ORCL"],
    "Financials": ["JPM", "BAC", "GS", "MS", "WFC", "BLK", "SCHW", "C"],
    "Energy": ["XOM", "CVX", "SLB", "COP", "EOG", "MPC", "VLO", "OXY"],
    "Healthcare": ["JNJ", "UNH", "PFE", "ABBV", "MRK", "LLY", "TMO", "ABT"],
    "Consumer Discretionary": ["AMZN", "TSLA", "HD", "NKE", "SBUX", "TGT", "LOW"],
    "Consumer Staples": ["PG", "KO", "PEP", "WMT", "COST", "PM", "MO"],
    "Industrials": ["CAT", "DE", "UPS", "HON", "GE", "BA", "RTX", "LMT"],
    "Utilities": ["NEE", "DUK", "SO", "D", "AEP", "EXC"],
}


class SectorRotationModel:
    """Track sector momentum and suggest where to focus scanning."""

    def __init__(self, polygon_client=None):
        self.polygon = polygon_client
        self._sector_data: Dict[str, Dict] = {}
        self._last_update = 0
        self._update_interval = 900  # 15 min
        self._load_cache()
        logger.info("Sector rotation model initialized")

    def _load_cache(self):
        try:
            if CACHE_FILE.exists():
                with open(CACHE_FILE) as f:
                    data = json.load(f)
                    self._sector_data = data.get("sectors", {})
                    self._last_update = data.get("updated_at", 0)
        except Exception:
            pass

    def _save_cache(self):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            with open(CACHE_FILE, "w") as f:
                json.dump({"sectors": self._sector_data, "updated_at": time.time()}, f, indent=2)
        except Exception:
            pass

    async def update(self) -> Dict[str, Dict]:
        """Update sector ETF performance data."""
        now = time.time()
        if now - self._last_update < self._update_interval and self._sector_data:
            return self._sector_data

        if not self.polygon:
            return self._sector_data

        for etf, sector_name in SECTOR_ETFS.items():
            try:
                price = self.polygon.get_price(etf)
                # Get previous close for daily change
                prev = self.polygon.get_previous_close(etf) if hasattr(self.polygon, 'get_previous_close') else 0
                change_pct = ((price - prev) / prev * 100) if prev > 0 else 0

                self._sector_data[etf] = {
                    "sector": sector_name,
                    "price": price,
                    "change_pct": round(change_pct, 2),
                    "updated": now,
                }
            except Exception:
                continue

        self._last_update = now
        self._save_cache()
        return self._sector_data

    def get_hot_sectors(self, top_n: int = 3) -> List[Tuple[str, str, float]]:
        """Get the top N performing sectors. Returns [(etf, sector_name, change_pct)]."""
        if not self._sector_data:
            return []
        ranked = sorted(
            [(etf, d["sector"], d.get("change_pct", 0))
             for etf, d in self._sector_data.items() if etf != "SPY"],
            key=lambda x: -x[2]
        )
        return ranked[:top_n]

    def get_cold_sectors(self, bottom_n: int = 3) -> List[Tuple[str, str, float]]:
        """Get the bottom N performing sectors (short candidates)."""
        if not self._sector_data:
            return []
        ranked = sorted(
            [(etf, d["sector"], d.get("change_pct", 0))
             for etf, d in self._sector_data.items() if etf != "SPY"],
            key=lambda x: x[2]
        )
        return ranked[:bottom_n]

    def get_stocks_in_hot_sectors(self) -> List[str]:
        """Get individual stocks in the hottest sectors."""
        hot = self.get_hot_sectors(3)
        stocks = []
        for _, sector_name, _ in hot:
            stocks.extend(SECTOR_STOCKS.get(sector_name, []))
        return stocks

    def get_sector_bias(self) -> str:
        """Overall market bias based on sector rotation."""
        if not self._sector_data:
            return "unknown"
        spy = self._sector_data.get("SPY", {})
        spy_change = spy.get("change_pct", 0)
        if spy_change > 0.5:
            return "risk_on"
        elif spy_change < -0.5:
            return "risk_off"
        return "neutral"

    def suggest_focus(self) -> Dict:
        """Suggest where the bot should focus scanning."""
        hot = self.get_hot_sectors(3)
        cold = self.get_cold_sectors(3)
        bias = self.get_sector_bias()

        long_sectors = [s[1] for s in hot if s[2] > 0]
        short_sectors = [s[1] for s in cold if s[2] < 0]

        return {
            "bias": bias,
            "long_sectors": long_sectors,
            "short_sectors": short_sectors,
            "long_stocks": self.get_stocks_in_hot_sectors(),
            "avoid_sectors": [s[1] for s in cold],
        }

    def get_dashboard_data(self) -> List[Dict]:
        """Format for dashboard display."""
        if not self._sector_data:
            return []
        result = []
        for etf, data in sorted(self._sector_data.items(), key=lambda x: -x[1].get("change_pct", 0)):
            result.append({
                "etf": etf,
                "sector": data["sector"],
                "change_pct": data.get("change_pct", 0),
                "price": data.get("price", 0),
            })
        return result
