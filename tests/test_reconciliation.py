import time
import unittest
from unittest.mock import patch

import src.main as main_module
from src.ai import trade_history
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


class _FakeEntryManagerWithRecentRemoved:
    def __init__(self, *, positions=None, recently_removed=None):
        self.positions = positions or {}
        self._recently_removed_positions = recently_removed or {}

    def get_recently_removed_position(self, symbol):
        payload = self._recently_removed_positions.get(symbol, {}) or {}
        return dict(payload.get("position", {}) or {})


class _FakeRiskManager:
    def __init__(self):
        self.recorded = []

    def get_risk_tier(self):
        return {"name": "TEST"}

    def record_trade(self, trade):
        self.recorded.append(trade)


class ReconcilerTests(unittest.TestCase):
    def test_snapshot_defaults_to_trading_session_day_not_balance_asof(self):
        alpaca = _FakeAlpaca(
            account={
                "equity": 10040.0,
                "last_equity": 10000.0,
                "cash": 10040.0,
                "balance_asof": "2026-03-31",
            },
            positions=[],
            activities=[
                {
                    "symbol": "TENX",
                    "side": "buy",
                    "qty": "10",
                    "price": "10.00",
                    "transaction_time": "2026-03-30T14:00:00Z",
                    "order_id": "open-1",
                },
                {
                    "symbol": "TENX",
                    "side": "sell",
                    "qty": "10",
                    "price": "14.00",
                    "transaction_time": "2026-03-30T15:00:00Z",
                    "order_id": "close-1",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 10040.0],
                "profit_loss": [0.0, 40.0],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.trading_session_day", return_value="2026-03-30"), \
             patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": 0.0}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 0.0, "total_trades": 0, "overall": {}, "by_symbol": {}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=[]), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot()

        self.assertEqual(snap["date"], "2026-03-30")
        self.assertEqual(snap["broker"]["date"], "2026-03-30")
        self.assertEqual(snap["broker"]["broker_balance_asof"], "2026-03-31")
        self.assertEqual(snap["internal"]["trade_history_trade_count"], 1)
        self.assertEqual(snap["reconciliation"]["status"], "healthy")

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
            positions=[{"symbol": "AAPL", "qty": "10", "unrealized_pnl": "50.0"}],
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

    def test_broker_fill_ledger_reconstructs_intraday_round_trip(self):
        alpaca = _FakeAlpaca(
            account={"equity": 10040.0, "last_equity": 10000.0, "cash": 10040.0},
            positions=[],
            activities=[
                {
                    "symbol": "TENX",
                    "side": "buy",
                    "qty": "10",
                    "price": "10.00",
                    "transaction_time": "2026-03-10T14:00:00Z",
                    "order_id": "open-1",
                },
                {
                    "symbol": "TENX",
                    "side": "sell",
                    "qty": "10",
                    "price": "14.00",
                    "transaction_time": "2026-03-10T15:00:00Z",
                    "order_id": "close-1",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 10040.0],
                "profit_loss": [0.0, 40.0],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": 0.0}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 0.0, "total_trades": 0, "overall": {}, "by_symbol": {}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=[]), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertEqual(snap["internal"]["trade_history_trade_count"], 1)
        self.assertAlmostEqual(snap["internal"]["trade_history_realized"], 40.0, places=6)
        self.assertNotIn("broker_symbols_missing_from_internal", snap["reconciliation"]["reasons"])
        self.assertEqual(snap["reconciliation"]["status"], "healthy")

    def test_broker_fill_ledger_preserves_local_exit_reason_when_order_matches_recently_removed(self):
        alpaca = _FakeAlpaca(
            account={"equity": 10020.0, "last_equity": 10000.0, "cash": 10020.0},
            positions=[],
            activities=[
                {
                    "symbol": "CRCL",
                    "side": "buy",
                    "qty": "10",
                    "price": "100.00",
                    "transaction_time": "2026-03-10T14:00:00Z",
                    "order_id": "open-1",
                },
                {
                    "symbol": "CRCL",
                    "side": "sell",
                    "qty": "10",
                    "price": "102.00",
                    "transaction_time": "2026-03-10T15:00:00Z",
                    "order_id": "exit-123",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 10020.0],
                "profit_loss": [0.0, 20.0],
            },
        )
        entry_manager = _FakeEntryManagerWithRecentRemoved(
            recently_removed={
                "CRCL": {
                    "removed_at": time.time(),
                    "last_exit_reason": "advisor_strategic_exit",
                    "exit_order_id": "exit-123",
                    "position": {
                        "entry_path": "jury",
                        "strategy_tag": "uw_flow_long",
                        "signal_sources": ["unusual_whales_stream"],
                        "anomaly_flags": ["carryover_sync", "broker_reloaded_after_local_removal"],
                    },
                }
            }
        )
        reconciler = Reconciler(alpaca, entry_manager=entry_manager)

        broker = reconciler.get_broker_truth("2026-03-10")
        trades = broker.get("broker_fill_ledger", {}).get("trades", [])

        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0]["reason"], "advisor_strategic_exit")
        self.assertEqual(trades[0]["exit_order_id"], "exit-123")
        self.assertNotIn("broker_reconstructed", trades[0]["anomaly_flags"])
        self.assertNotIn("carryover_sync", trades[0]["anomaly_flags"])

    def test_find_recent_history_trade_matches_exit_order_id_outside_time_window(self):
        reconciler = Reconciler(_FakeAlpaca())
        existing = [
            {
                "symbol": "CRCL",
                "asset_type": "equity",
                "exit_time": 1773154800.0,
                "quantity": 10.0,
                "pnl": 20.0,
                "reason": "advisor_strategic_exit",
                "exit_order_id": "exit-123",
            }
        ]
        trade = {
            "symbol": "CRCL",
            "asset_type": "equity",
            "exit_time": 1773155400.0,
            "quantity": 10.0,
            "pnl": 20.0,
            "reason": "broker_fill_reconstructed",
            "exit_order_id": "exit-123",
        }

        matched = reconciler._find_recent_history_trade(existing, trade, window_seconds=30.0)

        self.assertIsNotNone(matched)
        self.assertEqual(matched["reason"], "advisor_strategic_exit")

    def test_broker_fill_ledger_marks_unresolved_carryover_symbol(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9950.0, "last_equity": 10000.0, "cash": 9950.0},
            positions=[],
            activities=[
                {
                    "symbol": "CRCL",
                    "side": "sell",
                    "qty": "5",
                    "price": "110.00",
                    "transaction_time": "2026-03-10T14:00:00Z",
                    "order_id": "close-carry-1",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 9950.0],
                "profit_loss": [0.0, -50.0],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": 0.0}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 0.0, "total_trades": 0, "overall": {}, "by_symbol": {}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=[]), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertIn("broker_fill_ledger_unresolved", snap["reconciliation"]["reasons"])
        self.assertIn("CRCL", snap["internal"]["broker_reconstructed_unresolved_symbols"])

    def test_broker_fill_ledger_resolves_carryover_close_from_recent_snapshot_basis(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9950.0, "last_equity": 10000.0, "cash": 9950.0},
            positions=[],
            activities=[
                {
                    "symbol": "CRCL",
                    "side": "sell",
                    "qty": "5",
                    "price": "110.00",
                    "transaction_time": "2026-03-10T14:00:00Z",
                    "order_id": "close-carry-1",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 9950.0],
                "profit_loss": [0.0, -50.0],
            },
        )
        entry_manager = _FakeEntryManagerWithRecentRemoved(
            recently_removed={
                "CRCL": {
                    "removed_at": time.time(),
                    "last_exit_reason": "hard_stop",
                    "exit_order_id": "close-carry-1",
                    "position": {
                        "symbol": "CRCL",
                        "side": "long",
                        "quantity": 5.0,
                        "entry_price": 120.0,
                        "entry_time": 1773064800.0,
                        "strategy_tag": "uw_flow_long",
                        "signal_sources": ["unusual_whales_stream"],
                    },
                }
            }
        )
        reconciler = Reconciler(alpaca, entry_manager=entry_manager)

        broker = reconciler.get_broker_truth("2026-03-10")
        ledger = broker.get("broker_fill_ledger", {})

        self.assertEqual(ledger["trade_count"], 1)
        self.assertEqual(ledger["unresolved_symbols"], [])
        self.assertEqual(ledger["trades"][0]["symbol"], "CRCL")
        self.assertEqual(ledger["trades"][0]["reason"], "hard_stop")
        self.assertAlmostEqual(ledger["trades"][0]["entry_price"], 120.0, places=6)
        self.assertAlmostEqual(ledger["trades"][0]["exit_price"], 110.0, places=6)
        self.assertAlmostEqual(ledger["trades"][0]["pnl"], -50.0, places=6)

    def test_broker_fill_ledger_resolves_carryover_close_from_trusted_history_match(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9989.5, "last_equity": 10000.0, "cash": 9989.5},
            positions=[],
            activities=[
                {
                    "symbol": "MUD",
                    "side": "sell",
                    "qty": "3",
                    "price": "45.59",
                    "transaction_time": "2026-03-30T19:09:54Z",
                    "order_id": "801037f7-5904-419f-aa76-7d465e157acb",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 10002.82],
                "profit_loss": [0.0, 2.82],
            },
        )
        reconciler = Reconciler(alpaca)
        history_rows = [
            {
                "symbol": "MUD",
                "side": "sell",
                "entry_price": 44.65,
                "exit_price": 45.59,
                "quantity": 3.0,
                "pnl": 2.82,
                "reason": "ratchet_exit",
                "entry_time": 1774887516.9538057,
                "exit_time": 1774900994.788279,
                "exit_order_id": "801037f7-5904-419f-aa76-7d465e157acb",
                "trade_date": "2026-03-30",
            },
        ]

        with patch.object(trade_history, "load_all", return_value=history_rows):
            broker = reconciler.get_broker_truth("2026-03-30")

        ledger = broker.get("broker_fill_ledger", {})
        self.assertEqual(ledger["trade_count"], 1)
        self.assertEqual(ledger["unresolved_symbols"], [])
        self.assertEqual(ledger["trades"][0]["reason"], "ratchet_exit")
        self.assertAlmostEqual(ledger["trades"][0]["pnl"], 2.82, places=6)

    def test_broker_fill_ledger_resolves_carryover_with_same_day_adds_when_close_order_matches_history(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9997.15, "last_equity": 10000.0, "cash": 9997.15},
            positions=[],
            activities=[
                {
                    "symbol": "CODI",
                    "side": "buy",
                    "qty": "1.899002493",
                    "price": "8.02",
                    "transaction_time": "2026-03-30T16:19:13.43056Z",
                    "order_id": "open-codi-add",
                },
                {
                    "symbol": "CODI",
                    "side": "sell",
                    "qty": "7",
                    "price": "7.92",
                    "transaction_time": "2026-03-30T16:24:25.928969Z",
                    "order_id": "close-codi",
                },
                {
                    "symbol": "CODI",
                    "side": "sell",
                    "qty": "8",
                    "price": "7.92",
                    "transaction_time": "2026-03-30T16:24:26.31516Z",
                    "order_id": "close-codi",
                },
                {
                    "symbol": "CODI",
                    "side": "sell",
                    "qty": "10.899002493",
                    "price": "7.92",
                    "transaction_time": "2026-03-30T16:24:26.748417Z",
                    "order_id": "close-codi",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 9997.15],
                "profit_loss": [0.0, -2.85],
            },
        )
        reconciler = Reconciler(alpaca)
        history_rows = [
            {
                "symbol": "CODI",
                "side": "sell",
                "entry_price": 8.03,
                "exit_price": 7.92,
                "quantity": 25.899002493,
                "pnl": -2.85,
                "reason": "broker_exit_fill",
                "entry_time": 1774887552.2830014,
                "exit_time": 1774887866.745626,
                "exit_order_id": "close-codi",
                "trade_date": "2026-03-30",
            },
        ]

        with patch.object(trade_history, "load_all", return_value=history_rows):
            broker = reconciler.get_broker_truth("2026-03-30")

        ledger = broker.get("broker_fill_ledger", {})
        self.assertEqual(ledger["trade_count"], 1)
        self.assertEqual(ledger["unresolved_symbols"], [])
        self.assertEqual(ledger["trades"][0]["reason"], "broker_exit_fill")
        self.assertAlmostEqual(ledger["trades"][0]["quantity"], 25.899002493, places=6)

    def test_partial_broker_fill_ledger_does_not_override_broker_day_truth(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9990.0, "last_equity": 10000.0, "cash": 9990.0},
            positions=[],
            activities=[
                {
                    "symbol": "TENX",
                    "side": "buy",
                    "qty": "10",
                    "price": "10.00",
                    "transaction_time": "2026-03-10T14:00:00Z",
                    "order_id": "open-tenx",
                },
                {
                    "symbol": "TENX",
                    "side": "sell",
                    "qty": "10",
                    "price": "14.00",
                    "transaction_time": "2026-03-10T15:00:00Z",
                    "order_id": "close-tenx",
                },
                {
                    "symbol": "CRCL",
                    "side": "sell",
                    "qty": "5",
                    "price": "110.00",
                    "transaction_time": "2026-03-10T15:15:00Z",
                    "order_id": "close-crcl-carry",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 9990.0],
                "profit_loss": [0.0, -10.0],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": -10.0}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": -10.0, "total_trades": 2, "overall": {}, "by_symbol": {"TENX": {"pnl": 40.0}, "CRCL": {"pnl": -50.0}}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=[
                 {
                     "symbol": "TENX",
                     "exit_time": 1773154800.0,
                     "quantity": 10.0,
                     "pnl": 40.0,
                     "reason": "broker_fill_reconstructed",
                     "trade_date": "2026-03-10",
                 },
                 {
                     "symbol": "CRCL",
                     "exit_time": 1773155700.0,
                     "quantity": 5.0,
                     "pnl": -50.0,
                     "reason": "hard_stop",
                     "trade_date": "2026-03-10",
                 },
             ]), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertEqual(snap["reconciliation"]["canonical_realized_pnl"], -10.0)
        self.assertEqual(snap["reconciliation"]["canonical_realized_source"], "broker_day_estimate_partial_fill_ledger")
        self.assertEqual(snap["reconciliation"]["broker_vs_trade_history_diff"], 0.0)
        self.assertEqual(snap["reconciliation"]["status"], "healthy")

    def test_partial_broker_fill_ledger_reanchors_pnl_state_to_broker_day_estimate(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9990.0, "last_equity": 10000.0, "cash": 9990.0},
            positions=[],
            activities=[
                {
                    "symbol": "TENX",
                    "side": "buy",
                    "qty": "10",
                    "price": "10.00",
                    "transaction_time": "2026-03-10T14:00:00Z",
                    "order_id": "open-tenx",
                },
                {
                    "symbol": "TENX",
                    "side": "sell",
                    "qty": "10",
                    "price": "14.00",
                    "transaction_time": "2026-03-10T15:00:00Z",
                    "order_id": "close-tenx",
                },
                {
                    "symbol": "CRCL",
                    "side": "sell",
                    "qty": "5",
                    "price": "110.00",
                    "transaction_time": "2026-03-10T15:15:00Z",
                    "order_id": "close-crcl-carry",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 9990.0],
                "profit_loss": [0.0, -10.0],
            },
        )
        reconciler = Reconciler(alpaca)
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", side_effect=[
            {"today_realized_pnl": 40.0, "total_realized_pnl": 140.0},
            {"today_realized_pnl": 40.0, "total_realized_pnl": 140.0},
            {"today_realized_pnl": -10.0, "total_realized_pnl": 90.0},
        ]), \
             patch("src.reconciliation.reconciler.persistence.save_pnl_state") as save_pnl_state_mock, \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": 40.0, "total_trades": 1, "overall": {}, "by_symbol": {"TENX": {"pnl": 40.0}}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=[
                 {
                     "symbol": "TENX",
                     "exit_time": 1773154800.0,
                     "quantity": 10.0,
                     "pnl": 40.0,
                     "reason": "broker_fill_reconstructed",
                     "trade_date": "2026-03-10",
                 },
             ]), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-10")

        self.assertEqual(save_pnl_state_mock.call_count, 1)
        saved_pnl_state = save_pnl_state_mock.call_args.args[0]
        self.assertEqual(saved_pnl_state["today_realized_pnl"], -10.0)
        self.assertEqual(saved_pnl_state["total_realized_pnl"], 90.0)
        self.assertEqual(snap["internal"]["pnl_state_today_realized"], -10.0)
        self.assertEqual(snap["reconciliation"]["canonical_realized_source"], "broker_day_estimate_partial_fill_ledger")
        self.assertEqual(snap["reconciliation"]["broker_vs_pnl_state_diff"], 0.0)
        self.assertEqual(snap["reconciliation"]["broker_vs_trade_history_diff"], -50.0)
        self.assertEqual(snap["reconciliation"]["status"], "minor_mismatch")

    def test_complete_broker_fill_ledger_mismatch_does_not_override_broker_day_estimate(self):
        reconciler = Reconciler(_FakeAlpaca())
        broker = {
            "day_pnl": -270.2,
            "overnight_gap_pnl": -1.22,
            "current_open_unrealized": 0.0,
            "broker_positions": {},
        }
        internal = {
            "pnl_state_today_realized": -268.98,
            "trade_history_realized": -248.04,
            "broker_reconstructed_realized": -90.68,
            "broker_reconstructed_trade_count": 26,
            "broker_reconstructed_unresolved_symbols": [],
            "broker_supplemental_trade_count": 0,
            "symbols_in_trade_history": [],
            "internal_live_positions": {},
        }

        rec = reconciler.classify_mismatch(broker, internal)

        self.assertEqual(rec["canonical_realized_source"], "broker_day_estimate_fill_ledger_mismatch")
        self.assertAlmostEqual(rec["canonical_realized_pnl"], -268.98, places=2)
        self.assertIn("broker_fill_ledger_mismatch", rec["reasons"])

    def test_internal_analytics_does_not_repair_pnl_state_from_mismatched_complete_fill_ledger(self):
        reconciler = Reconciler(_FakeAlpaca())
        broker = {
            "broker_history_available": True,
            "day_pnl": -270.2,
            "overnight_gap_pnl": -1.22,
            "current_open_unrealized": 0.0,
            "broker_fill_ledger": {
                "realized_pnl": -601.12,
                "trade_count": 156,
                "unresolved_symbols": [],
                "trades": [
                    {
                        "symbol": "GLND",
                        "exit_time": 1774871732.180801,
                        "quantity": 100.0,
                        "entry_price": 10.0,
                        "exit_price": 9.0,
                        "pnl": -100.0,
                        "reason": "broker_fill_reconstructed",
                    }
                ],
            },
        }
        history = [
            {
                "symbol": "SOXL",
                "exit_time": 1774871732.180801,
                "quantity": 7.0,
                "pnl": -10.5,
                "reason": "hard_stop",
                "trade_date": "2026-03-30",
            }
        ]

        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": -268.98, "total_realized_pnl": -500.0}), \
             patch("src.reconciliation.reconciler.persistence.save_pnl_state") as save_pnl_state_mock, \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": -10.5, "total_trades": 1, "overall": {}, "by_symbol": {"SOXL": {"pnl": -10.5}}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=history), \
             patch.object(Reconciler, "_load_json", return_value={}):
            internal = reconciler.get_internal_analytics("2026-03-30", broker=broker)

        self.assertFalse(internal["pnl_state_repaired"])
        self.assertEqual(save_pnl_state_mock.call_count, 0)
        self.assertAlmostEqual(internal["pnl_state_today_realized"], -268.98, places=2)
        self.assertAlmostEqual(internal["trade_history_realized"], -10.5, places=2)
        self.assertEqual(internal["trade_history_trade_count"], 1)
        self.assertEqual(internal["broker_supplemental_trade_count"], 0)

    def test_supplemental_broker_trades_do_not_double_count_existing_exit(self):
        alpaca = _FakeAlpaca(
            account={"equity": 9989.5, "last_equity": 10000.0, "cash": 9989.5},
            positions=[],
            activities=[
                {
                    "symbol": "SOXL",
                    "side": "buy",
                    "qty": "7",
                    "price": "46.20",
                    "transaction_time": "2026-03-30T13:28:48Z",
                    "order_id": "open-soxl",
                },
                {
                    "symbol": "SOXL",
                    "side": "sell",
                    "qty": "7",
                    "price": "44.70",
                    "transaction_time": "2026-03-30T13:28:52Z",
                    "order_id": "close-soxl",
                },
            ],
            portfolio_history={
                "timestamp": [1, 2],
                "equity": [10000.0, 9989.5],
                "profit_loss": [0.0, -10.5],
            },
        )
        reconciler = Reconciler(alpaca)
        existing_history = [
            {
                "symbol": "SOXL",
                "exit_time": 1774877332.0,
                "entry_time": 1774877328.0,
                "quantity": 7.0,
                "entry_price": 46.2,
                "exit_price": 44.7,
                "pnl": -10.5,
                "reason": "hard_stop",
                "trade_date": "2026-03-30",
            },
        ]
        with patch("src.reconciliation.reconciler.persistence.load_pnl_state", return_value={"today_realized_pnl": -10.5}), \
             patch("src.reconciliation.reconciler.trade_history.get_analytics", return_value={"total_pnl": -10.5, "total_trades": 1, "overall": {}, "by_symbol": {"SOXL": {"pnl": -10.5}}}), \
             patch("src.reconciliation.reconciler.trade_history.load_all", return_value=existing_history), \
             patch.object(Reconciler, "_load_json", return_value={}):
            snap = reconciler.snapshot("2026-03-30")

        self.assertEqual(snap["internal"]["trade_history_trade_count"], 1)
        self.assertAlmostEqual(snap["internal"]["trade_history_realized"], -10.5, places=6)
        self.assertEqual(snap["internal"]["broker_supplemental_trade_count"], 0)
        self.assertEqual(snap["reconciliation"]["broker_vs_trade_history_diff"], 0.0)
        self.assertEqual(snap["reconciliation"]["status"], "healthy")

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
