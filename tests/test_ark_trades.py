import unittest
from unittest.mock import patch

import pandas as pd

from src.signals.ark_trades import ArkTradesScanner


class ArkTradesScannerTests(unittest.TestCase):
    def test_parse_workbook_normalizes_rows(self):
        scanner = ArkTradesScanner()
        frame = pd.DataFrame(
            [
                {
                    "fund": "ARKK",
                    "date": "2026/03/06",
                    "direction": "Buy",
                    "ticker": "HOOD",
                    "isin": "US7707001027",
                    "name": "Robinhood Markets Inc",
                    "shares": 16570,
                    "weight_pct": 0.0190,
                },
                {
                    "fund": "ARKG",
                    "date": "2026/03/06",
                    "direction": "Sell",
                    "ticker": "INCY",
                    "isin": "US45337C1027",
                    "name": "Incyte Corp",
                    "shares": 93,
                    "weight_pct": 0.0008,
                },
            ]
        )

        with patch("src.signals.ark_trades.pd.read_excel", return_value=frame):
            trades = scanner._parse_workbook(b"fake-xls")

        self.assertEqual(len(trades), 2)
        self.assertEqual(trades[0]["date"], "2026-03-06")
        self.assertIn(trades[0]["direction"], ("buy", "sell"))
        self.assertEqual(trades[0]["source"], "ark_trades")

    def test_group_signals_aggregates_across_funds(self):
        scanner = ArkTradesScanner()
        scanner._trades = [
            {"fund": "ARKK", "date": "2026-03-06", "direction": "buy", "ticker": "HOOD", "name": "Robinhood", "shares": 16_570, "weight_pct": 0.0190},
            {"fund": "ARKF", "date": "2026-03-06", "direction": "buy", "ticker": "HOOD", "name": "Robinhood", "shares": 532, "weight_pct": 0.0048},
            {"fund": "ARKG", "date": "2026-03-06", "direction": "sell", "ticker": "INCY", "name": "Incyte", "shares": 93, "weight_pct": 0.0008},
        ]
        scanner._last_fetch = 1e12

        buys = scanner.get_buy_signals()
        sells = scanner.get_sell_signals()

        self.assertEqual(buys[0]["ticker"], "HOOD")
        self.assertEqual(buys[0]["fund_count"], 2)
        self.assertGreater(buys[0]["shares"], 16_000)
        self.assertEqual(sells[0]["ticker"], "INCY")


if __name__ == "__main__":
    unittest.main()
