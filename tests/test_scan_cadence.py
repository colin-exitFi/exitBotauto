import unittest
from unittest.mock import patch

from src.main import TradingBot


class AdaptiveScanCadenceTests(unittest.TestCase):
    def test_risk_on_uses_fast_interval(self):
        with patch("src.main.settings.SCAN_INTERVAL_FAST_SECONDS", 60), \
             patch("src.main.settings.SCAN_INTERVAL_SLOW_SECONDS", 300), \
             patch("src.main.settings.SCAN_INTERVAL_SECONDS", 180):
            self.assertEqual(TradingBot._determine_scan_interval("risk_on"), 60)
            self.assertEqual(TradingBot._determine_scan_interval("risk_off"), 60)

    def test_choppy_uses_slow_interval(self):
        with patch("src.main.settings.SCAN_INTERVAL_FAST_SECONDS", 60), \
             patch("src.main.settings.SCAN_INTERVAL_SLOW_SECONDS", 300), \
             patch("src.main.settings.SCAN_INTERVAL_SECONDS", 180):
            self.assertEqual(TradingBot._determine_scan_interval("choppy"), 300)

    def test_mixed_uses_baseline_interval(self):
        with patch("src.main.settings.SCAN_INTERVAL_FAST_SECONDS", 60), \
             patch("src.main.settings.SCAN_INTERVAL_SLOW_SECONDS", 300), \
             patch("src.main.settings.SCAN_INTERVAL_SECONDS", 180):
            self.assertEqual(TradingBot._determine_scan_interval("mixed"), 180)

    def test_regime_hysteresis_requires_confirmation(self):
        bot = TradingBot.__new__(TradingBot)
        bot.scan_regime = "mixed"
        bot._scan_regime_history = []

        with patch("src.main.settings.SCAN_REGIME_HYSTERESIS_WINDOW", 3), \
             patch("src.main.settings.SCAN_REGIME_MIN_CONFIRMATIONS", 2):
            # First risk_on tick should not immediately flip.
            self.assertEqual(bot._smooth_scan_regime("risk_on"), "mixed")
            # Second confirmation flips regime.
            self.assertEqual(bot._smooth_scan_regime("risk_on"), "risk_on")

    def test_regime_hysteresis_prevents_single_tick_reversal(self):
        bot = TradingBot.__new__(TradingBot)
        bot.scan_regime = "risk_on"
        bot._scan_regime_history = ["risk_on", "risk_on"]

        with patch("src.main.settings.SCAN_REGIME_HYSTERESIS_WINDOW", 3), \
             patch("src.main.settings.SCAN_REGIME_MIN_CONFIRMATIONS", 2):
            self.assertEqual(bot._smooth_scan_regime("mixed"), "risk_on")
