import unittest

import pandas as pd

from backtester.engine import BacktestEngine
from backtester.indicators.registry import IndicatorSignal


class _OneShotIndicator:
    def __init__(self):
        self.params = {"test": 1}

    def __class__(self):
        return _OneShotIndicator


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.index = pd.date_range("2026-01-01", periods=6, freq="D", tz="UTC")
        self.df = pd.DataFrame(
            {
                "open": [100, 100, 104, 108, 109, 110],
                "high": [101, 105, 109, 110, 111, 112],
                "low": [99, 99, 103, 107, 108, 109],
                "close": [100, 104, 108, 109, 110, 111],
                "volume": [1000] * 6,
                "vwap": [100, 103, 107, 108, 109, 110],
            },
            index=self.index,
        )

    def test_single_run_returns_metrics(self):
        engine = BacktestEngine(initial_capital=10_000, slippage_pct=0.0)
        signal = IndicatorSignal(
            entries=pd.Series([False, True, False, False, False, False], index=self.index),
            exits=pd.Series([False, False, False, True, False, False], index=self.index),
            signal_strength=pd.Series([0, 1, 0, 0, 0, 0], index=self.index, dtype=float),
            side="long",
            name="test_indicator",
            params={"window": 1},
        )

        result = engine.run_single(self.df, signal, "AAPL")

        self.assertEqual(result.symbol, "AAPL")
        self.assertEqual(result.total_trades, 1)
        self.assertGreater(result.total_pnl, 0)
        self.assertGreaterEqual(result.total_return_pct, 0)

    def test_batch_runs_all_indicator_symbol_pairs(self):
        engine = BacktestEngine(initial_capital=10_000, slippage_pct=0.0)

        class _Indicator:
            def __init__(self, offset=0):
                self.params = {"offset": offset}
                self.offset = offset

            def param_grid(self):
                return [{"offset": 0}, {"offset": 1}]

            def generate_signals(self, df):
                return IndicatorSignal(
                    entries=pd.Series([False, True, False, False, False, False], index=df.index),
                    exits=pd.Series([False, False, False, True, False, False], index=df.index),
                    signal_strength=pd.Series([0, 1, 0, 0, 0, 0], index=df.index, dtype=float),
                    side="long",
                    name="mock",
                    params=self.params,
                )

        universe = {"AAPL": self.df, "MSFT": self.df}
        results = engine.run_batch(universe, [_Indicator()])
        self.assertEqual(len(results), 4)


if __name__ == "__main__":
    unittest.main()
