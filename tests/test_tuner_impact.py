import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import src.ai.tuner as tuner_module


class TunerImpactTests(unittest.IsolatedAsyncioTestCase):
    async def test_snapshot_performance_reads_current_metrics(self):
        with TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config_state.json"
            impact_file = Path(tmp_dir) / "tuner_impact.json"
            with patch.object(tuner_module, "CONFIG_STATE_FILE", config_file), \
                 patch.object(tuner_module, "IMPACT_STATE_FILE", impact_file), \
                 patch.object(tuner_module, "get_analytics", return_value={"total_trades": 12, "win_rate": 0.5, "total_pnl": 42.0, "sharpe_ratio": 1.2, "recent_20": {"win_rate_pct": 55.0, "pnl": 8.0}}):
                tuner = tuner_module.Tuner()
                snapshot = tuner._snapshot_performance()

        self.assertEqual(snapshot["trade_count"], 12)
        self.assertEqual(snapshot["total_pnl"], 42.0)
        self.assertEqual(snapshot["sharpe"], 1.2)

    async def test_measure_impact_waits_for_15_trades(self):
        with TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config_state.json"
            impact_file = Path(tmp_dir) / "tuner_impact.json"
            with patch.object(tuner_module, "CONFIG_STATE_FILE", config_file), \
                 patch.object(tuner_module, "IMPACT_STATE_FILE", impact_file), \
                 patch.object(tuner_module, "get_analytics", return_value={"total_trades": 20, "win_rate": 0.5, "total_pnl": 10.0, "sharpe_ratio": 1.0, "recent_20": {}}):
                tuner = tuner_module.Tuner()
                tuner._impact_history = [
                    {
                        "param": "STOP_LOSS_PCT",
                        "new_value": 1.5,
                        "snapshot_trade_count": 10,
                        "snapshot_win_rate": 0.45,
                        "snapshot_pnl": 5.0,
                        "snapshot_sharpe": 0.5,
                    }
                ]
                measured = await tuner.measure_impact()

        self.assertEqual(measured, [])

    async def test_measure_impact_marks_helped_and_hurtful_change(self):
        with TemporaryDirectory() as tmp_dir:
            config_file = Path(tmp_dir) / "config_state.json"
            impact_file = Path(tmp_dir) / "tuner_impact.json"
            with patch.object(tuner_module, "CONFIG_STATE_FILE", config_file), \
                 patch.object(tuner_module, "IMPACT_STATE_FILE", impact_file), \
                 patch.object(tuner_module, "get_analytics", return_value={"total_trades": 30, "win_rate": 0.6, "total_pnl": 20.0, "sharpe_ratio": 1.5, "recent_20": {}}):
                tuner = tuner_module.Tuner()
                tuner._impact_history = [
                    {
                        "param": "STOP_LOSS_PCT",
                        "new_value": 1.5,
                        "snapshot_trade_count": 10,
                        "snapshot_win_rate": 0.45,
                        "snapshot_pnl": 5.0,
                        "snapshot_sharpe": 0.5,
                    },
                    {
                        "param": "TRAILING_STOP_PCT",
                        "new_value": 0.8,
                        "snapshot_trade_count": 10,
                        "snapshot_win_rate": 0.7,
                        "snapshot_pnl": 30.0,
                        "snapshot_sharpe": 2.0,
                    },
                ]
                measured = await tuner.measure_impact()
                tuner._impact_history[1]["verdict"] = "hurt"

        self.assertEqual(measured[0]["verdict"], "helped")
        self.assertTrue(tuner._was_hurtful_change("TRAILING_STOP_PCT", 0.8))


if __name__ == "__main__":
    unittest.main()
