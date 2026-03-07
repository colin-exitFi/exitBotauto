"""
Squeeze Momentum Indicator (LazyBear variant).

Detects when Bollinger Bands contract inside Keltner Channels (squeeze),
then fires on the breakout direction using momentum histogram.
One of the most popular TradingView community indicators.

Squeeze ON: BB inside KC (consolidation)
Squeeze OFF: BB outside KC (breakout)
Buy: Squeeze fires OFF + momentum histogram turns positive
Sell: Momentum histogram turns negative

Source: LazyBear's "Squeeze Momentum Indicator" on TradingView (100K+ users)
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class SqueezeMomentumIndicator(BaseIndicator):
    def __init__(self, bb_length: int = 20, bb_mult: float = 2.0,
                 kc_length: int = 20, kc_mult: float = 1.5):
        self.bb_length = bb_length
        self.bb_mult = bb_mult
        self.kc_length = kc_length
        self.kc_mult = kc_mult

    def name(self) -> str:
        return "squeeze_momentum"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # Bollinger Bands
        bb_mid = close.rolling(self.bb_length).mean()
        bb_std = close.rolling(self.bb_length).std()
        bb_upper = bb_mid + bb_std * self.bb_mult
        bb_lower = bb_mid - bb_std * self.bb_mult

        # Keltner Channels
        kc_mid = close.ewm(span=self.kc_length, adjust=False).mean()
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.kc_length).mean()
        kc_upper = kc_mid + atr * self.kc_mult
        kc_lower = kc_mid - atr * self.kc_mult

        # Squeeze detection
        squeeze_on = (bb_lower > kc_lower) & (bb_upper < kc_upper)
        squeeze_off = ~squeeze_on

        # Momentum (linear regression of (close - midline))
        # Simplified: use close - (highest+lowest)/2 smoothed
        midline = (high.rolling(self.kc_length).max() + low.rolling(self.kc_length).min()) / 2
        momentum = close - (midline + bb_mid) / 2

        # Smooth momentum
        mom_smooth = momentum.rolling(5).mean()

        # Squeeze just fired (was on, now off)
        squeeze_fire = squeeze_off & squeeze_on.shift(1).fillna(False)

        # Buy: squeeze fires + momentum positive and increasing
        mom_positive = mom_smooth > 0
        mom_increasing = mom_smooth > mom_smooth.shift(1)
        entries = (squeeze_fire | (squeeze_off & squeeze_on.shift(2).fillna(False))) & mom_positive & mom_increasing

        # Sell: momentum turns negative
        exits = (mom_smooth < 0) & (mom_smooth.shift(1) >= 0)

        strength = (mom_smooth.abs() / close * 100).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"squeeze_mom_bb{self.bb_length}_kc{self.kc_mult}",
            params={"bb_length": self.bb_length, "bb_mult": self.bb_mult,
                    "kc_length": self.kc_length, "kc_mult": self.kc_mult},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"bb_length": 20, "bb_mult": 2.0, "kc_length": 20, "kc_mult": 1.5},
            {"bb_length": 20, "bb_mult": 2.0, "kc_length": 20, "kc_mult": 2.0},
            {"bb_length": 14, "bb_mult": 2.0, "kc_length": 14, "kc_mult": 1.5},
            {"bb_length": 20, "bb_mult": 1.5, "kc_length": 20, "kc_mult": 1.0},
        ]
