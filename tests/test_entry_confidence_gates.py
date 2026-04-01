import unittest
from types import SimpleNamespace
from unittest.mock import patch

import src.main as main_module


class EntryConfidenceGateTests(unittest.TestCase):
    def test_classifier_auto_keeps_base_floor(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        candidate = {"signal_tier": "tier_2", "entry_quality": "neutral"}
        verdict = SimpleNamespace(provider_used="classifier_auto", decision="BUY")

        with patch.object(main_module.settings, "MIN_JURY_CONFIDENCE", 35), \
             patch.object(main_module.settings, "DISCRETIONARY_MIN_JURY_CONFIDENCE", 50), \
             patch.object(main_module.settings, "SHORT_MIN_JURY_CONFIDENCE", 55), \
             patch.object(main_module.settings, "NEUTRAL_ENTRY_MIN_JURY_CONFIDENCE", 50):
            floor, reasons = bot._effective_entry_confidence_floor(candidate, verdict)

        self.assertEqual(floor, 35.0)
        self.assertEqual(reasons, ["base=35"])

    def test_discretionary_short_uses_higher_short_floor(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        candidate = {"signal_tier": "tier_2", "entry_quality": "neutral"}
        verdict = SimpleNamespace(provider_used="council", decision="SHORT")

        with patch.object(main_module.settings, "MIN_JURY_CONFIDENCE", 35), \
             patch.object(main_module.settings, "DISCRETIONARY_MIN_JURY_CONFIDENCE", 50), \
             patch.object(main_module.settings, "SHORT_MIN_JURY_CONFIDENCE", 55), \
             patch.object(main_module.settings, "NEUTRAL_ENTRY_MIN_JURY_CONFIDENCE", 50):
            floor, reasons = bot._effective_entry_confidence_floor(candidate, verdict)

        self.assertEqual(floor, 55.0)
        self.assertIn("discretionary=50", reasons)
        self.assertIn("short=55", reasons)

    def test_discretionary_long_neutral_uses_discretionary_floor(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        candidate = {"signal_tier": "tier_2", "entry_quality": "neutral"}
        verdict = SimpleNamespace(provider_used="council", decision="BUY")

        with patch.object(main_module.settings, "MIN_JURY_CONFIDENCE", 35), \
             patch.object(main_module.settings, "DISCRETIONARY_MIN_JURY_CONFIDENCE", 50), \
             patch.object(main_module.settings, "SHORT_MIN_JURY_CONFIDENCE", 55), \
             patch.object(main_module.settings, "NEUTRAL_ENTRY_MIN_JURY_CONFIDENCE", 50):
            floor, reasons = bot._effective_entry_confidence_floor(candidate, verdict)

        self.assertEqual(floor, 50.0)
        self.assertIn("discretionary=50", reasons)

    def test_classifier_auto_rejects_adversary_veto_and_neutral_entry(self):
        class _EntryManager:
            def is_market_open(self):
                return True

            def is_extended_hours(self):
                return False

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = _EntryManager()
        bot._mode_classifier_enforced = lambda: True
        bot._shorting_ready = lambda: True

        verdict = SimpleNamespace(
            consensus_detail={"agreement": "adversary_veto"},
            briefs={"risk": {"approved": True}},
        )
        candidate = {
            "direction_constraint": "short_only",
            "setup_mode": "continuation_short",
            "timing_state": "enter_now",
            "entry_quality": "neutral",
            "classifier_confidence": 0.8,
            "resolver_confidence": 0.7,
            "spread_pct": 0.2,
        }

        with patch.object(main_module.settings, "MODE_CLASSIFIER_AUTO_ENTER", True):
            allowed, reason, decision = bot._allow_classifier_auto_enter(candidate, verdict)

        self.assertFalse(allowed)
        self.assertEqual(reason, "adversary_veto")
        self.assertEqual(decision, "")

    def test_classifier_auto_requires_clean_entry_quality(self):
        class _EntryManager:
            def is_market_open(self):
                return True

            def is_extended_hours(self):
                return False

        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.entry_manager = _EntryManager()
        bot._mode_classifier_enforced = lambda: True
        bot._shorting_ready = lambda: True

        verdict = SimpleNamespace(
            consensus_detail={"agreement": "council_approved"},
            briefs={"risk": {"approved": True}},
        )
        candidate = {
            "direction_constraint": "long_only",
            "setup_mode": "continuation_long",
            "timing_state": "enter_now",
            "entry_quality": "neutral",
            "classifier_confidence": 0.8,
            "resolver_confidence": 0.7,
            "spread_pct": 0.2,
        }

        with patch.object(main_module.settings, "MODE_CLASSIFIER_AUTO_ENTER", True):
            allowed, reason, decision = bot._allow_classifier_auto_enter(candidate, verdict)

        self.assertFalse(allowed)
        self.assertEqual(reason, "entry_quality_not_clean")
        self.assertEqual(decision, "BUY")


if __name__ == "__main__":
    unittest.main()
