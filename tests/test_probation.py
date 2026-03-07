import unittest
from datetime import datetime, timedelta, timezone

from src.ai.game_film import GameFilm
from src.data import strategy_controls


class ProbationTests(unittest.TestCase):
    def test_disabled_strategy_becomes_probation_candidate_after_five_days(self):
        film = GameFilm()
        disabled_at = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat().replace("+00:00", "Z")
        controls = {
            "hard_disabled": {"fade_runner": {"disabled_at": disabled_at}},
            "soft_disabled": {},
            "manual_enabled": {},
            "manual_disabled": {},
            "size_reductions": {},
            "probation": {},
        }

        candidates = film.check_probation_candidates(controls)

        self.assertEqual(candidates[0]["strategy_tag"], "fade_runner")
        self.assertEqual(candidates[0]["probation_size_mult"], 0.25)

    def test_probation_success_and_failure(self):
        film = GameFilm()
        started_at = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat().replace("+00:00", "Z")
        controls = {"probation": {"fade_runner": {"started_at": started_at, "status": "active"}}}

        success_history = [
            {"strategy_tag": "fade_runner", "pnl": 5.0, "exit_time": datetime.now(timezone.utc).timestamp()}
            for _ in range(6)
        ] + [
            {"strategy_tag": "fade_runner", "pnl": -1.0, "exit_time": datetime.now(timezone.utc).timestamp()}
            for _ in range(4)
        ]
        failure_history = [
            {"strategy_tag": "fade_runner", "pnl": -2.0, "exit_time": datetime.now(timezone.utc).timestamp()}
            for _ in range(10)
        ]

        success = film.evaluate_probation(success_history, controls)
        failure = film.evaluate_probation(failure_history, controls)

        self.assertEqual(success["probation_passed"][0]["strategy_tag"], "fade_runner")
        self.assertEqual(failure["probation_failed"][0]["strategy_tag"], "fade_runner")

    def test_probation_multiplier_reenables_strategy_at_reduced_size(self):
        controls = strategy_controls.apply_recommendations(
            {"probation_candidates": [{"strategy_tag": "fade_runner", "probation_size_mult": 0.25, "reason": "retry"}]},
            strategy_controls.load_controls(),
        )

        self.assertNotIn("fade_runner", strategy_controls.get_effective_disabled(controls))
        self.assertEqual(strategy_controls.get_size_multiplier("fade_runner", controls), 0.25)


if __name__ == "__main__":
    unittest.main()
