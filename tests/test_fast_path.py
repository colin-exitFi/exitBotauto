import asyncio
import time
import unittest
from unittest.mock import patch

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

    async def test_idempotency_guard_prevents_duplicate_fast_path_tasks(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot._fast_path_pending = set()
        bot.entry_manager = _EntryNoNetwork()
        bot.risk_manager = _RiskOK()
        bot._fast_path_eval_queue = asyncio.Queue()

        calls = []
        gate = asyncio.Event()

        async def _stub(candidate):
            calls.append(candidate["symbol"])
            await gate.wait()

        bot._execute_fast_path_scout_entry = _stub

        with patch.object(main_module.settings, "FAST_PATH_ENABLED", True), \
             patch.object(main_module.settings, "FAST_PATH_MIN_CHANGE_PCT", 5.0), \
             patch.object(main_module.settings, "FAST_PATH_MIN_VOLUME_SPIKE", 2.0), \
             patch.object(main_module, "get_cached_rsi", return_value=60.0):
            bot._handle_fast_path_breakout("AAPL", 100.0, 6.0, 3.0)
            bot._handle_fast_path_breakout("AAPL", 100.0, 6.5, 3.2)
            await asyncio.sleep(0.01)

        self.assertEqual(calls, ["AAPL"])
        gate.set()
        await asyncio.sleep(0)

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
