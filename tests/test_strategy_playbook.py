import unittest
from unittest.mock import patch

import src.main as main_module
from src.data.signal_attribution import derive_strategy_tag
from src.data.strategy_playbook import annotate_candidate
from src.main import TradingBot


class StrategyTaggingTests(unittest.TestCase):
    def test_copy_trader_candidate_gets_copy_trader_tag(self):
        candidate = {
            "source": "polygon+copy_trader",
            "copy_trader_context": "3 respected traders aligned",
        }
        self.assertEqual(derive_strategy_tag(candidate, "BUY"), "copy_trader_long")

    def test_watchlist_short_candidate_gets_watchlist_tag(self):
        candidate = {
            "source": "watchlist",
            "watchlist_reason": "overnight thesis",
        }
        self.assertEqual(derive_strategy_tag(candidate, "SHORT"), "watchlist_short")

    def test_uw_candidate_gets_uw_flow_tag(self):
        candidate = {
            "source": "polygon+unusual_options",
            "uw_chain_summary": "bullish chain",
        }
        self.assertEqual(derive_strategy_tag(candidate, "BUY"), "uw_flow_long")


class StrategyPlaybookTests(unittest.TestCase):
    def test_social_momentum_stays_share_only(self):
        candidate = annotate_candidate({"strategy_tag": "social_momentum_long"})
        self.assertTrue(candidate["playbook_live"])
        self.assertEqual(candidate["playbook_options_mode"], "off")

    def test_uw_flow_prefers_options(self):
        candidate = annotate_candidate({"strategy_tag": "uw_flow_long"})
        self.assertEqual(candidate["playbook_options_mode"], "prefer")

    def test_trade_gate_blocks_bad_regime(self):
        bot = TradingBot.__new__(TradingBot)
        bot.scan_regime = "risk_off"
        bot.scan_regime_raw = "risk_off"
        bot._tomorrow_thesis_cache = {}
        bot._tomorrow_thesis_cache_at = 0.0

        gate = bot._evaluate_trade_gate(
            {
                "symbol": "AAPL",
                "strategy_tag": "momentum_long",
                "market_regime": "risk_off",
                "signal_timestamp": 0,
            },
            "BUY",
        )
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["reason"], "v2_passthrough")

    def test_trade_gate_requires_actionable_thesis_for_planned_playbooks(self):
        bot = TradingBot.__new__(TradingBot)
        bot.scan_regime = "risk_on"
        bot.scan_regime_raw = "risk_on"
        bot._tomorrow_thesis_cache = {}
        bot._tomorrow_thesis_cache_at = 0.0
        bot._load_tomorrow_thesis = lambda: {"market_bias": "unknown", "watchlist": []}

        with patch("time.time", return_value=600.0):
            gate = bot._evaluate_trade_gate(
                {
                    "symbol": "AAPL",
                    "strategy_tag": "momentum_long",
                    "market_regime": "risk_on",
                    "signal_timestamp": 0,
                },
                "BUY",
            )
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["reason"], "v2_passthrough")

    def test_trade_gate_allows_watchlist_name_in_actionable_plan(self):
        bot = TradingBot.__new__(TradingBot)
        bot.scan_regime = "risk_on"
        bot.scan_regime_raw = "risk_on"
        bot._tomorrow_thesis_cache = {}
        bot._tomorrow_thesis_cache_at = 0.0
        bot._load_tomorrow_thesis = lambda: {
            "market_bias": "bullish",
            "watchlist": [{"symbol": "NVDA"}],
        }

        gate = bot._evaluate_trade_gate(
            {
                "symbol": "NVDA",
                "strategy_tag": "watchlist_long",
                "market_regime": "risk_on",
                "signal_timestamp": 0,
            },
            "BUY",
        )
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["reason"], "v2_passthrough")

    def test_trade_gate_requires_uw_confirmation_for_uw_playbook(self):
        bot = TradingBot.__new__(TradingBot)
        bot.scan_regime = "risk_on"
        bot.scan_regime_raw = "risk_on"
        bot._tomorrow_thesis_cache = {}
        bot._tomorrow_thesis_cache_at = 0.0

        gate = bot._evaluate_trade_gate(
            {
                "symbol": "NVDA",
                "strategy_tag": "uw_flow_long",
                "market_regime": "risk_on",
                "signal_timestamp": 0,
            },
            "BUY",
        )
        self.assertTrue(gate["allowed"])
        self.assertEqual(gate["reason"], "v2_passthrough")

    def test_options_allocation_only_for_confirmed_uw_flow(self):
        bot = TradingBot.__new__(TradingBot)

        confirmed = bot._determine_options_allocation_pct(
            {
                "symbol": "NVDA",
                "strategy_tag": "uw_flow_long",
                "uw_flow_sentiment": "bullish",
                "uw_chain_bias": "bullish",
            },
            "BUY",
            88,
        )
        plain = bot._determine_options_allocation_pct(
            {
                "symbol": "AAPL",
                "strategy_tag": "momentum_long",
            },
            "BUY",
            92,
        )
        self.assertGreater(confirmed, 0.0)
        self.assertEqual(plain, 0.0)

    def test_options_pilot_allows_high_confidence_momentum_on_whitelisted_symbol(self):
        bot = TradingBot.__new__(TradingBot)

        with patch.object(main_module.OptionsMonitor, "is_regular_market_hours", return_value=True), \
             patch.object(main_module.settings, "OPTIONS_PILOT_ENABLED", True), \
             patch.object(main_module.settings, "OPTIONS_PILOT_SYMBOLS", {"QQQ"}), \
             patch.object(main_module.settings, "OPTIONS_PILOT_STRATEGY_TAGS", {"MOMENTUM_LONG"}), \
             patch.object(main_module.settings, "OPTIONS_PILOT_MIN_CONFIDENCE", 90.0), \
             patch.object(main_module.settings, "OPTIONS_PILOT_ALLOCATION_PCT", 35.0):
            pct = bot._determine_options_allocation_pct(
                {
                    "symbol": "QQQ",
                    "strategy_tag": "momentum_long",
                    "price": 500.0,
                    "market_regime": "risk_on",
                },
                "BUY",
                93,
            )

        self.assertGreater(pct, 0.0)

    def test_options_overlay_reports_pilot_symbol_not_whitelisted(self):
        bot = TradingBot.__new__(TradingBot)

        with patch.object(main_module.OptionsMonitor, "is_regular_market_hours", return_value=True), \
             patch.object(main_module.settings, "OPTIONS_PILOT_ENABLED", True), \
             patch.object(main_module.settings, "OPTIONS_PILOT_SYMBOLS", {"QQQ"}), \
             patch.object(main_module.settings, "OPTIONS_PILOT_STRATEGY_TAGS", {"MOMENTUM_LONG"}), \
             patch.object(main_module.settings, "OPTIONS_PILOT_MIN_CONFIDENCE", 90.0), \
             patch.object(main_module.settings, "OPTIONS_PILOT_ALLOCATION_PCT", 35.0):
            overlay = bot._evaluate_options_overlay(
                {
                    "symbol": "AAPL",
                    "strategy_tag": "momentum_long",
                    "price": 210.0,
                    "market_regime": "risk_on",
                },
                "BUY",
                95,
            )

        self.assertEqual(overlay["allocation_pct"], 0.0)
        self.assertEqual(overlay["reason"], "pilot_symbol_not_whitelisted")
        self.assertEqual(overlay["mode"], "pilot")

    def test_options_overlay_reports_uw_prefer_trace_reason(self):
        bot = TradingBot.__new__(TradingBot)

        with patch.object(main_module.OptionsMonitor, "is_regular_market_hours", return_value=True):
            overlay = bot._evaluate_options_overlay(
                {
                    "symbol": "NVDA",
                    "strategy_tag": "uw_flow_long",
                    "uw_flow_sentiment": "bullish",
                    "uw_chain_bias": "bullish",
                    "price": 900.0,
                },
                "BUY",
                88,
            )

        self.assertGreater(overlay["allocation_pct"], 0.0)
        self.assertEqual(overlay["reason"], "uw_prefer_high_confidence")
        self.assertEqual(overlay["mode"], "playbook_prefer")

    def test_options_pilot_blocks_extended_hours_even_for_whitelisted_setup(self):
        bot = TradingBot.__new__(TradingBot)

        with patch.object(main_module.OptionsMonitor, "is_regular_market_hours", return_value=False), \
             patch.object(main_module.settings, "OPTIONS_PILOT_ENABLED", True), \
             patch.object(main_module.settings, "OPTIONS_PILOT_SYMBOLS", {"QQQ"}), \
             patch.object(main_module.settings, "OPTIONS_PILOT_STRATEGY_TAGS", {"MOMENTUM_LONG"}), \
             patch.object(main_module.settings, "OPTIONS_PILOT_MIN_CONFIDENCE", 90.0), \
             patch.object(main_module.settings, "OPTIONS_PILOT_ALLOCATION_PCT", 35.0):
            pct = bot._determine_options_allocation_pct(
                {
                    "symbol": "QQQ",
                    "strategy_tag": "momentum_long",
                    "price": 500.0,
                    "market_regime": "risk_on",
                    "extended_hours": True,
                },
                "BUY",
                95,
            )

        self.assertEqual(pct, 0.0)


if __name__ == "__main__":
    unittest.main()
