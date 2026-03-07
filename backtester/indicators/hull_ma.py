"""Hull moving average direction-change indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import finalize_signal, hull_ma
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class HullMAIndicator(BaseIndicator):
    def name(self) -> str:
        return "hull_ma"

    def param_grid(self):
        return [{"period": 9}, {"period": 16}, {"period": 25}, {"period": 49}]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"period": 16}
        hma = hull_ma(df["close"], params["period"])
        slope = hma.diff()
        entries = (slope > 0) & (slope.shift(1) <= 0)
        exits = (slope < 0) & (slope.shift(1) >= 0)
        strength = (slope.abs() / pd.Series(df["close"], dtype=float).replace(0, pd.NA) * 30.0).fillna(0.0)
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
