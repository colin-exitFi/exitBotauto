import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.entry.entry_manager import EntryManager


class EntrySizeFloorTests(unittest.TestCase):
    def test_high_conf_notional_floor_lifts_small_entry(self):
        manager = EntryManager.__new__(EntryManager)
        manager.risk = SimpleNamespace(equity=27000.0, _equity=27000.0)
        sentiment = {
            "consensus_confidence": 82.0,
            "provider_used": "council",
            "entry_quality": "neutral",
        }

        with patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL_ENABLED", True), \
             patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL", 325.0), \
             patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL_MIN_CONFIDENCE", 75.0), \
             patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL_MAX_PCT", 1.5):
            lifted = manager._apply_high_confidence_min_notional(
                "SOFI",
                192.0,
                sentiment,
                "short",
            )

        self.assertEqual(lifted, 325.0)

    def test_low_conf_notional_floor_does_not_lift_entry(self):
        manager = EntryManager.__new__(EntryManager)
        manager.risk = SimpleNamespace(equity=27000.0, _equity=27000.0)
        sentiment = {
            "consensus_confidence": 58.0,
            "provider_used": "council",
            "entry_quality": "neutral",
        }

        with patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL_ENABLED", True), \
             patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL", 325.0), \
             patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL_MIN_CONFIDENCE", 75.0), \
             patch("src.entry.entry_manager.settings.HIGH_CONFIDENCE_MIN_NOTIONAL_MAX_PCT", 1.5):
            lifted = manager._apply_high_confidence_min_notional(
                "SOFI",
                192.0,
                sentiment,
                "short",
            )

        self.assertEqual(lifted, 192.0)

    def test_high_conf_short_can_lift_to_one_share(self):
        manager = EntryManager.__new__(EntryManager)
        manager.risk = SimpleNamespace(equity=27000.0, _equity=27000.0)
        sentiment = {
            "consensus_confidence": 83.0,
            "provider_used": "classifier_auto",
            "entry_quality": "neutral",
        }

        with patch("src.entry.entry_manager.settings.WHOLE_SHARE_FLOOR_ENABLED", True), \
             patch("src.entry.entry_manager.settings.WHOLE_SHARE_FLOOR_MIN_CONFIDENCE", 75.0), \
             patch("src.entry.entry_manager.settings.WHOLE_SHARE_FLOOR_MAX_NOTIONAL_PCT", 3.0):
            lifted = manager._apply_whole_share_floor_notional(
                "VOO",
                598.78,
                459.00,
                sentiment,
                "short",
            )

        self.assertEqual(lifted, 598.78)

    def test_low_conf_short_keeps_original_notional(self):
        manager = EntryManager.__new__(EntryManager)
        manager.risk = SimpleNamespace(equity=27000.0, _equity=27000.0)
        sentiment = {
            "consensus_confidence": 58.0,
            "provider_used": "council",
            "entry_quality": "neutral",
        }

        with patch("src.entry.entry_manager.settings.WHOLE_SHARE_FLOOR_ENABLED", True), \
             patch("src.entry.entry_manager.settings.WHOLE_SHARE_FLOOR_MIN_CONFIDENCE", 75.0), \
             patch("src.entry.entry_manager.settings.WHOLE_SHARE_FLOOR_MAX_NOTIONAL_PCT", 3.0):
            lifted = manager._apply_whole_share_floor_notional(
                "VOO",
                598.78,
                459.00,
                sentiment,
                "short",
            )

        self.assertEqual(lifted, 459.00)


if __name__ == "__main__":
    unittest.main()
