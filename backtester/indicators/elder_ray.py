"""
Elder Ray Index (Bull/Bear Power).

Dr. Alexander Elder's system combining EMA trend with bull/bear power.
Measures strength of buyers vs sellers relative to the EMA.

Buy: Bull power > 0 AND increasing, while EMA trending up
Sell: Bear power < 0 AND decreasing, while EMA trending down

One of the few indicators that directly measures buying/selling pressure.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class ElderRayIndicator(BaseIndicator):
    def __init__(self, ema_period: int = 13):
        self.ema_period = ema_period

    def name(self) -> str:
        return "elder_ray"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # EMA (trend reference)
        ema = close.ewm(span=self.ema_period, adjust=False).mean()

        # Bull Power: High - EMA (buyers pushing above consensus)
        bull_power = high - ema

        # Bear Power: Low - EMA (sellers pushing below consensus)
        bear_power = low - ema

        # EMA trend
        ema_rising = ema > ema.shift(1)
        ema_falling = ema < ema.shift(1)

        # Buy: EMA rising, bear power negative but increasing (sellers weakening)
        bear_rising = bear_power > bear_power.shift(1)
        entries = ema_rising & (bear_power < 0) & bear_rising

        # Sell: EMA falling, bull power positive but decreasing (buyers weakening)
        bull_falling = bull_power < bull_power.shift(1)
        exits = ema_falling & (bull_power > 0) & bull_falling

        # Strength: bull - bear power normalized
        total_power = bull_power - bear_power
        strength = (total_power / close).clip(-0.1, 0.1).add(0.1).mul(5).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"elder_ray_e{self.ema_period}",
            params={"ema_period": self.ema_period},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"ema_period": 9},
            {"ema_period": 13},   # Classic Elder
            {"ema_period": 21},
            {"ema_period": 26},
        ]
