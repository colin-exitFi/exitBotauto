import unittest

from src.scanner.scanner import Scanner


class _Watchlist:
    def get_all(self):
        return [
            {
                "ticker": "NVDA",
                "sources": "watchlist",
                "side": "long",
                "reason": "overnight thesis",
                "conviction": 0.91,
            },
            {
                "ticker": "TSLA",
                "sources": "stocktwits",
                "side": "short",
                "reason": "macro pressure",
                "conviction": 0.73,
            },
        ]


class ScannerResearchUniverseTests(unittest.TestCase):
    def test_falls_back_to_watchlist_when_cycle_cache_is_empty(self):
        scanner = Scanner(watchlist_provider=_Watchlist())
        rows = scanner.get_research_universe()

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["symbol"], "NVDA")
        self.assertTrue(rows[0]["research_only"])

    def test_returns_cycle_research_cache_when_available(self):
        scanner = Scanner(watchlist_provider=_Watchlist())
        scanner._research_cache = [
            {"symbol": "LWLG", "source": "unusual_whales", "side": "long", "score": 0.88, "research_only": True}
        ]

        rows = scanner.get_research_universe()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "LWLG")

    def test_scan_stats_are_exposed(self):
        scanner = Scanner()
        scanner._last_scan_stats = {"live": 1, "research": 25, "merged_unique": 25}

        stats = scanner.get_last_scan_stats()

        self.assertEqual(stats["live"], 1)
        self.assertEqual(stats["research"], 25)

    def test_scan_stats_have_safe_defaults_before_first_scan(self):
        scanner = Scanner()

        stats = scanner.get_last_scan_stats()

        self.assertEqual(stats["status"], "idle")
        self.assertEqual(stats["live"], 0)
        self.assertEqual(stats["research"], 0)
        self.assertIn("last_started_at", stats)
        self.assertIn("last_completed_at", stats)

    def test_background_research_cycle_primes_scan_status(self):
        scanner = Scanner(watchlist_provider=_Watchlist())
        scanner.record_background_research_cycle(
            live_candidates=[{"symbol": "NVDA", "score": 0.9}],
            regime="risk_on",
        )

        stats = scanner.get_last_scan_stats()

        self.assertEqual(stats["status"], "research_ready")
        self.assertEqual(stats["live"], 1)
        self.assertEqual(stats["research"], 2)
        self.assertEqual(stats["merged_unique"], 2)
        self.assertEqual(stats["regime"], "risk_on")
        self.assertIsNotNone(stats["last_completed_at"])


if __name__ == "__main__":
    unittest.main()
