import unittest

from src.data.bar_context import bar_context_is_stale, latest_bar_timestamp


class BarContextTests(unittest.TestCase):
    def test_latest_bar_timestamp_prefers_newest_bar_across_payload(self):
        payload = {
            "bars_1m": [{"timestamp": 1000}, {"timestamp": 2000}],
            "bars_5m": [{"timestamp": 1500}],
        }

        self.assertEqual(latest_bar_timestamp(payload), 2000)

    def test_bar_context_is_stale_for_missing_payload(self):
        self.assertTrue(bar_context_is_stale({}, now_ts=1_000.0))

    def test_bar_context_is_stale_when_latest_bar_is_too_old(self):
        payload = {"bars_1m": [{"timestamp": 1_700_000_000_000}]}

        self.assertTrue(
            bar_context_is_stale(payload, now_ts=1_700_001_000.0, max_age_seconds=900.0)
        )

    def test_bar_context_is_not_stale_for_recent_intraday_bars(self):
        payload = {"bars_1m": [{"timestamp": 1_700_000_000_000}]}

        self.assertFalse(
            bar_context_is_stale(payload, now_ts=1_700_000_300.0, max_age_seconds=900.0)
        )


if __name__ == "__main__":
    unittest.main()
