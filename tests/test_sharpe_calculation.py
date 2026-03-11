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

    def test_analytics_include_clean_pnl_and_first_five_minute_green_rates(self):
        with TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "trade_history.json"
            rows = [
                {
                    "symbol": "AAPL",
                    "entry_price": 100.0,
                    "quantity": 1,
                    "pnl": 5.0,
                    "pnl_pct": 5.0,
                    "strategy_tag": "fade_runner",
                    "price_at_1m": 101.0,
                    "price_at_3m": 103.0,
                    "price_at_5m": 104.0,
                    "mfe_pct": 6.0,
                    "mae_pct": -1.0,
                    "slippage_bps": 8.0,
                    "hold_seconds": 600,
                    "exit_time": 1_700_000_000,
                    "anomaly_flags": [],
                },
                {
                    "symbol": "TSLA",
                    "entry_price": 100.0,
                    "quantity": 1,
                    "pnl": -4.0,
                    "pnl_pct": -4.0,
                    "strategy_tag": "fade_runner",
                    "price_at_1m": 99.0,
                    "price_at_3m": 98.5,
                    "price_at_5m": 97.0,
                    "mfe_pct": 0.5,
                    "mae_pct": -4.5,
                    "slippage_bps": 12.0,
                    "hold_seconds": 420,
                    "exit_time": 1_700_000_600,
                    "anomaly_flags": ["duplicate_entry"],
                },
            ]
            history_file.write_text(__import__("json").dumps(rows))
            with patch.object(trade_history, "HISTORY_FILE", history_file):
                analytics = trade_history.get_analytics()

        fade = analytics["by_strategy_tag"]["fade_runner"]
        self.assertEqual(analytics["clean_pnl"], 5.0)
        self.assertEqual(fade["clean_pnl"], 5.0)
        self.assertEqual(fade["anomaly_count"], 1)
        self.assertEqual(fade["first_1m_green_rate_pct"], 50.0)
        self.assertEqual(fade["first_5m_green_rate_pct"], 50.0)
        self.assertAlmostEqual(fade["avg_slippage_bps"], 10.0, places=4)


if __name__ == "__main__":
    unittest.main()
