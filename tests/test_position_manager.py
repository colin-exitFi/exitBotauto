import json
import time
import types
import unittest
from unittest.mock import patch

import src.ai.position_manager as pm_module


class _FakeResponse:
    def __init__(self, payload):
        self.content = [types.SimpleNamespace(text=json.dumps(payload))]


class _FakeClient:
    def __init__(self, payload):
        self.messages = types.SimpleNamespace(create=lambda **kwargs: _FakeResponse(payload))


class _FakeExitManager:
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
        return {"status": "exit_pending", "symbol": position["symbol"]}


class _FakeEntryManager:
    def __init__(self, positions):
        self._positions = positions

    def get_positions(self):
        return list(self._positions)

    def is_market_open(self):
        return True


class _FakePolygon:
    def __init__(self, prices):
        self._prices = prices

    def get_price(self, symbol):
        return self._prices.get(symbol, 0)


class PositionManagerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rule_based_fallback_executes_without_ai_client(self):
        position = {
            "symbol": "NRG",
            "side": "short",
            "entry_price": 100.0,
            "quantity": 5.0,
            "entry_time": time.time() - 3600,
            "peak_price": 99.5,
        }
        exit_manager = _FakeExitManager()
        bot = types.SimpleNamespace(
            entry_manager=_FakeEntryManager([position]),
            risk_manager=types.SimpleNamespace(get_status=lambda: {}),
            observer=types.SimpleNamespace(get_last_output=lambda: {}),
            orchestrator=types.SimpleNamespace(exit_agent=types.SimpleNamespace(_last_briefs={})),
            polygon_client=_FakePolygon({"NRG": 101.2}),
            sentiment_analyzer=None,
            exit_manager=exit_manager,
            alpaca_client=None,
        )
        manager = pm_module.PositionManager()
        manager._client = None

        with patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_STRATEGIC_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_LOSS_PCT", 0.75, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_MIN_HOLD_MINUTES", 45.0, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_BAND_PCT", 0.25, create=True):
            result = await manager.run(bot, advisor_output={})

        self.assertEqual(result["portfolio_health"], "stressed")
        self.assertEqual(len(exit_manager.calls), 1)
        self.assertEqual(exit_manager.calls[0]["symbol"], "NRG")

    async def test_auto_executes_high_urgency_strategic_exit_for_real_loser(self):
        position = {
            "symbol": "NRG",
            "side": "short",
            "entry_price": 100.0,
            "quantity": 5.0,
            "entry_time": time.time() - 3600,
            "peak_price": 99.5,
        }
        exit_manager = _FakeExitManager()
        bot = types.SimpleNamespace(
            entry_manager=_FakeEntryManager([position]),
            risk_manager=types.SimpleNamespace(get_status=lambda: {}),
            observer=types.SimpleNamespace(get_last_output=lambda: {}),
            orchestrator=types.SimpleNamespace(exit_agent=types.SimpleNamespace(_last_briefs={})),
            polygon_client=_FakePolygon({"NRG": 101.2}),
            sentiment_analyzer=None,
            exit_manager=exit_manager,
            alpaca_client=None,
        )
        manager = pm_module.PositionManager()
        manager._client = _FakeClient(
            {
                "strategic_exits": [{"symbol": "NRG", "reason": "stop the bleed", "urgency": "high"}],
                "portfolio_health": "stressed",
            }
        )

        with patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_STRATEGIC_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_LOSS_PCT", 0.75, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_MIN_HOLD_MINUTES", 45.0, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_BAND_PCT", 0.25, create=True):
            result = await manager.run(bot, advisor_output={})

        self.assertEqual(result["portfolio_health"], "stressed")
        self.assertEqual(len(exit_manager.calls), 1)
        self.assertEqual(exit_manager.calls[0]["symbol"], "NRG")
        self.assertIn("pm_strategic_exit", exit_manager.calls[0]["reason"])

    async def test_profitable_position_stays_recommendation_only(self):
        position = {
            "symbol": "NAVN",
            "side": "long",
            "entry_price": 10.0,
            "quantity": 10.0,
            "entry_time": time.time() - 3600,
            "peak_price": 10.4,
        }
        exit_manager = _FakeExitManager()
        bot = types.SimpleNamespace(
            entry_manager=_FakeEntryManager([position]),
            risk_manager=types.SimpleNamespace(get_status=lambda: {}),
            observer=types.SimpleNamespace(get_last_output=lambda: {}),
            orchestrator=types.SimpleNamespace(exit_agent=types.SimpleNamespace(_last_briefs={})),
            polygon_client=_FakePolygon({"NAVN": 10.22}),
            sentiment_analyzer=None,
            exit_manager=exit_manager,
            alpaca_client=None,
        )
        manager = pm_module.PositionManager()
        manager._client = _FakeClient(
            {
                "strategic_exits": [{"symbol": "NAVN", "reason": "bank it", "urgency": "high"}],
                "portfolio_health": "healthy",
            }
        )

        with patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_STRATEGIC_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_LOSS_PCT", 0.75, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_MIN_HOLD_MINUTES", 45.0, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_BAND_PCT", 0.25, create=True):
            await manager.run(bot, advisor_output={})

        self.assertEqual(exit_manager.calls, [])

    async def test_dead_money_rule_does_not_clip_small_green_too_early(self):
        position = {
            "symbol": "IWM",
            "side": "short",
            "entry_price": 248.52,
            "quantity": 2.0,
            "entry_time": time.time() - (60 * 60),
            "peak_price": 247.58,
        }
        exit_manager = _FakeExitManager()
        bot = types.SimpleNamespace(
            entry_manager=_FakeEntryManager([position]),
            risk_manager=types.SimpleNamespace(get_status=lambda: {}),
            observer=types.SimpleNamespace(get_last_output=lambda: {}),
            orchestrator=types.SimpleNamespace(exit_agent=types.SimpleNamespace(_last_briefs={})),
            polygon_client=_FakePolygon({"IWM": 248.07}),
            sentiment_analyzer=None,
            exit_manager=exit_manager,
            alpaca_client=None,
        )
        manager = pm_module.PositionManager()
        manager._client = None

        with patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_STRATEGIC_EXIT_ENABLED", True, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_AUTO_EXIT_LOSS_PCT", 0.75, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_MIN_HOLD_MINUTES", 75.0, create=True), \
             patch.object(pm_module.settings, "POSITION_MANAGER_DEAD_MONEY_BAND_PCT", 0.15, create=True):
            result = await manager.run(bot, advisor_output={})

        self.assertEqual(result["portfolio_health"], "healthy")
        self.assertEqual(exit_manager.calls, [])
