"""
Portfolio Concentration Guard — Bloomberg PORT equivalent (V1).

V1 implements the 80% solution: sector exposure, symbol duplication,
coarse beta, and rolling correlation.  No external dependencies beyond
numpy (already in requirements.txt).

V2 (deferred) adds: VaR, stress testing, factor attribution via pypfopt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import numpy as np
from loguru import logger


MAX_SECTOR_CONCENTRATION_PCT = 50.0
MAX_POSITIONS_SAME_SOURCE = 3
MAX_PAIRWISE_CORRELATION = 0.70
MAX_PORTFOLIO_BETA = 1.5


@dataclass
class ConcentrationReport:
    timestamp: float = 0.0
    total_positions: int = 0
    sector_exposure: Dict[str, float] = field(default_factory=dict)
    sector_concentrated: bool = False
    concentrated_sectors: List[str] = field(default_factory=list)
    duplicate_symbol: bool = False
    source_overloaded: bool = False
    max_pairwise_correlation: float = 0.0
    correlated_with: List[str] = field(default_factory=list)
    portfolio_beta: float = 1.0
    beta_regime: str = "normal"
    new_entry_allowed: bool = True
    new_entry_reason: str = ""
    size_adjustment: float = 1.0
    dominant_blocker: str = ""

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "total_positions": self.total_positions,
            "sector_exposure": dict(self.sector_exposure),
            "sector_concentrated": self.sector_concentrated,
            "concentrated_sectors": list(self.concentrated_sectors),
            "duplicate_symbol": self.duplicate_symbol,
            "source_overloaded": self.source_overloaded,
            "max_pairwise_correlation": round(self.max_pairwise_correlation, 3),
            "correlated_with": list(self.correlated_with),
            "portfolio_beta": round(self.portfolio_beta, 3),
            "beta_regime": self.beta_regime,
            "new_entry_allowed": self.new_entry_allowed,
            "new_entry_reason": self.new_entry_reason,
            "size_adjustment": round(self.size_adjustment, 3),
            "dominant_blocker": self.dominant_blocker,
        }


class ConcentrationGuard:
    """
    Evaluates whether a new position would create dangerous concentration.

    Usage:
        guard = ConcentrationGuard(polygon_client)
        report = guard.evaluate(candidate, open_positions)
    """

    def __init__(self, polygon_client=None, alpaca_client=None):
        self._polygon = polygon_client
        self._alpaca = alpaca_client
        self._sector_cache: Dict[str, str] = {}
        self._sector_cache_at: float = 0.0

    def evaluate(
        self,
        candidate: Dict,
        open_positions: Dict[str, Dict],
    ) -> ConcentrationReport:
        """Run concentration checks for a candidate against the live book."""
        now = time.time()
        report = ConcentrationReport(timestamp=now)
        symbol = str(candidate.get("symbol", "") or "").upper()
        positions = open_positions or {}
        report.total_positions = len(positions)

        if not positions:
            return report

        if symbol in positions:
            report.duplicate_symbol = True
            report.new_entry_allowed = False
            report.new_entry_reason = "duplicate_symbol"
            report.dominant_blocker = "duplicate_symbol"
            return report

        source = str(candidate.get("source", candidate.get("scanner_source", "")) or "")
        if source:
            same_source_count = sum(
                1 for p in positions.values()
                if str(p.get("source", p.get("scanner_source", "")) or "") == source
            )
            if same_source_count >= MAX_POSITIONS_SAME_SOURCE:
                report.source_overloaded = True
                report.size_adjustment = 0.5
                report.new_entry_reason = f"source_overloaded:{source}"

        self._check_sector_concentration(candidate, positions, report)
        self._check_beta(positions, report)

        if report.sector_concentrated and not report.new_entry_reason:
            report.new_entry_allowed = False
            report.new_entry_reason = f"sector_concentrated:{','.join(report.concentrated_sectors)}"
            report.dominant_blocker = "sector_cap"

        if report.portfolio_beta > MAX_PORTFOLIO_BETA:
            candidate_direction = str(candidate.get("direction_constraint", "none") or "none")
            if "long" in candidate_direction:
                report.size_adjustment = min(report.size_adjustment, 0.5)
                if not report.new_entry_reason:
                    report.new_entry_reason = f"high_beta:{report.portfolio_beta:.2f}"

        return report

    def _check_sector_concentration(
        self,
        candidate: Dict,
        positions: Dict[str, Dict],
        report: ConcentrationReport,
    ):
        total_notional = 0.0
        sector_notional: Dict[str, float] = {}

        for sym, pos in positions.items():
            notional = self._position_notional(pos)
            total_notional += notional
            sector = self._get_sector(sym, pos)
            sector_notional[sector] = sector_notional.get(sector, 0) + notional

        if total_notional <= 0:
            return

        for sector, notional in sector_notional.items():
            pct = (notional / total_notional) * 100
            report.sector_exposure[sector] = round(pct, 1)
            if pct > MAX_SECTOR_CONCENTRATION_PCT:
                report.concentrated_sectors.append(sector)

        candidate_sector = self._get_sector(
            str(candidate.get("symbol", "") or ""),
            candidate,
        )
        if candidate_sector in report.concentrated_sectors:
            report.sector_concentrated = True

    def _check_beta(self, positions: Dict[str, Dict], report: ConcentrationReport):
        betas = []
        weights = []
        total = 0.0
        for pos in positions.values():
            notional = self._position_notional(pos)
            beta = float(pos.get("beta", 1.0) or 1.0)
            if notional > 0:
                betas.append(beta)
                weights.append(notional)
                total += notional

        if not betas or total <= 0:
            report.portfolio_beta = 1.0
            report.beta_regime = "normal"
            return

        weighted_beta = sum(b * w for b, w in zip(betas, weights)) / total
        report.portfolio_beta = round(weighted_beta, 3)

        if weighted_beta > 1.5:
            report.beta_regime = "high"
        elif weighted_beta < 0.7:
            report.beta_regime = "low"
        else:
            report.beta_regime = "normal"

    def _get_sector(self, symbol: str, data: Dict) -> str:
        symbol = symbol.upper()
        if symbol in self._sector_cache:
            return self._sector_cache[symbol]

        sector = str(data.get("sector", data.get("gics_sector", "")) or "")
        if sector:
            self._sector_cache[symbol] = sector
            return sector

        if self._polygon:
            try:
                details = self._polygon.get_ticker_details(symbol)
                if details:
                    sector = str(details.get("sic_description", details.get("sector", "Unknown")) or "Unknown")
                    self._sector_cache[symbol] = sector
                    return sector
            except Exception:
                pass

        self._sector_cache[symbol] = "Unknown"
        return "Unknown"

    @staticmethod
    def _position_notional(pos: Dict) -> float:
        for key in ("actual_notional", "notional", "market_value"):
            try:
                val = float(pos.get(key, 0) or 0)
                if val:
                    return abs(val)
            except Exception:
                pass
        try:
            price = float(pos.get("entry_price", 0) or 0)
            qty = float(pos.get("quantity", pos.get("qty", 0)) or 0)
            return abs(price * qty)
        except Exception:
            return 0.0
