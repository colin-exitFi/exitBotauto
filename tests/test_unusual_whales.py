import unittest

from src.signals.unusual_options import UnusualOptionsScanner
from src.signals.unusual_whales import UnusualWhalesClient


class UnusualWhalesParsingTests(unittest.TestCase):
    def test_normalize_flow_alerts_maps_core_fields(self):
        client = UnusualWhalesClient(api_token="test-token")
        records = [
            {
                "ticker_symbol": "TSLA",
                "strike": "250",
                "expiry": "2026-03-20",
                "type": "Call",
                "premium": "250000",
                "volume": "1200",
                "open_interest": "400",
            }
        ]

        alerts = client._normalize_flow_alerts(records)

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["ticker"], "TSLA")
        self.assertEqual(alerts[0]["type"], "call")
        self.assertEqual(alerts[0]["sentiment"], "bullish")
        self.assertEqual(alerts[0]["premium"], 250000.0)

    def test_market_tide_bias_turns_risk_off_when_puts_dominate(self):
        client = UnusualWhalesClient(api_token="test-token")
        payload = {
            "put_premium": 125_000_000,
            "call_premium": 66_000_000,
            "put_call_ratio": 1.89,
        }

        market_tide = client._normalize_market_tide(payload)

        self.assertEqual(market_tide["bias"], "risk_off")
        self.assertAlmostEqual(market_tide["put_call_ratio"], 1.89, places=2)

    def test_unusual_options_scanner_aggregates_uw_flow_by_ticker(self):
        flow_alerts = [
            {"ticker": "NVDA", "sentiment": "bullish", "premium": 600_000, "volume": 1000, "open_interest": 300},
            {"ticker": "NVDA", "sentiment": "bullish", "premium": 250_000, "volume": 500, "open_interest": 200},
            {"ticker": "AAPL", "sentiment": "bearish", "premium": 300_000, "volume": 900, "open_interest": 500},
        ]

        signals = UnusualOptionsScanner._aggregate_uw_flow(flow_alerts)

        self.assertEqual(signals[0]["ticker"], "NVDA")
        self.assertEqual(signals[0]["bias"], "bullish")
        self.assertGreater(signals[0]["premium"], signals[1]["premium"])
        self.assertEqual(signals[1]["ticker"], "AAPL")
        self.assertEqual(signals[1]["bias"], "bearish")

    def test_normalize_gamma_exposure_extracts_support_and_resistance(self):
        client = UnusualWhalesClient(api_token="test-token")
        gamma = client._normalize_gamma_exposure(
            "NVDA",
            [
                {"strike": "120", "gex": "5000"},
                {"strike": "130", "gex": "-8000"},
                {"strike": "125", "gex": "3000"},
            ],
        )

        self.assertEqual(gamma["ticker"], "NVDA")
        self.assertEqual(gamma["max_gamma_strike"], 130.0)
        self.assertIn(120.0, gamma["support_strikes"])
        self.assertIn(130.0, gamma["resistance_strikes"])

    def test_normalize_insider_trades_maps_buy_and_sell(self):
        client = UnusualWhalesClient(api_token="test-token")
        trades = client._normalize_insider_trades(
            [
                {"ticker": "AAPL", "transaction_type": "P", "shares": "1000", "price": "180"},
                {"ticker": "MSFT", "transaction_type": "S", "shares": "200", "price": "410"},
            ]
        )

        self.assertEqual(trades[0]["transaction"], "buy")
        self.assertEqual(trades[0]["shares"], 1000)
        self.assertEqual(trades[1]["transaction"], "sell")


if __name__ == "__main__":
    unittest.main()
