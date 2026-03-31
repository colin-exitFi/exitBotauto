import time
import unittest
from unittest.mock import AsyncMock, patch

import src.agents.exit_agent as exit_agent_module
from src.agents.exit_agent import ExitAgent


class _ExitManager:
    def __init__(self):
        self.calls = []

    async def _execute_exit(self, position, quantity, price, reason, pnl_pct):
        self.calls.append(
            {
                "symbol": position["symbol"],
                "quantity": quantity,
                "price": price,
                "reason": reason,
                "pnl_pct": pnl_pct,
            }
        )
        position["exit_pending"] = True
        return {"id": "order-1"}


class ExitAgentExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_now_requires_confirmation_before_execution(self):
        exit_manager = _ExitManager()
        agent = ExitAgent(exit_manager=exit_manager)
        pos = {
            "symbol": "SPY",
            "side": "short",
            "quantity": 1.0,
            "entry_time": time.time() - 600,
            "trail_pct": 3.0,
        }
        action = {
            "action": "EXIT_NOW",
            "reasoning": "failed breakdown",
            "pnl_pct": -0.1,
            "hold_seconds": 600,
            "current_price": 649.6,
        }

        with patch.object(exit_agent_module.settings, "EXIT_AGENT_EXECUTE_EXIT_NOW_ENABLED", True), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_CONFIRMATIONS", 2), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_MIN_HOLD_MINUTES", 3.0), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_MAX_PNL_PCT", 0.5), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_CONFIRM_WINDOW_SECONDS", 900.0):
            await agent._execute_action("SPY", pos, dict(action))
            self.assertEqual(exit_manager.calls, [])
            await agent._execute_action("SPY", pos, dict(action))

        self.assertEqual(len(exit_manager.calls), 1)
        self.assertTrue(pos["exit_pending"])

    async def test_exit_now_does_not_execute_for_profitable_runner(self):
        exit_manager = _ExitManager()
        agent = ExitAgent(exit_manager=exit_manager)
        pos = {
            "symbol": "NVDA",
            "side": "long",
            "quantity": 2.0,
            "entry_time": time.time() - 900,
            "trail_pct": 3.0,
        }
        action = {
            "action": "EXIT_NOW",
            "reasoning": "take caution",
            "pnl_pct": 1.2,
            "hold_seconds": 900,
            "current_price": 101.2,
        }

        with patch.object(exit_agent_module.settings, "EXIT_AGENT_EXECUTE_EXIT_NOW_ENABLED", True), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_CONFIRMATIONS", 2), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_MIN_HOLD_MINUTES", 3.0), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_MAX_PNL_PCT", 0.5), \
             patch.object(exit_agent_module.settings, "EXIT_AGENT_EXIT_NOW_CONFIRM_WINDOW_SECONDS", 900.0):
            await agent._execute_action("NVDA", pos, dict(action))
            await agent._execute_action("NVDA", pos, dict(action))

        self.assertEqual(exit_manager.calls, [])

    async def test_evaluate_position_attaches_runtime_metrics(self):
        agent = ExitAgent()
        agent._lookup_broker_position = lambda symbol: (False, None)
        pos = {
            "symbol": "SPY",
            "side": "short",
            "entry_price": 100.0,
            "quantity": 1.0,
            "entry_time": time.time() - 600,
            "trail_pct": 3.0,
            "peak_price": 99.8,
        }

        with patch("src.agents.exit_agent.call_claude", AsyncMock(return_value={"action": "EXIT_NOW", "reasoning": "failed"})):
            action = await agent._evaluate_position(pos)

        self.assertEqual(action["action"], "EXIT_NOW")
        self.assertIn("current_price", action)
        self.assertIn("pnl_pct", action)
        self.assertGreater(action["hold_seconds"], 0)

    async def test_tighten_updates_ratchet_metadata_without_broker_refresh(self):
        agent = ExitAgent(entry_manager=None, broker=None)
        pos = {
            "symbol": "NAVN",
            "side": "long",
            "quantity": 42.0,
            "trail_pct": 3.0,
        }

        tightened = await agent._apply_trail_adjustment("NAVN", pos, 1.5, "lock gains")

        self.assertTrue(tightened)
        self.assertEqual(pos["trail_pct"], 1.5)
        self.assertEqual(pos["ratchet_tighten_suggestion_pct"], 1.5)


if __name__ == "__main__":
    unittest.main()
