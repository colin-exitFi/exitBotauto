"""
Mean Reversion Z-Score Strategy.

When price moves >2 standard deviations from its rolling mean,
bet on reversion. Opposite of momentum — works in ranging markets
where breakout strategies fail.

Buy: Z-Score < -2 and turning up (reversal signal)
Sell: Z-Score > +2 and turning down
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class MeanReversionZScoreIndicator(BaseIndicator):
    def __init__(self, lookback: int = 20, entry_z: float = -2.0, exit_z: float = 0.0):
        self.lookback = lookback
        self.entry_z = entry_z
        self.exit_z = exit_z

    def name(self) -> str:
        return "mean_reversion_zscore"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]

        # Z-Score: (price - rolling_mean) / rolling_std
        rolling_mean = close.rolling(self.lookback).mean()
        rolling_std = close.rolling(self.lookback).std().replace(0, np.nan)
        zscore = (close - rolling_mean) / rolling_std

        # Buy: z-score was below entry threshold and is turning up
        z_turning_up = zscore > zscore.shift(1)
        entries = (zscore < self.entry_z) & z_turning_up

        # Exit: z-score crosses above exit threshold (returned to mean)
        exits = (zscore > self.exit_z) & (zscore.shift(1) <= self.exit_z)

        strength = (zscore.abs() / 3).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"mean_rev_z{self.lookback}_e{abs(self.entry_z)}",
            params={"lookback": self.lookback, "entry_z": self.entry_z, "exit_z": self.exit_z},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"lookback": 14, "entry_z": -2.0, "exit_z": 0.0},
            {"lookback": 20, "entry_z": -2.0, "exit_z": 0.0},
            {"lookback": 20, "entry_z": -1.5, "exit_z": 0.0},
            {"lookback": 30, "entry_z": -2.0, "exit_z": 0.5},
            {"lookback": 50, "entry_z": -2.0, "exit_z": 0.0},
        ]
