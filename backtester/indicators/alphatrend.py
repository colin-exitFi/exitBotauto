"""
AlphaTrend Indicator.

Blends CCI and ATR for dynamic trend detection. Color-coded channel
that adapts to volatility. Reported 62% win rate, 2.1 PF in 2025 backtests.

Original Pine Script by KivancOzbilgic on TradingView.
Settings: CCI Length 14, ATR Multiplier 1.5. Filter with 200 EMA optional.
"""

import numpy as np
import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class AlphaTrendIndicator(BaseIndicator):
    def __init__(self, cci_length: int = 14, atr_mult: float = 1.5):
        self.cci_length = cci_length
        self.atr_mult = atr_mult

    def name(self) -> str:
        return "alphatrend"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]
        high = df["high"]
        low = df["low"]

        # ATR
        tr = pd.concat([
            high - low,
            (high - close.shift(1)).abs(),
            (low - close.shift(1)).abs(),
        ], axis=1).max(axis=1)
        atr = tr.rolling(self.cci_length).mean()

        # CCI
        tp = (high + low + close) / 3
        tp_sma = tp.rolling(self.cci_length).mean()
        tp_mad = tp.rolling(self.cci_length).apply(lambda x: np.mean(np.abs(x - np.mean(x))), raw=True)
        cci = (tp - tp_sma) / (0.015 * tp_mad.replace(0, np.nan))
        cci = cci.fillna(0)

        # AlphaTrend line
        up_t = low - atr * self.atr_mult
        down_t = high + atr * self.atr_mult

        alpha_trend = pd.Series(np.nan, index=df.index)
        for i in range(1, len(df)):
            if cci.iloc[i] >= 0:
                alpha_trend.iloc[i] = max(up_t.iloc[i], alpha_trend.iloc[i - 1]) if not np.isnan(alpha_trend.iloc[i - 1]) else up_t.iloc[i]
            else:
                alpha_trend.iloc[i] = min(down_t.iloc[i], alpha_trend.iloc[i - 1]) if not np.isnan(alpha_trend.iloc[i - 1]) else down_t.iloc[i]

        # Signals: price crosses above/below alpha_trend
        entries = (close > alpha_trend) & (close.shift(1) <= alpha_trend.shift(1))
        exits = (close < alpha_trend) & (close.shift(1) >= alpha_trend.shift(1))

        strength = ((close - alpha_trend) / close).abs().clip(0, 1).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"alphatrend_cci{self.cci_length}_m{self.atr_mult}",
            params={"cci_length": self.cci_length, "atr_mult": self.atr_mult},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"cci_length": 14, "atr_mult": 1.0},
            {"cci_length": 14, "atr_mult": 1.5},
            {"cci_length": 14, "atr_mult": 2.0},
            {"cci_length": 20, "atr_mult": 1.5},
            {"cci_length": 10, "atr_mult": 1.5},
        ]
