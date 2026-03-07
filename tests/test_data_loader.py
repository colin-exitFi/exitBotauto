import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from backtester.data_loader import DataLoader


class DataLoaderTests(unittest.TestCase):
    def test_cache_hit_returns_cached_frame(self):
        with TemporaryDirectory() as tmp_dir:
            loader = DataLoader("test-key", cache_dir=tmp_dir, min_request_interval=0.0)
            df = pd.DataFrame(
                {
                    "open": [1.0, 2.0],
                    "high": [1.5, 2.5],
                    "low": [0.5, 1.5],
                    "close": [1.2, 2.2],
                    "volume": [100, 200],
                    "vwap": [1.1, 2.1],
                },
                index=pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC"),
            )
            parquet_path, pickle_path = loader._cache_paths("AAPL", "1day", "2026-01-01", "2026-01-02")
            loader._write_cache(df, parquet_path, pickle_path)

            with patch.object(loader, "_fetch_window", side_effect=AssertionError("cache should satisfy request")):
                cached = loader.get_bars("AAPL", "1day", "2026-01-01", "2026-01-02")

            self.assertEqual(len(cached), 2)
            self.assertAlmostEqual(float(cached.iloc[0]["close"]), 1.2)

    def test_fetch_window_follows_pagination(self):
        loader = DataLoader("test-key", min_request_interval=0.0)
        responses = [
            {
                "results": [{"o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1000, "vw": 10.4, "t": 1_700_000_000_000}],
                "next_url": "https://api.polygon.io/next",
            },
            {
                "results": [{"o": 10.5, "h": 12, "l": 10, "c": 11.5, "v": 1500, "vw": 11.2, "t": 1_700_000_060_000}],
            },
        ]

        with patch.object(loader, "_request_json", side_effect=responses):
            df = loader._fetch_window("AAPL", "1min", loader._parse_date("2026-01-01"), loader._parse_date("2026-01-02"))

        self.assertEqual(len(df), 2)
        self.assertIn("close", df.columns)
        self.assertAlmostEqual(float(df.iloc[-1]["vwap"]), 11.2)

    def test_rate_limit_sleeps_when_needed(self):
        loader = DataLoader("test-key", min_request_interval=2.0)
        loader._last_request_ts = 100.0

        with patch("backtester.data_loader.time.time", side_effect=[101.0, 101.0]), \
             patch.object(loader, "_sleep") as sleep_mock:
            loader._rate_limit()

        sleep_mock.assert_called_once()
        self.assertAlmostEqual(float(sleep_mock.call_args.args[0]), 1.0, places=2)


if __name__ == "__main__":
    unittest.main()
