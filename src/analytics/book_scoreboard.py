"""
Book Scoreboard — per-book and per-mode performance scoring.

Bloomberg PORT equivalent for strategy-level analytics.
Reads from the analytics trade ledger (not raw) to avoid contamination
from broker-reconstructed fills and dust cleanup trades.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger


@dataclass
class BookScore:
    strategy_tag: str
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    win_rate: float = 0.0
    expectancy_per_trade: float = 0.0
    trade_count: int = 0
    open_position_count: int = 0
    capital_allocated: float = 0.0
    capital_utilization: float = 0.0
    avg_hold_seconds: float = 0.0
    ratchet_activation_rate: float = 0.0
    dead_money_count: int = 0
    giveback_rate: float = 0.0
    drawdown_pct: float = 0.0
    activation_rate: float = 0.0

    def to_dict(self) -> Dict:
        return {
            "strategy_tag": self.strategy_tag,
            "realized_pnl": round(self.realized_pnl, 2),
            "unrealized_pnl": round(self.unrealized_pnl, 2),
            "win_rate": round(self.win_rate, 3),
            "expectancy_per_trade": round(self.expectancy_per_trade, 2),
            "trade_count": self.trade_count,
            "open_position_count": self.open_position_count,
            "capital_allocated": round(self.capital_allocated, 2),
            "capital_utilization": round(self.capital_utilization, 3),
            "avg_hold_seconds": round(self.avg_hold_seconds, 1),
            "ratchet_activation_rate": round(self.ratchet_activation_rate, 3),
            "dead_money_count": self.dead_money_count,
            "giveback_rate": round(self.giveback_rate, 3),
            "drawdown_pct": round(self.drawdown_pct, 3),
            "activation_rate": round(self.activation_rate, 3),
        }


@dataclass
class ModeScore:
    mode: str
    expectancy_per_trade: float = 0.0
    win_rate: float = 0.0
    trade_count: int = 0
    avg_slippage_bps: float = 0.0
    avg_hold_seconds: float = 0.0
    trigger_conversion_rate: float = 0.0
    entry_conversion_rate: float = 0.0
    best_session: str = ""

    def to_dict(self) -> Dict:
        return {
            "mode": self.mode,
            "expectancy_per_trade": round(self.expectancy_per_trade, 2),
            "win_rate": round(self.win_rate, 3),
            "trade_count": self.trade_count,
            "avg_slippage_bps": round(self.avg_slippage_bps, 2),
            "avg_hold_seconds": round(self.avg_hold_seconds, 1),
            "trigger_conversion_rate": round(self.trigger_conversion_rate, 3),
            "entry_conversion_rate": round(self.entry_conversion_rate, 3),
            "best_session": self.best_session,
        }


class BookScoreboard:
    """
    Computes per-book and per-mode scores from the analytics trade ledger.

    Usage:
        scoreboard = BookScoreboard()
        scoreboard.refresh(analytics_trades, open_positions, funnel_summary)
        books = scoreboard.book_scores
        modes = scoreboard.mode_scores
    """

    def __init__(self):
        self.book_scores: List[BookScore] = []
        self.mode_scores: List[ModeScore] = []

    def refresh(
        self,
        trades: List[Dict],
        open_positions: Optional[Dict[str, Dict] | List[Dict]] = None,
        funnel_summary: Optional[Dict] = None,
    ):
        self.book_scores = self._compute_book_scores(trades, open_positions)
        self.mode_scores = self._compute_mode_scores(trades, funnel_summary)

    def _compute_book_scores(
        self,
        trades: List[Dict],
        open_positions: Optional[Dict[str, Dict] | List[Dict]],
    ) -> List[BookScore]:
        by_book: Dict[str, List[Dict]] = defaultdict(list)
        for t in trades:
            tag = str(t.get("strategy_tag", "unknown") or "unknown")
            by_book[tag].append(t)

        position_rows = self._iter_open_positions(open_positions)

        scores = []
        for tag, book_trades in by_book.items():
            score = BookScore(strategy_tag=tag)
            pnls = [float(t.get("pnl", 0) or 0) for t in book_trades]
            score.trade_count = len(book_trades)
            score.realized_pnl = sum(pnls)
            wins = [p for p in pnls if p > 0]
            score.win_rate = len(wins) / max(1, len(pnls))
            score.expectancy_per_trade = score.realized_pnl / max(1, score.trade_count)

            holds = [float(t.get("hold_seconds", 0) or 0) for t in book_trades if float(t.get("hold_seconds", 0) or 0) > 0]
            score.avg_hold_seconds = sum(holds) / max(1, len(holds))

            slippages = [float(t.get("slippage_bps", 0) or 0) for t in book_trades]
            ratchet_exits = sum(1 for t in book_trades if "ratchet" in str(t.get("reason", "") or "").lower())
            score.ratchet_activation_rate = ratchet_exits / max(1, score.trade_count)

            givebacks = [float(t.get("giveback_pct", 0) or 0) for t in book_trades if float(t.get("giveback_pct", 0) or 0) > 0]
            score.giveback_rate = sum(givebacks) / max(1, len(givebacks)) if givebacks else 0.0

            if position_rows:
                count = sum(1 for p in position_rows if str(p.get("strategy_tag", "") or "") == tag)
                score.open_position_count = count
                score.capital_allocated = sum(
                    abs(float(p.get("notional", 0) or 0))
                    for p in position_rows
                    if str(p.get("strategy_tag", "") or "") == tag
                )

            scores.append(score)

        return sorted(scores, key=lambda s: s.realized_pnl, reverse=True)

    def _compute_mode_scores(
        self,
        trades: List[Dict],
        funnel_summary: Optional[Dict],
    ) -> List[ModeScore]:
        by_mode: Dict[str, List[Dict]] = defaultdict(list)
        for t in trades:
            mode = str(t.get("setup_mode", "unknown") or "unknown")
            by_mode[mode].append(t)

        scores = []
        for mode, mode_trades in by_mode.items():
            score = ModeScore(mode=mode)
            pnls = [float(t.get("pnl", 0) or 0) for t in mode_trades]
            score.trade_count = len(mode_trades)
            wins = [p for p in pnls if p > 0]
            score.win_rate = len(wins) / max(1, len(pnls))
            score.expectancy_per_trade = sum(pnls) / max(1, score.trade_count)

            slips = [float(t.get("slippage_bps", 0) or 0) for t in mode_trades]
            score.avg_slippage_bps = sum(slips) / max(1, len(slips))

            holds = [float(t.get("hold_seconds", 0) or 0) for t in mode_trades if float(t.get("hold_seconds", 0) or 0) > 0]
            score.avg_hold_seconds = sum(holds) / max(1, len(holds))

            if funnel_summary:
                mode_funnel = funnel_summary.get("by_mode", {}).get(mode, {})
                triggers_created = mode_funnel.get("pending_trigger", 0)
                triggers_fired = mode_funnel.get("trigger_fired", 0)
                entries = mode_funnel.get("entry_submitted", 0)
                score.trigger_conversion_rate = triggers_fired / max(1, triggers_created)
                score.entry_conversion_rate = entries / max(1, triggers_fired)

            session_pnl: Dict[str, float] = defaultdict(float)
            for t in mode_trades:
                sess = str(t.get("session_type", "regular") or "regular")
                session_pnl[sess] += float(t.get("pnl", 0) or 0)
            if session_pnl:
                score.best_session = max(session_pnl, key=session_pnl.get)

            scores.append(score)

        return sorted(scores, key=lambda s: s.expectancy_per_trade, reverse=True)

    @staticmethod
    def _iter_open_positions(
        open_positions: Optional[Dict[str, Dict] | List[Dict]],
    ) -> List[Dict]:
        if isinstance(open_positions, dict):
            return [dict(row or {}) for row in open_positions.values()]
        if isinstance(open_positions, list):
            return [dict(row or {}) for row in open_positions]
        return []

    def get_summary(self) -> Dict:
        return {
            "books": [s.to_dict() for s in self.book_scores],
            "modes": [s.to_dict() for s in self.mode_scores],
        }
