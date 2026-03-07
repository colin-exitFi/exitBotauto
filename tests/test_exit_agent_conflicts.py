import unittest

from src.agents.exit_agent import ExitAgent


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


class ExitAgentConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_now_cancels_conflicting_orders_before_market_sell(self):
        broker = FakeBroker(
            open_orders=[
                {"id": "sell-stop", "symbol": "AAPL", "side": "sell", "type": "trailing_stop"},
                {"id": "buy-limit", "symbol": "AAPL", "side": "buy", "type": "limit"},
                {"id": "other-sell", "symbol": "MSFT", "side": "sell", "type": "limit"},
            ]
        )
        agent = ExitAgent(broker=broker, entry_manager=None, risk_manager=None)
        pos = {
            "symbol": "AAPL",
            "quantity": 4,
            "side": "long",
            "trailing_stop_order_id": "sell-stop",
        }

        await agent._execute_action("AAPL", pos, {"action": "EXIT_NOW", "reasoning": "risk breach"})

        self.assertEqual(broker.cancelled, ["sell-stop"])
        self.assertEqual(broker.market_sells, [("AAPL", 4)])


if __name__ == "__main__":
    unittest.main()
