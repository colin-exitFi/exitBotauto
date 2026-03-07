import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai.game_film import GameFilm
from src.data import strategy_controls


class StrategyControlsTests(unittest.TestCase):
    def test_manual_enable_stays_sticky_over_auto_disable(self):
        controls = strategy_controls.load_controls()
        recs = [
            {
                "strategy_tag": "social_momentum_long",
                "trades": 38,
                "win_rate_pct": 35.0,
                "pnl": -142.0,
                "reason": "win_rate=35%, pnl=-$142, trades=38",
            }
        ]
        controls = strategy_controls.apply_auto_disables(recs, controls)
        self.assertIn("social_momentum_long", strategy_controls.get_effective_disabled(controls))

        controls = strategy_controls.manual_enable(
            "social_momentum_long",
            "manual retest",
            controls,
        )
        self.assertNotIn("social_momentum_long", strategy_controls.get_effective_disabled(controls))

        controls = strategy_controls.apply_auto_disables(recs, controls)
        self.assertNotIn("social_momentum_long", strategy_controls.get_effective_disabled(controls))

    def test_manual_disable_and_enable_clear_conflicts(self):
        controls = strategy_controls.load_controls()
        controls = strategy_controls.manual_enable("fade_short", "test", controls)
        self.assertIn("fade_short", controls["manual_enabled"])

        controls = strategy_controls.manual_disable("fade_short", "bad edge", controls)
        self.assertNotIn("fade_short", controls["manual_enabled"])
        self.assertIn("fade_short", controls["manual_disabled"])

        controls = strategy_controls.manual_enable("fade_short", "retry", controls)
        self.assertNotIn("fade_short", controls["manual_disabled"])
        self.assertIn("fade_short", controls["manual_enabled"])

    def test_persistence_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td)
            controls_file = data_dir / "strategy_controls.json"
            with patch.object(strategy_controls, "DATA_DIR", data_dir), \
                 patch.object(strategy_controls, "CONTROLS_FILE", controls_file):
                controls = strategy_controls.load_controls()
                controls = strategy_controls.manual_disable("momentum_long", "bad", controls)
                strategy_controls.save_controls(controls)
                loaded = strategy_controls.load_controls()

        self.assertIn("momentum_long", loaded["manual_disabled"])


class GameFilmDisableThresholdTests(unittest.TestCase):
    def test_generate_recommendations_flags_only_strategies_past_threshold(self):
        film = GameFilm()
        insights = {
            "by_symbol": {},
            "by_hour": {},
            "by_exit_reason": {},
            "avg_winner_hold_min": 5,
            "avg_loser_hold_min": 8,
            "by_strategy_tag": {
                "social_momentum_long": {
                    "trades": 38,
                    "win_rate_pct": 25.0,
                    "pnl": -142.0,
                    "avg_pnl": -3.7,
                    "first_half": {"pnl": -70.0},
                    "second_half": {"pnl": -72.0},
                },
                "pharma_event_long": {
                    "trades": 22,
                    "win_rate_pct": 31.0,
                    "pnl": -70.0,
                    "avg_pnl": -3.2,
                    "first_half": {"pnl": -30.0},
                    "second_half": {"pnl": -40.0},
                },
                "copy_trader_long": {
                    "trades": 12,
                    "win_rate_pct": 32.0,
                    "pnl": -18.0,
                    "avg_pnl": -1.5,
                    "first_half": {"pnl": -10.0},
                    "second_half": {"pnl": 2.0},
                },
                "breakout_fast_path": {
                    "trades": 40,
                    "win_rate_pct": 52.0,
                    "pnl": 90.0,
                    "avg_pnl": 2.2,
                    "first_half": {"pnl": 20.0},
                    "second_half": {"pnl": 70.0},
                },
            },
        }
        recs = film._generate_recommendations(insights)
        hard_tags = {r.get("strategy_tag") for r in recs.get("disable_strategies", [])}
        soft_tags = {r.get("strategy_tag") for r in recs.get("soft_disable_strategies", [])}
        watch_tags = {r.get("strategy_tag") for r in recs.get("watch_list_strategies", [])}

        self.assertIn("social_momentum_long", hard_tags)
        self.assertIn("pharma_event_long", soft_tags)
        self.assertIn("copy_trader_long", watch_tags)
        self.assertNotIn("breakout_fast_path", hard_tags | soft_tags | watch_tags)


if __name__ == "__main__":
    unittest.main()
