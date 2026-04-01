import time
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parents[1] / "src" / "exit" / "profit_ratchet.py"
_SPEC = spec_from_file_location("profit_ratchet_test_module", _MODULE_PATH)
_MODULE = module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
ProfitRatchet = _MODULE.ProfitRatchet


class ProfitRatchetFragilityTests(unittest.TestCase):
    def test_min_hold_suppresses_early_ratchet_exit_after_spike_and_reversal(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - 30.0,
            "side": "long",
            "peak_price": 101.7,
            "holding_horizon": "intraday",
        }

        action = ProfitRatchet.check_position(position, current_price=99.2, now=now)

        self.assertEqual(action["action"], "hold")
        self.assertTrue(action["min_hold_active"])
        self.assertFalse(action["ratchet_active"])
        self.assertIsNone(action["floor_pct"])

    def test_at_highs_entry_tightens_hard_stop(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - 300,
            "side": "long",
            "peak_price": 100.2,
            "entry_quality": "at_highs",
        }

        action = ProfitRatchet.check_position(position, current_price=98.0, now=now)

        self.assertEqual(action["action"], "hard_stop")
        self.assertAlmostEqual(action["hard_stop_pct"], ProfitRatchet.AT_HIGHS_HARD_STOP_PCT)
        self.assertIn("at_highs_entry", action["hard_stop_flags"])

    def test_observe_book_tightens_hard_stop(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - 300,
            "side": "long",
            "peak_price": 100.1,
            "allocator_status": "observe",
        }

        action = ProfitRatchet.check_position(position, current_price=97.75, now=now)

        self.assertEqual(action["action"], "hard_stop")
        self.assertAlmostEqual(action["hard_stop_pct"], ProfitRatchet.OBSERVE_HARD_STOP_PCT)
        self.assertIn("observe_book", action["hard_stop_flags"])

    def test_probation_book_tightens_hard_stop(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - 300,
            "side": "long",
            "peak_price": 100.1,
            "allocator_status": "probation",
        }

        action = ProfitRatchet.check_position(position, current_price=98.0, now=now)

        self.assertEqual(action["action"], "hard_stop")
        self.assertAlmostEqual(action["hard_stop_pct"], ProfitRatchet.PROBATION_HARD_STOP_PCT)
        self.assertIn("probation_book", action["hard_stop_flags"])

    def test_stalled_loser_tightens_and_triggers_earlier(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - (ProfitRatchet.STALLED_LOSER_HOURS * 3600.0) - 60.0,
            "side": "long",
            "peak_price": 100.3,
            "holding_horizon": "intraday",
        }

        action = ProfitRatchet.check_position(position, current_price=98.2, now=now)

        self.assertEqual(action["action"], "hard_stop")
        self.assertFalse(action["dead_money"])
        self.assertAlmostEqual(action["hard_stop_pct"], ProfitRatchet.STALLED_LOSER_HARD_STOP_PCT)
        self.assertIn("stalled_loser", action["hard_stop_flags"])

    def test_dead_money_still_owns_tightest_stop(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - (ProfitRatchet.DEAD_MONEY_HOURS * 3600.0) - 60.0,
            "side": "long",
            "peak_price": 100.2,
            "entry_quality": "at_highs",
        }

        action = ProfitRatchet.check_position(position, current_price=98.4, now=now)

        self.assertEqual(action["action"], "hard_stop")
        self.assertTrue(action["dead_money"])
        self.assertEqual(action["reason"], "dead_money_tight_stop_breached")
        self.assertAlmostEqual(action["hard_stop_pct"], ProfitRatchet.DEAD_MONEY_TIGHT_STOP_PCT)
        self.assertIn("at_highs_entry", action["hard_stop_flags"])
        self.assertIn("dead_money", action["hard_stop_flags"])


if __name__ == "__main__":
    unittest.main()
