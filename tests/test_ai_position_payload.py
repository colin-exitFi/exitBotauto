import unittest

from src.ai.position_payload import sanitize_candidates_for_ai, sanitize_positions_for_ai


class AIPayloadSanitizerTests(unittest.TestCase):
    def test_sanitize_positions_strips_stale_bar_payloads_and_relabels_entry_metadata(self):
        rows = sanitize_positions_for_ai(
            [
                {
                    "symbol": "CVX",
                    "side": "long",
                    "entry_price": 214.04,
                    "current_price": 214.38,
                    "strategy_tag": "uw_flow_long",
                    "data_age_seconds": 215.46,
                    "feature_quality_score": 0.66,
                    "feature_quality": "medium",
                    "entry_path": "broker_sync_missing_local",
                    "anomaly_flags": ["carryover_sync", "broker_reloaded_after_local_removal"],
                    "bar_context": {"bars_1m": [{"timestamp": 123}]},
                    "mode_features": {"data_age_seconds": 215.46},
                }
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "CVX")
        self.assertEqual(rows[0]["entry_signal_age_seconds"], 215.46)
        self.assertEqual(rows[0]["entry_feature_quality"], "medium")
        self.assertEqual(rows[0]["position_origin"], "broker_restored_live")
        self.assertNotIn("bar_context", rows[0])
        self.assertNotIn("mode_features", rows[0])
        self.assertNotIn("entry_path", rows[0])
        self.assertNotIn("anomaly_flags", rows[0])

    def test_sanitize_candidates_keeps_live_summary_and_drops_bar_context(self):
        rows = sanitize_candidates_for_ai(
            [
                {
                    "symbol": "SGML",
                    "price": 13.86,
                    "setup_mode": "exhaustion_fade_short",
                    "timing_state": "wait_for_trigger",
                    "feature_quality": "medium",
                    "feature_quality_score": 0.76,
                    "data_age_seconds": 67.77,
                    "bar_context": {"bars_1m": [{"timestamp": 123}]},
                }
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "SGML")
        self.assertEqual(rows[0]["signal_age_seconds"], 67.77)
        self.assertEqual(rows[0]["feature_quality"], "medium")
        self.assertNotIn("bar_context", rows[0])

    def test_sanitize_positions_marks_broker_confirmed_positions_with_context_as_tracked(self):
        rows = sanitize_positions_for_ai(
            [
                {
                    "symbol": "AMZN",
                    "side": "long",
                    "entry_price": 201.55,
                    "current_price": 203.72,
                    "strategy_tag": "momentum_long",
                    "setup_mode": "continuation_long",
                    "best_play": "continuation_long",
                    "direction_constraint": "long_only",
                    "hold_style": "intraday",
                    "entry_time_source": "broker_orders",
                    "hard_stop_price": 195.5,
                    "entry_reason_code": "broker_sync",
                    "signal_sources": ["broker_sync"],
                    "broker_synced_at": 123.0,
                    "entry_path": "broker_sync_missing_local",
                    "anomaly_flags": ["carryover_sync"],
                }
            ]
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["position_origin"], "tracked_live_position")
        self.assertEqual(rows[0]["entry_reason_code"], "continuation_long")
        self.assertNotIn("signal_sources", rows[0])
        self.assertNotIn("broker_synced_at", rows[0])
        self.assertNotIn("entry_path", rows[0])
        self.assertNotIn("anomaly_flags", rows[0])


if __name__ == "__main__":
    unittest.main()
