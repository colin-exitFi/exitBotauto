"""Universe builder for momentum-style backtests."""

from __future__ import annotations

from typing import List


class UniverseBuilder:
    """Starter universe builder for momentum names."""

    def __init__(self, polygon_api_key: str):
        self.polygon_api_key = polygon_api_key or ""

    def build_momentum_universe(
        self,
        lookback_months: int = 12,
        min_price: float = 5.0,
        max_price: float = 500.0,
        min_avg_volume: int = 500_000,
        min_days_with_big_moves: int = 10,
        max_symbols: int = 100,
    ) -> List[str]:
        del lookback_months, min_price, max_price, min_avg_volume, min_days_with_big_moves
        return self.get_default_universe()[: max(1, int(max_symbols or 1))]

    @staticmethod
    def get_default_universe() -> List[str]:
        return [
            "TSLA", "NVDA", "AMD", "MARA", "COIN", "PLTR", "SOFI", "RIVN",
            "LCID", "NIO", "PLUG", "DKNG", "RBLX", "SNAP", "SQ", "SHOP",
            "ROKU", "CRWD", "NET", "DDOG", "ZS", "MDB", "SNOW", "ABNB",
            "DASH", "PINS", "U", "HOOD", "AFRM", "UPST", "PATH", "IONQ",
            "SMCI", "ARM", "CELH", "HIMS", "DUOL", "CAVA", "BIRK", "ONON",
            "MSTR", "RIOT", "CLSK", "HUT", "BITF", "CORZ", "IREN", "XBI",
            "ARKK", "QQQ",
        ]

