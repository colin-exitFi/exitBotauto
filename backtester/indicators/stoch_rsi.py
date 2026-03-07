"""Stochastic RSI momentum indicator."""

from __future__ import annotations

from backtester.indicators.common import finalize_signal, stochastic_rsi
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class StochRSIIndicator(BaseIndicator):
    def name(self) -> str:
        return "stoch_rsi"

    def param_grid(self):
        return [{"rsi_period": 14, "stoch_period": 14, "k_smooth": 3, "d_smooth": 3}]

    def generate_signals(self, df):
        params = self.params or {"rsi_period": 14, "stoch_period": 14, "k_smooth": 3, "d_smooth": 3}
        _, k, d = stochastic_rsi(df["close"], params["rsi_period"], params["stoch_period"], params["k_smooth"], params["d_smooth"])
        entries = (k > 20) & (k.shift(1) <= 20) & (k >= d)
        exits = (k < 80) & (k.shift(1) >= 80) & (k <= d)
        strength = (k / 100.0).clip(0.0, 1.0)
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
