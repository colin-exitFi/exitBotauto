import unittest
from unittest.mock import patch

import src.agents.exit_agent as exit_agent_module
from src.agents.exit_agent import ExitAgent


class FakeBroker:
    def __init__(self, open_orders=None):
        self.open_orders = list(open_orders or [])
        self.cancelled = []
        self.market_sells = []
        self.market_buys = []
        self.positions = []

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

    def get_positions(self):
        return list(self.positions)


class _EntryManager:
    def __init__(self):
        self.removed = []

    def remove_position(self, symbol):
        self.removed.append(symbol)


class ExitAgentConflictTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_now_cancels_conflicting_orders_before_market_sell(self):
        broker = FakeBroker(
            open_orders=[
                {"id": "sell-stop", "symbol": "AAPL", "side": "sell", "type": "trailing_stop"},
                {"id": "buy-limit", "symbol": "AAPL", "side": "buy", "type": "limit"},
                {"id": "other-sell", "symbol": "MSFT", "side": "sell", "type": "limit"},
            ]
        )
        entry_manager = _EntryManager()
        agent = ExitAgent(broker=broker, entry_manager=entry_manager, risk_manager=None)
        pos = {
            "symbol": "AAPL",
            "quantity": 4,
            "side": "long",
            "trailing_stop_order_id": "sell-stop",
        }
        action = {
            "action": "EXIT_NOW",
            "reasoning": "risk breach",
            "hold_seconds": 600,
            "pnl_pct": -0.2,
            "current_price": 189.5,
        }

        with patch.object(exit_agent_module.settings, "EXIT_AGENT_EXECUTE_EXIT_NOW_ENABLED", True), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_CONFIRMATIONS", 2), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_MIN_HOLD_MINUTES", 3.0), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_MAX_PNL_PCT", 0.5), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_CONFIRM_WINDOW_SECONDS", 900.0):
            await agent._execute_action("AAPL", pos, dict(action))
            self.assertEqual(broker.cancelled, [])
            self.assertEqual(broker.market_sells, [])
            await agent._execute_action("AAPL", pos, dict(action))

        self.assertEqual(broker.cancelled, ["sell-stop"])
        self.assertEqual(broker.market_sells, [("AAPL", 4)])
        self.assertEqual(entry_manager.removed, [])
        self.assertTrue(pos.get("exit_pending"))
        self.assertEqual(pos.get("exit_order_id"), "mkt-sell")


if __name__ == "__main__":
    unittest.main()
