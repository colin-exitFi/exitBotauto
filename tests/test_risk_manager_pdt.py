import time
import unittest
from unittest.mock import patch

from src.risk.risk_manager import RiskManager


class RiskManagerPDTTests(unittest.TestCase):
    def _build_manager(self) -> RiskManager:
        with patch.object(RiskManager, "_load_state", lambda self: None):
            manager = RiskManager()
        manager._equity = 24900.0
        manager.trading_halted = False
        manager.daily_pnl = 0.0
        return manager

    def test_can_open_position_allows_entries_when_day_trade_cap_hit(self):
        manager = self._build_manager()
        now = time.time()
        manager._round_trips = [
            {"symbol": "AAPL", "entry_time": now - 3600, "exit_time": now - 1800, "pnl": 10.0},
            {"symbol": "MSFT", "entry_time": now - 7200, "exit_time": now - 3600, "pnl": -5.0},
            {"symbol": "NVDA", "entry_time": now - 10800, "exit_time": now - 5400, "pnl": 3.0},
        ]

        allowed = manager.can_open_position(current_positions=[], symbol="TSLA")
        self.assertTrue(allowed)

    def test_can_open_position_uses_alpaca_daytrade_count_over_internal_count(self):
        manager = self._build_manager()
        now = time.time()
        manager._round_trips = [
            {"symbol": "AAPL", "entry_time": now - 3600, "exit_time": now - 1800, "pnl": 10.0},
            {"symbol": "MSFT", "entry_time": now - 7200, "exit_time": now - 3600, "pnl": -5.0},
            {"symbol": "NVDA", "entry_time": now - 10800, "exit_time": now - 5400, "pnl": 3.0},
            {"symbol": "TSLA", "entry_time": now - 14400, "exit_time": now - 7200, "pnl": 2.0},
        ]
        manager.update_equity(24900.0, daytrade_count=2)

        allowed = manager.can_open_position(current_positions=[], symbol="META")

        self.assertTrue(allowed)
        self.assertEqual(manager.remaining_day_trades(), 1)

    def test_suspicious_alpaca_daytrade_count_falls_back_to_internal(self):
        manager = self._build_manager()
        manager._round_trips = []
        manager.update_equity(24900.0, daytrade_count=168)

        allowed = manager.can_open_position(current_positions=[], symbol="META")

        self.assertTrue(allowed)
        self.assertEqual(manager.remaining_day_trades(), 3)
        self.assertFalse(manager.is_swing_mode())

    def test_can_open_position_still_blocks_on_max_positions(self):
        manager = self._build_manager()
        manager._round_trips = []
        manager.get_risk_tier = lambda equity=None: {
            "name": "TEST",
            "max_positions": 1,
            "daily_loss_pct": 4.0,
        }

        allowed = manager.can_open_position(current_positions=[{"symbol": "AAPL"}], symbol="TSLA")
        self.assertFalse(allowed)

    def test_swing_mode_reduces_position_size(self):
        manager = self._build_manager()
        manager.update_equity(24900.0, daytrade_count=3)
        with patch("src.risk.risk_manager.settings.SWING_MODE_SIZE_REDUCTION", 0.7):
            size = manager.get_position_size(price=100.0, buying_power=100000.0, conviction="normal")
        expected = round(24900.0 * (2.5 / 100.0) * 0.7, 2)
        self.assertEqual(size, expected)

    def test_paper_mode_bypasses_wash_sale_block(self):
        manager = self._build_manager()
        manager._wash_sale_list["AAPL"] = {"loss": -12.5, "exit_time": time.time()}

        with patch("src.risk.risk_manager.settings.PAPER_MODE", True):
            self.assertFalse(manager.is_wash_sale("AAPL"))

    def test_alpaca_paper_mode_bypasses_wash_sale_even_if_paper_mode_flag_is_false(self):
        manager = self._build_manager()
        manager._wash_sale_list["AAPL"] = {"loss": -12.5, "exit_time": time.time()}

        with patch("src.risk.risk_manager.settings.PAPER_MODE", False), \
             patch("src.risk.risk_manager.settings.ALPACA_PAPER", True):
            self.assertFalse(manager.is_wash_sale("AAPL"))

    def test_sector_map_blocks_biotech_cluster_for_new_healthcare_names(self):
        manager = self._build_manager()
        manager._equity = 1000.0
        positions = [
            {"symbol": "BHVN", "entry_price": 20.0, "quantity": 25.0},
        ]

        with patch("src.risk.risk_manager.settings.MAX_SECTOR_PCT", 40.0):
            allowed = manager.can_enter_sector("DAWN", positions)

        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
