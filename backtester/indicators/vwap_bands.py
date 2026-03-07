"""VWAP bands breakout indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import crossed_above, crossed_below, daily_vwap, finalize_signal
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class VWAPBandsIndicator(BaseIndicator):
    def name(self) -> str:
        return "vwap_bands"

    def param_grid(self):
        return [{"std_mult": 1.0}, {"std_mult": 1.5}, {"std_mult": 2.0}]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"std_mult": 1.5}
        close = pd.Series(df["close"], dtype=float)
        vwap = daily_vwap(df).fillna(close)
        std = close.groupby(close.index.normalize()).transform(lambda s: s.expanding().std()).fillna(0.0)
        upper = vwap + std * float(params["std_mult"])
        lower = vwap - std * float(params["std_mult"])
        entries = crossed_above(close, upper)
        exits = crossed_below(close, vwap) | crossed_below(close, lower)
        strength = ((close - vwap).abs() / close.replace(0, pd.NA)) * 20.0
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
