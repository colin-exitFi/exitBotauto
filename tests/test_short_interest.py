import unittest

from src.signals.short_interest import ShortInterestScanner


class ShortInterestScannerTests(unittest.TestCase):
    def test_parse_finra_csv_extracts_rows(self):
        text = (
            '"symbolCode","currentShortPositionQuantity","averageDailyVolumeQuantity","daysToCoverQuantity","changePercent","settlementDate"\n'
            '"AAPL","1000000","250000","4.0","12.5","2026-03-01"\n'
        )

        rows = ShortInterestScanner._parse_finra_csv(text)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbolCode"], "AAPL")
        self.assertEqual(rows[0]["daysToCoverQuantity"], "4.0")

    def test_recent_settlement_guard_rejects_stale_finra_partition(self):
        self.assertTrue(ShortInterestScanner._is_recent_settlement("2026-03-01", max_age_days=60))
        self.assertFalse(ShortInterestScanner._is_recent_settlement("2020-04-15", max_age_days=60))

    def test_get_squeeze_candidates_accepts_days_to_cover_signal(self):
        scanner = ShortInterestScanner()
        scanner._data = [
            {"ticker": "AAPL", "short_float_pct": 10.0, "days_to_cover": 6.5},
            {"ticker": "MSFT", "short_float_pct": 25.0, "days_to_cover": 1.5},
        ]

        candidates = scanner.get_squeeze_candidates()

        self.assertEqual({row["ticker"] for row in candidates}, {"AAPL", "MSFT"})


if __name__ == "__main__":
    unittest.main()
