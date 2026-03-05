import unittest

from src.scanner.scanner import Scanner


class ScannerAdaptiveScoringTests(unittest.TestCase):
    def setUp(self):
        self.scanner = Scanner(
            polygon_client=None,
            sentiment_analyzer=None,
            stocktwits_client=None,
            alpaca_client=None,
            pharma_scanner=None,
            fade_scanner=None,
            grok_x_trending=None,
        )

    def test_detect_market_regime_risk_on(self):
        candidates = [
            {"change_pct": 5.0},
            {"change_pct": 4.2},
            {"change_pct": 3.1},
            {"change_pct": 4.8},
            {"change_pct": -0.2},
        ]
        regime = self.scanner._detect_market_regime(candidates)
        self.assertEqual(regime, "risk_on")

    def test_calculate_score_higher_in_risk_on_for_momentum_setup(self):
        candidate = {
            "volume_spike": 4.5,
            "change_pct": 6.0,
            "sentiment_score": 0.2,
            "stocktwits_trending_score": 20.0,
            "pharma_score": 0.0,
            "news_headlines": ["headline"],
            "side": "long",
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
        }
        neutral_perf = {"by_strategy": {}, "by_source": {}}

        risk_on_score = self.scanner._calculate_score(candidate, regime="risk_on", performance=neutral_perf)
        choppy_score = self.scanner._calculate_score(candidate, regime="choppy", performance=neutral_perf)
        self.assertGreater(risk_on_score, choppy_score)

    def test_performance_multiplier_rewards_high_sample_high_winrate(self):
        candidate = {
            "strategy_tag": "momentum_long",
            "signal_sources": ["polygon"],
        }
        performance = {
            "by_strategy": {
                "momentum_long": {"trades": 30, "win_rate": 0.64, "score": 0.72},
            },
            "by_source": {
                "polygon": {"trades": 25, "win_rate": 0.61, "score": 0.68},
            },
        }
        mult = self.scanner._performance_multiplier(candidate, performance)
        self.assertGreater(mult, 1.0)
        self.assertLessEqual(mult, 1.25)

