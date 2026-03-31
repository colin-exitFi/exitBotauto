import tempfile
import unittest
from pathlib import Path

from src.data import setup_snapshots
from src.data.trade_schema import normalize_position_context, normalize_trade_record


class TradeSchemaInferenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.snapshots_file = Path(self._tmp_dir.name) / "setup_snapshots.json"
        self.snapshots_file.write_text("[]")
        self._setup_snapshots_file = setup_snapshots.SETUP_SNAPSHOTS_FILE
        setup_snapshots.SETUP_SNAPSHOTS_FILE = self.snapshots_file

    def tearDown(self):
        setup_snapshots.SETUP_SNAPSHOTS_FILE = self._setup_snapshots_file
        self._tmp_dir.cleanup()

    def test_normalize_trade_record_infers_short_continuation_from_cover(self):
        row = normalize_trade_record(
            {
                "symbol": "AMZN",
                "side": "buy_to_cover",
                "strategy_tag": "unknown",
                "entry_path": "unknown",
                "entry_reason_code": "unknown",
                "setup_mode": "invalid",
                "best_play": "",
                "timing_state": "no_edge",
                "signal_sources": ["unknown"],
                "holding_horizon": "intraday",
                "entry_quality": "neutral",
                "quantity": 5,
                "entry_time": 1711461600,
                "exit_time": 1711462500,
                "pnl": 42.0,
            }
        )

        self.assertEqual(row["strategy_tag"], "momentum_short")
        self.assertEqual(row["setup_mode"], "continuation_short")
        self.assertEqual(row["best_play"], "continuation_short")
        self.assertEqual(row["direction_constraint"], "short_only")
        self.assertEqual(row["timing_state"], "enter_now")
        self.assertEqual(row["entry_path"], "derived_play")

    def test_normalize_trade_record_infers_swing_catalyst_from_congress(self):
        row = normalize_trade_record(
            {
                "symbol": "RKLB",
                "side": "buy",
                "strategy_tag": "unknown",
                "entry_path": "unknown",
                "entry_reason_code": "unknown",
                "setup_mode": "invalid",
                "best_play": "",
                "timing_state": "no_edge",
                "signal_sources": ["congress"],
                "holding_horizon": "swing",
                "entry_quality": "neutral",
                "quantity": 10,
                "entry_time": 1711461600,
                "exit_time": 1711548000,
                "pnl": 85.0,
            }
        )

        self.assertEqual(row["strategy_tag"], "congress_follow")
        self.assertEqual(row["setup_mode"], "swing_catalyst_long")
        self.assertEqual(row["best_play"], "swing_catalyst_long")
        self.assertEqual(row["direction_constraint"], "long_only")
        self.assertEqual(row["timing_state"], "enter_now")

    def test_record_setup_snapshot_normalizes_unknown_placeholders(self):
        setup_snapshots.record_setup_snapshot(
            {
                "symbol": "ARKK",
                "side": "short",
                "strategy_tag": "unknown",
                "signal_sources": ["unusual_whales"],
                "setup_mode": "invalid",
                "best_play": "",
                "timing_state": "no_edge",
                "symbol_state": "pending_trigger",
                "holding_horizon": "intraday",
                "entry_quality": "neutral",
            }
        )

        rows = setup_snapshots.load_setup_snapshots()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["strategy_tag"], "uw_flow_short")
        self.assertEqual(row["setup_mode"], "continuation_short")
        self.assertEqual(row["best_play"], "continuation_short")
        self.assertEqual(row["direction_constraint"], "short_only")
        self.assertEqual(row["timing_state"], "wait_for_trigger")

    def test_normalize_position_context_infers_from_artifact_strategy_tag(self):
        row = normalize_position_context(
            {
                "symbol": "AMZN",
                "side": "short",
                "strategy_tag": "carryover",
                "entry_path": "broker_sync",
                "signal_sources": ["broker_sync"],
                "setup_mode": "invalid",
                "best_play": "",
                "timing_state": "enter_now",
            }
        )

        self.assertEqual(row["strategy_tag"], "momentum_short")
        self.assertEqual(row["setup_mode"], "continuation_short")
        self.assertEqual(row["best_play"], "continuation_short")
        self.assertEqual(row["direction_constraint"], "short_only")

