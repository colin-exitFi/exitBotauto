import time
import unittest

from src.exit.exit_manager import ExitManager


class FakeRisk:
    def get_risk_tier(self):
        return {"stop_pct": 1.0}

    def record_trade(self, trade):
        # ExitManager calls this; no-op is fine for unit tests.
        pass


class FakeEntry:
    def __init__(self):
        self.positions = {}

    def update_peak_price(self, symbol, current_price):
        if symbol in self.positions and current_price > self.positions[symbol].get("peak_price", 0):
            self.positions[symbol]["peak_price"] = current_price

    def remove_position(self, symbol):
        self.positions.pop(symbol, None)


class ExitManagerShortLogicTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_atr_stop_uses_price_above_entry(self):
        entry = FakeEntry()
        em = ExitManager(alpaca_client=None, polygon_client=None, risk_manager=FakeRisk(), entry_manager=entry)
        position = {
            "symbol": "TSLA",
            "entry_price": 100.0,
            "quantity": 2,
            "side": "short",
            "peak_price": 100.0,
            "entry_time": time.time() - 60,
            "partial_exit": False,
            "atr_at_entry": 1.0,  # ATR stop should trigger at 101.5 with default multiplier 1.5
        }

        result = await em.check_and_exit(position, current_price=102.0, sentiment_score=0.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("reason"), "atr_stop_loss")
        self.assertEqual(result.get("side"), "buy_to_cover")

    async def test_short_trailing_stop_uses_low_water_mark_retrace(self):
        entry = FakeEntry()
        em = ExitManager(alpaca_client=None, polygon_client=None, risk_manager=FakeRisk(), entry_manager=entry)
        position = {
            "symbol": "NVDA",
            "entry_price": 100.0,
            "quantity": 1,
            "side": "short",
            "peak_price": 90.0,  # stored low-water mark for short
            "entry_time": time.time() - 300,
            "partial_exit": True,
            "atr_at_entry": None,
        }

        # Still profitable (+1%), but bounced hard from 90 -> 99, so trailing retrace should trigger.
        result = await em.check_and_exit(position, current_price=99.0, sentiment_score=0.0)
        self.assertIsNotNone(result)
        self.assertEqual(result.get("reason"), "trailing_stop")
        self.assertEqual(result.get("side"), "buy_to_cover")

