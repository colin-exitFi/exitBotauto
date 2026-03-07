"""EMA crossover indicator."""

from __future__ import annotations

import pandas as pd

from backtester.indicators.common import crossed_above, crossed_below, ema, finalize_signal
from backtester.indicators.registry import BaseIndicator, IndicatorRegistry


@IndicatorRegistry.register
class EMACrossoverIndicator(BaseIndicator):
    def name(self) -> str:
        return "ema_crossover"

    def param_grid(self):
        return [
            {"fast": 9, "slow": 21},
            {"fast": 12, "slow": 26},
            {"fast": 5, "slow": 13},
            {"fast": 8, "slow": 21},
            {"fast": 20, "slow": 50},
        ]

    def generate_signals(self, df: pd.DataFrame):
        params = self.params or {"fast": 9, "slow": 21}
        fast = ema(df["close"], params["fast"])
        slow = ema(df["close"], params["slow"])
        spread = (fast - slow).abs() / pd.Series(df["close"], dtype=float).replace(0, pd.NA)
        return finalize_signal(
            df.index,
            crossed_above(fast, slow),
            crossed_below(fast, slow),
            spread * 25.0,
            "long",
            self.name(),
            params,
        )
