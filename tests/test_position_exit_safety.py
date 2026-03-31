import time
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import src.main as main_module
from src.agents.exit_agent import ExitAgent
from src.broker.alpaca_client import AlpacaClient
from src.entry.entry_manager import EntryManager
from src.exit.profit_ratchet import ProfitRatchet


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
    def test_get_positions_skips_option_contracts(self):
        class _Position:
            def __init__(self, symbol, qty, asset_class="us_equity"):
                self.symbol = symbol
                self.qty = qty
                self.side = "long"
                self.avg_entry_price = "10"
                self.current_price = "10.5"
                self.market_value = "105"
                self.unrealized_pl = "5"
                self.unrealized_plpc = "0.05"
                self.asset_class = asset_class

        class _TradingClient:
            def get_all_positions(self):
                return [
                    _Position("AAPL", "10", "us_equity"),
                    _Position("AAPL260417C00180000", "1", "us_option"),
                ]

        client = AlpacaClient()
        client._initialized = True
        client._trading_client = _TradingClient()

        positions = client.get_positions()

        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "AAPL")

    def test_market_buy_float_short_cover_uses_share_qty_not_notional(self):
        client = AlpacaClient()
        client._initialized = True
        client._trading_client = _FakeTradingClient()
        client._order_to_dict = lambda order: dict(client._trading_client.last_order)
        client.get_position = lambda symbol: {"symbol": symbol, "quantity": 0.9985, "side": "short"}

        with patch("src.broker.alpaca_client.MarketOrderRequest", side_effect=lambda **kwargs: kwargs):
            order = client.place_market_buy("SPY", 1.0)

        self.assertIsNotNone(order)
        self.assertAlmostEqual(order["qty"], 0.9985, places=6)
        self.assertNotIn("notional", order)

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
    def test_reconciler_sync_ignores_option_like_symbols(self):
        from src.reconciliation.reconciler import Reconciler

        class _EntryManager:
            def __init__(self):
                self.calls = []

            def sync_positions_from_brokerage(self, rows):
                self.calls.append(list(rows))
                return len(rows)

        manager = _EntryManager()
        reconciler = Reconciler(alpaca_client=None, entry_manager=manager)

        reconciler._sync_internal_positions_with_broker(
            {
                "broker_positions": {
                    "AAPL": {"qty": 10, "side": "long", "avg_entry_price": 180},
                    "AAPL260417C00180000": {"qty": 1, "side": "long", "avg_entry_price": 2.5},
                }
            }
        )

        self.assertEqual(len(manager.calls), 1)
        self.assertEqual(manager.calls[0], [{"symbol": "AAPL", "quantity": 10.0, "side": "long", "average_price": 180.0, "current_price": 180.0}])

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

    def test_sync_positions_from_brokerage_marks_recently_removed_reload_reason(self):
        class _Broker:
            def get_orders(self, status="open"):
                if status == "open":
                    return [
                        {"symbol": "CRCL", "side": "buy", "id": "exit-1"},
                    ]
                return []

        manager = EntryManager.__new__(EntryManager)
        manager.positions = {}
        manager.broker = _Broker()
        manager._recently_removed_positions = {
            "CRCL": {
                "removed_at": time.time(),
                "last_exit_reason": "advisor_strategic_exit",
                "exit_order_id": "exit-1",
                "quantity": 1.0,
                "side": "short",
                "position": {
                    "strategy_tag": "uw_flow_short",
                    "entry_path": "jury",
                    "entry_reason_code": "uw_flow",
                    "signal_sources": ["unusual_whales_stream"],
                },
            }
        }

        updates = manager.sync_positions_from_brokerage(
            [
                {
                    "symbol": "CRCL",
                    "quantity": -0.65,
                    "side": "short",
                    "average_price": 113.65,
                    "current_price": 118.70,
                    "open_pnl": -3.30,
                }
            ]
        )

        self.assertEqual(updates, 1)
        self.assertEqual(manager.positions["CRCL"]["reload_reason"], "broker_still_open_after_local_removal_pending_exit")
        self.assertTrue(manager.positions["CRCL"]["reloaded_from_broker"])
        self.assertTrue(manager.positions["CRCL"]["exit_pending"])
        self.assertNotIn("carryover_sync", manager.positions["CRCL"]["anomaly_flags"])
        self.assertNotIn("broker_reloaded_after_local_removal", manager.positions["CRCL"]["anomaly_flags"])

    def test_sync_positions_from_brokerage_marks_unknown_restore_as_carryover(self):
        manager = EntryManager.__new__(EntryManager)
        manager.positions = {}
        manager.broker = None
        manager._recently_removed_positions = {}

        updates = manager.sync_positions_from_brokerage(
            [
                {
                    "symbol": "RLMD",
                    "quantity": 3.0,
                    "side": "long",
                    "average_price": 6.07,
                    "current_price": 6.13,
                }
            ]
        )

        self.assertEqual(updates, 1)
        self.assertIn("carryover_sync", manager.positions["RLMD"]["anomaly_flags"])

    def test_sync_positions_from_brokerage_recent_reload_is_not_labeled_carryover(self):
        manager = EntryManager.__new__(EntryManager)
        manager.positions = {}
        manager.broker = None
        manager._recently_removed_positions = {
            "MUD": {
                "removed_at": time.time(),
                "last_exit_reason": "hard_stop",
                "exit_order_id": "exit-77",
                "quantity": 2.0,
                "side": "long",
                "position": {},
            }
        }

        updates = manager.sync_positions_from_brokerage(
            [
                {
                    "symbol": "MUD",
                    "quantity": 2.0,
                    "side": "long",
                    "average_price": 4.25,
                    "current_price": 4.11,
                }
            ]
        )

        self.assertEqual(updates, 1)
        self.assertNotIn("carryover_sync", manager.positions["MUD"]["anomaly_flags"])
        self.assertIn("broker_reloaded_after_local_removal", manager.positions["MUD"]["anomaly_flags"])

    def test_sync_positions_from_brokerage_infers_meaningful_play_context_for_restored_short(self):
        manager = EntryManager.__new__(EntryManager)
        manager.positions = {}
        manager.broker = None

        updates = manager.sync_positions_from_brokerage(
            [
                {
                    "symbol": "ARKK",
                    "quantity": -2.0,
                    "side": "short",
                    "average_price": 51.25,
                    "current_price": 50.75,
                    "open_pnl": 1.0,
                }
            ]
        )

        self.assertEqual(updates, 1)
        self.assertEqual(manager.positions["ARKK"]["strategy_tag"], "momentum_short")
        self.assertEqual(manager.positions["ARKK"]["setup_mode"], "continuation_short")
        self.assertEqual(manager.positions["ARKK"]["best_play"], "continuation_short")
        self.assertEqual(manager.positions["ARKK"]["direction_constraint"], "short_only")


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


class ProfitRatchetOverrideTests(unittest.TestCase):
    def test_position_specific_trail_pct_tightens_floor(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - 3600,
            "side": "long",
            "peak_price": 110.0,
            "trail_pct": 2.0,
        }

        action = ProfitRatchet.check_position(position, current_price=110.0, now=now)

        self.assertEqual(action["action"], "update_limit")
        self.assertEqual(action["floor_pct"], 8.0)

    def test_tighten_suggestion_overrides_base_trail(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - 3600,
            "side": "long",
            "peak_price": 110.0,
            "trail_pct": 3.0,
            "ratchet_tighten_suggestion_pct": 1.5,
        }

        action = ProfitRatchet.check_position(position, current_price=110.0, now=now)

        self.assertEqual(action["action"], "update_limit")
        self.assertEqual(action["floor_pct"], 8.5)

    def test_activation_and_floor_overrides_enable_close_lock_floor(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - 3600,
            "side": "long",
            "peak_price": 100.9,
            "trail_pct": 3.0,
            "ratchet_activation_override_pct": 0.75,
            "ratchet_initial_floor_override_pct": 0.35,
        }

        action = ProfitRatchet.check_position(position, current_price=100.9, now=now)

        self.assertEqual(action["action"], "update_limit")
        self.assertEqual(action["floor_pct"], 0.35)


class CloseCarryReviewTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_review_flattens_thin_profit_without_overnight_thesis(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = object()
        bot.get_overnight_bias_context = lambda refresh=False: {"direction": "flat", "confidence": 0.3}
        bot._position_exit_side = main_module.TradingBot._position_exit_side
        bot._submit_software_managed_exit = AsyncMock(return_value=True)
        bot._get_close_review_earnings_event = AsyncMock(return_value=None)

        position = {
            "symbol": "AAPL",
            "side": "long",
            "entry_price": 100.0,
            "peak_price": 100.4,
            "entry_time": datetime(2026, 3, 27, 18, 30, tzinfo=timezone.utc).timestamp(),
            "quantity": 2.0,
            "holding_horizon": "intraday",
            "market_regime": "mixed",
            "signal_sources": ["scanner"],
            "order_state": {},
        }
        now_ts = datetime(2026, 3, 27, 19, 42, tzinfo=timezone.utc).timestamp()

        with patch.object(main_module.settings, "CLOSE_CARRY_REVIEW_ENABLED", True), \
             patch.object(main_module.settings, "CLOSE_CARRY_REVIEW_MINUTES", 25), \
             patch.object(main_module.settings, "CLOSE_CARRY_REVIEW_COOLDOWN_SECONDS", 0), \
             patch.object(main_module, "cancel_conflicting_exit_orders", AsyncMock(return_value=0)):
            triggered = await bot._run_close_carry_review(position, 100.22, {"AAPL": []}, now_ts=now_ts)

        self.assertTrue(triggered)
        self.assertEqual(position["close_carry_decision"], "flatten")
        self.assertIn("thin_profit_no_overnight_thesis", position["close_carry_reason"])
        bot._submit_software_managed_exit.assert_awaited_once()

    async def test_close_review_keeps_amc_earnings_carry(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = object()
        bot.get_overnight_bias_context = lambda refresh=False: {"direction": "bullish", "confidence": 0.8}
        bot._position_exit_side = main_module.TradingBot._position_exit_side
        bot._submit_software_managed_exit = AsyncMock(return_value=True)
        bot._get_close_review_earnings_event = AsyncMock(
            return_value={"ticker": "NVDA", "date": "2026-03-27", "timing": "AMC"}
        )

        position = {
            "symbol": "NVDA",
            "side": "long",
            "entry_price": 100.0,
            "peak_price": 101.9,
            "entry_time": datetime(2026, 3, 27, 17, 0, tzinfo=timezone.utc).timestamp(),
            "quantity": 3.0,
            "holding_horizon": "intraday",
            "market_regime": "risk_on",
            "signal_sources": ["earnings"],
            "order_state": {},
        }
        now_ts = datetime(2026, 3, 27, 19, 47, tzinfo=timezone.utc).timestamp()

        with patch.object(main_module.settings, "CLOSE_CARRY_REVIEW_ENABLED", True), \
             patch.object(main_module.settings, "CLOSE_CARRY_REVIEW_MINUTES", 25), \
             patch.object(main_module.settings, "CLOSE_CARRY_REVIEW_COOLDOWN_SECONDS", 0):
            triggered = await bot._run_close_carry_review(position, 101.5, {"NVDA": []}, now_ts=now_ts)

        self.assertFalse(triggered)
        self.assertEqual(position["close_carry_decision"], "carry")
        self.assertIn("earnings_today_amc", position["close_carry_reason_codes"])
        bot._submit_software_managed_exit.assert_not_awaited()


class ProtectionOrderIdentityTests(unittest.IsolatedAsyncioTestCase):
    async def test_place_or_replace_ratchet_reuses_current_order_id_without_client_order_id(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = object()
        bot._entry_session_label = lambda: "regular"
        bot._position_exit_side = main_module.TradingBot._position_exit_side

        position = {
            "symbol": "XLI",
            "side": "short",
            "quantity": 2.0,
            "ratchet_limit_order_id": "ratchet-1",
            "order_state": {},
        }
        open_orders_by_symbol = {
            "XLI": [
                {"id": "ratchet-1", "side": "buy", "type": "stop", "stop_price": "161.43", "client_order_id": ""}
            ]
        }

        placed = await bot._place_or_replace_ratchet_order(position, 161.43, open_orders_by_symbol)

        self.assertTrue(placed)
        self.assertEqual(position["ratchet_limit_order_id"], "ratchet-1")
        self.assertEqual(position["order_state"]["ratchet"], "placed")


class ExtendedExitRepriceTests(unittest.IsolatedAsyncioTestCase):
    def test_syncs_pending_exit_from_open_limit_order(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._position_exit_side = main_module.TradingBot._position_exit_side
        bot._order_is_hard_stop = main_module.TradingBot._order_is_hard_stop
        bot._order_is_ratchet = main_module.TradingBot._order_is_ratchet
        bot._parse_iso_ts = main_module.TradingBot._parse_iso_ts

        position = {
            "symbol": "DGX",
            "side": "short",
            "quantity": 2.0,
            "exit_pending": False,
        }
        open_orders_by_symbol = {
            "DGX": [
                {
                    "id": "limit-exit-1",
                    "symbol": "DGX",
                    "side": "buy",
                    "type": "limit",
                    "qty": "2",
                    "submitted_at": "2026-03-27T21:33:40.231168+00:00",
                    "limit_price": "195.87",
                    "client_order_id": "manual-exit",
                }
            ]
        }

        synced = bot._sync_pending_exit_from_open_orders(position, open_orders_by_symbol)

        self.assertTrue(synced)
        self.assertTrue(position["exit_pending"])
        self.assertEqual(position["exit_order_id"], "limit-exit-1")
        self.assertEqual(position["pending_exit_qty"], 2.0)
        self.assertEqual(position["order_state"]["exit"], "open")

    async def test_reprices_stale_extended_short_exit(self):
        class _Broker:
            def __init__(self):
                self.cancelled = []
                self.limit_covers = []

            def cancel_order(self, order_id):
                self.cancelled.append(order_id)
                return True

            def place_limit_cover(self, symbol, qty, price, extended_hours, client_order_id=None, whole_only=False):
                self.limit_covers.append((symbol, qty, price, extended_hours, whole_only))
                return {"id": "repriced-cover-1"}

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = _Broker()
        bot.entry_manager = object()
        bot._entry_session_label = lambda: "after"

        position = {
            "symbol": "F",
            "side": "short",
            "quantity": 53.0,
            "pending_exit_qty": 53.0,
            "exit_pending": True,
            "exit_order_id": "old-cover",
            "exit_submitted_at": time.time() - 45,
        }
        open_orders_by_symbol = {
            "F": [{"id": "old-cover", "side": "buy", "type": "limit", "limit_price": "11.35"}]
        }

        with patch.object(main_module.settings, "EXTENDED_HOURS_EXIT_REPRICE_AFTER_SECONDS", 20.0), \
             patch.object(main_module.settings, "EXTENDED_HOURS_EXIT_REPRICE_STEP_BPS", 30.0), \
             patch.object(main_module.settings, "EXTENDED_HOURS_EXIT_REPRICE_MAX_ATTEMPTS", 3):
            repriced = await bot._reprice_stale_extended_exit_pending(
                position,
                current_price=11.35,
                open_orders_by_symbol=open_orders_by_symbol,
                now_ts=time.time(),
            )

        self.assertTrue(repriced)
        self.assertEqual(bot.alpaca_client.cancelled, ["old-cover"])
        self.assertEqual(position["exit_order_id"], "repriced-cover-1")
        self.assertEqual(position["extended_exit_reprice_count"], 1)
        self.assertEqual(len(bot.alpaca_client.limit_covers), 1)
        symbol, qty, price, extended_hours, whole_only = bot.alpaca_client.limit_covers[0]
        self.assertEqual(symbol, "F")
        self.assertEqual(qty, 53.0)
        self.assertGreater(price, 11.35)
        self.assertTrue(extended_hours)
        self.assertFalse(whole_only)

    async def test_ensure_hard_stop_does_not_cancel_ratchet_when_client_order_id_is_missing(self):
        class _Broker:
            def place_stop_loss_order(self, symbol, qty, stop_price, side, client_order_id):
                return {"id": "hard-stop-2", "type": "stop", "stop_price": stop_price}

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = _Broker()
        bot._entry_session_label = lambda: "regular"
        bot._position_exit_side = main_module.TradingBot._position_exit_side
        bot._cancel_order_and_confirm = AsyncMock(return_value=True)

        position = {
            "symbol": "XLI",
            "side": "short",
            "quantity": 2.0,
            "entry_price": 160.0,
            "hard_stop_price": 166.68,
            "hard_stop_order_id": "",
            "ratchet_limit_order_id": "ratchet-1",
            "order_state": {},
        }
        open_orders_by_symbol = {
            "XLI": [
                {"id": "ratchet-1", "side": "buy", "type": "stop", "stop_price": "161.43", "client_order_id": ""}
            ]
        }

        await bot._ensure_hard_stop(position, open_orders_by_symbol, current_price=160.0)

        bot._cancel_order_and_confirm.assert_not_awaited()
        self.assertEqual(position["hard_stop_order_id"], "hard-stop-2")

    async def test_place_or_replace_ratchet_reuses_probable_ratchet_stop_without_ids(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = object()
        bot._entry_session_label = lambda: "regular"
        bot._position_exit_side = main_module.TradingBot._position_exit_side

        position = {
            "symbol": "NVD",
            "side": "long",
            "quantity": 10.0,
            "entry_price": 8.00,
            "hard_stop_price": 7.82,
            "ratchet_limit_order_id": "",
            "order_state": {},
        }
        open_orders_by_symbol = {
            "NVD": [
                {"id": "probable-ratchet", "side": "sell", "type": "stop", "stop_price": "8.08", "client_order_id": ""}
            ]
        }

        placed = await bot._place_or_replace_ratchet_order(position, 8.08, open_orders_by_symbol)

        self.assertTrue(placed)
        self.assertEqual(position["ratchet_limit_order_id"], "probable-ratchet")
        self.assertEqual(position["order_state"]["ratchet"], "placed")

    async def test_ratchet_active_cancels_superseded_hard_stop_instead_of_replacing_forever(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._entry_session_label = lambda: "regular"
        bot._position_exit_side = main_module.TradingBot._position_exit_side
        bot._cancel_order_and_confirm = AsyncMock(return_value=True)
        bot._ensure_hard_stop = AsyncMock()
        bot._submit_software_managed_exit = AsyncMock()
        bot._place_or_replace_ratchet_order = AsyncMock(return_value=True)
        bot._cancel_existing_ratchet_orders = AsyncMock(return_value=0)
        bot._exit_target_crossed = lambda position, current_price, target_price: False

        position = {
            "symbol": "XLI",
            "side": "short",
            "quantity": 2.0,
            "entry_price": 161.80,
            "hard_stop_price": 166.68,
            "hard_stop_order_id": "hard-1",
            "order_state": {},
        }
        open_orders_by_symbol = {
            "XLI": [
                {"id": "hard-1", "side": "buy", "type": "stop", "stop_price": "166.68", "client_order_id": "XLI_hardstop_1"}
            ]
        }
        action = {
            "ratchet_active": True,
            "target_exit_price": 161.43,
            "peak_pnl_pct": 1.29,
            "current_pnl_pct": 0.99,
            "floor_pct": 0.25,
            "action": "update_limit",
            "hard_stop_price": 166.68,
            "dead_money": False,
        }

        await bot._apply_profit_ratchet_action(position, 160.22, action, open_orders_by_symbol)

        bot._ensure_hard_stop.assert_not_awaited()
        bot._cancel_order_and_confirm.assert_awaited_once_with("hard-1")
        bot._place_or_replace_ratchet_order.assert_awaited_once()
        self.assertEqual(position["order_state"]["hard_stop"], "superseded_by_ratchet")


class DustCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_submit_dust_cleanup_exit_keeps_position_until_broker_confirms(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)

        class _Broker:
            def __init__(self):
                self.calls = []

            def close_position(self, symbol, qty=None):
                self.calls.append((symbol, qty))
                return {"id": "close-123", "status": "accepted"}

        class _EntryManager:
            def __init__(self, position):
                self.positions = {position["symbol"]: position}
                self.removed = []

            def remove_position(self, symbol):
                self.removed.append(symbol)
                self.positions.pop(symbol, None)

        position = {
            "symbol": "XENE",
            "quantity": 0.11,
            "side": "long",
            "current_price": 62.05,
            "entry_price": 59.85,
            "from_brokerage": True,
            "entry_path": "broker_sync_missing_local",
        }
        broker = _Broker()
        entry_manager = _EntryManager(position)
        bot.alpaca_client = broker
        bot.entry_manager = entry_manager

        submitted = await bot._submit_dust_cleanup_exit(position, "fractional_carryover")

        self.assertTrue(submitted)
        self.assertEqual(broker.calls, [("XENE", 0.11)])
        self.assertTrue(position["exit_pending"])
        self.assertEqual(position["exit_order_id"], "close-123")
        self.assertEqual(position["last_exit_reason"], "fractional_carryover")
        self.assertEqual(entry_manager.removed, [])


if __name__ == "__main__":
    unittest.main()
