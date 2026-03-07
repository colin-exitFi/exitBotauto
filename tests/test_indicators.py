import unittest

import pandas as pd

from backtester.indicators import IndicatorRegistry


class IndicatorTests(unittest.TestCase):
    def setUp(self):
        index = pd.date_range("2026-01-01", periods=120, freq="h", tz="UTC")
        close = pd.Series([100 + (i * 0.4) for i in range(120)], index=index)
        self.df = pd.DataFrame(
            {
                "open": close.shift(1).fillna(close.iloc[0]),
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": [1_000_000 + (i * 1000) for i in range(120)],
                "vwap": close - 0.2,
            },
            index=index,
        )

    def test_all_registered_indicators_produce_aligned_signals(self):
        indicators = IndicatorRegistry.instantiate_all()
        self.assertGreaterEqual(len(indicators), 10)

        for indicator in indicators:
            signal = indicator.generate_signals(self.df)
            self.assertEqual(len(signal.entries), len(self.df), indicator.name())
            self.assertEqual(len(signal.exits), len(self.df), indicator.name())
            self.assertEqual(len(signal.signal_strength), len(self.df), indicator.name())
            self.assertTrue(signal.entries.dtype == bool)
            self.assertTrue(signal.exits.dtype == bool)
            self.assertGreaterEqual(len(indicator.param_grid()), 1)

    def test_ema_indicator_fires_on_clear_trend_shift(self):
        df = self.df.copy()
        df["close"] = pd.Series([100] * 20 + [100 + i for i in range(100)], index=df.index)
        signal = IndicatorRegistry.get("ema_crossover")().generate_signals(df)
        self.assertTrue(bool(signal.entries.any()))


if __name__ == "__main__":
    unittest.main()
