"""
Setup Funnel — tracks the full conversion pipeline from scan to exit.

candidates_scanned -> classified -> pending_trigger -> trigger_fired ->
pre_trade_passed -> concentration_passed -> entry_submitted -> filled ->
protected -> exited

Every event is recorded with setup_id for full attribution.
"""

from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger


FUNNEL_STAGES = [
    "scanned",
    "classified",
    "pending_trigger",
    "trigger_fired",
    "pre_trade_passed",
    "concentration_passed",
    "entry_submitted",
    "filled",
    "protected",
    "exited",
    "blocked",
    "expired",
    "shadow",
]


@dataclass
class FunnelEvent:
    timestamp: float
    symbol: str
    setup_id: str
    stage: str
    mode: str
    book: str
    session_label: str
    reason: str = ""
    blocking_details: Optional[Dict] = None

    def to_dict(self) -> Dict:
        d = {
            "timestamp": self.timestamp,
            "symbol": self.symbol,
            "setup_id": self.setup_id,
            "stage": self.stage,
            "mode": self.mode,
            "book": self.book,
            "session_label": self.session_label,
            "reason": self.reason,
        }
        if self.blocking_details:
            d["blocking_details"] = self.blocking_details
        return d


class SetupFunnel:
    """
    Records and queries conversion funnel events.

    Usage:
        funnel = SetupFunnel()
        funnel.record("AAPL", "setup-123", "classified", "continuation_long", "momentum_long")
        summary = funnel.get_summary()
    """

    def __init__(self, max_events: int = 10000):
        self._events: List[FunnelEvent] = []
        self._max_events = max_events

    def record(
        self,
        symbol: str,
        setup_id: str,
        stage: str,
        mode: str = "",
        book: str = "",
        session_label: str = "",
        reason: str = "",
        blocking_details: Optional[Dict] = None,
    ):
        event = FunnelEvent(
            timestamp=time.time(),
            symbol=symbol,
            setup_id=setup_id,
            stage=stage,
            mode=mode,
            book=book,
            session_label=session_label,
            reason=reason,
            blocking_details=blocking_details,
        )
        self._events.append(event)
        if len(self._events) > self._max_events:
            self._events = self._events[-self._max_events:]

    def get_summary(self, since: float = 0.0) -> Dict:
        """Funnel counts by stage, optionally filtered by time."""
        counts: Dict[str, int] = defaultdict(int)
        by_mode: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        by_book: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        block_reasons: Dict[str, int] = defaultdict(int)

        for e in self._events:
            if e.timestamp < since:
                continue
            counts[e.stage] += 1
            if e.mode:
                by_mode[e.mode][e.stage] += 1
            if e.book:
                by_book[e.book][e.stage] += 1
            if e.stage == "blocked" and e.reason:
                block_reasons[e.reason] += 1

        return {
            "total": dict(counts),
            "by_mode": {k: dict(v) for k, v in by_mode.items()},
            "by_book": {k: dict(v) for k, v in by_book.items()},
            "block_reasons": dict(block_reasons),
        }

    def get_conversion_rates(self, since: float = 0.0) -> Dict:
        """Key conversion rates."""
        summary = self.get_summary(since)
        total = summary["total"]
        scanned = total.get("scanned", 0)
        classified = total.get("classified", 0)
        triggered = total.get("trigger_fired", 0)
        entered = total.get("entry_submitted", 0)
        filled = total.get("filled", 0)

        def _rate(num, denom):
            return round(num / max(1, denom) * 100, 1)

        return {
            "scan_to_classify_pct": _rate(classified, scanned),
            "classify_to_trigger_pct": _rate(triggered, classified),
            "trigger_to_entry_pct": _rate(entered, triggered),
            "entry_to_fill_pct": _rate(filled, entered),
            "scan_to_fill_pct": _rate(filled, scanned),
        }

    def get_recent_events(self, limit: int = 50) -> List[Dict]:
        return [e.to_dict() for e in self._events[-limit:]]
