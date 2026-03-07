import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import src.main as main_module
from src.scanner.scanner import Scanner
from src.signals.copy_trader import CopyTraderMonitor


class CopyTraderMonitorTests(unittest.TestCase):
    def test_parse_tweet_extracts_explicit_long_signal(self):
        monitor = CopyTraderMonitor()
        tweet = {
            "tweet_id": "1",
            "handle": "traderstewie",
            "text": "Starter $HOOD long here 44.20",
            "created_at": "2026-03-07T09:00:00Z",
        }

        parsed = monitor._parse_tweet(tweet)

        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["ticker"], "HOOD")
        self.assertEqual(parsed[0]["side"], "long")

    def test_parse_tweet_ignores_exit_language(self):
        monitor = CopyTraderMonitor()
        tweet = {
            "tweet_id": "2",
            "handle": "alphatrends",
            "text": "Trimmed $NVDA into strength",
            "created_at": "2026-03-07T09:02:00Z",
        }

        parsed = monitor._parse_tweet(tweet)

        self.assertEqual(parsed, [])

    def test_get_candidate_signals_groups_convergence(self):
        monitor = CopyTraderMonitor()
        monitor._bearer = "test-token"
        tweets = [
            {"tweet_id": "1", "handle": "traderstewie", "text": "Starter $HOOD long here", "created_at": "2026-03-07T09:00:00Z"},
            {"tweet_id": "2", "handle": "alphatrends", "text": "Added $HOOD long", "created_at": "2026-03-07T09:01:00Z"},
        ]

        with patch.object(monitor, "_fetch_recent_tweets", return_value=tweets):
            signals = monitor.get_candidate_signals()

        self.assertEqual(signals[0]["symbol"], "HOOD")
        self.assertEqual(signals[0]["copy_trader_signal_count"], 2)
        self.assertIn("traderstewie", signals[0]["copy_trader_handles"])
        self.assertGreaterEqual(float(signals[0]["copy_trader_size_multiplier"]), 1.08)
        self.assertAlmostEqual(float(signals[0]["copy_trader_weight"]), 1.0, places=3)

    def test_record_trade_result_updates_stats_and_weight(self):
        with TemporaryDirectory() as tmp_dir:
            monitor = CopyTraderMonitor()
            monitor._performance_file = Path(tmp_dir) / "copy_trader_performance.json"

            for _ in range(5):
                monitor.record_trade_result({"copy_trader_handles": ["traderstewie"], "pnl": 25.0})

            stats = {row["handle"]: row for row in monitor.get_trader_stats()}
            stewie = stats["traderstewie"]
            self.assertEqual(stewie["signals_correct"], 5)
            self.assertEqual(stewie["signals_wrong"], 0)
            self.assertGreater(stewie["weight"], 1.0)
            self.assertTrue(monitor._performance_file.exists())

    def test_get_exit_signals_groups_exit_tweets(self):
        monitor = CopyTraderMonitor()
        monitor._bearer = "test-token"
        tweets = [
            {"tweet_id": "10", "handle": "traderstewie", "text": "Sold $HOOD into the pop", "created_at": "2026-03-07T10:00:00Z"},
            {"tweet_id": "11", "handle": "alphatrends", "text": "Trimmed $HOOD after open", "created_at": "2026-03-07T10:01:00Z"},
        ]

        with patch.object(monitor, "_fetch_recent_tweets", return_value=tweets):
            exits = monitor.get_exit_signals()

        self.assertEqual(exits[0]["symbol"], "HOOD")
        self.assertEqual(exits[0]["copy_trader_exit_count"], 2)
        self.assertEqual(exits[0]["copy_trader_exit_action"], "mixed")
        self.assertIn("traderstewie", exits[0]["copy_trader_exit_handles"])


class CopyTraderScannerTests(unittest.TestCase):
    def test_copy_trader_candidates_get_lenient_filter(self):
        scanner = Scanner()
        candidate = {
            "symbol": "HOOD",
            "price": 42.0,
            "change_pct": 0.0,
            "volume": 150_000,
            "avg_volume": 150_000,
            "source": "copy_trader",
            "copy_trader_context": "2 Tier-1 trader signals",
            "copy_trader_score_adjustment": 0.2,
        }

        self.assertTrue(scanner._passes_filter(candidate))


class CopyTraderExitHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_copy_trader_exit_signal_tightens_position_trail(self):
        class _FakeMonitor:
            def get_exit_signals(self):
                return [
                    {
                        "symbol": "HOOD",
                        "copy_trader_exit_count": 2,
                        "copy_trader_exit_action": "exit",
                        "copy_trader_exit_handles": ["traderstewie", "alphatrends"],
                        "copy_trader_exit_context": "2 Tier-1 exit signal(s): traderstewie, alphatrends",
                        "copy_trader_exit_tweet_ids": ["10", "11"],
                    }
                ]

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        position = {
            "symbol": "HOOD",
            "trail_pct": 3.0,
            "copy_trader_handles": ["traderstewie"],
            "signal_sources": ["copy_trader"],
            "swing_only": False,
        }
        bot.copy_trader_monitor = _FakeMonitor()
        bot.entry_manager = type("EntryMgr", (), {"positions": {"HOOD": position}})()
        bot.ai_layers = {}
        bot._processed_copy_trader_exit_ids = set()

        async def _fake_refresh(pos, new_trail_pct):
            pos["trail_pct"] = new_trail_pct
            return False

        bot._refresh_position_trailing_stop = _fake_refresh

        with patch.object(main_module, "log_activity"):
            await bot._process_copy_trader_exit_signals()

        self.assertLess(position["trail_pct"], 3.0)
        self.assertEqual(position["copy_trader_exit_action"], "exit")
        self.assertEqual(position["copy_trader_exit_count"], 2)
        self.assertIn("HOOD", bot.ai_layers["last_copy_trader_exit_signal"])


if __name__ == "__main__":
    unittest.main()
