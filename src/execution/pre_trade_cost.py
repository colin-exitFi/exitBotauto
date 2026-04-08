"""
Pre-Trade Cost Estimator — Bloomberg TRA equivalent.

Evaluates whether the theoretical edge of a trade survives execution cost
BEFORE placing the order. Computes spread cost, slippage, market impact,
liquidity score, and implementation shortfall estimate.

Every candidate that reaches this layer exits with a structured
ExecutabilityReport — no silent drop-offs.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger


STALE_THRESHOLDS = {
    "continuation_long": 300,
    "continuation_short": 300,
    "exhaustion_fade_short": 600,
    "swing_catalyst_long": 3600,
    "general_momentum_long": 300,
    "general_momentum_short": 300,
}

FALLBACK_EDGE_BPS = {
    "continuation_long": 80,
    "continuation_short": 70,
    "exhaustion_fade_short": 100,
    "swing_catalyst_long": 150,
    "general_momentum_long": 50,
    "general_momentum_short": 40,
}


@dataclass
class ExecutabilityReport:
    symbol: str
    timestamp: float
    spread_pct: float
    spread_acceptable: bool
    avg_daily_volume: float
    relative_volume: float
    liquidity_score: float
    shortable: bool
    easy_to_borrow: bool
    protection_placeable: bool
    protection_type: str
    staleness_seconds: float
    stale: bool
    estimated_slippage_bps: float
    estimated_market_impact_bps: float
    estimated_implementation_shortfall_bps: float
    execution_quality_score: float
    execution_verdict: str
    reason_codes: List[str] = field(default_factory=list)
    expected_edge_bps: float = 0.0
    edge_survives_cost: bool = True
    extended_hours_risk: float = 0.0
    dominant_blocker: str = ""

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "timestamp": self.timestamp,
            "spread_pct": self.spread_pct,
            "spread_acceptable": self.spread_acceptable,
            "avg_daily_volume": self.avg_daily_volume,
            "relative_volume": self.relative_volume,
            "liquidity_score": self.liquidity_score,
            "shortable": self.shortable,
            "easy_to_borrow": self.easy_to_borrow,
            "protection_placeable": self.protection_placeable,
            "protection_type": self.protection_type,
            "staleness_seconds": self.staleness_seconds,
            "stale": self.stale,
            "estimated_slippage_bps": round(self.estimated_slippage_bps, 2),
            "estimated_market_impact_bps": round(self.estimated_market_impact_bps, 2),
            "estimated_implementation_shortfall_bps": round(self.estimated_implementation_shortfall_bps, 2),
            "execution_quality_score": round(self.execution_quality_score, 3),
            "execution_verdict": self.execution_verdict,
            "reason_codes": list(self.reason_codes),
            "expected_edge_bps": round(self.expected_edge_bps, 2),
            "edge_survives_cost": self.edge_survives_cost,
            "extended_hours_risk": round(self.extended_hours_risk, 3),
            "dominant_blocker": self.dominant_blocker,
        }


def estimate_market_impact(
    order_notional: float,
    avg_daily_volume_notional: float,
    volatility_pct: float,
    urgency: float = 0.5,
) -> float:
    """Simplified Almgren-Chriss market impact in basis points."""
    if avg_daily_volume_notional <= 0:
        return 50.0
    trading_window = avg_daily_volume_notional * 0.02
    if trading_window <= 0:
        return 50.0
    participation_rate = min(1.0, order_notional / trading_window)
    vol = max(0.01, volatility_pct / 100.0)
    temporary = urgency * vol * math.sqrt(participation_rate) * 10000
    permanent = 0.1 * vol * math.sqrt(participation_rate) * 10000
    return temporary + permanent


def estimate_slippage(
    spread_pct: float,
    relative_volume: float,
    is_momentum: bool,
) -> float:
    """Estimate slippage in basis points."""
    spread_pct = max(0.0, float(spread_pct or 0.0))
    base = (spread_pct / 2.0) * 100
    rvol = max(0.01, relative_volume)
    volume_penalty = max(0.0, (1.0 / rvol) - 1.0) * 10.0
    momentum_penalty = 15.0 if is_momentum else 0.0
    return base + volume_penalty + momentum_penalty


def estimate_expected_edge(
    mode: str,
    entry_quality: str,
    classifier_confidence: float,
    risk_tone: str = "neutral",
    mode_stats: Optional[Dict] = None,
) -> float:
    """Estimate expected edge in bps from historical mode performance."""
    if mode_stats and int(mode_stats.get("trade_count", 0) or 0) >= 10:
        avg_winner = float(mode_stats.get("avg_winner_bps", 0) or 0)
        win_rate = float(mode_stats.get("win_rate", 0.5) or 0.5)
        base_edge = avg_winner * win_rate
    else:
        base_edge = float(FALLBACK_EDGE_BPS.get(mode, 50))

    confidence_mult = 0.5 + (min(1.0, max(0.0, classifier_confidence)) * 0.5)
    quality_mult = {"pullback": 1.2, "at_highs": 0.9, "chasing": 0.6}.get(
        str(entry_quality or "").lower(), 0.8
    )
    regime_mult = {"risk_on": 1.1, "neutral": 1.0, "risk_off": 0.7}.get(risk_tone, 1.0)
    return base_edge * confidence_mult * quality_mult * regime_mult


def compute_extended_hours_risk(
    session_label: str,
    spread_pct: float,
) -> float:
    if session_label == "regular":
        return 0.0
    penalty = 0.3
    if spread_pct > 0.5:
        penalty += 0.2
    if session_label in ("pre", "pre_market"):
        penalty += 0.1
    return min(1.0, penalty)


def check_protection_placeable(session_label: str) -> tuple[bool, str]:
    if session_label == "regular":
        return True, "stop_order"
    return True, "software_managed_limit"


class PreTradeCostEstimator:
    """
    Evaluates executability for a candidate before any AI time is spent.

    Usage:
        estimator = PreTradeCostEstimator(broker_client)
        report = estimator.evaluate(candidate, session_context_snapshot)
    """

    def __init__(self, broker_client=None, polygon_client=None):
        self._broker = broker_client
        self._polygon = polygon_client

    def evaluate(
        self,
        candidate: Dict,
        session_snapshot=None,
        mode_stats: Optional[Dict] = None,
    ) -> ExecutabilityReport:
        """Run full pre-trade cost analysis. Returns structured report."""
        symbol = str(candidate.get("symbol", "") or "").upper()
        now = time.time()
        reasons: List[str] = []

        spread_pct = max(0.0, float(candidate.get("spread_pct", 0.0) or 0.0))
        volume = float(candidate.get("volume", 0) or 0)
        avg_volume = float(candidate.get("avg_volume", candidate.get("average_volume", 0)) or 0)
        price = float(candidate.get("price", candidate.get("entry_price", 0)) or 0)
        relative_volume = (volume / avg_volume) if avg_volume > 0 else 1.0
        mode = str(candidate.get("setup_mode", "") or "").lower()
        direction = str(candidate.get("direction_constraint", "none") or "none").lower()
        entry_quality = str(candidate.get("entry_quality", "neutral") or "neutral")
        classifier_confidence = float(candidate.get("classifier_confidence", 0.5) or 0.5)
        session_label = str(candidate.get("session_type", "regular") or "regular").lower()
        data_age = float(candidate.get("data_age_seconds", 0.0) or 0.0)
        signal_ts = float(candidate.get("signal_timestamp", 0) or 0)
        if signal_ts > 0:
            data_age = max(data_age, now - signal_ts)

        atr_pct = float(candidate.get("atr_pct", candidate.get("atr_at_entry", 0)) or 0)
        if atr_pct <= 0:
            atr_pct = max(1.0, abs(float(candidate.get("daily_pct", 3.0) or 3.0)) * 0.3)

        shortable = True
        easy_to_borrow = True
        if direction == "short_only" and self._broker:
            try:
                shortable = self._broker.is_shortable(symbol)
                easy_to_borrow = shortable
            except Exception:
                pass

        stale_threshold = STALE_THRESHOLDS.get(mode, 300)
        stale = data_age > stale_threshold

        protection_ok, protection_type = check_protection_placeable(session_label)
        ext_risk = compute_extended_hours_risk(session_label, spread_pct)

        adv_notional = avg_volume * price if avg_volume > 0 and price > 0 else 0
        order_notional = float(candidate.get("intended_notional", 500) or 500)

        slippage = estimate_slippage(spread_pct, relative_volume, "momentum" in mode or "continuation" in mode)
        impact = estimate_market_impact(order_notional, adv_notional, atr_pct)

        risk_tone = "neutral"
        if session_snapshot:
            risk_tone = getattr(session_snapshot, "broad_risk_tone", "neutral")

        expected_edge = estimate_expected_edge(
            mode, entry_quality, classifier_confidence, risk_tone, mode_stats
        )

        total_cost = slippage + impact
        is_estimate = total_cost + (ext_risk * 20)

        liquidity_score = self._compute_liquidity_score(avg_volume, spread_pct, price)
        spread_ok = spread_pct <= 0.8
        edge_survives = expected_edge > total_cost

        quality_score = 1.0
        dominant_blocker = ""

        if not shortable and direction == "short_only":
            quality_score = 0.0
            dominant_blocker = "not_shortable"
            reasons.append("not_shortable")
        if stale:
            quality_score -= 0.3
            reasons.append("stale_signal")
            if not dominant_blocker:
                dominant_blocker = "stale"
        if not spread_ok:
            quality_score -= 0.2
            reasons.append("wide_spread")
            if not dominant_blocker:
                dominant_blocker = "spread_too_wide"
        if not edge_survives:
            quality_score -= 0.3
            reasons.append("slippage_exceeds_edge")
            if not dominant_blocker:
                dominant_blocker = "slippage_exceeds_edge"
        if liquidity_score < 0.3:
            quality_score -= 0.2
            reasons.append("too_thin")
            if not dominant_blocker:
                dominant_blocker = "too_thin"
        if ext_risk > 0.7:
            quality_score -= 0.15
            reasons.append("extended_hours_risk")
            if not dominant_blocker:
                dominant_blocker = "extended_hours_risk"

        quality_score = max(0.0, min(1.0, quality_score))

        if quality_score <= 0.0 and dominant_blocker == "not_shortable":
            verdict = "broker_blocked"
        elif quality_score < 0.3:
            verdict = "execution_unfavorable"
        elif stale and mode in ("continuation_long", "continuation_short"):
            verdict = "execution_unfavorable"
        else:
            verdict = "trade_now"

        return ExecutabilityReport(
            symbol=symbol,
            timestamp=now,
            spread_pct=spread_pct,
            spread_acceptable=spread_ok,
            avg_daily_volume=avg_volume,
            relative_volume=round(relative_volume, 2),
            liquidity_score=round(liquidity_score, 3),
            shortable=shortable,
            easy_to_borrow=easy_to_borrow,
            protection_placeable=protection_ok,
            protection_type=protection_type,
            staleness_seconds=round(data_age, 1),
            stale=stale,
            estimated_slippage_bps=slippage,
            estimated_market_impact_bps=impact,
            estimated_implementation_shortfall_bps=is_estimate,
            execution_quality_score=quality_score,
            execution_verdict=verdict,
            reason_codes=reasons,
            expected_edge_bps=expected_edge,
            edge_survives_cost=edge_survives,
            extended_hours_risk=ext_risk,
            dominant_blocker=dominant_blocker,
        )

    @staticmethod
    def _compute_liquidity_score(avg_volume: float, spread_pct: float, price: float) -> float:
        if avg_volume <= 0 or price <= 0:
            return 0.1
        spread_pct = max(0.0, float(spread_pct or 0.0))
        adv_notional = avg_volume * price
        vol_score = min(1.0, adv_notional / 5_000_000)
        spread_score = max(0.0, 1.0 - (spread_pct / 1.0))
        return (0.6 * vol_score) + (0.4 * spread_score)

    def get_size_adjustment(self, report: ExecutabilityReport) -> float:
        """Return sizing multiplier based on executability."""
        if report.execution_verdict in ("broker_blocked", "execution_unfavorable"):
            return 0.0
        if report.expected_edge_bps <= 0:
            return 1.0
        cost_ratio = report.estimated_implementation_shortfall_bps / max(1.0, report.expected_edge_bps)
        if cost_ratio > 0.4:
            return 0.5
        if cost_ratio > 0.2:
            return 0.75
        return 1.0
