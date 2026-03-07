"""MACD histogram momentum indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import crossed_above, crossed_below, finalize_signal, macd
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class MACDHistogramIndicator(BaseIndicator):
    def name(self) -> str:
        return "macd_histogram"

    def param_grid(self):
        return [
            {"fast": 12, "slow": 26, "signal": 9},
            {"fast": 8, "slow": 17, "signal": 9},
            {"fast": 5, "slow": 13, "signal": 6},
        ]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"fast": 12, "slow": 26, "signal": 9}
        _, _, hist = macd(df["close"], params["fast"], params["slow"], params["signal"])
        zero = pd.Series(0.0, index=df.index)
        strength = (hist.abs() / pd.Series(df["close"], dtype=float).replace(0, pd.NA) * 30.0).fillna(0.0)
        return finalize_signal(df.index, crossed_above(hist, zero), crossed_below(hist, zero), strength, "long", self.name(), params)
