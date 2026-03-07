"""
Multi-Factor Momentum Strategy.

Combines RSI, MACD, and Volume into a single composite signal.
Only triggers when ALL three factors agree. Higher bar = fewer but
higher-quality signals.

From ResearchGate paper: "Combining momentum and mean-reversion indicators
such as Z-score, RSI, and 240-day moving average to generate buy signals"

Buy: RSI > 50 (bullish) + MACD histogram positive + Volume > 1.5x average
Sell: RSI < 50 + MACD histogram negative
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class MultiFactorMomentumIndicator(BaseIndicator):
    def __init__(self, rsi_period: int = 14, macd_fast: int = 12, macd_slow: int = 26,
                 macd_signal: int = 9, vol_mult: float = 1.5, vol_period: int = 20):
        self.rsi_period = rsi_period
        self.macd_fast = macd_fast
        self.macd_slow = macd_slow
        self.macd_signal = macd_signal
        self.vol_mult = vol_mult
        self.vol_period = vol_period

    def name(self) -> str:
        return "multi_factor_momentum"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        volume = df["volume"]

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/self.rsi_period, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/self.rsi_period, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))

        # MACD
        ema_fast = close.ewm(span=self.macd_fast, adjust=False).mean()
        ema_slow = close.ewm(span=self.macd_slow, adjust=False).mean()
        macd_line = ema_fast - ema_slow
        signal_line = macd_line.ewm(span=self.macd_signal, adjust=False).mean()
        histogram = macd_line - signal_line

        # Volume
        avg_volume = volume.rolling(self.vol_period).mean()
        high_volume = volume > (avg_volume * self.vol_mult)

        # RSI conditions
        rsi_bullish = rsi > 50
        rsi_bearish = rsi < 50

        # MACD conditions
        macd_bullish = (histogram > 0) & (histogram.shift(1) <= 0)  # Histogram crosses positive
        macd_bearish = (histogram < 0) & (histogram.shift(1) >= 0)

        # Multi-factor BUY: all three agree
        entries = rsi_bullish & macd_bullish & high_volume

        # Multi-factor SELL: RSI + MACD both bearish
        exits = rsi_bearish & macd_bearish

        # Strength: average of normalized components
        rsi_str = ((rsi - 50) / 50).clip(0, 1)
        macd_str = (histogram / close * 100).clip(0, 1)
        vol_str = (volume / avg_volume.replace(0, np.nan) / 5).clip(0, 1)
        strength = ((rsi_str + macd_str.fillna(0) + vol_str.fillna(0)) / 3).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"multi_factor_r{self.rsi_period}_m{self.macd_fast}_{self.macd_slow}",
            params={"rsi_period": self.rsi_period, "macd_fast": self.macd_fast,
                    "macd_slow": self.macd_slow, "macd_signal": self.macd_signal,
                    "vol_mult": self.vol_mult, "vol_period": self.vol_period},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "vol_mult": 1.5, "vol_period": 20},
            {"rsi_period": 14, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "vol_mult": 2.0, "vol_period": 20},
            {"rsi_period": 7, "macd_fast": 8, "macd_slow": 17, "macd_signal": 9, "vol_mult": 1.5, "vol_period": 10},
            {"rsi_period": 21, "macd_fast": 12, "macd_slow": 26, "macd_signal": 9, "vol_mult": 1.5, "vol_period": 30},
        ]
