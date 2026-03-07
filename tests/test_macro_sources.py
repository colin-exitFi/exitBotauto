import unittest
from unittest.mock import patch

from src.signals.finnhub import FinnhubClient
from src.signals.fred import FredClient


class FredClientTests(unittest.TestCase):
    def test_macro_snapshot_summarizes_series(self):
        client = FredClient(api_key="fred-test")
        cpi_obs = [{"value": "320.0"}] + [{"value": "0"}] * 11 + [{"value": "300.0"}]
        fed_obs = [{"value": "5.25"}]
        unrate_obs = [{"value": "4.8"}]
        curve_obs = [{"value": "-0.55"}]

        with patch.object(
            client,
            "get_series_observations",
            side_effect=[cpi_obs, fed_obs, unrate_obs, curve_obs],
        ):
            snapshot = client.get_macro_snapshot()

        self.assertEqual(snapshot["macro_bias"], "risk_off")
        self.assertIn("inflation_hot", snapshot["headwinds"])
        self.assertIn("yield_curve_inverted", snapshot["headwinds"])
        self.assertIn("CPI", snapshot["summary"])


class FinnhubClientTests(unittest.TestCase):
    def test_summarize_economic_calendar_prioritizes_us_events(self):
        client = FinnhubClient(api_key="fh-test")
        events = [
            {"date": "2026-03-10", "country": "US", "event": "CPI", "impact": "high"},
            {"date": "2026-03-11", "country": "EU", "event": "ECB Rate Decision", "impact": "high"},
            {"date": "2026-03-12", "country": "US", "event": "Jobless Claims", "impact": "medium"},
        ]

        with patch.object(client, "get_economic_calendar", return_value=events):
            summary = client.summarize_economic_calendar(days=7)

        self.assertEqual(summary["high_impact_count"], 1)
        self.assertIn("CPI", summary["summary"])
        self.assertNotIn("ECB", summary["summary"])

    def test_normalize_ipo_calendar_extracts_symbols(self):
        client = FinnhubClient(api_key="fh-test")
        payload = {
            "ipoCalendar": [
                {
                    "symbol": "ACME",
                    "date": "2026-03-14",
                    "name": "Acme Biotech",
                    "exchange": "NASDAQ",
                    "price": "14-16",
                }
            ]
        }

        rows = client._normalize_ipo_calendar(payload)

        self.assertEqual(rows[0]["symbol"], "ACME")
        self.assertEqual(rows[0]["date"], "2026-03-14")
        self.assertEqual(rows[0]["exchange"], "NASDAQ")


if __name__ == "__main__":
    unittest.main()
