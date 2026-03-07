import unittest

from backtester.engine import BacktestResult
from backtester.scorer import StrategyScorer


def _result(symbol="AAPL", sharpe=1.0, trades=40, pnl=100.0, profit_factor=1.5, win_rate=0.55):
    return BacktestResult(
        symbol=symbol,
        indicator_name="supertrend",
        params={"period": 10},
        side="long",
        total_trades=trades,
        win_rate=win_rate,
        profit_factor=profit_factor,
        total_pnl=pnl,
        total_return_pct=5.0,
        sharpe_ratio=sharpe,
        sortino_ratio=1.0,
        max_drawdown_pct=10.0,
        avg_drawdown_pct=4.0,
        calmar_ratio=1.2,
        avg_win_pct=2.0,
        avg_loss_pct=-1.0,
        avg_hold_bars=4.0,
        best_trade_pct=5.0,
        worst_trade_pct=-2.0,
        win_rate_first_half=0.55,
        win_rate_second_half=0.52,
        pnl_first_half=50.0,
        pnl_second_half=40.0,
        regime_stability=0.93,
        start_date="2026-01-01",
        end_date="2026-03-01",
        total_bars=200,
        timeframe="1day",
    )


class ScorerTests(unittest.TestCase):
    def test_minimum_filters_zero_out_weak_results(self):
        scorer = StrategyScorer()
        self.assertEqual(scorer.score(_result(trades=10)), 0.0)
        self.assertEqual(scorer.score(_result(win_rate=0.2)), 0.0)
        self.assertEqual(scorer.score(_result(profit_factor=0.8)), 0.0)

    def test_rank_orders_by_score(self):
        scorer = StrategyScorer(min_symbols_profitable=2)
        results = [
            _result(symbol="AAPL", sharpe=1.8, pnl=200.0),
            _result(symbol="MSFT", sharpe=1.6, pnl=180.0),
            _result(symbol="TSLA", sharpe=0.2, pnl=10.0, profit_factor=1.0, win_rate=0.4),
        ]
        ranked = scorer.rank(results)
        self.assertGreaterEqual(ranked[0]["score"], ranked[1]["score"])
        self.assertEqual(ranked[0]["rank"], 1)

    def test_min_symbols_profitable_gate_applies(self):
        scorer = StrategyScorer(min_symbols_profitable=3)
        results = [
            _result(symbol="AAPL", pnl=200.0),
            _result(symbol="MSFT", pnl=150.0),
            _result(symbol="TSLA", pnl=-50.0),
        ]
        ranked = scorer.rank(results)
        self.assertEqual(ranked[0]["score"], 0.0)


if __name__ == "__main__":
    unittest.main()
