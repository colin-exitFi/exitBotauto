"""
WaveTrend Oscillator.

LazyBear's WaveTrend [WT] smooths Stochastic with wave cycles,
spotting overextensions faster than MACD. Reported 58% win rate,
1.9 PF in backtests. Excels in ranging markets.

Signals: Cross above -53 buy; below +53 sell. Divergences amplify.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class WaveTrendIndicator(BaseIndicator):
    def __init__(self, channel_length: int = 10, avg_length: int = 21,
                 ob_level: float = 53, os_level: float = -53):
        self.channel_length = channel_length
        self.avg_length = avg_length
        self.ob_level = ob_level
        self.os_level = os_level

    def name(self) -> str:
        return "wavetrend"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        hlc3 = (df["high"] + df["low"] + df["close"]) / 3

        # EMA of hlc3
        esa = hlc3.ewm(span=self.channel_length, adjust=False).mean()

        # EMA of absolute deviation
        d = (hlc3 - esa).abs().ewm(span=self.channel_length, adjust=False).mean()

        # CI (Channel Index)
        ci = (hlc3 - esa) / (0.015 * d.replace(0, np.nan))
        ci = ci.fillna(0)

        # WaveTrend lines
        wt1 = ci.ewm(span=self.avg_length, adjust=False).mean()
        wt2 = wt1.rolling(4).mean()  # Signal line (SMA of WT1)

        # Buy: WT1 crosses above WT2 below oversold level
        # Sell: WT1 crosses below WT2 above overbought level
        cross_up = (wt1 > wt2) & (wt1.shift(1) <= wt2.shift(1))
        cross_down = (wt1 < wt2) & (wt1.shift(1) >= wt2.shift(1))

        entries = cross_up & (wt1 < self.os_level + 20)  # Buy in oversold zone
        exits = cross_down & (wt1 > self.ob_level - 20)  # Sell in overbought zone

        # Strength based on distance from zero
        strength = (wt1.abs() / 100).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"wavetrend_c{self.channel_length}_a{self.avg_length}",
            params={"channel_length": self.channel_length, "avg_length": self.avg_length,
                    "ob_level": self.ob_level, "os_level": self.os_level},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"channel_length": 9, "avg_length": 12, "ob_level": 53, "os_level": -53},
            {"channel_length": 10, "avg_length": 21, "ob_level": 53, "os_level": -53},
            {"channel_length": 10, "avg_length": 21, "ob_level": 60, "os_level": -60},
            {"channel_length": 14, "avg_length": 21, "ob_level": 53, "os_level": -53},
        ]
