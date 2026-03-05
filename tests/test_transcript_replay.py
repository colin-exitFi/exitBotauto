import unittest
from pathlib import Path
from unittest.mock import patch

import src.main as main_module
from tests.replay_harness import BotReplayHarness, load_transcript_fixture


class TranscriptReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_ws_first_long_transcript_fixture(self):
        fixture_path = Path(__file__).parent / "fixtures" / "alpaca_ws_first_long.json"
        transcript = load_transcript_fixture(str(fixture_path))
        harness = BotReplayHarness.from_transcript(transcript)

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"), \
             patch.object(main_module, "log_activity"):
            bot = await harness.replay_transcript(transcript)

        self.assertEqual(record_trade_mock.call_count, 1)
        trade = record_trade_mock.call_args[0][0]
        self.assertEqual(trade["symbol"], "AAPL")
        self.assertEqual(trade["reason"], "trailing_stop_ws")
        self.assertEqual(trade["strategy_tag"], "social_momentum_long")
        self.assertEqual(trade["provider_used"], "claude")
        self.assertAlmostEqual(trade["pnl"], 50.0, places=6)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)

    async def test_monitor_first_short_transcript_fixture(self):
        fixture_path = Path(__file__).parent / "fixtures" / "alpaca_monitor_first_short.json"
        transcript = load_transcript_fixture(str(fixture_path))
        harness = BotReplayHarness.from_transcript(transcript)

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"), \
             patch.object(main_module, "log_activity"):
            bot = await harness.replay_transcript(transcript)

        self.assertEqual(record_trade_mock.call_count, 1)
        trade = record_trade_mock.call_args[0][0]
        self.assertEqual(trade["symbol"], "NVDA")
        self.assertEqual(trade["side"], "buy_to_cover")
        self.assertEqual(trade["reason"], "trailing_stop")
        self.assertEqual(trade["strategy_tag"], "fade_short")
        self.assertEqual(trade["provider_used"], "grok")
        self.assertAlmostEqual(trade["quantity"], 8.0, places=6)
        self.assertAlmostEqual(trade["pnl"], 40.0, places=6)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)

