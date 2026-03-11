import time
import unittest
from datetime import datetime, timezone
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

    def test_market_sell_falls_back_to_close_position_for_htb_rejection(self):
        client = AlpacaClient()
        client._initialized = True
        client.get_position = lambda symbol: {"symbol": symbol, "quantity": 5.0}
        client.cancel_related_orders_from_error = lambda symbol, message, preferred_side="sell": 0

        class _TradingClient:
            def submit_order(self, req):
                raise RuntimeError("asset BATL cannot be sold short")

            def close_position(self, symbol, close_options=None):
                self.symbol = symbol
                self.close_options = close_options
                return type("OrderStub", (), {"id": "close-1"})()

        client._trading_client = _TradingClient()
        client._order_to_dict = lambda order: {"id": order.id}

        order = client.place_market_sell("BATL", 5)

        self.assertEqual(order["id"], "close-1")
        self.assertEqual(client._trading_client.symbol, "BATL")
        self.assertEqual(client._trading_client.close_options.qty, "5.0")


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

    def test_sync_positions_from_brokerage_backfills_carryover_entry_time_from_orders(self):
        t1 = datetime(2026, 3, 6, 14, 0, tzinfo=timezone.utc).timestamp()
        t2 = datetime(2026, 3, 6, 18, 0, tzinfo=timezone.utc).timestamp()
        t3 = datetime(2026, 3, 7, 13, 30, tzinfo=timezone.utc).timestamp()

        class _Broker:
            def get_orders(self, status="open"):
                self.last_status = status
                return [
                    {"symbol": "RLMD", "side": "buy", "filled_qty": "10", "created_at": datetime.fromtimestamp(t1, timezone.utc).isoformat()},
                    {"symbol": "RLMD", "side": "sell", "filled_qty": "10", "created_at": datetime.fromtimestamp(t2, timezone.utc).isoformat()},
                    {"symbol": "RLMD", "side": "buy", "filled_qty": "3", "created_at": datetime.fromtimestamp(t3, timezone.utc).isoformat()},
                ]

        manager = EntryManager.__new__(EntryManager)
        manager.positions = {}
        manager.broker = _Broker()

        updates = manager.sync_positions_from_brokerage(
            [
                {
                    "symbol": "RLMD",
                    "quantity": 3.0,
                    "side": "long",
                    "average_price": 6.07,
                    "current_price": 6.13,
                    "open_pnl": 0.18,
                }
            ]
        )

        self.assertEqual(updates, 1)
        self.assertEqual(manager.positions["RLMD"]["entry_time_source"], "broker_orders")
        self.assertAlmostEqual(manager.positions["RLMD"]["entry_time"], t3, delta=1.0)


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

    async def test_stale_tracked_position_is_removed_before_ai_call(self):
        class _Broker:
            def get_positions(self):
                return []

        class _EntryManager:
            def __init__(self):
                self.removed = []

            def remove_position(self, symbol):
                self.removed.append(symbol)

        entry_manager = _EntryManager()
        agent = ExitAgent(broker=_Broker(), entry_manager=entry_manager, risk_manager=None)
        pos = {
            "symbol": "BHVN",
            "entry_price": 10.0,
            "quantity": 0.58,
            "side": "long",
            "trail_pct": 3.0,
            "entry_time": time.time() - 300,
        }

        with patch("src.agents.exit_agent.call_claude", side_effect=AssertionError("AI should not be called")):
            action = await agent._evaluate_position(pos)

        self.assertIsNone(action)
        self.assertEqual(entry_manager.removed, ["BHVN"])


if __name__ == "__main__":
    unittest.main()
