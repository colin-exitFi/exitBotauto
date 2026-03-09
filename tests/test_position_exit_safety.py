import unittest
from unittest.mock import patch

from src.agents.exit_agent import ExitAgent
from src.broker.alpaca_client import AlpacaClient
from src.entry.entry_manager import EntryManager


class _FakeTradingClient:
    def __init__(self):
        self.last_order = None

    def submit_order(self, req):
        self.last_order = req
        return type("OrderStub", (), {"id": "order-1"})()


class _FakeResponse:
    def __init__(self, status_code=201, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return dict(self._payload)


class AlpacaExitSafetyTests(unittest.TestCase):
    def test_market_sell_uses_fractional_qty_and_clamps_to_broker_position(self):
        client = AlpacaClient()
        client._initialized = True
        client._trading_client = _FakeTradingClient()
        client._order_to_dict = lambda order: dict(client._trading_client.last_order)
        client.get_position = lambda symbol: {"symbol": symbol, "quantity": 1.75}

        with patch("src.broker.alpaca_client.MarketOrderRequest", side_effect=lambda **kwargs: kwargs):
            order = client.place_market_sell("BNO", 7)

        self.assertIsNotNone(order)
        self.assertEqual(order["qty"], 1.75)
        self.assertNotIn("notional", order)

    def test_trailing_stop_uses_day_tif_and_whole_broker_qty(self):
        client = AlpacaClient()
        client._initialized = True
        client.get_position = lambda symbol: {"symbol": symbol, "quantity": 12.9}
        captured = {}

        def _fake_post(url, headers=None, json=None, timeout=10):
            captured["payload"] = dict(json or {})
            return _FakeResponse(
                status_code=201,
                payload={"id": "trail-1", "status": "new", "hwm": "90", "stop_price": "87"},
            )

        with patch("src.broker.alpaca_client.requests.post", side_effect=_fake_post):
            order = client.place_trailing_stop("HIMS", 20, trail_percent=3.0)

        self.assertIsNotNone(order)
        self.assertEqual(captured["payload"]["time_in_force"], "day")
        self.assertEqual(captured["payload"]["qty"], "12")


class EntryManagerSyncTests(unittest.TestCase):
    def test_sync_positions_from_brokerage_updates_qty_and_marks_fractional_remainder(self):
        manager = EntryManager.__new__(EntryManager)
        manager.positions = {
            "XENE": {
                "symbol": "XENE",
                "quantity": 4.0,
                "side": "long",
            }
        }

        updates = manager.sync_positions_from_brokerage(
            [
                {
                    "symbol": "XENE",
                    "quantity": 0.11,
                    "side": "long",
                    "average_price": 59.85,
                    "current_price": 62.05,
                    "open_pnl": 0.25,
                }
            ]
        )

        self.assertEqual(updates, 1)
        self.assertAlmostEqual(manager.positions["XENE"]["quantity"], 0.11, places=6)
        self.assertTrue(manager.positions["XENE"]["_dust_remainder"])


class ExitAgentFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_rule_based_exit_now_when_ai_is_unavailable_and_position_is_losing(self):
        class _Broker:
            def get_positions(self):
                return [{"symbol": "BATL", "current_price": 87.0}]

        agent = ExitAgent(broker=_Broker(), entry_manager=None, risk_manager=None)
        pos = {
            "symbol": "BATL",
            "entry_price": 100.0,
            "quantity": 5.0,
            "side": "long",
            "trail_pct": 3.0,
            "entry_time": 0,
        }

        with patch("src.agents.exit_agent.call_claude", side_effect=RuntimeError("rate limited")):
            action = await agent._evaluate_position(pos)

        self.assertEqual(action["action"], "EXIT_NOW")


if __name__ == "__main__":
    unittest.main()
