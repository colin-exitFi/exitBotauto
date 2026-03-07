"""RSI cross and divergence indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import crossed_above, crossed_below, finalize_signal, rsi
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class RSIDivergenceIndicator(BaseIndicator):
    def name(self) -> str:
        return "rsi_divergence"

    def param_grid(self):
        return [{"period": 7}, {"period": 14}, {"period": 21}]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"period": 14}
        close = pd.Series(df["close"], dtype=float)
        rsi_series = rsi(close, params["period"])
        bullish_div = (
            (close < close.shift(1).rolling(5).min()) & (rsi_series > rsi_series.shift(1).rolling(5).min())
        ).astype(bool)
        bearish_div = (
            (close > close.shift(1).rolling(5).max()) & (rsi_series < rsi_series.shift(1).rolling(5).max())
        ).astype(bool)
        entries = crossed_above(rsi_series, pd.Series(30.0, index=df.index)) | bullish_div
        exits = crossed_below(rsi_series, pd.Series(70.0, index=df.index)) | bearish_div
        strength = ((50.0 - (rsi_series - 50.0).abs()) / 50.0).clip(lower=0.0, upper=1.0)
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
