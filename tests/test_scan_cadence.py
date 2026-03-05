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

