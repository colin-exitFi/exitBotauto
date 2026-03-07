"""Volume profile proxy using rolling high-volume node breakouts."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import finalize_signal
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class VolumeProfileIndicator(BaseIndicator):
    def name(self) -> str:
        return "volume_profile"

    def param_grid(self):
        return [{"lookback": 20}, {"lookback": 50}, {"lookback": 100}]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"lookback": 50}
        close = pd.Series(df["close"], dtype=float)
        volume = pd.Series(df["volume"], dtype=float).fillna(0.0)
        lookback = max(2, int(params["lookback"]))
        node = ((close * volume).rolling(lookback).sum() / volume.rolling(lookback).sum().replace(0, pd.NA)).fillna(close)
        vol_ratio = (volume / volume.rolling(lookback).mean().replace(0, pd.NA)).fillna(0.0)
        entries = (close > node) & (close.shift(1) <= node.shift(1)) & (vol_ratio > 1.2)
        exits = (close < node) & (close.shift(1) >= node.shift(1)) & (vol_ratio > 1.0)
        strength = (vol_ratio / 3.0).clip(0.0, 1.0)
        return finalize_signal(df.index, entries, exits, strength, "long", self.name(), params)
