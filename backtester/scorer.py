"""Backtest result scoring and ranking."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Dict, List

from backtester.engine import BacktestResult


class StrategyScorer:
    """Score and rank indicator backtests."""

    WEIGHTS = {
        "sharpe_ratio": 0.25,
        "profit_factor": 0.20,
        "win_rate": 0.15,
        "edge_stability": 0.20,
        "max_drawdown_penalty": 0.10,
        "trade_frequency": 0.10,
    }

    def __init__(self, min_symbols_profitable: int = 5):
        self.min_symbols_profitable = max(1, int(min_symbols_profitable or 1))

    def score(self, result: BacktestResult) -> float:
        if int(result.total_trades or 0) < 30:
            return 0.0
        if float(result.win_rate or 0.0) < 0.40:
            return 0.0
        if float(result.profit_factor or 0.0) < 1.0:
            return 0.0
        if float(result.sharpe_ratio or 0.0) < 0.0:
            return 0.0

        sharpe = min(1.0, max(0.0, float(result.sharpe_ratio or 0.0) / 3.0))
        profit_factor = min(1.0, max(0.0, float(result.profit_factor or 0.0) / 3.0))
        win_rate = min(1.0, max(0.0, float(result.win_rate or 0.0)))
        edge_stability = min(1.0, max(0.0, float(result.regime_stability or 0.0)))
        drawdown_penalty = min(1.0, max(0.0, 1.0 - (abs(float(result.max_drawdown_pct or 0.0)) / 50.0)))
        trade_frequency = min(1.0, max(0.0, float(result.total_trades or 0) / max(30.0, float(result.total_bars or 1) * 0.2)))

        weighted = (
            sharpe * self.WEIGHTS["sharpe_ratio"]
            + profit_factor * self.WEIGHTS["profit_factor"]
            + win_rate * self.WEIGHTS["win_rate"]
            + edge_stability * self.WEIGHTS["edge_stability"]
            + drawdown_penalty * self.WEIGHTS["max_drawdown_penalty"]
            + trade_frequency * self.WEIGHTS["trade_frequency"]
        )
        return round(weighted * 100.0, 2)

    def rank(self, results: List[BacktestResult]) -> List[Dict]:
        if not results:
            return []

        profitable_counts = defaultdict(set)
        symbols = {result.symbol for result in results if result.symbol}
        min_profitable = min(len(symbols), self.min_symbols_profitable) or 1

        for result in results:
            if result.total_trades < 10:
                continue
            if result.total_pnl <= 0 or result.profit_factor < 1.0:
                continue
            profitable_counts[self._group_key(result)].add(result.symbol)

        ranked = []
        for result in results:
            if len(profitable_counts[self._group_key(result)]) < min_profitable:
                score = 0.0
            else:
                score = self.score(result)
            ranked.append(
                {
                    "score": score,
                    "result": result,
                    "symbols_profitable": len(profitable_counts[self._group_key(result)]),
                }
            )

        ranked.sort(
            key=lambda row: (
                row["score"],
                float(getattr(row["result"], "sharpe_ratio", 0.0) or 0.0),
                float(getattr(row["result"], "profit_factor", 0.0) or 0.0),
            ),
            reverse=True,
        )
        for idx, row in enumerate(ranked, start=1):
            row["rank"] = idx
        return ranked

    @staticmethod
    def _group_key(result: BacktestResult) -> str:
        return "|".join(
            [
                str(result.indicator_name),
                json.dumps(result.params or {}, sort_keys=True),
                str(result.side or "long"),
            ]
        )

