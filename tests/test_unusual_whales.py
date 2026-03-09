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

    def test_normalize_flow_recent_uses_tags_for_sentiment(self):
        client = UnusualWhalesClient(api_token="test-token")
        records = [
            {
                "underlying_symbol": "NVDA",
                "option_type": "put",
                "premium": "250000",
                "volume": 700,
                "open_interest": 200,
                "tags": ["ask_side", "bearish"],
            }
        ]

        flows = client._normalize_flow_alerts(records)

        self.assertEqual(flows[0]["ticker"], "NVDA")
        self.assertEqual(flows[0]["sentiment"], "bearish")

    def test_option_screener_summary_aggregates_by_ticker(self):
        client = UnusualWhalesClient(api_token="test-token")
        records = client._normalize_option_screener(
            [
                {
                    "ticker_symbol": "NVDA",
                    "type": "call",
                    "premium": "300000",
                    "volume": 1200,
                    "open_interest": 300,
                    "ask_side_volume": 900,
                },
                {
                    "ticker_symbol": "NVDA",
                    "type": "call",
                    "premium": "250000",
                    "volume": 900,
                    "open_interest": 200,
                    "ask_side_volume": 700,
                },
            ]
        )

        summary = client.summarize_option_screener(records)

        self.assertEqual(summary[0]["ticker"], "NVDA")
        self.assertEqual(summary[0]["bias"], "bullish")
        self.assertEqual(summary[0]["contracts"], 2)
        self.assertGreater(summary[0]["avg_vol_to_oi"], 1.0)

    def test_summarize_net_premium_ticks_detects_bearish_bias(self):
        client = UnusualWhalesClient(api_token="test-token")
        client.get_net_premium_ticks = lambda symbol, date=None: [
            {
                "net_call_premium": -100000.0,
                "net_put_premium": -450000.0,
                "net_delta": -25000.0,
            }
        ]

        summary = client.summarize_net_premium_ticks("NVDA")

        self.assertEqual(summary["bias"], "bearish")
        self.assertLess(summary["net_delta"], 0)

    def test_summarize_options_volume_detects_bullish_bias(self):
        client = UnusualWhalesClient(api_token="test-token")
        client.get_options_volume = lambda symbol, limit=1: [
            {
                "bias": "bullish",
                "call_volume": 200000,
                "put_volume": 100000,
                "call_put_ratio": 2.0,
                "call_premium": 5000000.0,
                "put_premium": 2000000.0,
                "bullish_premium": 4200000.0,
                "bearish_premium": 1800000.0,
            }
        ]

        summary = client.summarize_options_volume("AAPL")

        self.assertEqual(summary["bias"], "bullish")
        self.assertGreater(summary["call_put_ratio"], 1.0)

    def test_usage_stats_track_latest_headers(self):
        client = UnusualWhalesClient(api_token="test-token")
        client._update_usage_stats(
            {
                "x-uw-daily-req-count": "21",
                "x-uw-minute-req-counter": "2",
                "x-uw-req-per-minute-remaining": "118",
                "x-uw-req-per-minute-reset": "4096",
                "x-uw-token-req-limit": "50000",
                "x-request-id": "abc123",
            },
            "/api/market/market-tide",
        )

        stats = client.get_usage_stats()

        self.assertEqual(stats["daily_request_count"], 21)
        self.assertEqual(stats["minute_remaining"], 118)
        self.assertEqual(stats["request_id"], "abc123")


if __name__ == "__main__":
    unittest.main()
