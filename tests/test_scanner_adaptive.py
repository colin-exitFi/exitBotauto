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

    def test_detect_market_regime_uses_index_when_tape_is_mixed(self):
        candidates = [
            {"change_pct": 1.0},
            {"change_pct": -1.0},
            {"change_pct": 0.8},
            {"change_pct": -0.7},
        ]
        index_context = {"SPY": -0.9, "QQQ": -1.1, "DIA": -0.8, "avg_change_pct": -0.93, "count": 3}
        regime = self.scanner._detect_market_regime(candidates, index_context=index_context)
        self.assertEqual(regime, "risk_off")

    def test_detect_market_regime_conflicting_tape_and_index_returns_mixed(self):
        candidates = [
            {"change_pct": 4.0},
            {"change_pct": 5.0},
            {"change_pct": 3.5},
            {"change_pct": -0.6},
        ]
        index_context = {"SPY": -0.8, "QQQ": -1.0, "DIA": -0.7, "avg_change_pct": -0.83, "count": 3}
        regime = self.scanner._detect_market_regime(candidates, index_context=index_context)
        self.assertEqual(regime, "mixed")

    def test_merge_candidate_preserves_fade_short_metadata(self):
        existing = {
            "symbol": "BATL",
            "source": "polygon",
            "price": 24.5,
            "change_pct": 12.0,
            "volume": 5_000_000,
        }
        incoming = {
            "symbol": "BATL",
            "source": "fade",
            "side": "short",
            "fade_signal": "FADE_CONFIRMED",
            "fade_score": 0.82,
            "fade_run_pct": 42.0,
            "run_volume": 8_000_000,
            "run_close": 26.0,
            "price_change_from_run": -5.8,
        }

        self.scanner._merge_candidate(existing, incoming)
        strategy_tag = self.scanner._derive_strategy_tag(existing)

        self.assertEqual(existing["side"], "short")
        self.assertEqual(existing["fade_signal"], "FADE_CONFIRMED")
        self.assertEqual(strategy_tag, "fade_short")
        self.assertIn("fade", existing["source"])

    def test_calculate_score_inverts_sentiment_for_short_candidates(self):
        candidate = {
            "volume_spike": 3.0,
            "change_pct": -4.5,
            "sentiment_score": -0.6,
            "stocktwits_trending_score": 5.0,
            "pharma_score": 0.0,
            "news_headlines": [],
            "side": "short",
            "strategy_tag": "fade_short",
            "signal_sources": ["fade"],
            "fade_signal": "FADE_CONFIRMED",
            "fade_score": 0.85,
        }
        neutral_perf = {"by_strategy": {}, "by_source": {}}

        short_score = self.scanner._calculate_score(candidate, regime="risk_off", performance=neutral_perf)
        candidate["side"] = "long"
        long_score = self.scanner._calculate_score(candidate, regime="risk_off", performance=neutral_perf)

        self.assertGreater(short_score, long_score)

    def test_merge_sources_keeps_alpaca_and_polygon_movers(self):
        existing = {"symbol": "AAPL", "source": "alpaca_movers"}
        incoming = {"symbol": "AAPL", "source": "polygon"}

        self.scanner._merge_candidate(existing, incoming)

        self.assertEqual(existing["source"], "alpaca_movers+polygon")


class ScannerUnusualWhalesEnrichmentTests(unittest.IsolatedAsyncioTestCase):
    async def test_apply_unusual_whales_enrichment_adds_gamma_levels(self):
        class _UW:
            def is_configured(self):
                return True

            def get_flow_alerts(self, *args, **kwargs):
                return []

            def get_dark_pool(self, *args, **kwargs):
                return []

            def get_gamma_exposure(self, symbol):
                return {
                    "ticker": symbol,
                    "levels": [{"strike": 120.0, "gamma": 5000.0}],
                    "max_gamma_strike": 120.0,
                    "support_strikes": [120.0],
                    "resistance_strikes": [],
                }

            def summarize_recent_flow_for_symbol(self, symbol, min_premium=100_000):
                return {"has_flow": False, "bias": "neutral", "summary": "No recent ticker flow"}

            def summarize_net_premium_ticks(self, symbol, date=None):
                return {"bias": "neutral", "summary": "No net premium ticks"}

            def summarize_options_volume(self, symbol, limit=1):
                return {"bias": "neutral", "summary": "No options volume snapshot"}

            def summarize_interpolated_iv(self, symbol, target_days=30, date=None):
                return {"iv_context": "normal", "summary": "30D IV percentile 0.50 (normal)", "percentile": 0.5}

        scanner = Scanner(unusual_whales_client=_UW())
        candidates = [{"symbol": "NVDA", "side": "long"}]

        await scanner._apply_unusual_whales_enrichment(candidates)

        self.assertEqual(candidates[0]["gamma_support"], [120.0])
        self.assertEqual(candidates[0]["gamma_max_strike"], 120.0)

    async def test_apply_unusual_whales_enrichment_prefers_fresh_stream_snapshot(self):
        class _UW:
            def is_configured(self):
                return True

            def get_flow_alerts(self, *args, **kwargs):
                raise AssertionError("REST flow fallback should not be used when stream is fresh")

            def get_dark_pool(self, *args, **kwargs):
                raise AssertionError("REST dark pool fallback should not be used when stream is fresh")

            def get_gamma_exposure(self, symbol):
                return {
                    "ticker": symbol,
                    "levels": [],
                    "max_gamma_strike": 0,
                    "support_strikes": [],
                    "resistance_strikes": [],
                }

            def summarize_recent_flow_for_symbol(self, symbol, min_premium=100_000):
                return {"has_flow": False, "bias": "neutral", "summary": "No recent ticker flow"}

            def summarize_net_premium_ticks(self, symbol, date=None):
                return {"bias": "neutral", "summary": "No net premium ticks"}

            def summarize_options_volume(self, symbol, limit=1):
                return {"bias": "neutral", "summary": "No options volume snapshot"}

            def summarize_interpolated_iv(self, symbol, target_days=30, date=None):
                return {"iv_context": "normal", "summary": "30D IV percentile 0.50 (normal)", "percentile": 0.5}

        class _UWStream:
            def is_fresh(self):
                return True

            def get_snapshot(self):
                return {
                    "flow_alerts": [
                        {"ticker": "NVDA", "sentiment": "bullish", "premium": 600_000},
                    ],
                    "dark_pool": [
                        {"ticker": "NVDA", "sentiment": "bullish", "premium": 900_000},
                    ],
                    "market_tide": {"bias": "risk_on"},
                }

        scanner = Scanner(unusual_whales_client=_UW(), unusual_whales_stream=_UWStream())
        candidates = [{"symbol": "NVDA", "side": "long"}]

        await scanner._apply_unusual_whales_enrichment(candidates)

        self.assertEqual(candidates[0]["uw_flow_sentiment"], "bullish")
        self.assertEqual(candidates[0]["uw_dark_pool_bias"], "bullish")
        self.assertGreater(candidates[0]["uw_score_adjustment"], 0.0)

    def test_get_unusual_whales_snapshot_returns_stream_market_tide_when_fresh(self):
        class _UWStream:
            def is_fresh(self):
                return True

            def get_snapshot(self):
                return {"market_tide": {"bias": "risk_off", "put_call_ratio": 1.8}}

        scanner = Scanner(unusual_whales_stream=_UWStream())
        snapshot = scanner._get_unusual_whales_snapshot()

        self.assertEqual(snapshot["market_tide"]["bias"], "risk_off")

    async def test_apply_unusual_whales_enrichment_adds_ticker_level_context(self):
        class _UW:
            def is_configured(self):
                return True

            def get_flow_alerts(self, *args, **kwargs):
                return []

            def get_dark_pool(self, *args, **kwargs):
                return []

            def get_gamma_exposure(self, symbol):
                return {"ticker": symbol, "levels": [], "max_gamma_strike": 0, "support_strikes": [], "resistance_strikes": []}

            def summarize_recent_flow_for_symbol(self, symbol, min_premium=100_000):
                return {"has_flow": True, "bias": "bullish", "summary": "3 recent flows; bullish $900,000; bearish $0; bias bullish"}

            def summarize_net_premium_ticks(self, symbol, date=None):
                return {"bias": "bullish", "summary": "net premium bias bullish; call $250,000; put $-50,000; delta 15,000"}

            def summarize_options_volume(self, symbol, limit=1):
                return {"bias": "bullish", "summary": "options volume bias bullish; call/put vol 1.80; call prem $500,000; put prem $120,000"}

            def summarize_interpolated_iv(self, symbol, target_days=30, date=None):
                return {"iv_context": "elevated", "summary": "30D IV percentile 0.91 (elevated)", "percentile": 0.91}

        scanner = Scanner(unusual_whales_client=_UW())
        candidates = [{"symbol": "NVDA", "side": "long"}]

        await scanner._apply_unusual_whales_enrichment(candidates)

        self.assertEqual(candidates[0]["uw_recent_flow_bias"], "bullish")
        self.assertEqual(candidates[0]["uw_net_premium_bias"], "bullish")
        self.assertEqual(candidates[0]["uw_options_volume_bias"], "bullish")
        self.assertEqual(candidates[0]["uw_iv_context"], "elevated")
        self.assertGreater(candidates[0]["uw_score_adjustment"], 0.0)
