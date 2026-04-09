import unittest
from unittest.mock import patch

from config import settings
from src.ai.game_film import GameFilm
from src.data import strategy_controls
from src.entry.entry_manager import EntryManager


class GameFilmGraduatedTests(unittest.TestCase):
    def test_generate_recommendations_warns_soft_disables_and_hard_disables(self):
        film = GameFilm()
        insights = {
            "by_strategy_tag": {
                "watch_me": {"trades": 10, "win_rate_pct": 30.0, "pnl": -20.0, "first_half": {"pnl": -5.0}, "second_half": {"pnl": 1.0}},
                "soft_me": {"trades": 20, "win_rate_pct": 30.0, "pnl": -40.0, "first_half": {"pnl": -5.0}, "second_half": {"pnl": -10.0}},
                "hard_me": {"trades": 30, "win_rate_pct": 25.0, "pnl": -80.0, "first_half": {"pnl": -20.0}, "second_half": {"pnl": -30.0}},
            },
            "by_symbol": {},
            "by_hour": {},
            "by_exit_reason": {},
            "avg_winner_hold_min": 0,
            "avg_loser_hold_min": 0,
        }

        recs = film._generate_recommendations(insights)

        self.assertIn("watch_list_strategies", recs)
        self.assertIn("soft_disable_strategies", recs)
        self.assertIn("disable_strategies", recs)
        self.assertEqual(len(recs["size_reductions"]), 2)

    def test_run_uses_learning_history_not_quarantined_rows(self):
        film = GameFilm()
        clean_history = [
            {"symbol": f"WIN{i}", "strategy_tag": "momentum_long", "pnl": 5.0, "exit_time": 1_700_000_000 + i}
            for i in range(5)
        ]
        quarantined_history = [
            {
                "symbol": f"LOSS{i}",
                "strategy_tag": "momentum_long",
                "pnl": -20.0,
                "exit_time": 1_700_000_100 + i,
                "anomaly_flags": ["broker_reconstructed"],
            }
            for i in range(10)
        ]
        raw_history = clean_history + quarantined_history

        with patch("src.ai.game_film.load_all", return_value=raw_history), \
             patch("src.ai.game_film.get_analytic_history", return_value=raw_history), \
             patch("src.ai.game_film.get_learning_history", return_value=clean_history), \
             patch("src.ai.game_film.get_quarantined_history", return_value=quarantined_history), \
             patch("src.ai.game_film.get_analytics", return_value={"quarantine": {"by_flag": {"broker_reconstructed": 10}, "by_reason": {}}}), \
             patch("src.ai.game_film.strategy_controls.load_controls", return_value=strategy_controls.load_controls()), \
             patch("src.ai.game_film.strategy_controls.apply_recommendations", side_effect=lambda recs, controls: controls), \
             patch("src.ai.game_film.strategy_controls.save_controls"), \
             patch.object(GameFilm, "_save"):
            import asyncio
            insights = asyncio.run(film.run())

        self.assertEqual(insights["total_trades"], 5)
        self.assertEqual(insights["raw_total_trades"], 15)
        self.assertEqual(insights["quarantined_trades"], 10)
        self.assertEqual(insights["by_strategy_tag"]["momentum_long"]["pnl"], 25.0)
        self.assertNotIn("disable_strategies", insights.get("recommendations", {}))

    def test_strategy_controls_multiplier_and_disable_gate(self):
        controls = strategy_controls.apply_recommendations(
            {
                "soft_disable_strategies": [{"strategy_tag": "fade_runner", "reason": "bad", "trades": 20, "win_rate_pct": 30.0, "pnl": -10.0}],
                "size_reductions": [{"strategy_tag": "watch_me", "size_multiplier": 0.5, "reason": "watch"}],
            },
            strategy_controls.load_controls(),
        )

        self.assertIn("fade_runner", strategy_controls.get_effective_disabled(controls))
        with patch.object(settings, "PAPER_MODE_IGNORE_CONTROL_PLANE_SIZE_REDUCTIONS", False), \
             patch.object(settings, "PAPER_MODE", True), \
             patch.object(settings, "ALPACA_PAPER", True):
            self.assertEqual(strategy_controls.get_size_multiplier("watch_me", controls), 0.5)

    def test_strategy_controls_ignores_size_reductions_in_paper_mode(self):
        controls = strategy_controls.apply_recommendations(
            {
                "size_reductions": [{"strategy_tag": "watch_me", "size_multiplier": 0.5, "reason": "watch"}],
                "probation_candidates": [{"strategy_tag": "retry_me", "probation_size_mult": 0.25, "reason": "retry"}],
            },
            strategy_controls.load_controls(),
        )

        with patch.object(settings, "PAPER_MODE_IGNORE_CONTROL_PLANE_SIZE_REDUCTIONS", True), \
             patch.object(settings, "PAPER_MODE", True), \
             patch.object(settings, "ALPACA_PAPER", True):
            self.assertEqual(strategy_controls.get_size_multiplier("watch_me", controls), 1.0)
            self.assertEqual(strategy_controls.get_size_multiplier("retry_me", controls), 0.25)

    def test_entry_manager_blocks_disabled_strategy(self):
        manager = EntryManager(alpaca_client=None, polygon_client=None, risk_manager=None)
        controls = strategy_controls.apply_recommendations(
            {"disable_strategies": [{"strategy_tag": "fade_runner", "reason": "bad", "trades": 30, "win_rate_pct": 25.0, "pnl": -10.0}]},
            strategy_controls.load_controls(),
        )
        with patch.object(strategy_controls, "load_controls", return_value=controls):
            adjusted = manager._apply_strategy_controls("AAPL", {"strategy_tag": "fade_runner"}, 1000.0)

        self.assertIsNone(adjusted)


if __name__ == "__main__":
    unittest.main()
