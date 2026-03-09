import unittest
from unittest.mock import patch
from types import SimpleNamespace

from src.streams.market_stream import MarketStream
from src.streams.trade_stream import TradeStream


class StreamAuthTests(unittest.TestCase):
    def test_trade_stream_uses_modern_auth_payload(self):
        with patch("src.streams.trade_stream.settings.ALPACA_API_KEY", "key"), \
             patch("src.streams.trade_stream.settings.ALPACA_SECRET_KEY", "secret"):
            auth = TradeStream._build_auth_message()

        self.assertEqual(auth, {"action": "auth", "key": "key", "secret": "secret"})

    def test_trade_stream_accepts_modern_and_legacy_auth_responses(self):
        self.assertTrue(TradeStream._auth_succeeded([{"T": "success", "msg": "authenticated"}]))
        self.assertTrue(
            TradeStream._auth_succeeded(
                {"stream": "authorization", "data": {"status": "authorized"}}
            )
        )

    def test_market_stream_uses_modern_auth_payload(self):
        with patch("src.streams.market_stream.settings.ALPACA_API_KEY", "key"), \
             patch("src.streams.market_stream.settings.ALPACA_SECRET_KEY", "secret"):
            auth = MarketStream._build_auth_message()

        self.assertEqual(auth, {"action": "auth", "key": "key", "secret": "secret"})

    def test_market_stream_accepts_modern_and_legacy_auth_responses(self):
        self.assertTrue(MarketStream._auth_succeeded([{"T": "success", "msg": "authenticated"}]))
        self.assertTrue(
            MarketStream._auth_succeeded(
                {"stream": "authorization", "data": {"status": "authorized"}}
            )
        )

    def test_market_stream_detects_retryable_connection_limit_auth_error(self):
        error = MarketStream._extract_auth_error([{"T": "error", "code": 406, "msg": "connection limit exceeded"}])

        self.assertEqual(error["code"], 406)
        self.assertTrue(MarketStream._is_retryable_auth_error(error))

    def test_market_stream_subscription_includes_statuses_and_lulds(self):
        msg = MarketStream._build_subscription_message(["AAPL", "TSLA"], action="subscribe")

        self.assertEqual(msg["action"], "subscribe")
        self.assertEqual(msg["statuses"], ["AAPL", "TSLA"])
        self.assertEqual(msg["lulds"], ["AAPL", "TSLA"])


class MarketStreamEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_handle_status_message_updates_halt_state_and_callback(self):
        stream = MarketStream()
        events = []
        stream.set_halt_callback(lambda symbol, status_code, reason, halted: events.append((symbol, status_code, reason, halted)))

        await stream._handle_message({"T": "s", "S": "AAPL", "sc": "H", "rc": "LUDP"})

        self.assertTrue(stream.market_status["AAPL"]["halted"])
        self.assertEqual(events, [("AAPL", "H", "LUDP", True)])

    async def test_handle_luld_message_updates_bands_and_callback(self):
        stream = MarketStream()
        events = []
        stream.set_luld_callback(lambda symbol, band_data: events.append((symbol, band_data)))

        await stream._handle_message({"T": "l", "S": "TSLA", "u": 240.5, "d": 231.2, "i": "B"})

        self.assertEqual(stream.luld_bands["TSLA"]["upper_band"], 240.5)
        self.assertEqual(stream.luld_bands["TSLA"]["lower_band"], 231.2)
        self.assertEqual(events[0][0], "TSLA")


if __name__ == "__main__":
    unittest.main()
