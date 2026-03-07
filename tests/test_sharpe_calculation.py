import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from src.ai import trade_history


class SharpeCalculationTests(unittest.TestCase):
    def test_no_trades_returns_zero_sharpe(self):
        with TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "trade_history.json"
            with patch.object(trade_history, "HISTORY_FILE", history_file):
                analytics = trade_history.get_analytics()

        self.assertEqual(analytics["sharpe_ratio"], 0.0)

    def test_positive_series_produces_positive_sharpe(self):
        with TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "trade_history.json"
            rows = [
                {"symbol": "AAPL", "entry_price": 100.0, "quantity": 1, "pnl": pnl, "pnl_pct": pct, "exit_time": 1_700_000_000 + i * 86400}
                for i, (pnl, pct) in enumerate([(10, 10), (5, 5), (8, 8), (-2, -2), (6, 6)])
            ]
            history_file.write_text(__import__("json").dumps(rows))
            with patch.object(trade_history, "HISTORY_FILE", history_file):
                analytics = trade_history.get_analytics()

        self.assertGreater(analytics["sharpe_ratio"], 0.0)

    def test_recent_50_sharpe_is_independent(self):
        with TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "trade_history.json"
            rows = []
            for i in range(60):
                pct = 5 if i < 10 else -1
                rows.append({"symbol": "AAPL", "entry_price": 100.0, "quantity": 1, "pnl": pct, "pnl_pct": pct, "exit_time": 1_700_000_000 + i * 3600})
            history_file.write_text(__import__("json").dumps(rows))
            with patch.object(trade_history, "HISTORY_FILE", history_file):
                analytics = trade_history.get_analytics()

        self.assertNotEqual(analytics["sharpe_ratio"], analytics["sharpe_ratio_recent_50"])


if __name__ == "__main__":
    unittest.main()
