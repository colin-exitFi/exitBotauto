import unittest

from src.signals.mode_classifier import ModeClassification, ModeFeatures
from src.signals.play_resolver import resolve_play


def _features(**overrides):
    payload = {
        "symbol": "AAPL",
        "price": 100.0,
        "daily_pct": 12.0,
        "range_pct": 92.0,
        "spread_pct": 0.2,
        "volume_rel": 2.0,
        "volume_accel": 0.4,
        "halt_count": 0,
        "sentiment_pct": 82.0,
        "rsi_5m": 71.0,
        "rsi_divergence": -0.2,
        "macd_hist_slope": -0.1,
        "vwap_distance_pct": 0.3,
        "reclaiming_vwap": True,
        "losing_vwap": False,
        "uw_bias": "bullish",
        "uw_premium": 100000.0,
        "catalyst_tag": None,
        "catalyst_age_hours": None,
        "session": "regular",
        "entry_quality": "pullback",
        "holding_horizon": "intraday",
        "sector": "TECH",
        "market_regime": "mixed",
        "created_at": 1.0,
        "last_refreshed_at": 1.0,
        "data_age_seconds": 5.0,
        "feature_quality_score": 0.9,
        "feature_quality": "high",
        "missing_fields": [],
        "minute_notional_liquidity": 100000.0,
        "bar_context": {},
        "anomaly_flags": [],
    }
    payload.update(overrides)
    return ModeFeatures(**payload)


class PlayResolverStateTests(unittest.TestCase):
    def test_low_feature_quality_preserves_play_but_maps_to_data_insufficient(self):
        features = _features(feature_quality_score=0.1, feature_quality="low", missing_fields=["price"])
        classification = ModeClassification(
            mode="general_momentum_long",
            direction_constraint="long_only",
            classifier_confidence=0.9,
            reason_codes=["low_feature_quality", "missing_price"],
        )

        resolution = resolve_play(features, classification, now_ts=10.0)

        self.assertEqual(resolution.timing_state, "data_insufficient")
        self.assertEqual(resolution.best_play, "general_momentum_long")
        self.assertEqual(resolution.no_trade_reason, "low_feature_quality")

    def test_exhaustion_fade_not_confirmed_waits_for_trigger(self):
        features = _features(
            daily_pct=28.0,
            reclaiming_vwap=True,
            losing_vwap=False,
            volume_accel=0.4,
        )
        classification = ModeClassification(
            mode="exhaustion_fade_short",
            direction_constraint="short_only",
            classifier_confidence=0.84,
            reason_codes=["daily_extension_extreme"],
        )

        resolution = resolve_play(features, classification, now_ts=10.0)

        self.assertEqual(resolution.timing_state, "wait_for_trigger")
        self.assertEqual(resolution.no_trade_reason, "fade_not_confirmed_yet")
        self.assertIsNotNone(resolution.trigger_spec)

    def test_flat_no_directional_edge_keeps_play_family_but_blocks_entry(self):
        features = _features(daily_pct=0.05, volume_accel=-0.1, sentiment_pct=47.0)
        classification = ModeClassification(
            mode="general_momentum_short",
            direction_constraint="short_only",
            classifier_confidence=0.2,
            reason_codes=["flat_no_directional_edge"],
        )

        resolution = resolve_play(features, classification, now_ts=10.0)

        self.assertEqual(resolution.timing_state, "mode_conflict")
        self.assertEqual(resolution.best_play, "general_momentum_short")
        self.assertEqual(resolution.no_trade_reason, "flat_no_directional_edge")
