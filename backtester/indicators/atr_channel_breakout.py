"""
ATR Channel Breakout (Turtle Trading variant).

Classic trend-following system based on the Turtle Traders. Uses ATR
for dynamic channel width that adapts to volatility.

Buy: Price breaks above EMA + (ATR * multiplier)
Sell: Price breaks below EMA - (ATR * multiplier) or trails back to EMA

Source: Original Turtle Trading Rules (Richard Dennis, 1983)
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class ATRChannelBreakoutIndicator(BaseIndicator):
    def __init__(self, ema_period: int = 20, atr_period: int = 14, atr_mult: float = 2.0):
        self.ema_period = ema_period
        self.atr_period = atr_period
        self.atr_mult = atr_mult

    def name(self) -> str:
        return "atr_channel_breakout"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # EMA midline
        ema = close.ewm(span=self.ema_period, adjust=False).mean()

        # ATR
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.atr_period).mean()

        # Channels
        upper = ema + atr * self.atr_mult
        lower = ema - atr * self.atr_mult

        # Buy: close breaks above upper channel
        entries = (close > upper) & (close.shift(1) <= upper.shift(1))

        # Sell: close breaks below EMA (trail to midline)
        exits = (close < ema) & (close.shift(1) >= ema.shift(1))

        # Strength: distance above channel as fraction of ATR
        dist_from_upper = ((close - upper) / atr.replace(0, np.nan)).clip(0, 3) / 3
        strength = dist_from_upper.fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"atr_channel_e{self.ema_period}_a{self.atr_period}_m{self.atr_mult}",
            params={"ema_period": self.ema_period, "atr_period": self.atr_period,
                    "atr_mult": self.atr_mult},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"ema_period": 20, "atr_period": 14, "atr_mult": 1.5},
            {"ema_period": 20, "atr_period": 14, "atr_mult": 2.0},
            {"ema_period": 20, "atr_period": 14, "atr_mult": 2.5},
            {"ema_period": 10, "atr_period": 10, "atr_mult": 2.0},
            {"ema_period": 50, "atr_period": 20, "atr_mult": 2.0},
        ]
