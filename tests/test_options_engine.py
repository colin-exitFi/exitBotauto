import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, Mock

from src.options.options_engine import OptionsEngine


class OptionsEngineUnitTests(unittest.TestCase):
    def setUp(self):
        self.engine = OptionsEngine(api_key="k", secret_key="s", base_url="https://paper-api.alpaca.markets")

    def _future_date(self, days: int) -> str:
        return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%d")

    def test_find_contract_prefers_liquid_delta_target(self):
        contracts = [
            {
                "symbol": "AAPL1",
                "tradable": True,
                "strike_price": "95",
                "expiration_date": self._future_date(10),
                "open_interest": 200,
            },
            {
                "symbol": "AAPL2",
                "tradable": True,
                "strike_price": "100",
                "expiration_date": self._future_date(12),
                "open_interest": 300,
            },
            {
                "symbol": "AAPL3",
                "tradable": True,
                "strike_price": "110",
                "expiration_date": self._future_date(14),
                "open_interest": 500,
            },
        ]

        self.engine._fetch_contract_chain = lambda *args, **kwargs: contracts
        self.engine.estimate_delta = lambda underlying_price, strike, days_to_expiry, option_type, change_pct=0, volume_spike=1: {
            95.0: 0.72,
            100.0: 0.41,
            110.0: 0.18,
        }[float(strike)]
        self.engine.get_option_quote = lambda sym, force_refresh=False: {
            "AAPL1": {"bp": 1.20, "ap": 1.80},
            "AAPL2": {"bp": 1.05, "ap": 1.15},
            "AAPL3": {"bp": 0.35, "ap": 0.90},
        }.get(sym)

        selected = self.engine.find_contract("AAPL", 100.0, "BUY")
        self.assertIsNotNone(selected)
        self.assertEqual(selected["symbol"], "AAPL2")

    def test_find_contract_uses_snapshot_greeks_when_available(self):
        contracts = [
            {
                "symbol": "AAPL1",
                "tradable": True,
                "strike_price": "95",
                "expiration_date": self._future_date(10),
                "open_interest": 200,
            },
            {
                "symbol": "AAPL2",
                "tradable": True,
                "strike_price": "100",
                "expiration_date": self._future_date(12),
                "open_interest": 300,
            },
        ]

        self.engine._fetch_contract_chain = lambda *args, **kwargs: contracts
        self.engine.get_option_chain_snapshots = lambda *args, **kwargs: {
            "AAPL1": {"greeks": {"delta": 0.72}, "latestQuote": {"bp": 1.20, "ap": 1.22}},
            "AAPL2": {"greeks": {"delta": 0.41}, "latestQuote": {"bp": 1.05, "ap": 1.10}},
        }
        self.engine.get_option_quote = lambda *args, **kwargs: None
        self.engine.estimate_delta = lambda *args, **kwargs: 0.9  # should be ignored

        selected = self.engine.find_contract("AAPL", 100.0, "BUY")

        self.assertIsNotNone(selected)
        self.assertEqual(selected["symbol"], "AAPL2")
        self.assertEqual(selected["_delta_source"], "alpaca_snapshot")
        self.assertAlmostEqual(selected["_estimated_delta"], 0.41, places=2)

    def test_find_contract_rejects_illiquid_spreads(self):
        contracts = [
            {
                "symbol": "PENNYC",
                "tradable": True,
                "strike_price": "8",
                "expiration_date": self._future_date(10),
                "open_interest": 50,
            }
        ]
        self.engine._fetch_contract_chain = lambda *args, **kwargs: contracts
        self.engine.estimate_delta = lambda *args, **kwargs: 0.40
        self.engine.get_option_quote = lambda sym, force_refresh=False: {"bp": 0.10, "ap": 0.20}

        # Underlying < $20 allows up to 30% spread; this is 66% and should be rejected.
        selected = self.engine.find_contract("PENNY", 10.0, "BUY")
        self.assertIsNone(selected)

    def test_calculate_contract_qty_caps_at_five(self):
        qty = self.engine.calculate_contract_qty(budget=5000, premium_per_contract=100)
        self.assertEqual(qty, 5)

    def test_exit_rule_time_decay(self):
        contract = "AAPL_CALL"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "underlying": "AAPL",
            "entry_premium": 1.0,
            "current_premium": 1.0,
            "premium_hwm": 1.2,
            "premium_trail_pct": 35,
            "premium_stop_loss_pct": 50,
            "premium_profit_target_pct": 100,
            "qty": 2,
            "status": "open",
            "expiry": self._future_date(1),
            "entry_time": time.time() - 3600,
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 1.1
        self.engine.get_underlying_price = lambda *args, **kwargs: 0.0

        action = self.engine.check_exit_rules(contract)
        self.assertIsNotNone(action)
        self.assertEqual(action["reason"], "time_decay_exit")
        self.assertEqual(action["action"], "close")

    def test_exit_rule_stop_loss(self):
        contract = "AAPL_STOP"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "underlying": "AAPL",
            "entry_premium": 1.0,
            "current_premium": 1.0,
            "premium_hwm": 1.2,
            "premium_trail_pct": 35,
            "premium_stop_loss_pct": 50,
            "premium_profit_target_pct": 100,
            "qty": 1,
            "status": "open",
            "expiry": self._future_date(10),
            "entry_time": time.time() - 3600,
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 0.49
        self.engine.get_underlying_price = lambda *args, **kwargs: 0.0

        action = self.engine.check_exit_rules(contract)
        self.assertIsNotNone(action)
        self.assertEqual(action["reason"], "premium_stop_loss")

    def test_exit_rule_profit_target_partial(self):
        contract = "AAPL_WIN"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "underlying": "AAPL",
            "entry_premium": 1.0,
            "current_premium": 1.0,
            "premium_hwm": 1.3,
            "premium_trail_pct": 35,
            "premium_stop_loss_pct": 50,
            "premium_profit_target_pct": 100,
            "qty": 4,
            "partial_exit_done": False,
            "status": "open",
            "expiry": self._future_date(10),
            "entry_time": time.time() - 3600,
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 2.1
        self.engine.get_underlying_price = lambda *args, **kwargs: 0.0

        action = self.engine.check_exit_rules(contract)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial_take_profit")
        self.assertEqual(action["qty"], 2)
        self.assertTrue(self.engine.positions[contract]["partial_exit_done"])
        self.assertLessEqual(self.engine.positions[contract]["premium_trail_pct"], 20.0)

    def test_exit_rule_triple_target_partial(self):
        contract = "AAPL_TRIPLE"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "underlying": "AAPL",
            "entry_premium": 1.0,
            "current_premium": 1.0,
            "premium_hwm": 2.5,
            "premium_trail_pct": 20,
            "premium_stop_loss_pct": 50,
            "premium_profit_target_pct": 100,
            "premium_triple_target_mult": 3.0,
            "qty": 4,
            "partial_exit_done": True,
            "triple_partial_exit_done": False,
            "status": "open",
            "expiry": self._future_date(10),
            "entry_time": time.time() - 3600,
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 3.1
        self.engine.get_underlying_price = lambda *args, **kwargs: 0.0

        action = self.engine.check_exit_rules(contract)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "partial_take_profit")
        self.assertEqual(action["reason"], "premium_triple_target_partial")
        self.assertEqual(action["qty"], 2)
        self.assertTrue(self.engine.positions[contract]["triple_partial_exit_done"])

    def test_exit_rule_expiry_day_cleanup_friday_after_330pm(self):
        contract = "AAPL_EXPIRY"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "underlying": "AAPL",
            "entry_premium": 1.0,
            "current_premium": 1.0,
            "premium_hwm": 1.1,
            "premium_trail_pct": 35,
            "premium_stop_loss_pct": 50,
            "premium_profit_target_pct": 100,
            "qty": 1,
            "status": "open",
            "expiry": self._future_date(10),
            "entry_time": time.time() - 3600,
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 1.05
        self.engine.get_underlying_price = lambda *args, **kwargs: 0.0
        self.engine._is_expiry_day_cleanup_window = lambda expiry: True

        action = self.engine.check_exit_rules(contract)
        self.assertIsNotNone(action)
        self.assertEqual(action["action"], "close")
        self.assertEqual(action["reason"], "expiry_day_cleanup")

    def test_exit_rule_quote_unavailable_skips_without_forced_exit(self):
        contract = "AAPL_NOQUOTE"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "underlying": "AAPL",
            "entry_premium": 1.0,
            "current_premium": 1.0,
            "premium_hwm": 1.2,
            "premium_trail_pct": 35,
            "premium_stop_loss_pct": 50,
            "premium_profit_target_pct": 100,
            "qty": 2,
            "status": "open",
            "expiry": self._future_date(10),
            "entry_time": time.time() - 3600,
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 0.0
        self.engine.get_underlying_price = lambda *args, **kwargs: 0.0

        action = self.engine.check_exit_rules(contract)
        self.assertIsNone(action)
        self.assertEqual(self.engine.positions[contract]["qty"], 2)

    def test_exit_rule_trailing_stop(self):
        contract = "AAPL_TRAIL"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "underlying": "AAPL",
            "entry_premium": 1.0,
            "current_premium": 1.0,
            "premium_hwm": 1.5,
            "premium_trail_pct": 35,
            "premium_stop_loss_pct": 50,
            "premium_profit_target_pct": 100,
            "qty": 1,
            "partial_exit_done": True,
            "status": "open",
            "expiry": self._future_date(10),
            "entry_time": time.time() - 3600,
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 0.90
        self.engine.get_underlying_price = lambda *args, **kwargs: 0.0

        action = self.engine.check_exit_rules(contract)
        self.assertIsNotNone(action)
        self.assertEqual(action["reason"], "premium_trailing_stop")

    def test_close_option_position_uses_bid_aware_limit(self):
        contract = "AAPL_EXIT_LIMIT"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "qty": 3,
            "status": "open",
        }
        self.engine.get_option_quote = lambda *args, **kwargs: {"bp": 1.50, "ap": 1.70}
        calls = []

        def _place(contract_symbol, qty=1, side="buy", order_type="market", limit_price=None):
            calls.append(
                {
                    "symbol": contract_symbol,
                    "qty": qty,
                    "side": side,
                    "order_type": order_type,
                    "limit_price": limit_price,
                }
            )
            return {"id": "o-limit", "type": order_type}

        self.engine.place_option_order = _place
        with patch("src.options.options_engine.settings.OPTIONS_EXIT_BID_AWARE_LIMIT", True), \
             patch("src.options.options_engine.settings.OPTIONS_EXIT_BID_DISCOUNT_PCT", 0.0), \
             patch("src.options.options_engine.settings.OPTIONS_EXIT_MARKET_FALLBACK", True):
            order = self.engine.close_option_position(contract, qty=2, reason="test")

        self.assertIsNotNone(order)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["order_type"], "limit")
        self.assertEqual(calls[0]["limit_price"], 1.5)
        self.assertEqual(self.engine.positions[contract]["status"], "closing")
        self.assertEqual(self.engine.positions[contract]["pending_close_qty"], 2)
        self.assertEqual(self.engine.positions[contract]["close_order_type"], "limit")

    def test_close_option_position_falls_back_to_market_when_no_limit_quote(self):
        contract = "AAPL_EXIT_FALLBACK"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "qty": 1,
            "status": "open",
        }
        self.engine.get_option_quote = lambda *args, **kwargs: None
        calls = []

        def _place(contract_symbol, qty=1, side="buy", order_type="market", limit_price=None):
            calls.append(order_type)
            return {"id": "o-mkt", "type": order_type}

        self.engine.place_option_order = _place
        with patch("src.options.options_engine.settings.OPTIONS_EXIT_BID_AWARE_LIMIT", True), \
             patch("src.options.options_engine.settings.OPTIONS_EXIT_MARKET_FALLBACK", True):
            order = self.engine.close_option_position(contract, qty=1, reason="test")

        self.assertIsNotNone(order)
        self.assertEqual(calls, ["market"])
        self.assertEqual(self.engine.positions[contract]["close_order_type"], "market")

    def test_close_option_position_caches_pdt_rejections_and_stops_retrying(self):
        contract = "AAPL_PDT"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "qty": 1,
            "status": "open",
        }
        response = Mock()
        response.status_code = 403
        response.text = '{"code":40310100,"message":"trade denied due to pattern day trading protection"}'
        response.json.return_value = {
            "code": 40310100,
            "message": "trade denied due to pattern day trading protection",
        }

        with patch("src.options.options_engine.httpx.post", return_value=response) as post_mock, \
             patch("src.options.options_engine.settings.OPTIONS_EXIT_BID_AWARE_LIMIT", False), \
             patch("src.options.options_engine.settings.OPTIONS_EXIT_MARKET_FALLBACK", True):
            first = self.engine.close_option_position(contract, qty=1, reason="test")
            second = self.engine.close_option_position(contract, qty=1, reason="test")

        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(post_mock.call_count, 1)
        self.assertIn(contract, self.engine._pdt_rejection_cache)
        self.assertTrue(self.engine.positions[contract]["pdt_close_blocked"])

    def test_close_paired_options_skips_unfilled_orders(self):
        contract = "AAPLOPT"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "symbol": contract,
            "underlying": "AAPL",
            "qty": 2,
            "entry_premium": 1.2,
            "current_premium": 1.1,
            "status": "open",
            "entry_time": time.time() - 300,
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
            "decision_confidence": 88,
            "provider_used": "claude",
        }
        self.engine.get_current_premium = lambda *args, **kwargs: 1.0
        self.engine.place_option_order = lambda *args, **kwargs: {
            "id": "o-pending",
            "type": "limit",
            "status": "new",
            "filled_qty": "0",
            "filled_avg_price": None,
        }

        exits = self.engine.close_paired_options("AAPL", reason="underlying_exit")

        self.assertEqual(len(exits), 1)
        self.assertEqual(exits[0]["status"], "new")
        self.assertIn(contract, self.engine.positions)
        self.assertEqual(self.engine.positions[contract]["status"], "closing")

    def test_finalize_exit_keeps_remaining_position_closing_on_partial_fill(self):
        contract = "AAPL_PARTIAL"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "symbol": contract,
            "underlying": "AAPL",
            "qty": 2,
            "entry_premium": 1.0,
            "current_premium": 1.1,
            "status": "closing",
            "entry_time": time.time() - 300,
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
            "decision_confidence": 80,
            "provider_used": "claude",
        }
        order = {
            "id": "o-partial",
            "status": "partially_filled",
            "filled_qty": "1",
            "filled_avg_price": "1.2",
        }

        trade = self.engine.finalize_exit(contract, qty=2, exit_premium=0.0, reason="partial_take_profit", order=order)

        self.assertIsNotNone(trade)
        self.assertEqual(int(trade["quantity"]), 1)
        self.assertIn(contract, self.engine.positions)
        self.assertEqual(int(self.engine.positions[contract]["qty"]), 1)
        self.assertEqual(self.engine.positions[contract]["status"], "closing")

    def test_reconcile_with_broker_records_partial_option_close(self):
        contract = "AAPL_RECON"
        self.engine.positions[contract] = {
            "contract_symbol": contract,
            "symbol": contract,
            "underlying": "AAPL",
            "qty": 3,
            "entry_premium": 1.0,
            "current_premium": 1.1,
            "status": "closing",
            "entry_time": time.time() - 300,
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
            "decision_confidence": 80,
            "provider_used": "claude",
            "pending_close_fill_price": 1.3,
            "pending_close_fill_at": time.time(),
            "last_exit_reason": "partial_take_profit",
        }
        response = Mock()
        response.status_code = 200
        response.json.return_value = [
            {
                "symbol": contract,
                "asset_class": "option",
                "qty": "2",
                "avg_entry_price": "1.0",
                "underlying_symbol": "AAPL",
            }
        ]

        with patch("src.options.options_engine.httpx.get", return_value=response):
            recon = self.engine.reconcile_with_broker()

        self.assertEqual(len(recon["reconciled_trades"]), 1)
        self.assertEqual(int(recon["reconciled_trades"][0]["quantity"]), 1)
        self.assertEqual(int(self.engine.positions[contract]["qty"]), 2)
        self.assertEqual(self.engine.positions[contract]["status"], "open")


if __name__ == "__main__":
    unittest.main()
