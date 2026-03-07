import unittest
from unittest.mock import patch

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

    def test_strategy_controls_multiplier_and_disable_gate(self):
        controls = strategy_controls.apply_recommendations(
            {
                "soft_disable_strategies": [{"strategy_tag": "fade_runner", "reason": "bad", "trades": 20, "win_rate_pct": 30.0, "pnl": -10.0}],
                "size_reductions": [{"strategy_tag": "watch_me", "size_multiplier": 0.5, "reason": "watch"}],
            },
            strategy_controls.load_controls(),
        )

        self.assertIn("fade_runner", strategy_controls.get_effective_disabled(controls))
        self.assertEqual(strategy_controls.get_size_multiplier("watch_me", controls), 0.5)

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
