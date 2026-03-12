import unittest
from unittest.mock import patch

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
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["reason"], "regime_block")

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
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["reason"], "thesis_not_actionable")

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
        self.assertEqual(gate["reason"], "ok")

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
        self.assertFalse(gate["allowed"])
        self.assertEqual(gate["reason"], "uw_unconfirmed")

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


if __name__ == "__main__":
    unittest.main()
