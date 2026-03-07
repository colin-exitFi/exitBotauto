import time
import unittest

from src.exit.exit_manager import ExitManager


class FakeRisk:
    def get_risk_tier(self):
        return {"stop_pct": 1.0}

    def record_trade(self, trade):
        # ExitManager calls this; no-op is fine for unit tests.
        pass

    def can_exit_position(self, position, reason: str = "", log_block: bool = True):
        return True


class FakeEntry:
    def __init__(self):
        self.positions = {}

    def update_peak_price(self, symbol, current_price):
        if symbol in self.positions and current_price > self.positions[symbol].get("peak_price", 0):
            self.positions[symbol]["peak_price"] = current_price

    def remove_position(self, symbol):
        self.positions.pop(symbol, None)


class FakeBroker:
    def __init__(self, open_orders=None):
        self.open_orders = list(open_orders or [])
        self.cancelled = []
        self.market_sells = []
        self.market_buys = []

    def get_orders(self, status="open"):
        if status == "open":
            return list(self.open_orders)
        return []

    def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        return True

    def place_market_sell(self, symbol, qty):
        self.market_sells.append((symbol, qty))
        return {"id": "mkt-sell"}

    def place_market_buy(self, symbol, qty):
        self.market_buys.append((symbol, qty))
        return {"id": "mkt-buy"}


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

    async def test_execute_exit_cancels_conflicting_open_orders_before_market_exit(self):
        entry = FakeEntry()
        broker = FakeBroker(
            open_orders=[
                {"id": "trail-1", "symbol": "AAPL", "side": "sell", "type": "trailing_stop"},
                {"id": "buy-open", "symbol": "AAPL", "side": "buy", "type": "limit"},
                {"id": "other-sym", "symbol": "MSFT", "side": "sell", "type": "limit"},
            ]
        )
        em = ExitManager(alpaca_client=broker, polygon_client=None, risk_manager=FakeRisk(), entry_manager=entry)
        position = {
            "symbol": "AAPL",
            "entry_price": 100.0,
            "quantity": 2,
            "side": "long",
            "entry_time": time.time() - 60,
        }

        trade = await em._execute_exit(position, quantity=2, price=99.0, reason="stop_loss", pnl_pct=-1.0)
        self.assertIsNotNone(trade)
        self.assertEqual(broker.cancelled, ["trail-1"])
        self.assertEqual(broker.market_sells, [("AAPL", 2)])

    async def test_execute_exit_blocks_same_day_swing_only_position(self):
        class _SwingRisk(FakeRisk):
            def can_exit_position(self, position, reason: str = "", log_block: bool = True):
                return False

        entry = FakeEntry()
        broker = FakeBroker()
        em = ExitManager(alpaca_client=broker, polygon_client=None, risk_manager=_SwingRisk(), entry_manager=entry)
        position = {
            "symbol": "AAPL",
            "entry_price": 100.0,
            "quantity": 2,
            "side": "long",
            "entry_time": time.time() - 60,
            "swing_only": True,
        }

        trade = await em._execute_exit(position, quantity=2, price=99.0, reason="stop_loss", pnl_pct=-1.0)
        self.assertIsNone(trade)
        self.assertEqual(broker.market_sells, [])
