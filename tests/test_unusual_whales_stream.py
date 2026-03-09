import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock

import src.main as main_module
from src.signals.unusual_whales import UnusualWhalesClient
from src.streams.unusual_whales_stream import UnusualWhalesStream


class UnusualWhalesStreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_join_ack_marks_channel_joined(self):
        stream = UnusualWhalesStream(rest_client=UnusualWhalesClient(api_token="test-token"))

        await stream._handle_raw_message(json.dumps(["flow-alerts", {"response": {}, "status": "ok"}]))

        self.assertIn("flow-alerts", stream.get_stats()["joined_channels"])

    async def test_flow_alert_message_normalizes_and_emits(self):
        stream = UnusualWhalesStream(rest_client=UnusualWhalesClient(api_token="test-token"))
        events = []
        stream.set_signal_callback(lambda event: events.append(event))

        payload = [
            "flow-alerts",
            {
                "ticker": "TSLA",
                "option_chain": "TSLA260320C00300000",
                "type": "call",
                "total_premium": 250000,
                "total_ask_side_prem": 250000,
                "volume": 1200,
                "open_interest": 500,
                "underlying_price": 280.5,
                "price": 7.3,
                "start_time": 1726670212648,
            },
        ]

        await stream._handle_raw_message(json.dumps(payload))

        flow = stream.get_recent_flow()
        self.assertEqual(len(flow), 1)
        self.assertEqual(flow[0]["ticker"], "TSLA")
        self.assertEqual(flow[0]["contract_symbol"], "TSLA260320C00300000")
        self.assertEqual(flow[0]["premium"], 250000.0)
        self.assertEqual(events[0]["event_type"], "flow_alert")

    async def test_off_lit_trade_message_derives_dark_pool_sentiment(self):
        stream = UnusualWhalesStream(rest_client=UnusualWhalesClient(api_token="test-token"))

        payload = [
            "off_lit_trades",
            {
                "ticker": "AAPL",
                "price": "101.00",
                "size": 10000,
                "premium": "1010000",
                "nbbo_bid": "100.80",
                "nbbo_ask": "101.00",
                "executed_at": "2026-03-08T23:30:00Z",
            },
        ]

        await stream._handle_raw_message(json.dumps(payload))

        dark_pool = stream.get_recent_dark_pool()
        self.assertEqual(len(dark_pool), 1)
        self.assertEqual(dark_pool[0]["ticker"], "AAPL")
        self.assertEqual(dark_pool[0]["sentiment"], "bullish")

    def test_stale_stream_uses_rest_fallback_for_market_tide(self):
        client = UnusualWhalesClient(api_token="test-token")
        client.get_market_tide = lambda: {"bias": "risk_off", "put_call_ratio": 1.7}
        stream = UnusualWhalesStream(rest_client=client)
        stream._connected = True
        stream._last_event_ts = time.time() - 999

        self.assertTrue(stream.using_rest_fallback())
        self.assertEqual(stream.get_market_tide()["bias"], "risk_off")


class _RiskCanTrade:
    def can_trade(self):
        return True


class _EntryManagerStub:
    positions = {}

    def get_positions(self):
        return []


class UnusualWhalesQueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_realtime_flow_signal_queues_once_and_routes_to_process_candidates(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._uw_signal_queue = asyncio.Queue()
        bot._recent_uw_signal_keys = {}
        bot.ai_layers = {}
        bot.entry_manager = _EntryManagerStub()
        bot.risk_manager = _RiskCanTrade()
        bot._process_candidates = AsyncMock()

        event = {
            "event_type": "flow_alert",
            "ticker": "NVDA",
            "sentiment": "bullish",
            "type": "call",
            "premium": 400000,
            "volume": 1200,
            "contract_symbol": "NVDA260320C00120000",
            "underlying_price": 118.0,
            "stream_channel": "flow-alerts",
        }

        await bot._on_unusual_whales_signal(event)
        await bot._on_unusual_whales_signal(event)
        await bot._process_unusual_whales_signal_queue()

        self.assertEqual(bot._process_candidates.await_count, 1)
        candidate = bot._process_candidates.await_args.args[0][0]
        self.assertEqual(candidate["symbol"], "NVDA")
        self.assertEqual(candidate["source"], "unusual_whales_stream")
        self.assertEqual(candidate["side"], "long")
        self.assertEqual(bot.ai_layers["last_uw_stream_signal"].split()[0], "NVDA")

