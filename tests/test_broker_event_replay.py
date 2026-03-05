import unittest
from unittest.mock import patch

import src.main as main_module
from tests.replay_harness import BotReplayHarness, ReplayVerdict


def _candidate(symbol="AAPL"):
    return {
        "symbol": symbol,
        "price": 100.0,
        "change_pct": 4.2,
        "volume": 2_500_000,
        "volume_spike": 3.1,
        "sentiment_score": 0.7,
        "stocktwits_trending_score": 18,
        "source": "polygon+grok_x",
        "grok_x_reason": "Trend acceleration on X",
        "score": 0.91,
    }


def _short_candidate(symbol="NVDA"):
    return {
        "symbol": symbol,
        "price": 100.0,
        "change_pct": -8.3,
        "volume": 3_200_000,
        "volume_spike": 4.2,
        "sentiment_score": -0.6,
        "source": "fade",
        "fade_signal": "failed_breakout",
        "score": 0.89,
    }


class BrokerEventReplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_lifecycle_ws_then_monitor_records_once_with_attribution(self):
        verdict = ReplayVerdict(symbol="AAPL", decision="BUY", confidence=88, provider_used="claude")
        harness = BotReplayHarness(verdicts={"AAPL": verdict})
        events = [
            {"type": "scan", "candidates": [_candidate("AAPL")]},
            {"type": "broker_close", "symbol": "AAPL", "exit_price": 105.0, "side": "sell"},
            {"type": "ws_stop_fill", "symbol": "AAPL", "fill_price": 105.0, "qty": 10.0},
            {"type": "monitor_positions"},
        ]

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"), \
             patch.object(main_module, "log_activity"):
            bot = await harness.replay(events)

        self.assertEqual(record_trade_mock.call_count, 1)
        trade = record_trade_mock.call_args[0][0]
        self.assertEqual(trade["symbol"], "AAPL")
        self.assertEqual(trade["reason"], "trailing_stop_ws")
        self.assertEqual(trade["strategy_tag"], "social_momentum_long")
        self.assertIn("polygon", trade["signal_sources"])
        self.assertIn("grok_x", trade["signal_sources"])
        self.assertEqual(trade["decision_confidence"], 88)
        self.assertEqual(trade["provider_used"], "claude")
        self.assertAlmostEqual(trade["entry_price"], 100.0, places=6)
        self.assertAlmostEqual(trade["exit_price"], 105.0, places=6)
        self.assertAlmostEqual(trade["pnl"], 50.0, places=6)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)
        self.assertAlmostEqual(bot.pnl_state.get("total_realized_pnl", 0), 50.0, places=6)

    async def test_replay_lifecycle_monitor_then_ws_records_once(self):
        verdict = ReplayVerdict(symbol="AAPL", decision="BUY", confidence=76, provider_used="gpt")
        harness = BotReplayHarness(verdicts={"AAPL": verdict})
        events = [
            {"type": "scan", "candidates": [_candidate("AAPL")]},
            {"type": "broker_close", "symbol": "AAPL", "exit_price": 104.0, "side": "sell"},
            {"type": "monitor_positions"},
            {"type": "ws_stop_fill", "symbol": "AAPL", "fill_price": 104.0, "qty": 10.0},
        ]

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"), \
             patch.object(main_module, "log_activity"):
            bot = await harness.replay(events)

        self.assertEqual(record_trade_mock.call_count, 1)
        trade = record_trade_mock.call_args[0][0]
        self.assertEqual(trade["reason"], "trailing_stop")
        self.assertEqual(trade["strategy_tag"], "social_momentum_long")
        self.assertIn("polygon", trade["signal_sources"])
        self.assertIn("grok_x", trade["signal_sources"])
        self.assertEqual(trade["decision_confidence"], 76)
        self.assertEqual(trade["provider_used"], "gpt")
        self.assertAlmostEqual(trade["pnl"], 40.0, places=6)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)
        self.assertAlmostEqual(bot.pnl_state.get("total_realized_pnl", 0), 40.0, places=6)

    async def test_replay_short_lifecycle_with_custom_entry_quantity(self):
        verdict = ReplayVerdict(symbol="NVDA", decision="SHORT", confidence=84, provider_used="grok")
        harness = BotReplayHarness(
            verdicts={"NVDA": verdict},
            default_quantity=8.0,
            entry_quantities={"NVDA": 8.0},
        )
        events = [
            {"type": "scan", "candidates": [_short_candidate("NVDA")]},
            {"type": "broker_close", "symbol": "NVDA", "exit_price": 95.0, "side": "buy"},
            {"type": "monitor_positions"},
        ]

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"), \
             patch.object(main_module, "log_activity"):
            bot = await harness.replay(events)

        self.assertEqual(record_trade_mock.call_count, 1)
        trade = record_trade_mock.call_args[0][0]
        self.assertEqual(trade["symbol"], "NVDA")
        self.assertEqual(trade["side"], "buy_to_cover")
        self.assertEqual(trade["strategy_tag"], "fade_short")
        self.assertIn("fade", trade["signal_sources"])
        self.assertEqual(trade["decision_confidence"], 84)
        self.assertEqual(trade["provider_used"], "grok")
        self.assertAlmostEqual(trade["quantity"], 8.0, places=6)
        self.assertAlmostEqual(trade["pnl"], 40.0, places=6)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)
        self.assertAlmostEqual(bot.pnl_state.get("total_realized_pnl", 0), 40.0, places=6)
