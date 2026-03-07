"""
RSI + EMA Crossover Combo Strategy.

Multi-signal confirmation: RSI for momentum + EMA for trend.
Best for swing trading on 1H-4H charts.

Buy: RSI exits oversold (crosses above 30) AND price crosses above EMA.
Sell: RSI enters overbought (crosses below 70) AND price crosses below EMA.
"""

import pandas as pd
from typing import Dict, List, Any

from backtester.indicators.registry import BaseIndicator, IndicatorSignal, IndicatorRegistry


@IndicatorRegistry.register
class RSIEMAComboIndicator(BaseIndicator):
    def __init__(self, rsi_length: int = 14, ema_length: int = 50,
                 oversold: float = 30, overbought: float = 70):
        self.rsi_length = rsi_length
        self.ema_length = ema_length
        self.oversold = oversold
        self.overbought = overbought

    def name(self) -> str:
        return "rsi_ema_combo"

    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        close = df["close"]

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0).ewm(alpha=1/self.rsi_length, adjust=False).mean()
        loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/self.rsi_length, adjust=False).mean()
        rs = gain / loss.replace(0, 1e-10)
        rsi = 100 - (100 / (1 + rs))

        # EMA
        ema = close.ewm(span=self.ema_length, adjust=False).mean()

        # Buy: price crosses above EMA AND RSI > oversold (momentum confirming)
        price_above_ema = (close > ema) & (close.shift(1) <= ema.shift(1))
        rsi_not_oversold = rsi > self.oversold

        # Sell: price crosses below EMA AND RSI < overbought
        price_below_ema = (close < ema) & (close.shift(1) >= ema.shift(1))
        rsi_not_overbought = rsi < self.overbought

        entries = price_above_ema & rsi_not_oversold
        exits = price_below_ema & rsi_not_overbought

        # Strength: combine RSI distance from 50 and price distance from EMA
        rsi_strength = ((rsi - 50).abs() / 50).clip(0, 1)
        ema_strength = ((close - ema) / close).abs().clip(0, 0.1) * 10
        strength = ((rsi_strength + ema_strength) / 2).fillna(0)

        return IndicatorSignal(
            entries=entries.fillna(False),
            exits=exits.fillna(False),
            signal_strength=strength,
            side="long",
            name=f"rsi_ema_r{self.rsi_length}_e{self.ema_length}",
            params={"rsi_length": self.rsi_length, "ema_length": self.ema_length,
                    "oversold": self.oversold, "overbought": self.overbought},
        )

    def param_grid(self) -> List[Dict[str, Any]]:
        return [
            {"rsi_length": 14, "ema_length": 20, "oversold": 30, "overbought": 70},
            {"rsi_length": 14, "ema_length": 50, "oversold": 30, "overbought": 70},
            {"rsi_length": 14, "ema_length": 50, "oversold": 25, "overbought": 75},
            {"rsi_length": 7, "ema_length": 21, "oversold": 30, "overbought": 70},
            {"rsi_length": 21, "ema_length": 50, "oversold": 35, "overbought": 65},
        ]
