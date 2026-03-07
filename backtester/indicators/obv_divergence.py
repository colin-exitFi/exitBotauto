"""
On-Balance Volume (OBV) Divergence.

OBV tracks cumulative volume flow. When price makes new lows but OBV
doesn't, smart money is accumulating. When price makes new highs but
OBV doesn't, distribution is happening.

Buy: Bullish divergence — price makes lower low, OBV makes higher low
Sell: Bearish divergence — price makes higher high, OBV makes lower high

Source: Joe Granville's original OBV theory (1963) + divergence trading
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class OBVDivergenceIndicator(BaseIndicator):
    def __init__(self, lookback: int = 14, smooth: int = 5):
        self.lookback = lookback
        self.smooth = smooth

    def name(self) -> str:
        return "obv_divergence"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        volume = df["volume"]

        # OBV calculation
        obv = (volume * np.sign(close.diff())).fillna(0).cumsum()

        # Smooth OBV for cleaner divergence detection
        obv_smooth = obv.rolling(self.smooth).mean()

        # Rolling highs/lows for divergence
        price_high = close.rolling(self.lookback).max()
        price_low = close.rolling(self.lookback).min()
        obv_high = obv_smooth.rolling(self.lookback).max()
        obv_low = obv_smooth.rolling(self.lookback).min()

        # Price at new low but OBV not at new low = bullish divergence
        price_at_low = close <= price_low * 1.01  # Within 1% of low
        obv_above_low = obv_smooth > obv_low * 1.02  # OBV not making new low

        # Price at new high but OBV not at new high = bearish divergence
        price_at_high = close >= price_high * 0.99
        obv_below_high = obv_smooth < obv_high * 0.98

        # OBV trend confirmation
        obv_rising = obv_smooth > obv_smooth.shift(3)

        # Buy: bullish divergence + OBV turning up
        entries = price_at_low & obv_above_low & obv_rising

        # Sell: bearish divergence
        exits = price_at_high & obv_below_high & (~obv_rising)

        # Strength: volume trend
        vol_ratio = volume / volume.rolling(20).mean().replace(0, np.nan)
        strength = (vol_ratio / 3).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"obv_div_l{self.lookback}_s{self.smooth}",
            params={"lookback": self.lookback, "smooth": self.smooth},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"lookback": 10, "smooth": 3},
            {"lookback": 14, "smooth": 5},
            {"lookback": 20, "smooth": 5},
            {"lookback": 20, "smooth": 10},
        ]
