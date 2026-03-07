"""
Parabolic SAR + ADX Combo Strategy.

Parabolic SAR for stop-and-reverse signals, ADX to filter only strong trends.
Classic TradingView combination. Only trades when ADX confirms trend strength.

Buy: SAR flips below price (bullish) AND ADX > 25 (strong trend)
Sell: SAR flips above price (bearish)

Source: TradingView "Parabolic SAR Strategy with MACD" + ADX filter
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class ParabolicSARADXIndicator(BaseIndicator):
    def __init__(self, sar_start: float = 0.02, sar_increment: float = 0.02,
                 sar_max: float = 0.2, adx_period: int = 14, adx_threshold: float = 25):
        self.sar_start = sar_start
        self.sar_increment = sar_increment
        self.sar_max = sar_max
        self.adx_period = adx_period
        self.adx_threshold = adx_threshold

    def name(self) -> str:
        return "parabolic_sar_adx"

    def _compute_sar(self, high, low, close):
        """Compute Parabolic SAR values."""
        n = len(close)
        sar = np.zeros(n)
        trend = np.ones(n)  # 1 = bullish, -1 = bearish
        af = self.sar_start
        ep = high.iloc[0]  # Extreme point

        sar[0] = low.iloc[0]

        for i in range(1, n):
            if trend[i-1] == 1:  # Bullish
                sar[i] = sar[i-1] + af * (ep - sar[i-1])
                sar[i] = min(sar[i], low.iloc[i-1])
                if i >= 2:
                    sar[i] = min(sar[i], low.iloc[i-2])

                if low.iloc[i] < sar[i]:
                    trend[i] = -1
                    sar[i] = ep
                    af = self.sar_start
                    ep = low.iloc[i]
                else:
                    trend[i] = 1
                    if high.iloc[i] > ep:
                        ep = high.iloc[i]
                        af = min(af + self.sar_increment, self.sar_max)
            else:  # Bearish
                sar[i] = sar[i-1] + af * (ep - sar[i-1])
                sar[i] = max(sar[i], high.iloc[i-1])
                if i >= 2:
                    sar[i] = max(sar[i], high.iloc[i-2])

                if high.iloc[i] > sar[i]:
                    trend[i] = 1
                    sar[i] = ep
                    af = self.sar_start
                    ep = high.iloc[i]
                else:
                    trend[i] = -1
                    if low.iloc[i] < ep:
                        ep = low.iloc[i]
                        af = min(af + self.sar_increment, self.sar_max)

        return pd.Series(sar, index=close.index), pd.Series(trend, index=close.index)

    def _compute_adx(self, high, low, close):
        """Compute ADX."""
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)

        plus_dm = high.diff()
        minus_dm = -low.diff()
        plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0)
        minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0)

        atr = tr.rolling(self.adx_period).mean()
        plus_di = 100 * (plus_dm.rolling(self.adx_period).mean() / atr.replace(0, np.nan))
        minus_di = 100 * (minus_dm.rolling(self.adx_period).mean() / atr.replace(0, np.nan))

        dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
        adx = dx.rolling(self.adx_period).mean()

        return adx.fillna(0), plus_di.fillna(0), minus_di.fillna(0)

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        high = df["high"]
        low = df["low"]
        close = df["close"]

        sar, trend = self._compute_sar(high, low, close)
        adx, plus_di, minus_di = self._compute_adx(high, low, close)

        # SAR flips bullish (trend changes from -1 to 1)
        sar_bullish_flip = (trend == 1) & (trend.shift(1) == -1)
        sar_bearish_flip = (trend == -1) & (trend.shift(1) == 1)

        # ADX filter: only trade strong trends
        strong_trend = adx > self.adx_threshold

        entries = sar_bullish_flip & strong_trend
        exits = sar_bearish_flip

        strength = (adx / 50).clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"psar_adx_a{self.adx_period}_t{self.adx_threshold}",
            params={"sar_start": self.sar_start, "sar_increment": self.sar_increment,
                    "sar_max": self.sar_max, "adx_period": self.adx_period,
                    "adx_threshold": self.adx_threshold},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"sar_start": 0.02, "sar_increment": 0.02, "sar_max": 0.2, "adx_period": 14, "adx_threshold": 20},
            {"sar_start": 0.02, "sar_increment": 0.02, "sar_max": 0.2, "adx_period": 14, "adx_threshold": 25},
            {"sar_start": 0.02, "sar_increment": 0.02, "sar_max": 0.2, "adx_period": 14, "adx_threshold": 30},
            {"sar_start": 0.01, "sar_increment": 0.01, "sar_max": 0.1, "adx_period": 14, "adx_threshold": 25},
        ]
