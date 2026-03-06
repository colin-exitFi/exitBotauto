import asyncio
import threading
import time
import unittest
from unittest.mock import patch

import src.main as main_module
from src.broker.alpaca_client import AlpacaClient
from src.entry.entry_manager import EntryManager


class FakeRiskManager:
    def __init__(self):
        self.recorded = []
        self.reset_calls = 0

    def get_risk_tier(self):
        return {"name": "TEST"}

    def record_trade(self, trade):
        self.recorded.append(trade)

    def reset_daily(self):
        self.reset_calls += 1


class FakeEntryManager:
    def __init__(self, position, remove_on_exit=True):
        self.positions = {position["symbol"]: position}
        self._remove_on_exit = remove_on_exit

    def get_positions(self):
        return list(self.positions.values())

    def remove_position(self, symbol):
        if self._remove_on_exit:
            self.positions.pop(symbol, None)


class FakeAlpacaForMonitor:
    def __init__(self, closed_orders):
        self._closed_orders = closed_orders

    def get_positions(self):
        # Simulate that broker no longer has the position (trailing stop filled).
        return []

    def get_orders(self, status="open"):
        if status == "closed":
            return list(self._closed_orders)
        return []


class ExitAccountingTests(unittest.IsolatedAsyncioTestCase):
    def test_ws_trailing_stop_dedupes_duplicate_callbacks(self):
        pos = {
            "symbol": "AAPL",
            "entry_price": 100.0,
            "quantity": 10.0,
            "entry_time": time.time() - 60,
            "side": "long",
            "_exit_recorded": False,
        }
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = FakeEntryManager(pos, remove_on_exit=False)
        bot.risk_manager = FakeRiskManager()
        bot.pnl_state = {}

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"):
            bot._on_trailing_stop_filled("AAPL", 105.0, 10.0)
            bot._on_trailing_stop_filled("AAPL", 105.0, 10.0)

        trade = record_trade_mock.call_args[0][0]
        self.assertEqual(record_trade_mock.call_count, 1)
        self.assertEqual(len(bot.risk_manager.recorded), 1)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)
        self.assertAlmostEqual(bot.pnl_state.get("total_realized_pnl", 0), 50.0, places=6)
        self.assertEqual(trade.get("strategy_tag"), "unknown")
        self.assertEqual(trade.get("signal_sources"), ["unknown"])
        self.assertAlmostEqual(trade.get("slippage_bps", 0), 0.0, places=6)

    async def test_monitor_positions_uses_latest_side_matched_trailing_fill(self):
        pos = {
            "symbol": "TSLA",
            "entry_price": 100.0,
            "quantity": 5.0,
            "entry_time": time.time() - 120,
            "side": "short",
            "_exit_recorded": False,
        }
        closed_orders = [
            # Wrong side for short close (must be buy), should be ignored
            {
                "symbol": "TSLA",
                "type": "trailing_stop",
                "side": "sell",
                "filled_avg_price": "90",
                "filled_at": "2026-03-05T10:00:00Z",
            },
            # Valid but older
            {
                "symbol": "TSLA",
                "type": "trailing_stop",
                "side": "buy",
                "filled_avg_price": "95",
                "filled_at": "2026-03-05T09:59:00Z",
            },
            # Valid and latest; should be selected
            {
                "symbol": "TSLA",
                "type": "trailing_stop",
                "side": "buy",
                "filled_avg_price": "97",
                "filled_at": "2026-03-05T10:01:00Z",
            },
        ]

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = FakeEntryManager(pos, remove_on_exit=True)
        bot.risk_manager = FakeRiskManager()
        bot.alpaca_client = FakeAlpacaForMonitor(closed_orders)
        bot.pnl_state = {}

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"):
            await bot._monitor_positions()

        self.assertEqual(record_trade_mock.call_count, 1)
        trade = record_trade_mock.call_args[0][0]
        self.assertEqual(trade["side"], "buy_to_cover")
        self.assertAlmostEqual(trade["exit_price"], 97.0, places=6)
        self.assertAlmostEqual(trade["pnl"], 15.0, places=6)
        self.assertEqual(trade.get("strategy_tag"), "unknown")
        self.assertEqual(trade.get("signal_sources"), ["unknown"])
        self.assertIn("slippage_bps", trade)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)
        self.assertAlmostEqual(bot.pnl_state.get("total_realized_pnl", 0), 15.0, places=6)

    async def test_monitor_positions_keeps_position_without_confirmed_closed_fill(self):
        pos = {
            "symbol": "AAPL",
            "entry_price": 100.0,
            "quantity": 10.0,
            "entry_time": time.time() - 60,
            "side": "long",
            "_exit_recorded": False,
        }
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = FakeEntryManager(pos, remove_on_exit=True)
        bot.risk_manager = FakeRiskManager()
        bot.alpaca_client = FakeAlpacaForMonitor(closed_orders=[])
        bot.pnl_state = {}

        with patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"):
            await bot._monitor_positions()

        self.assertEqual(record_trade_mock.call_count, 0)
        self.assertEqual(len(bot.risk_manager.recorded), 0)
        self.assertIn("AAPL", bot.entry_manager.positions)

    async def test_monitor_and_ws_race_records_exit_once(self):
        pos = {
            "symbol": "AAPL",
            "entry_price": 100.0,
            "quantity": 10.0,
            "entry_time": time.time() - 60,
            "side": "long",
            "_exit_recorded": False,
        }
        closed_orders = [
            {
                "symbol": "AAPL",
                "type": "trailing_stop",
                "side": "sell",
                "filled_avg_price": "105",
                "filled_at": "2026-03-05T10:01:00Z",
            },
        ]

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = FakeEntryManager(pos, remove_on_exit=False)
        bot.risk_manager = FakeRiskManager()
        bot.alpaca_client = FakeAlpacaForMonitor(closed_orders)
        bot.pnl_state = {}

        record_started = threading.Event()
        release_record = threading.Event()
        trade_calls = []

        def blocking_record_trade(trade):
            trade_calls.append(trade)
            record_started.set()
            release_record.wait(timeout=1.0)

        with patch.object(main_module.trade_history, "record_trade", side_effect=blocking_record_trade), \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"):
            ws_task = asyncio.create_task(
                asyncio.to_thread(bot._on_trailing_stop_filled, "AAPL", 105.0, 10.0)
            )
            started = await asyncio.to_thread(record_started.wait, 1.0)
            self.assertTrue(started, "WS path did not reach record_trade in time")

            await bot._monitor_positions()

            release_record.set()
            await ws_task

        self.assertEqual(len(trade_calls), 1)
        self.assertEqual(len(bot.risk_manager.recorded), 1)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)
        self.assertAlmostEqual(bot.pnl_state.get("total_realized_pnl", 0), 50.0, places=6)

    def test_roll_daily_state_resets_risk_once_on_new_trading_day(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.risk_manager = FakeRiskManager()
        bot.pnl_state = {"today_realized_pnl": 125.0, "today_date": "2026-03-05"}
        bot._last_daily_reset_date = "2026-03-05"

        with patch.object(main_module.TradingBot, "_current_trading_day", return_value="2026-03-06"), \
             patch.object(main_module.persistence, "save_pnl_state") as save_pnl_mock:
            bot._roll_daily_state_if_needed()
            bot._roll_daily_state_if_needed()

        self.assertEqual(bot.pnl_state.get("today_realized_pnl"), 0.0)
        self.assertEqual(bot.pnl_state.get("today_date"), "2026-03-06")
        self.assertEqual(bot.risk_manager.reset_calls, 1)
        self.assertEqual(save_pnl_mock.call_count, 1)


class ShortRestartSyncTests(unittest.TestCase):
    def test_alpaca_client_normalizes_short_quantity_and_side(self):
        class FakePosition:
            symbol = "QQQ"
            qty = "-3"
            side = "short"
            avg_entry_price = "100"
            current_price = "98"
            market_value = "-294"
            unrealized_pl = "6"
            unrealized_plpc = "0.02"

        class FakeTradingClient:
            def get_all_positions(self):
                return [FakePosition()]

        client = AlpacaClient()
        client._initialized = True
        client._trading_client = FakeTradingClient()

        positions = client.get_positions()
        self.assertEqual(len(positions), 1)
        self.assertEqual(positions[0]["symbol"], "QQQ")
        self.assertEqual(positions[0]["side"], "short")
        self.assertAlmostEqual(positions[0]["quantity"], 3.0, places=6)

    def test_entry_manager_loads_short_positions_from_brokerage(self):
        class FakeBroker:
            def get_positions(self):
                return [{
                    "symbol": "QQQ",
                    "quantity": -3,
                    "average_price": 100.0,
                    "current_price": 98.0,
                    "open_pnl": 6.0,
                }]

            def get_orders(self, status="open"):
                return []

        manager = EntryManager(alpaca_client=FakeBroker(), polygon_client=None, risk_manager=None)
        self.assertIn("QQQ", manager.positions)
        pos = manager.positions["QQQ"]
        self.assertEqual(pos["side"], "short")
        self.assertAlmostEqual(pos["quantity"], 3.0, places=6)
        self.assertAlmostEqual(pos["peak_price"], 98.0, places=6)
