"""
Range Filter Strategy.

Smooths price action using a dynamic range filter based on average range.
Generates cleaner trend signals than raw price by filtering out noise.
Popular TradingView indicator with multiple high-rated variants.

Buy: Filtered price crosses above its previous value (uptrend starts)
Sell: Filtered price crosses below its previous value (downtrend starts)

Source: guikroth's "Range Filter" on TradingView
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class RangeFilterIndicator(BaseIndicator):
    def __init__(self, period: int = 50, multiplier: float = 3.0):
        self.period = period
        self.multiplier = multiplier

    def name(self) -> str:
        return "range_filter"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Average range
        avg_range = (high - low).ewm(span=self.period, adjust=False).mean() * self.multiplier

        # Range filter
        rf = pd.Series(np.nan, index=df.index)
        rf.iloc[0] = close.iloc[0]

        for i in range(1, len(close)):
            prev_rf = rf.iloc[i-1]
            curr_close = close.iloc[i]
            curr_range = avg_range.iloc[i]

            if np.isnan(prev_rf) or np.isnan(curr_range):
                rf.iloc[i] = curr_close
                continue

            if curr_close > prev_rf:
                rf.iloc[i] = max(prev_rf, curr_close - curr_range)
            elif curr_close < prev_rf:
                rf.iloc[i] = min(prev_rf, curr_close + curr_range)
            else:
                rf.iloc[i] = prev_rf

        # Filter direction
        rf_up = rf > rf.shift(1)
        rf_down = rf < rf.shift(1)

        # Entries: filter direction changes to up
        entries = rf_up & (~rf_up.shift(1, fill_value=False))
        exits = rf_down & (~rf_down.shift(1, fill_value=False))

        # Strength: distance from filter line
        strength = ((close - rf).abs() / close).clip(0, 0.1).mul(10).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"range_filter_p{self.period}_m{self.multiplier}",
            params={"period": self.period, "multiplier": self.multiplier},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"period": 20, "multiplier": 2.0},
            {"period": 50, "multiplier": 3.0},
            {"period": 50, "multiplier": 2.0},
            {"period": 100, "multiplier": 3.0},
        ]
