"""
Daily Operating Review — post-close report generated at 4:15 PM ET.

Observational first, not prescriptive.  Surfaces facts and patterns.
Human review decides what to change.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger


@dataclass
class DailyOperatingReview:
    date: str = ""
    session_context_summary: Dict = field(default_factory=dict)
    setup_funnel: Dict = field(default_factory=dict)
    conversion_rates: Dict = field(default_factory=dict)
    mode_confusion: Dict = field(default_factory=dict)
    execution_quality: Dict = field(default_factory=dict)
    book_performance: List[Dict] = field(default_factory=list)
    mode_performance: List[Dict] = field(default_factory=list)
    blocked_reasons: Dict = field(default_factory=dict)
    biggest_misses: List[Dict] = field(default_factory=list)
    biggest_saves: List[Dict] = field(default_factory=list)
    provider_health: Dict = field(default_factory=dict)
    cost_model_calibration: Dict = field(default_factory=dict)
    trigger_analysis: Dict = field(default_factory=dict)
    shadow_pnl_comparison: Dict = field(default_factory=dict)
    observations: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "date": self.date,
            "session_context_summary": self.session_context_summary,
            "setup_funnel": self.setup_funnel,
            "conversion_rates": self.conversion_rates,
            "mode_confusion": self.mode_confusion,
            "execution_quality": self.execution_quality,
            "book_performance": self.book_performance,
            "mode_performance": self.mode_performance,
            "blocked_reasons": self.blocked_reasons,
            "biggest_misses": self.biggest_misses,
            "biggest_saves": self.biggest_saves,
            "provider_health": self.provider_health,
            "cost_model_calibration": self.cost_model_calibration,
            "trigger_analysis": self.trigger_analysis,
            "shadow_pnl_comparison": self.shadow_pnl_comparison,
            "observations": self.observations,
        }


def build_daily_review(
    date_str: str,
    session_snapshot: Optional[Dict] = None,
    funnel_summary: Optional[Dict] = None,
    conversion_rates: Optional[Dict] = None,
    book_scores: Optional[List[Dict]] = None,
    mode_scores: Optional[List[Dict]] = None,
    trades_today: Optional[List[Dict]] = None,
    shadow_trades: Optional[List[Dict]] = None,
    provider_snapshot: Optional[Dict] = None,
    state_transitions: Optional[List[Dict]] = None,
) -> DailyOperatingReview:
    """Build the daily operating review from all available data."""
    review = DailyOperatingReview(date=date_str)

    if session_snapshot:
        review.session_context_summary = {
            "risk_tone": session_snapshot.get("broad_risk_tone", "unknown"),
            "vix_regime": session_snapshot.get("vix_regime", "unknown"),
            "market_regime": session_snapshot.get("market_regime", "unknown"),
            "overnight_bias": session_snapshot.get("overnight_index_bias", "unknown"),
        }

    if funnel_summary:
        review.setup_funnel = funnel_summary
    if conversion_rates:
        review.conversion_rates = conversion_rates
    if book_scores:
        review.book_performance = book_scores
    if mode_scores:
        review.mode_performance = mode_scores
    if provider_snapshot:
        review.provider_health = provider_snapshot

    trades = trades_today or []
    shadows = shadow_trades or []

    review.blocked_reasons = _compute_blocked_reasons(funnel_summary)
    review.mode_confusion = _compute_mode_confusion(funnel_summary)
    review.execution_quality = _compute_execution_quality(trades)
    review.cost_model_calibration = _compute_cost_calibration(trades)
    review.biggest_misses = _compute_biggest_misses(shadows)
    review.biggest_saves = _compute_biggest_saves(shadows)
    review.shadow_pnl_comparison = _compute_shadow_comparison(trades, shadows)
    review.observations = _generate_observations(review)

    return review


def _compute_blocked_reasons(funnel: Optional[Dict]) -> Dict:
    if not funnel:
        return {}
    return dict(funnel.get("block_reasons", {}))


def _compute_mode_confusion(funnel: Optional[Dict]) -> Dict:
    if not funnel:
        return {}
    by_mode = funnel.get("by_mode", {})
    total_classified = sum(v.get("classified", 0) for v in by_mode.values())
    distribution = {}
    warnings = []
    for mode, stages in by_mode.items():
        classified = stages.get("classified", 0)
        pct = (classified / max(1, total_classified)) * 100
        distribution[mode] = round(pct, 1)
        if pct > 60:
            warnings.append(f"{mode} at {pct:.1f}% — classifier may be too loose")
        if mode == "continuation_long" and pct < 5 and total_classified > 20:
            warnings.append(f"continuation_long at {pct:.1f}% — classifier may be too strict")
    data_insufficient_pct = distribution.get("data_insufficient", 0) + distribution.get("mode_conflict", 0)
    if data_insufficient_pct > 30:
        warnings.append(f"data_insufficient+mode_conflict at {data_insufficient_pct:.1f}% — feature quality degraded")
    return {"distribution": distribution, "warnings": warnings, "total_classified": total_classified}


def _compute_execution_quality(trades: List[Dict]) -> Dict:
    if not trades:
        return {}
    by_mode: Dict[str, List[Dict]] = defaultdict(list)
    for t in trades:
        by_mode[str(t.get("setup_mode", "unknown") or "unknown")].append(t)

    quality = {}
    for mode, mode_trades in by_mode.items():
        slippages = [float(t.get("slippage_bps", 0) or 0) for t in mode_trades]
        latencies = [float(t.get("signal_to_fill_ms", 0) or 0) for t in mode_trades if float(t.get("signal_to_fill_ms", 0) or 0) > 0]
        quality[mode] = {
            "avg_slippage_bps": round(sum(slippages) / max(1, len(slippages)), 2),
            "avg_latency_ms": round(sum(latencies) / max(1, len(latencies)), 1) if latencies else 0,
            "trade_count": len(mode_trades),
        }
    return quality


def _compute_cost_calibration(trades: List[Dict]) -> Dict:
    """Compare pre-trade estimates to actual outcomes."""
    calibration_errors: Dict[str, List[float]] = defaultdict(list)
    for t in trades:
        estimated = float(t.get("estimated_slippage_bps", 0) or 0)
        actual = float(t.get("slippage_bps", 0) or 0)
        if estimated > 0:
            mode = str(t.get("setup_mode", "unknown") or "unknown")
            calibration_errors[mode].append(actual - estimated)
    result = {}
    for mode, errors in calibration_errors.items():
        avg_error = sum(errors) / len(errors) if errors else 0
        result[mode] = {
            "avg_calibration_error_bps": round(avg_error, 2),
            "sample_count": len(errors),
            "model_bias": "underestimates" if avg_error > 5 else ("overestimates" if avg_error < -5 else "calibrated"),
        }
    return result


def _compute_biggest_misses(shadows: List[Dict]) -> List[Dict]:
    """Shadow trades where MFE > 1.5% — the trade was right but we didn't take it."""
    misses = []
    for s in shadows:
        mfe = float(s.get("mfe", 0) or 0)
        if mfe > 1.5:
            misses.append({
                "symbol": s.get("symbol", ""),
                "strategy_tag": s.get("strategy_tag", ""),
                "direction": s.get("direction", ""),
                "mfe_pct": round(mfe, 2),
                "signal_price": s.get("signal_price", 0),
                "timestamp": s.get("timestamp", 0),
            })
    return sorted(misses, key=lambda m: m["mfe_pct"], reverse=True)[:10]


def _compute_biggest_saves(shadows: List[Dict]) -> List[Dict]:
    """Shadow trades where MAE > 2% — the trade would have lost and we correctly avoided it."""
    saves = []
    for s in shadows:
        mae = float(s.get("mae", 0) or 0)
        if mae > 2.0:
            saves.append({
                "symbol": s.get("symbol", ""),
                "strategy_tag": s.get("strategy_tag", ""),
                "direction": s.get("direction", ""),
                "mae_pct": round(mae, 2),
                "signal_price": s.get("signal_price", 0),
                "timestamp": s.get("timestamp", 0),
            })
    return sorted(saves, key=lambda s: s["mae_pct"], reverse=True)[:10]


def _compute_shadow_comparison(trades: List[Dict], shadows: List[Dict]) -> Dict:
    real_pnl = sum(float(t.get("pnl", 0) or 0) for t in trades)
    shadow_pnl = 0.0
    for s in shadows:
        mfe = float(s.get("mfe", 0) or 0)
        mae = float(s.get("mae", 0) or 0)
        shadow_pnl += (mfe - mae) * 0.5 * float(s.get("signal_price", 0) or 0) * 0.01
    return {
        "real_pnl": round(real_pnl, 2),
        "shadow_estimated_pnl": round(shadow_pnl, 2),
        "shadow_trade_count": len(shadows),
        "real_trade_count": len(trades),
    }


def _generate_observations(review: DailyOperatingReview) -> List[str]:
    """Generate observational summaries. NOT prescriptions."""
    obs = []

    for ms in review.mode_performance:
        mode = ms.get("mode", "")
        exp = ms.get("expectancy_per_trade", 0)
        count = ms.get("trade_count", 0)
        if count >= 5:
            obs.append(f"{mode} expectancy ${exp:.2f}/trade over {count} trades")

    for warning in review.mode_confusion.get("warnings", []):
        obs.append(f"Mode confusion: {warning}")

    for mode, cal in review.cost_model_calibration.items():
        bias = cal.get("model_bias", "calibrated")
        if bias != "calibrated":
            error = cal.get("avg_calibration_error_bps", 0)
            obs.append(f"Cost model {bias} for {mode} by avg {abs(error):.1f}bps")

    miss_count = len(review.biggest_misses)
    if miss_count > 0:
        total_mfe = sum(m.get("mfe_pct", 0) for m in review.biggest_misses)
        obs.append(f"{miss_count} shadow trades had MFE >1.5% (avg {total_mfe/miss_count:.1f}%)")

    save_count = len(review.biggest_saves)
    if save_count > 0:
        obs.append(f"{save_count} correctly blocked trades would have lost >2%")

    return obs
