"""Bollinger squeeze breakout indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import bollinger, finalize_signal
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class BollingerSqueezeIndicator(BaseIndicator):
    def name(self) -> str:
        return "bollinger_squeeze"

    def param_grid(self):
        return [
            {"period": 14, "std_mult": 1.5},
            {"period": 20, "std_mult": 2.0},
            {"period": 30, "std_mult": 2.5},
        ]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"period": 20, "std_mult": 2.0}
        close = pd.Series(df["close"], dtype=float)
        mid, upper, lower, bandwidth = bollinger(close, params["period"], params["std_mult"])
        squeeze = bandwidth < 0.04
        entries = squeeze.shift(1).fillna(False) & (close > upper)
        exits = (close < mid) | (squeeze.shift(1).fillna(False) & (close < lower))
        strength = ((0.04 - bandwidth).clip(lower=0.0) / 0.04).clip(0.0, 1.0)
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
