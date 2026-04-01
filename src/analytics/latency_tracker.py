"""
Latency budget tracker — monitors pipeline speed by mode.

Tracks actual latencies and compares against mode-specific budgets.
A continuation setup with 12 seconds of delay is different from
a swing setup with 12 seconds of delay.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger


LATENCY_BUDGETS = {
    "continuation_long": {"total_ms": 15000, "jury_ms": 8000, "order_ms": 3000},
    "continuation_short": {"total_ms": 15000, "jury_ms": 8000, "order_ms": 3000},
    "exhaustion_fade_short": {"total_ms": 20000, "jury_ms": 10000, "order_ms": 3000},
    "swing_catalyst_long": {"total_ms": 60000, "jury_ms": 15000, "order_ms": 5000},
    "general_momentum_long": {"total_ms": 15000, "jury_ms": 8000, "order_ms": 3000},
    "general_momentum_short": {"total_ms": 15000, "jury_ms": 8000, "order_ms": 3000},
}


@dataclass
class LatencyRecord:
    symbol: str
    setup_id: str
    mode: str
    scan_to_classify_ms: float = 0.0
    classify_to_resolve_ms: float = 0.0
    resolve_to_jury_ms: float = 0.0
    jury_to_order_ms: float = 0.0
    order_to_fill_ms: float = 0.0
    total_ms: float = 0.0
    budget_exceeded: bool = False
    timestamp: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "setup_id": self.setup_id,
            "mode": self.mode,
            "scan_to_classify_ms": round(self.scan_to_classify_ms, 1),
            "classify_to_resolve_ms": round(self.classify_to_resolve_ms, 1),
            "resolve_to_jury_ms": round(self.resolve_to_jury_ms, 1),
            "jury_to_order_ms": round(self.jury_to_order_ms, 1),
            "order_to_fill_ms": round(self.order_to_fill_ms, 1),
            "total_ms": round(self.total_ms, 1),
            "budget_exceeded": self.budget_exceeded,
        }


class LatencyTracker:
    """
    Tracks pipeline latency per trade and compares to budget.

    Usage:
        tracker = LatencyTracker()
        tracker.record(record)
        summary = tracker.get_summary()
    """

    def __init__(self, max_records: int = 1000):
        self._records: List[LatencyRecord] = []
        self._max = max_records

    def record(self, rec: LatencyRecord):
        budget = LATENCY_BUDGETS.get(rec.mode, {})
        if budget and rec.total_ms > budget.get("total_ms", 99999):
            rec.budget_exceeded = True
            logger.warning(
                f"LATENCY BUDGET EXCEEDED: {rec.symbol} {rec.mode} "
                f"{rec.total_ms:.0f}ms > {budget['total_ms']}ms budget"
            )
        self._records.append(rec)
        if len(self._records) > self._max:
            self._records = self._records[-self._max:]

    def record_from_trade(self, trade: Dict):
        """Build a LatencyRecord from trade record timestamps."""
        signal_ts = float(trade.get("signal_timestamp", 0) or 0)
        order_ts = float(trade.get("entry_order_timestamp", 0) or 0)
        fill_ts = float(trade.get("fill_timestamp", 0) or 0)
        s2o = float(trade.get("signal_to_order_ms", 0) or 0)
        s2f = float(trade.get("signal_to_fill_ms", 0) or 0)

        total = s2f if s2f > 0 else (
            (fill_ts - signal_ts) * 1000 if fill_ts > 0 and signal_ts > 0 else 0
        )

        rec = LatencyRecord(
            symbol=str(trade.get("symbol", "") or ""),
            setup_id=str(trade.get("setup_id", "") or ""),
            mode=str(trade.get("setup_mode", "") or ""),
            total_ms=total,
            jury_to_order_ms=s2o if s2o > 0 else 0,
            order_to_fill_ms=(s2f - s2o) if s2f > 0 and s2o > 0 else 0,
            timestamp=time.time(),
        )
        self.record(rec)

    def get_summary(self) -> Dict:
        by_mode: Dict[str, List[float]] = defaultdict(list)
        exceeded_count = 0
        for r in self._records:
            if r.total_ms > 0:
                by_mode[r.mode].append(r.total_ms)
            if r.budget_exceeded:
                exceeded_count += 1

        mode_stats = {}
        for mode, latencies in by_mode.items():
            budget = LATENCY_BUDGETS.get(mode, {}).get("total_ms", 0)
            mode_stats[mode] = {
                "avg_ms": round(sum(latencies) / len(latencies), 1),
                "max_ms": round(max(latencies), 1),
                "min_ms": round(min(latencies), 1),
                "count": len(latencies),
                "budget_ms": budget,
                "exceeded_count": sum(1 for l in latencies if l > budget) if budget else 0,
            }

        return {
            "by_mode": mode_stats,
            "total_records": len(self._records),
            "budget_exceeded_total": exceeded_count,
        }
