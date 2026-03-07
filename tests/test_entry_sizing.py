import unittest

from src.entry.entry_manager import EntryManager


class _DummyPolygon:
    @staticmethod
    def get_price(symbol: str) -> float:
        return 100.0


class _DummyBroker:
    def __init__(self):
        self.smart_buy_calls = []
        self.trailing_stop_calls = []

    @staticmethod
    def get_positions():
        return []

    @staticmethod
    def get_balances():
        return {"buying_power": 100000.0}

    def smart_buy(self, symbol: str, notional: float):
        self.smart_buy_calls.append((symbol, float(notional)))
        qty = max(1, int(notional / 100.0))
        return {"id": "order-1", "filled_qty": str(qty), "qty": str(qty)}

    @staticmethod
    def place_limit_buy(symbol: str, qty: int, limit_price: float, extended_hours: bool = False):
        return {"id": "order-limit-1", "filled_qty": str(qty), "qty": str(qty), "limit_price": str(limit_price)}

    def place_trailing_stop(self, symbol: str, qty: int, trail_pct: float):
        self.trailing_stop_calls.append((symbol, qty, float(trail_pct)))
        return {"id": "trail-1", "symbol": symbol, "qty": str(qty), "trail_percent": str(trail_pct)}


class _DummyRisk:
    @staticmethod
    def get_buying_power_field(balances: dict):
        return float(balances.get("buying_power", 0) or 0)

    @staticmethod
    def get_position_size(price: float, buying_power: float, conviction: str):
        return 1000.0

    @staticmethod
    def get_shares(price: float, notional: float):
        return int(notional / price)

    @staticmethod
    def get_risk_tier():
        return {"name": "TEST", "size_pct": 2.5}

    @staticmethod
    def can_open_position(current_positions, symbol: str = ""):
        return True

    @staticmethod
    def can_enter_sector(symbol: str, current_positions):
        return True

    @staticmethod
    def is_swing_mode():
        return False


class EntrySizingTests(unittest.IsolatedAsyncioTestCase):
    async def test_can_enter_blocks_duplicate_positions_unconditionally(self):
        entry = EntryManager(
            alpaca_client=_DummyBroker(),
            polygon_client=_DummyPolygon(),
            risk_manager=_DummyRisk(),
        )
        entry.is_market_open = lambda: True
        entry.positions["AAPL"] = {
            "symbol": "AAPL",
            "entry_price": 100.0,
            "peak_price": 110.0,
            "quantity": 5,
        }

        allowed = await entry.can_enter("AAPL", 0.8, current_positions=entry.get_positions())

        self.assertFalse(allowed)

    async def test_share_notional_multiplier_reduces_share_budget(self):
        broker = _DummyBroker()
        entry = EntryManager(
            alpaca_client=broker,
            polygon_client=_DummyPolygon(),
            risk_manager=_DummyRisk(),
        )
        entry.is_extended_hours = lambda: False

        sentiment_data = {
            "score": 0.4,
            "share_notional_multiplier": 0.5,
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
        }
        pos = await entry.enter_position("AAPL", sentiment_data)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(float(pos.get("notional", 0)), 500.0, places=2)
        self.assertEqual(int(float(pos.get("quantity", 0))), 5)
        self.assertTrue(broker.smart_buy_calls)
        self.assertAlmostEqual(broker.smart_buy_calls[0][1], 500.0, places=2)

    async def test_position_uses_filled_qty_for_quantity_and_notional(self):
        class _PartialFillBroker(_DummyBroker):
            def smart_buy(self, symbol: str, notional: float):
                self.smart_buy_calls.append((symbol, float(notional)))
                return {
                    "id": "order-2",
                    "qty": "5",
                    "filled_qty": "3",
                    "filled_avg_price": "101",
                    "status": "partially_filled",
                }

        broker = _PartialFillBroker()
        entry = EntryManager(
            alpaca_client=broker,
            polygon_client=_DummyPolygon(),
            risk_manager=_DummyRisk(),
        )
        entry.is_extended_hours = lambda: False

        sentiment_data = {
            "score": 0.4,
            "share_notional_multiplier": 0.5,
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
        }
        pos = await entry.enter_position("AAPL", sentiment_data)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(float(pos.get("quantity", 0)), 3.0, places=6)
        self.assertAlmostEqual(float(pos.get("entry_price", 0)), 101.0, places=6)
        self.assertAlmostEqual(float(pos.get("notional", 0)), 303.0, places=6)

    async def test_copy_trader_size_multiplier_boosts_notional(self):
        broker = _DummyBroker()
        entry = EntryManager(
            alpaca_client=broker,
            polygon_client=_DummyPolygon(),
            risk_manager=_DummyRisk(),
        )
        entry.is_extended_hours = lambda: False

        sentiment_data = {
            "score": 0.4,
            "copy_trader_size_multiplier": 1.2,
            "copy_trader_handles": ["traderstewie", "alphatrends"],
            "copy_trader_context": "2 Tier-1 trader signals",
            "strategy_tag": "momentum_long",
            "signal_sources": ["copy_trader"],
        }
        pos = await entry.enter_position("AAPL", sentiment_data)
        self.assertIsNotNone(pos)
        self.assertAlmostEqual(float(pos.get("notional", 0)), 1200.0, places=2)
        self.assertEqual(int(float(pos.get("quantity", 0))), 12)
        self.assertAlmostEqual(broker.smart_buy_calls[0][1], 1200.0, places=2)

    async def test_swing_only_entry_defers_trailing_stop_and_widens_trail(self):
        class _SwingRisk(_DummyRisk):
            @staticmethod
            def is_swing_mode():
                return True

        broker = _DummyBroker()
        entry = EntryManager(
            alpaca_client=broker,
            polygon_client=_DummyPolygon(),
            risk_manager=_SwingRisk(),
        )
        entry.is_extended_hours = lambda: False

        sentiment_data = {
            "score": 0.4,
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
            "jury_trail_pct": 2.0,
        }

        pos = await entry.enter_position("AAPL", sentiment_data)

        self.assertIsNotNone(pos)
        self.assertTrue(pos.get("swing_only"))
        self.assertFalse(pos.get("has_trailing_stop"))
        self.assertEqual(broker.trailing_stop_calls, [])
        self.assertGreaterEqual(float(pos.get("trail_pct", 0)), 4.5)


if __name__ == "__main__":
    unittest.main()
