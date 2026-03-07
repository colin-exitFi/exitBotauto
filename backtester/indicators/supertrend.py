"""ATR-based supertrend indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import atr, finalize_signal
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class SupertrendIndicator(BaseIndicator):
    def name(self) -> str:
        return "supertrend"

    def param_grid(self):
        return [
            {"period": 7, "multiplier": 2.0},
            {"period": 10, "multiplier": 3.0},
            {"period": 14, "multiplier": 4.0},
        ]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"period": 10, "multiplier": 3.0}
        high = pd.Series(df["high"], dtype=float)
        low = pd.Series(df["low"], dtype=float)
        close = pd.Series(df["close"], dtype=float)
        atr_series = atr(df, params["period"]).bfill().fillna(0.0)
        hl2 = (high + low) / 2.0
        upper_band = hl2 + float(params["multiplier"]) * atr_series
        lower_band = hl2 - float(params["multiplier"]) * atr_series
        trend = pd.Series(index=df.index, dtype=int)
        trend.iloc[0] = 1
        for i in range(1, len(df)):
            if close.iloc[i] > upper_band.iloc[i - 1]:
                trend.iloc[i] = 1
            elif close.iloc[i] < lower_band.iloc[i - 1]:
                trend.iloc[i] = -1
            else:
                trend.iloc[i] = trend.iloc[i - 1]
                if trend.iloc[i] > 0:
                    lower_band.iloc[i] = max(lower_band.iloc[i], lower_band.iloc[i - 1])
                else:
                    upper_band.iloc[i] = min(upper_band.iloc[i], upper_band.iloc[i - 1])
        entries = (trend > 0) & (trend.shift(1) <= 0)
        exits = (trend < 0) & (trend.shift(1) >= 0)
        strength = (atr_series / close.replace(0, pd.NA) * 20.0).fillna(0.0).clip(0.0, 1.0)
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
