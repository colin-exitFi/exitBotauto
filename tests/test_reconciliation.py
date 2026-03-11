import time
import unittest
from unittest.mock import patch

import src.main as main_module
from src.reconciliation.reconciler import Reconciler


class _FakeAlpaca:
    def __init__(self, account=None, positions=None, activities=None, portfolio_history=None):
        self._account = account or {}
        self._positions = positions or []
        self._activities = activities or []
        self._portfolio_history = portfolio_history or {}

    def get_account(self):
        return dict(self._account)

    def get_positions(self):
        return list(self._positions)

    def get_account_activities(self, activity_types="FILL", date=None):
        return list(self._activities)

    def get_portfolio_history(self, period="1D", timeframe="15Min", intraday_reporting="market_hours", pnl_reset="per_day"):
        return dict(self._portfolio_history)


class _FakeEntryManager:
    def __init__(self, position):
        self.positions = {position["symbol"]: position}

    def remove_position(self, symbol):
        self.positions.pop(symbol, None)


class _FakeRiskManager:
    def __init__(self):
        self.recorded = []

    def get_risk_tier(self):
        return {"name": "TEST"}

    def record_trade(self, trade):
        self.recorded.append(trade)


class ReconcilerTests(unittest.TestCase):
    def test_classifies_critical_mismatch(self):
        alpaca = _FakeAlpaca(
            account={"equity": 24910.30, "last_equity": 25342.33, "cash": 23990.06},
            positions=[{"symbol": "HIMS", "unrealized_pnl": "-7.44"}],
            activities=[{"symbol": "CRCL"}, {"symbol": "HIMS"}],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [25147.02, 24910.30],
                "profit_loss": [-189.58, -432.03],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"total_realized_pnl": 247.20, "today_realized_pnl": 247.20, "total_trades": 44}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 103.15, "total_trades": 25, "overall": {"win_rate_pct": 76.0}, "by_symbol": {"ACHR": {"pnl": 1.0}}}), \
             patch.object(Reconciler, "_load_json", return_value={"total_pnl": 103.15, "total_trades": 25, "overall_win_rate_pct": 76.0, "by_symbol": {"ACHR": {"pnl": 1.0}}}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertEqual(snap["reconciliation"]["status"], "critical_mismatch")
        self.assertIn("broker_truth_canary_triggered", snap["reconciliation"]["reasons"])
        self.assertIn("broker_symbols_missing_from_internal", snap["reconciliation"]["reasons"])
        self.assertTrue(snap["trust"]["broker_only_mode"])
        self.assertTrue(any(c["code"] == "realized_pnl_mismatch" for c in snap["canaries"]))

    def test_open_position_activity_is_not_flagged_missing_from_internal(self):
        alpaca = _FakeAlpaca(
            account={"equity": 10050.0, "last_equity": 10000.0, "cash": 5000.0},
            positions=[{"symbol": "AAPL", "unrealized_pnl": "50.0"}],
            activities=[{"symbol": "AAPL", "side": "buy", "qty": "10"}],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 10050.0],
                "profit_loss": [0.0, 50.0],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": 0.0}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 0.0, "total_trades": 0, "overall": {}, "by_symbol": {}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=[]), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertNotIn("broker_symbols_missing_from_internal", snap["reconciliation"]["reasons"])
        self.assertEqual(snap["reconciliation"]["status"], "healthy")
        self.assertFalse(snap["trust"]["broker_only_mode"])

    def test_carryover_gap_alone_is_warning_not_critical(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9800.0, "last_equity": 10000.0, "cash": 9000.0},
            positions=[{"symbol": "MSFT", "unrealized_pnl": "-25.0"}],
            activities=[],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [9825.0, 9800.0],
                "profit_loss": [-175.0, -200.0],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": 0.0}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 0.0, "total_trades": 0, "overall": {}, "by_symbol": {}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=[]), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertIn("carryover_gap", snap["reconciliation"]["reasons"])
        self.assertEqual(snap["reconciliation"]["status"], "minor_mismatch")
        self.assertFalse(snap["trust"]["broker_only_mode"])

    def test_marks_degraded_when_broker_history_missing(self):
        alpaca = _FakeAlpaca(account={"equity": 1000, "last_equity": 1000}, positions=[], activities=[], portfolio_history={})
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 0, "total_trades": 0, "overall": {}, "by_symbol": {}}), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertEqual(snap["reconciliation"]["status"], "minor_mismatch")
        self.assertIn("broker_history_unavailable", snap["reconciliation"]["reasons"])


class ExitFinalizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_trade_update_fill_finalizes_pending_exit_once(self):
        entry_time = time.time() - 120
        position = {
            "symbol": "AAPL",
            "entry_price": 100.0,
            "quantity": 10.0,
            "entry_time": entry_time,
            "side": "long",
            "signal_price": 100.0,
            "strategy_tag": "test_strategy",
            "signal_sources": ["scanner"],
            "exit_pending": True,
            "exit_order_id": "exit-123",
            "last_exit_reason": "advisor_strategic_exit",
        }
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = _FakeEntryManager(position)
        bot.risk_manager = _FakeRiskManager()
        bot.pnl_state = {}
        bot._recorded_realized_keys = set()

        order_payload = {
            "order": {
                "id": "exit-123",
                "symbol": "AAPL",
                "side": "sell",
                "type": "market",
                "filled_avg_price": "103.50",
                "filled_qty": "10",
                "filled_at": "2026-03-10T15:31:00Z",
            }
        }

        with patch.object(main_module.trade_history, "load_all", return_value=[]), \
             patch.object(main_module.trade_history, "record_trade") as record_trade_mock, \
             patch.object(main_module.persistence, "save_pnl_state"), \
             patch.object(main_module.persistence, "save_positions"), \
             patch.object(main_module.persistence, "save_trades"):
            await bot._on_trade_update_fill(order_payload, "fill")
            await bot._on_trade_update_fill(order_payload, "fill")

        self.assertEqual(record_trade_mock.call_count, 1)
        self.assertEqual(bot.pnl_state.get("total_trades"), 1)
        self.assertAlmostEqual(bot.pnl_state.get("total_realized_pnl", 0), 35.0, places=6)
        self.assertEqual(len(bot.risk_manager.recorded), 1)
