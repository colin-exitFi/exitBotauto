"""
Donchian Channel Breakout.

The original Turtle Trading entry system. Buy when price exceeds the
highest high of the last N bars. Simple but historically one of the
most robust trend-following systems.

Buy: Close > highest high of last N bars
Sell: Close < lowest low of last N/2 bars (faster exit than entry)
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class DonchianBreakoutIndicator(BaseIndicator):
    def __init__(self, entry_period: int = 20, exit_period: int = 10):
        self.entry_period = entry_period
        self.exit_period = exit_period

    def name(self) -> str:
        return "donchian_breakout"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Entry channel: N-bar high/low
        upper_entry = high.rolling(self.entry_period).max().shift(1)
        lower_entry = low.rolling(self.entry_period).min().shift(1)

        # Exit channel: N/2-bar low
        lower_exit = low.rolling(self.exit_period).min().shift(1)

        # Buy: close breaks above upper channel
        entries = (close > upper_entry) & (close.shift(1) <= upper_entry.shift(1))

        # Sell: close breaks below exit channel
        exits = (close < lower_exit) & (close.shift(1) >= lower_exit.shift(1))

        # Strength: how far above the channel
        channel_width = upper_entry - lower_entry
        strength = ((close - upper_entry) / channel_width.replace(0, np.nan)).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"donchian_e{self.entry_period}_x{self.exit_period}",
            params={"entry_period": self.entry_period, "exit_period": self.exit_period},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"entry_period": 10, "exit_period": 5},
            {"entry_period": 20, "exit_period": 10},
            {"entry_period": 55, "exit_period": 20},  # Original Turtle system
            {"entry_period": 20, "exit_period": 5},   # Fast exit variant
        ]
