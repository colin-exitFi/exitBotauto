import unittest
from unittest.mock import patch

import src.main as main_module
from src.entry.entry_manager import EntryManager


class _RiskOK:
    def can_open_position(self, current_positions, symbol=None):
        return True

    def can_enter_sector(self, symbol, positions):
        return True


class HaltHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_halted_symbol_blocks_new_entry(self):
        manager = EntryManager(alpaca_client=None, polygon_client=None, risk_manager=_RiskOK())
        manager._halted_symbols.add("AAPL")

        with patch.object(manager, "is_market_open", return_value=True):
            allowed = await manager.can_enter("AAPL", 0.8, [])

        self.assertFalse(allowed)
        self.assertEqual(manager.last_gate["reason"], "halted")

    async def test_halt_status_updates_global_set_and_position(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = type("EntryMgr", (), {"positions": {"AAPL": {}}, "_halted_symbols": set()})()

        with patch.object(main_module, "log_activity"):
            bot._on_halt_status("AAPL", "H", "LUDP", True)
            self.assertIn("AAPL", bot.entry_manager._halted_symbols)
            self.assertTrue(bot.entry_manager.positions["AAPL"]["halted"])
            bot._on_halt_status("AAPL", "T", "", False)

        self.assertNotIn("AAPL", bot.entry_manager._halted_symbols)
        self.assertFalse(bot.entry_manager.positions["AAPL"]["halted"])

    async def test_luld_sets_and_clears_at_risk_flag(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = type("EntryMgr", (), {"positions": {"AAPL": {"symbol": "AAPL", "side": "long", "entry_price": 100.0}}})()

        with patch.object(main_module, "log_activity"):
            bot._on_luld_status("AAPL", {"lower_band": 98.0, "upper_band": 110.0})
            self.assertTrue(bot.entry_manager.positions["AAPL"]["luld_at_risk"])
            bot._on_luld_status("AAPL", {"lower_band": 90.0, "upper_band": 110.0})

        self.assertFalse(bot.entry_manager.positions["AAPL"]["luld_at_risk"])


if __name__ == "__main__":
    unittest.main()
