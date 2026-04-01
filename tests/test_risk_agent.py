import unittest

from unittest.mock import patch

from src.agents import risk_agent


class _RiskStub:
    equity = 27500.0

    @staticmethod
    def get_status():
        return {"equity": 27500.0, "heat_pct": 10.0, "consecutive_losses": 0}

    @staticmethod
    def get_risk_tier():
        return {"size_pct": 2.5, "max_positions": 20}

    @staticmethod
    def is_wash_sale(symbol: str):
        return False

    @staticmethod
    def can_trade():
        return True


class RiskAgentExtendedHoursTests(unittest.IsolatedAsyncioTestCase):
    async def test_extended_hours_can_allow_tier2_tight_spread(self):
        signals = {
            "signal_tier": "tier_2",
            "strategy_tag": "momentum_short",
            "entry_quality": "pullback",
            "extended_hours": True,
            "spread_pct": 0.12,
        }

        with patch.object(risk_agent.settings, "EXTENDED_HOURS_TIER1_ONLY", True, create=True), \
             patch.object(risk_agent.settings, "EXTENDED_HOURS_ALLOW_TIER2", True, create=True), \
             patch.object(risk_agent.settings, "EXTENDED_HOURS_MAX_SPREAD_PCT", 0.35, create=True), \
             patch.object(risk_agent.settings, "EXTENDED_HOURS_BLOCK_AT_HIGHS", True, create=True), \
             patch.object(risk_agent.settings, "EXTENDED_HOURS_SIZE_MULT", 0.5, create=True):
            brief = await risk_agent.analyze(
                symbol="QQQ",
                price=500.0,
                signals=signals,
                risk_manager=_RiskStub(),
                positions=[],
                direction="SHORT",
            )

        self.assertTrue(brief["can_trade"])
        self.assertIn("extended_hours_tier2_allowed", brief["constraint_flags"])
        self.assertIn("size_reduced_extended_hours", brief["constraint_flags"])
        self.assertNotIn("extended_hours_tier_block", brief["constraint_flags"])

    async def test_extended_hours_blocks_wide_spread_and_at_highs(self):
        signals = {
            "signal_tier": "tier_2",
            "strategy_tag": "momentum_long",
            "entry_quality": "at_highs",
            "extended_hours": True,
            "spread_pct": 0.5,
        }

        with patch.object(risk_agent.settings, "EXTENDED_HOURS_TIER1_ONLY", True, create=True), \
             patch.object(risk_agent.settings, "EXTENDED_HOURS_ALLOW_TIER2", True, create=True), \
             patch.object(risk_agent.settings, "EXTENDED_HOURS_MAX_SPREAD_PCT", 0.35, create=True), \
             patch.object(risk_agent.settings, "EXTENDED_HOURS_BLOCK_AT_HIGHS", True, create=True):
            brief = await risk_agent.analyze(
                symbol="AAPL",
                price=200.0,
                signals=signals,
                risk_manager=_RiskStub(),
                positions=[],
                direction="BUY",
            )

        self.assertFalse(brief["can_trade"])
        self.assertIn("extended_hours_entry_quality_block", brief["constraint_flags"])
