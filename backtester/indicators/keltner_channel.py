"""Keltner channel breakout indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import atr, ema, finalize_signal
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class KeltnerChannelIndicator(BaseIndicator):
    def name(self) -> str:
        return "keltner_channel"

    def param_grid(self):
        grids = []
        for ema_period in (20, 30):
            for atr_mult in (1.5, 2.0, 2.5):
                for atr_period in (10, 14, 20):
                    grids.append({"ema_period": ema_period, "atr_mult": atr_mult, "atr_period": atr_period})
        return grids

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"ema_period": 20, "atr_mult": 2.0, "atr_period": 14}
        close = pd.Series(df["close"], dtype=float)
        center = ema(close, params["ema_period"])
        atr_series = atr(df, params["atr_period"]).fillna(0.0)
        upper = center + atr_series * float(params["atr_mult"])
        lower = center - atr_series * float(params["atr_mult"])
        entries = (close > upper) & (close.shift(1) <= upper.shift(1))
        exits = (close < lower) & (close.shift(1) >= lower.shift(1))
        strength = (atr_series * float(params["atr_mult"]) / close.replace(0, pd.NA) * 20.0).fillna(0.0)
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
