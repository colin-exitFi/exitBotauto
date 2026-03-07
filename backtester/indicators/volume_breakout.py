"""
Volume Momentum Breakout Strategy.

Breakout above N-day high on above-average volume = strong momentum signal.
This is what Velox primarily trades — momentum stocks with volume confirmation.

Buy: Price breaks above lookback-period high AND volume > 2x average
Sell: Price breaks below lookback-period low OR volume dries up

Source: TradingView community "Volume Momentum Breakout + TP/SL"
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class VolumeBreakoutIndicator(BaseIndicator):
    def __init__(self, lookback: int = 20, vol_mult: float = 2.0, vol_avg_period: int = 20):
        self.lookback = lookback
        self.vol_mult = vol_mult
        self.vol_avg_period = vol_avg_period

    def name(self) -> str:
        return "volume_breakout"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        high = df["high"]
        low = df["low"]
        volume = df["volume"]

        # Breakout levels
        highest_high = high.rolling(self.lookback).max().shift(1)  # Previous N bars
        lowest_low = low.rolling(self.lookback).min().shift(1)

        # Volume filter
        avg_volume = volume.rolling(self.vol_avg_period).mean()
        high_volume = volume > (avg_volume * self.vol_mult)

        # Buy: close above previous high on strong volume
        entries = (close > highest_high) & high_volume

        # Sell: close below previous low (breakdown)
        exits = close < lowest_low

        # Strength: volume relative to average
        vol_ratio = (volume / avg_volume.replace(0, np.nan)).clip(0, 5) / 5
        strength = vol_ratio.fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"vol_breakout_l{self.lookback}_v{self.vol_mult}x",
            params={"lookback": self.lookback, "vol_mult": self.vol_mult,
                    "vol_avg_period": self.vol_avg_period},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"lookback": 10, "vol_mult": 1.5, "vol_avg_period": 20},
            {"lookback": 20, "vol_mult": 2.0, "vol_avg_period": 20},
            {"lookback": 20, "vol_mult": 1.5, "vol_avg_period": 20},
            {"lookback": 5, "vol_mult": 2.0, "vol_avg_period": 10},
            {"lookback": 10, "vol_mult": 2.5, "vol_avg_period": 20},
        ]
