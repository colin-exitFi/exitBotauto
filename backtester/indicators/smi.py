"""
Stochastic Momentum Index (SMI).

Smoother than regular Stochastic, oscillates -100 to +100 for
precise reversal detection. Reported 57% win rate, 1.8 PF.

Signals: Cross above -40 buy; cross below +40 sell.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class SMIIndicator(BaseIndicator):
    def __init__(self, k_length: int = 10, d_length: int = 3,
                 smooth_length: int = 3, ob: float = 40, os: float = -40):
        self.k_length = k_length
        self.d_length = d_length
        self.smooth_length = smooth_length
        self.ob = ob
        self.os = os

    def name(self) -> str:
        return "smi"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        high = df["high"]
        low = df["low"]
        close = df["close"]

        # Highest high and lowest low over k_length
        hh = high.rolling(self.k_length).max()
        ll = low.rolling(self.k_length).min()

        # Distance of close from midpoint of range
        diff = close - (hh + ll) / 2
        range_hl = hh - ll

        # Double smooth
        diff_smooth = diff.ewm(span=self.d_length, adjust=False).mean().ewm(span=self.smooth_length, adjust=False).mean()
        range_smooth = range_hl.ewm(span=self.d_length, adjust=False).mean().ewm(span=self.smooth_length, adjust=False).mean()

        # SMI
        smi = 100 * diff_smooth / (range_smooth / 2).replace(0, np.nan)
        smi = smi.fillna(0)

        # Signal line
        smi_signal = smi.ewm(span=self.d_length, adjust=False).mean()

        # Buy: SMI crosses above signal below oversold
        # Sell: SMI crosses below signal above overbought
        cross_up = (smi > smi_signal) & (smi.shift(1) <= smi_signal.shift(1))
        cross_down = (smi < smi_signal) & (smi.shift(1) >= smi_signal.shift(1))

        entries = cross_up & (smi < self.os + 20)
        exits = cross_down & (smi > self.ob - 20)

        strength = (smi.abs() / 100).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"smi_k{self.k_length}_d{self.d_length}",
            params={"k_length": self.k_length, "d_length": self.d_length,
                    "smooth_length": self.smooth_length, "ob": self.ob, "os": self.os},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"k_length": 10, "d_length": 3, "smooth_length": 3, "ob": 40, "os": -40},
            {"k_length": 14, "d_length": 3, "smooth_length": 3, "ob": 40, "os": -40},
            {"k_length": 10, "d_length": 5, "smooth_length": 5, "ob": 40, "os": -40},
            {"k_length": 5, "d_length": 3, "smooth_length": 3, "ob": 40, "os": -40},
        ]
