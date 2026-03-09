import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
import time
import requests

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
        with TemporaryDirectory() as tmp_dir:
            monitor = CopyTraderMonitor()
            monitor._performance_file = Path(tmp_dir) / "copy_trader_performance.json"
            for trader in monitor._traders.values():
                trader["weight"] = 1.0
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
            self.assertGreater(float(signals[0]["copy_trader_size_multiplier"]), 1.0)
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

    def test_size_multiplier_can_reduce_for_underperforming_traders(self):
        monitor = CopyTraderMonitor()
        monitor._traders["traderstewie"]["weight"] = 0.5
        tweet = {
            "tweet_id": "3",
            "handle": "traderstewie",
            "text": "Starter $SNAP long here",
            "created_at": "2026-03-07T09:05:00Z",
        }

        with patch.object(monitor, "_fetch_recent_tweets", return_value=[tweet]):
            monitor._bearer = "test-token"
            signals = monitor.get_candidate_signals()

        self.assertLess(float(signals[0]["copy_trader_size_multiplier"]), 1.0)

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

    def test_stream_payload_ingests_signal_without_recent_search(self):
        monitor = CopyTraderMonitor()
        monitor._bearer = "test-token"
        monitor._mode = "stream"
        monitor._stream_connected = True
        now = time.time()
        monitor._stream_last_event_ts = now
        payload = {
            "data": {
                "id": "20",
                "author_id": "42",
                "text": "Starter $HOOD long here 44.20",
                "created_at": "2026-03-07T10:02:00Z",
            },
            "includes": {
                "users": [{"id": "42", "username": "TraderStewie"}],
            },
        }

        monitor._ingest_stream_payload(payload, now=now)

        with patch.object(monitor, "_ensure_streaming"):
            signals = monitor.get_candidate_signals()

        self.assertEqual(signals[0]["symbol"], "HOOD")
        self.assertEqual(signals[0]["copy_trader_signal_count"], 1)

    def test_auto_mode_falls_back_to_poll_when_stream_is_stale(self):
        monitor = CopyTraderMonitor()
        monitor._bearer = "test-token"
        monitor._mode = "auto"
        monitor._stream_connected = False
        monitor._cache_ts = 0.0
        tweet = {
            "tweet_id": "21",
            "handle": "traderstewie",
            "text": "Starter $SNAP long here",
            "created_at": "2026-03-07T10:05:00Z",
        }

        with patch.object(monitor, "_ensure_streaming"), \
             patch.object(monitor, "_fetch_recent_tweets", return_value=[tweet]):
            signals = monitor.get_candidate_signals()

        self.assertEqual(signals[0]["symbol"], "SNAP")

    def test_stream_429_enters_cooldown(self):
        monitor = CopyTraderMonitor()
        monitor._stream_429_cooldown_seconds = 120.0
        error = requests.HTTPError("429 Too Many Requests")
        error.response = type("Response", (), {"status_code": 429})()

        backoff = monitor._compute_stream_backoff(error, 5.0)

        self.assertEqual(backoff, 120.0)
        self.assertGreater(monitor._stream_cooldown_until, time.time())
        self.assertTrue(monitor._stream_in_cooldown())


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
