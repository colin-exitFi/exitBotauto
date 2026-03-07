"""
Ichimoku Cloud Strategy.

Classic Japanese trend-following system. Provides support/resistance,
momentum, and trend direction all in one indicator.

Buy: Price crosses above the cloud (both Senkou spans).
Sell: Price crosses below the cloud.
Best in trending markets; avoid choppy/sideways conditions.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class IchimokuIndicator(BaseIndicator):
    def __init__(self, tenkan: int = 9, kijun: int = 26, senkou_b: int = 52):
        self.tenkan = tenkan
        self.kijun = kijun
        self.senkou_b = senkou_b

    def name(self) -> str:
        return "ichimoku"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        high = df["high"]
        low = df["low"]
        close = df["close"]

        # Tenkan-sen (Conversion Line)
        tenkan_sen = (high.rolling(self.tenkan).max() + low.rolling(self.tenkan).min()) / 2

        # Kijun-sen (Base Line)
        kijun_sen = (high.rolling(self.kijun).max() + low.rolling(self.kijun).min()) / 2

        # Senkou Span A (Leading Span A) — shifted forward by kijun periods
        senkou_a = ((tenkan_sen + kijun_sen) / 2).shift(self.kijun)

        # Senkou Span B (Leading Span B) — shifted forward by kijun periods
        senkou_b = ((high.rolling(self.senkou_b).max() + low.rolling(self.senkou_b).min()) / 2).shift(self.kijun)

        # Cloud top and bottom
        cloud_top = pd.concat([senkou_a, senkou_b], axis=1).max(axis=1)
        cloud_bottom = pd.concat([senkou_a, senkou_b], axis=1).min(axis=1)

        # Buy: price crosses above cloud top
        # Sell: price crosses below cloud bottom
        above_cloud = close > cloud_top
        below_cloud = close < cloud_bottom

        entries = above_cloud & (~above_cloud.shift(1).fillna(False))
        exits = below_cloud & (~below_cloud.shift(1).fillna(False))

        # TK cross adds confirmation
        tk_bullish = tenkan_sen > kijun_sen

        # Strength: distance from cloud as % of price, boosted by TK cross
        distance = ((close - cloud_top) / close).clip(0, 0.1) * 10
        strength = distance.where(tk_bullish, distance * 0.5).fillna(0).clip(0, 1)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"ichimoku_t{self.tenkan}_k{self.kijun}_s{self.senkou_b}",
            params={"tenkan": self.tenkan, "kijun": self.kijun, "senkou_b": self.senkou_b},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"tenkan": 9, "kijun": 26, "senkou_b": 52},      # Classic
            {"tenkan": 7, "kijun": 22, "senkou_b": 44},       # Faster
            {"tenkan": 12, "kijun": 30, "senkou_b": 60},      # Slower
        ]
