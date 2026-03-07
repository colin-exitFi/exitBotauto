import io
import json
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import pandas as pd

from backtester import cli


class BacktesterCliTests(unittest.TestCase):
    def test_list_indicators_prints_registry_names(self):
        stdout = io.StringIO()
        with patch.object(sys, "argv", ["python", "--list-indicators"]), redirect_stdout(stdout):
            cli.main()

        output = stdout.getvalue()
        self.assertIn("supertrend", output)
        self.assertIn("ema_crossover", output)

    def test_fetch_only_writes_cache_manifest(self):
        df = pd.DataFrame(
            {
                "open": [1.0, 2.0],
                "high": [1.2, 2.2],
                "low": [0.9, 1.9],
                "close": [1.1, 2.1],
                "volume": [100, 200],
                "vwap": [1.05, 2.05],
            },
            index=pd.date_range("2026-01-01", periods=2, freq="D", tz="UTC"),
        )

        with TemporaryDirectory() as tmp_dir:
            stdout = io.StringIO()
            output_dir = Path(tmp_dir) / "out"
            cache_dir = Path(tmp_dir) / "cache"
            argv = [
                "python",
                "--fetch-only",
                "--symbols",
                "AAPL,MSFT",
                "--timeframe",
                "1day",
                "--start",
                "2026-01-01",
                "--end",
                "2026-01-02",
                "--output",
                str(output_dir),
                "--cache-dir",
                str(cache_dir),
            ]
            with patch.object(sys, "argv", argv), \
                 patch("backtester.cli.DataLoader.get_bars", return_value=df), \
                 redirect_stdout(stdout):
                cli.main()

            manifest = json.loads((output_dir / "cache_manifest.json").read_text())

        self.assertEqual(len(manifest["symbols"]), 2)
        self.assertTrue(manifest["symbols"][0]["has_data"])


if __name__ == "__main__":
    unittest.main()
