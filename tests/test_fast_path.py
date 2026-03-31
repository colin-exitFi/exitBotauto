import asyncio
import json
import time
import unittest
from unittest.mock import AsyncMock, patch

import src.main as main_module


class _RiskOK:
    def can_trade(self):
        return True

    def is_wash_sale(self, symbol: str):
        return False

    def can_open_position(self, current_positions, symbol: str = None):
        return True

    def can_enter_sector(self, symbol: str, positions):
        return True

    def get_risk_tier(self):
        return {"size_pct": 2.5}

    def is_swing_mode(self):
        return False


class _EntryNoNetwork:
    def __init__(self):
        self.positions = {}
        self.add_calls = 0

    def get_positions(self):
        return list(self.positions.values())

    async def can_enter(self, symbol, sentiment, positions):
        raise AssertionError("deterministic fast-path screen should not call can_enter")

    async def add_to_scout(self, symbol, sentiment_data):
        self.add_calls += 1
        pos = self.positions.get(symbol)
        if not pos:
            return None
        pos["scout_escalated"] = True
        return pos


class _Verdict:
    def __init__(self, decision="BUY", confidence=85, size_pct=2.5, trail_pct=2.0):
        self.decision = decision
        self.confidence = confidence
        self.size_pct = size_pct
        self.trail_pct = trail_pct
        self.provider_used = "test"
        self.reasoning = "ok"

    def to_dict(self):
        return {
            "decision": self.decision,
            "confidence": self.confidence,
            "size_pct": self.size_pct,
            "trail_pct": self.trail_pct,
            "provider_used": self.provider_used,
            "reasoning": self.reasoning,
        }


class _Orchestrator:
    def __init__(self, verdict):
        self.verdict = verdict
        self.calls = 0

    async def evaluate(self, symbol: str, price: float, signals_data: dict):
        self.calls += 1
        return self.verdict


class FastPathTests(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_screen_is_zero_network_and_passes_without_cached_rsi(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_pending = set()
        bot.entry_manager = _EntryNoNetwork()
        bot.risk_manager = _RiskOK()
        with patch.object(main_module.settings, "FAST_PATH_ENABLED", True), \
             patch.object(main_module.settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0), \
             patch.object(main_module.settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0), \
             patch.object(main_module, "get_cached_rsi", return_value=None):
            ok, reason = bot._passes_fast_path_deterministic_screen(
                symbol="AAPL",
                price=100.0,
                pct_change=6.0,
                volume_spike=2.5,
            )
        self.assertTrue(ok, reason)

    async def test_deterministic_screen_accepts_downside_breakouts_by_absolute_move(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_pending = set()
        bot._jury_vetoed_symbols = {}
        bot.entry_manager = _EntryNoNetwork()
        bot.risk_manager = _RiskOK()
        bot._latest_broker_position_symbols = set()
        with patch.object(main_module.settings, "FAST_PATH_ENABLED", True), \
             patch.object(main_module.settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0), \
             patch.object(main_module.settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0), \
             patch.object(main_module, "get_cached_rsi", return_value=55.0):
            ok, reason = bot._passes_fast_path_deterministic_screen(
                symbol="AAPL",
                price=100.0,
                pct_change=-6.0,
                volume_spike=2.5,
            )
        self.assertTrue(ok, reason)

    async def test_on_breakout_detected_routes_downside_breakouts(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        called = {}

        def _capture(**kwargs):
            called.update(kwargs)

        bot._handle_fast_path_breakout = _capture

        with patch.object(main_module.settings, "FAST_PATH_ENABLED", True), \
             patch.object(main_module, "log_activity"):
            bot._on_breakout_detected("AAPL", 100.0, 3.2, -5.5)

        self.assertEqual(called["symbol"], "AAPL")
        self.assertEqual(called["pct_change"], -5.5)

    async def test_idempotency_guard_prevents_duplicate_fast_path_tasks(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_pending = set()
        bot.entry_manager = _EntryNoNetwork()
        bot.risk_manager = _RiskOK()
        bot._breakout_queue = asyncio.Queue()
        bot._fast_path_eval_queue = asyncio.Queue()

        with patch.object(main_module.settings, "FAST_PATH_ENABLED", True), \
             patch.object(main_module.settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0), \
             patch.object(main_module.settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0), \
             patch.object(main_module, "get_cached_rsi", return_value=60.0):
            bot._handle_fast_path_breakout("AAPL", 100.0, 6.0, 3.0)
            bot._handle_fast_path_breakout("AAPL", 100.0, 6.5, 3.2)
            await asyncio.sleep(0.01)

        self.assertEqual(bot._breakout_queue.qsize(), 1)
        queued = bot._breakout_queue.get_nowait()
        self.assertEqual(queued["symbol"], "AAPL")

    async def test_scout_queue_escalates_to_full_once(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_eval_queue = asyncio.Queue()
        bot._fast_path_pending = set()
        bot.risk_manager = _RiskOK()
        bot.entry_manager = _EntryNoNetwork()
        bot.entry_manager.positions["AAPL"] = {
            "symbol": "AAPL",
            "side": "long",
            "strategy_tag": "breakout_fast_path",
            "order_status": "filled",
            "entry_price": 100.0,
            "quantity": 10.0,
            "sentiment_at_entry": 0.5,
            "scout_escalated": False,
            "trail_pct": 3.0,
        }
        bot.orchestrator = _Orchestrator(_Verdict(decision="BUY"))
        bot.ai_layers = {}

        await bot._fast_path_eval_queue.put(
            {"symbol": "AAPL", "price": 100.0, "change_pct": 6.0, "volume_spike": 2.5}
        )

        with patch.object(main_module, "log_activity"):
            await bot._evaluate_fast_path_scouts()

        self.assertEqual(bot.orchestrator.calls, 1)
        self.assertEqual(bot.entry_manager.add_calls, 1)
        self.assertTrue(bot.entry_manager.positions["AAPL"]["scout_escalated"])

    async def test_pending_scout_is_deferred_without_ai_call(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_eval_queue = asyncio.Queue()
        bot._fast_path_pending = set()
        bot.risk_manager = _RiskOK()
        bot.entry_manager = _EntryNoNetwork()
        bot.entry_manager.positions["AAPL"] = {
            "symbol": "AAPL",
            "side": "long",
            "strategy_tag": "breakout_fast_path",
            "order_status": "pending",
            "entry_price": 100.0,
            "quantity": 10.0,
            "sentiment_at_entry": 0.5,
            "scout_escalated": False,
            "trail_pct": 3.0,
        }
        bot.orchestrator = _Orchestrator(_Verdict(decision="BUY"))
        bot.ai_layers = {}

        await bot._fast_path_eval_queue.put(
            {
                "symbol": "AAPL",
                "price": 100.0,
                "change_pct": 6.0,
                "volume_spike": 2.5,
                "attempts": 0,
                "first_enqueued_at": time.time(),
            }
        )

        with patch.object(main_module.settings, "FAST_PATH_EVAL_MAX_CYCLES", 3), \
             patch.object(main_module.settings, "FAST_PATH_EVAL_MAX_AGE_SECONDS", 90), \
             patch.object(main_module, "log_activity"):
            await bot._evaluate_fast_path_scouts()

        self.assertEqual(bot.orchestrator.calls, 0)
        self.assertEqual(bot._fast_path_eval_queue.qsize(), 1)
        queued = bot._fast_path_eval_queue.get_nowait()
        self.assertEqual(queued.get("attempts"), 1)

    async def test_jury_veto_blocks_fast_path_until_expiry(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_pending = set()
        bot._jury_vetoed_symbols = {"AAPL": time.time()}
        bot.entry_manager = _EntryNoNetwork()
        bot.risk_manager = _RiskOK()

        with patch.object(main_module.settings, "FAST_PATH_ENABLED", True), \
             patch.object(main_module.settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0), \
             patch.object(main_module.settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0), \
             patch.object(main_module, "get_cached_rsi", return_value=60.0):
            ok, reason = bot._passes_fast_path_deterministic_screen(
                symbol="AAPL",
                price=100.0,
                pct_change=6.0,
                volume_spike=3.0,
            )

        self.assertFalse(ok)
        self.assertEqual(reason, "jury_vetoed")

    async def test_swing_mode_disables_fast_path(self):
        class _SwingRisk(_RiskOK):
            def is_swing_mode(self):
                return True

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_pending = set()
        bot._jury_vetoed_symbols = {}
        bot.entry_manager = _EntryNoNetwork()
        bot.risk_manager = _SwingRisk()

        with patch.object(main_module.settings, "FAST_PATH_ENABLED", True), \
             patch.object(main_module.settings, "SWING_MODE_DISABLE_FAST_PATH", True), \
             patch.object(main_module.settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0), \
             patch.object(main_module.settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0), \
             patch.object(main_module, "get_cached_rsi", return_value=60.0):
            ok, reason = bot._passes_fast_path_deterministic_screen(
                symbol="AAPL",
                price=100.0,
                pct_change=6.0,
                volume_spike=3.0,
            )

        self.assertFalse(ok)
        self.assertEqual(reason, "swing_mode_disabled")

    async def test_crossed_short_ratchet_submits_software_exit(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = object()
        bot._entry_session_label = lambda: "regular"

        async def _fake_ensure_hard_stop(position, open_orders_by_symbol, current_price):
            return None

        canceled = []

        async def _fake_cancel(order_id):
            canceled.append(order_id)
            return True

        submitted = []

        async def _fake_submit(position, current_price, reason):
            submitted.append((position["symbol"], current_price, reason))
            position["exit_pending"] = True
            return True

        bot._ensure_hard_stop = _fake_ensure_hard_stop
        bot._cancel_order_and_confirm = _fake_cancel
        bot._submit_software_managed_exit = _fake_submit

        position = {
            "symbol": "NDLS",
            "side": "short",
            "quantity": 104,
            "ratchet_limit_order_id": "ratchet-1",
            "order_state": {},
        }
        action = {
            "ratchet_active": True,
            "target_exit_price": 9.48,
            "action": "ratchet_exit",
            "peak_pnl_pct": 3.05,
            "current_pnl_pct": -0.21,
            "floor_pct": 0.25,
        }
        open_orders = {
            "NDLS": [
                {
                    "id": "ratchet-1",
                    "symbol": "NDLS",
                    "side": "buy",
                    "type": "stop",
                    "client_order_id": "NDLS_ratchet_123",
                }
            ]
        }

        await bot._apply_profit_ratchet_action(position, 9.70, action, open_orders)

        self.assertEqual(canceled, ["ratchet-1"])
        self.assertEqual(submitted, [("NDLS", 9.70, "ratchet_exit")])

    async def test_short_ratchet_broker_rejection_submits_software_exit(self):
        class _RejectingAlpaca:
            def place_stop_order(self, symbol, qty, target_price, side, client_order_id, whole_only):
                return None

            def pop_order_error(self, client_order_id):
                return {
                    "status_code": 422,
                    "body": json.dumps(
                        {
                            "code": 42210000,
                            "market_price": "9.51",
                            "message": "stop price must be greater than current price",
                            "stop_price": "9.48",
                        }
                    ),
                }

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = _RejectingAlpaca()
        bot._entry_session_label = lambda: "regular"

        async def _fake_ensure_hard_stop(position, open_orders_by_symbol, current_price):
            return None

        submitted = []

        async def _fake_submit(position, current_price, reason):
            submitted.append((position["symbol"], current_price, reason))
            position["exit_pending"] = True
            return True

        bot._ensure_hard_stop = _fake_ensure_hard_stop
        bot._cancel_order_and_confirm = AsyncMock(return_value=True)
        bot._submit_software_managed_exit = _fake_submit

        position = {
            "symbol": "NDLS",
            "side": "short",
            "quantity": 104,
            "order_state": {},
        }
        action = {
            "ratchet_active": True,
            "target_exit_price": 9.48,
            "action": "tighten",
            "peak_pnl_pct": 3.05,
            "current_pnl_pct": 0.65,
            "floor_pct": 0.25,
        }

        await bot._apply_profit_ratchet_action(position, 9.47, action, {})

        self.assertEqual(submitted, [("NDLS", 9.51, "ratchet_exit")])
        self.assertEqual(position.get("ratchet_order_type"), "software_exit")

    async def test_same_target_ratchet_respects_replace_cooldown(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.alpaca_client = object()
        bot._entry_session_label = lambda: "regular"

        async def _unexpected_cancel(order_id):
            raise AssertionError("Cooldown path should not cancel ratchet orders")

        bot._cancel_order_and_confirm = _unexpected_cancel

        position = {
            "symbol": "CCL",
            "side": "short",
            "quantity": 6,
            "ratchet_limit_order_id": "ratchet-1",
            "ratchet_last_target_price": 24.57,
            "ratchet_last_place_attempt_at": time.time(),
            "order_state": {},
        }

        with patch.object(main_module.settings, "PROFIT_RATCHET_REPLACE_COOLDOWN_SECONDS", 20.0):
            ok = await bot._place_or_replace_ratchet_order(position, 24.57, {})

        self.assertTrue(ok)
        self.assertEqual(position["order_state"]["ratchet"], "cooldown_skip")

    async def test_uw_signal_queue_schedules_immediate_drain(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._uw_signal_queue = asyncio.Queue(maxsize=50)
        bot._uw_signal_drain_task = None
        bot._recent_uw_signal_keys = {}
        bot.ai_layers = {}

        drained = []

        async def _fake_process_queue():
            drained.append("drain")
            while not bot._uw_signal_queue.empty():
                await bot._uw_signal_queue.get()

        bot._process_unusual_whales_signal_queue = _fake_process_queue

        with patch.object(main_module, "log_activity"), \
             patch.object(main_module.settings, "UW_STREAM_MIN_DARK_POOL_PREMIUM", 1.0):
            await bot._on_unusual_whales_signal(
                {
                    "event_type": "dark_pool",
                    "ticker": "TSLA",
                    "premium": 500000.0,
                    "sentiment": "bullish",
                    "price": 100.0,
                    "size": 1000.0,
                }
            )
            await asyncio.sleep(0.05)

        self.assertEqual(drained, ["drain"])
        self.assertTrue(bot._uw_signal_queue.empty())

    async def test_hold_decision_requeues_with_tightened_trail(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_eval_queue = asyncio.Queue()
        bot._fast_path_pending = set()
        bot.risk_manager = _RiskOK()
        bot.entry_manager = _EntryNoNetwork()
        bot.entry_manager.positions["AAPL"] = {
            "symbol": "AAPL",
            "side": "long",
            "strategy_tag": "breakout_fast_path",
            "order_status": "filled",
            "entry_price": 100.0,
            "quantity": 10.0,
            "sentiment_at_entry": 0.5,
            "scout_escalated": False,
            "trail_pct": 3.0,
        }
        bot.orchestrator = _Orchestrator(_Verdict(decision="SKIP", trail_pct=2.0))
        bot.ai_layers = {}

        await bot._fast_path_eval_queue.put(
            {
                "symbol": "AAPL",
                "price": 100.0,
                "change_pct": 6.0,
                "volume_spike": 2.5,
                "attempts": 0,
                "first_enqueued_at": time.time(),
            }
        )

        with patch.object(main_module.settings, "FAST_PATH_EVAL_MAX_CYCLES", 3), \
             patch.object(main_module.settings, "FAST_PATH_EVAL_MAX_AGE_SECONDS", 90), \
             patch.object(main_module, "log_activity"):
            await bot._evaluate_fast_path_scouts()

        self.assertEqual(bot.orchestrator.calls, 1)
        self.assertEqual(bot._fast_path_eval_queue.qsize(), 1)
        self.assertEqual(bot.entry_manager.positions["AAPL"]["trail_pct"], 2.0)
        queued = bot._fast_path_eval_queue.get_nowait()
        self.assertEqual(queued.get("attempts"), 1)


if __name__ == "__main__":
    unittest.main()
