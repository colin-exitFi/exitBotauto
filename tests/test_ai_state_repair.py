import unittest

import src.main as main_module


class _EntryManager:
    def __init__(self):
        self.positions = {
            "AMZN": {
                "symbol": "AMZN",
                "setup_mode": "continuation_long",
                "timing_state": "enter_now",
                "best_play": "buy_pullback",
                "direction_constraint": "long_only",
                "hold_style": "intraday",
            }
        }

    def get_positions(self):
        return list(self.positions.values())


class AIStateRepairTests(unittest.TestCase):
    def test_repair_last_consensus_uses_live_position_context(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = _EntryManager()
        bot.ai_layers = {
            "last_consensus": {
                "symbol": "AMZN",
                "decision": "BUY",
                "setup_mode": "invalid",
                "timing_state": "mode_conflict",
                "best_play": "",
                "direction_constraint": "none",
                "hold_style": "",
                "entry_now": True,
            }
        }

        bot._repair_last_consensus_snapshot()

        snapshot = bot.ai_layers["last_consensus"]
        self.assertEqual(snapshot["setup_mode"], "continuation_long")
        self.assertEqual(snapshot["timing_state"], "enter_now")
        self.assertEqual(snapshot["best_play"], "buy_pullback")
        self.assertEqual(snapshot["direction_constraint"], "long_only")
        self.assertEqual(snapshot["hold_style"], "intraday")


if __name__ == "__main__":
    unittest.main()
