import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.ai import trade_history
from src.ai.setup_replay import build_setup_replay
from src.dashboard import dashboard as dashboard_module
from src.data import entry_controls
from src.data import setup_snapshots


def _ts(year: int, month: int, day: int, hour: int, minute: int = 0) -> float:
    try:
        import zoneinfo

        tz = zoneinfo.ZoneInfo("America/Chicago")
    except Exception:
        tz = None
    return datetime(year, month, day, hour, minute, tzinfo=tz).timestamp()


class EntryControlsSameSymbolLockTests(unittest.TestCase):
    def setUp(self):
        self._orig_controls_file = entry_controls.CONTROLS_FILE
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.write(b"{}")
        self._tmp.close()
        entry_controls.CONTROLS_FILE = Path(self._tmp.name)

    def tearDown(self):
        entry_controls.CONTROLS_FILE = self._orig_controls_file
        try:
            Path(self._tmp.name).unlink(missing_ok=True)
        except Exception:
            pass

    def test_consecutive_losses_trigger_symbol_lock(self):
        now_ts = datetime.now().timestamp()
        entry_controls.record_symbol_trade_result(
            "PTLE",
            -44.0,
            exit_confirmed_at=now_ts,
            reason="hard_stop",
            setup_id="ptle-1",
            loss_limit=2,
            lock_seconds=3600,
        )
        blocked, reason = entry_controls.is_entry_blocked("PTLE")
        self.assertFalse(blocked)
        self.assertEqual(reason, "ok")

        entry_controls.record_symbol_trade_result(
            "PTLE",
            -18.0,
            exit_confirmed_at=now_ts + 60,
            reason="hard_stop",
            setup_id="ptle-2",
            loss_limit=2,
            lock_seconds=3600,
        )
        blocked, reason = entry_controls.is_entry_blocked("PTLE")
        self.assertTrue(blocked)
        self.assertEqual(reason, "symbol_loss_lock")

        state = entry_controls.get_symbol_trade_state("PTLE")
        self.assertEqual(state["consecutive_losses"], 2)
        self.assertEqual(state["attempts_since_win"], 2)
        self.assertEqual(state["active_lock"]["reason"], "consecutive_losses_2")


class SetupReplayAndModeReportTests(unittest.TestCase):
    def setUp(self):
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.history_file = Path(self._tmp_dir.name) / "trade_history.json"
        self.snapshots_file = Path(self._tmp_dir.name) / "setup_snapshots.json"
        self.history_file.write_text("[]")
        self.snapshots_file.write_text("[]")
        self._trade_history_file = trade_history.HISTORY_FILE
        self._setup_snapshots_file = setup_snapshots.SETUP_SNAPSHOTS_FILE
        trade_history.HISTORY_FILE = self.history_file
        setup_snapshots.SETUP_SNAPSHOTS_FILE = self.snapshots_file

        morning = _ts(2026, 3, 24, 10, 5)
        later = _ts(2026, 3, 24, 10, 20)
        trigger_fire = _ts(2026, 3, 24, 10, 28)
        entry_live = _ts(2026, 3, 24, 10, 31)
        exit_time = _ts(2026, 3, 24, 10, 43)

        snapshots = [
            {
                "snapshot_id": "snap-ptle-1",
                "setup_id": "PTLE:exhaustion_fade_short:1",
                "symbol": "PTLE",
                "recorded_at": morning,
                "symbol_state": "pending_trigger",
                "setup_mode": "exhaustion_fade_short",
                "timing_state": "wait_for_trigger",
                "trigger": "lose VWAP and fail first bounce on declining volume",
                "trigger_live": False,
                "invalidation": "reclaim HOD on expanding volume",
                "classifier_confidence": 0.86,
                "resolver_confidence": 0.74,
                "expires_at": morning + 900,
                "no_trade_reason": None,
            },
            {
                "snapshot_id": "snap-ptle-2",
                "setup_id": "PTLE:exhaustion_fade_short:1",
                "symbol": "PTLE",
                "recorded_at": later,
                "symbol_state": "pending_trigger",
                "setup_mode": "exhaustion_fade_short",
                "timing_state": "wait_for_trigger",
                "trigger": "lose VWAP and fail first bounce on declining volume",
                "trigger_live": True,
                "invalidation": "reclaim HOD on expanding volume",
                "classifier_confidence": 0.87,
                "resolver_confidence": 0.78,
                "expires_at": morning + 900,
                "no_trade_reason": None,
            },
            {
                "snapshot_id": "snap-artl-1",
                "setup_id": "ARTL:continuation_long:1",
                "symbol": "ARTL",
                "recorded_at": trigger_fire,
                "symbol_state": "pending_trigger",
                "setup_mode": "continuation_long",
                "timing_state": "wait_for_trigger",
                "trigger": "reclaim VWAP and hold with re-accelerating volume",
                "trigger_live": False,
                "invalidation": "lose VWAP or lose pullback low",
                "classifier_confidence": 0.79,
                "resolver_confidence": 0.66,
                "expires_at": trigger_fire + 1800,
                "no_trade_reason": None,
            },
            {
                "snapshot_id": "snap-artl-2",
                "setup_id": "ARTL:continuation_long:1",
                "symbol": "ARTL",
                "recorded_at": entry_live,
                "symbol_state": "live_position",
                "setup_mode": "continuation_long",
                "timing_state": "enter_now",
                "trigger": "already above VWAP with volume re-acceleration",
                "trigger_live": True,
                "invalidation": "lose VWAP or lose pullback low",
                "classifier_confidence": 0.81,
                "resolver_confidence": 0.82,
                "expires_at": trigger_fire + 1800,
                "no_trade_reason": None,
            },
        ]
        self.snapshots_file.write_text(json.dumps(snapshots))

        trades = [
            {
                "symbol": "ARTL",
                "setup_id": "ARTL:continuation_long:1",
                "setup_mode": "continuation_long",
                "strategy_tag": "momentum_long",
                "entry_price": 10.0,
                "quantity": 100,
                "entry_time": entry_live,
                "exit_time": exit_time,
                "pnl": 24.5,
                "pnl_pct": 2.45,
                "reason": "ratchet_exit",
                "hold_seconds": exit_time - entry_live,
                "profitable": True,
                "ratchet_activated": True,
                "hard_stopped": False,
            }
        ]
        self.history_file.write_text(json.dumps(trades))

    def tearDown(self):
        trade_history.HISTORY_FILE = self._trade_history_file
        setup_snapshots.SETUP_SNAPSHOTS_FILE = self._setup_snapshots_file
        self._tmp_dir.cleanup()

    def test_mode_confusion_report_summarizes_triggered_and_expired_setups(self):
        report = trade_history.get_mode_confusion_report(day="2026-03-24", now_ts=_ts(2026, 3, 24, 12, 0))

        self.assertEqual(report["classification_counts"]["continuation_long"], 1)
        self.assertEqual(report["classification_counts"]["exhaustion_fade_short"], 1)
        self.assertEqual(report["pending_setups_created"], 2)
        self.assertEqual(report["pending_setups_triggered"], 1)
        self.assertEqual(report["pending_setups_expired"], 1)
        self.assertEqual(report["executed_trades_by_mode"]["continuation_long"]["trades"], 1)
        self.assertEqual(report["top_trigger_misses"][0]["symbol"], "PTLE")

    def test_setup_replay_returns_symbol_timeline(self):
        replay = build_setup_replay(symbol="ARTL", day="2026-03-24", now_ts=_ts(2026, 3, 24, 12, 0))

        self.assertEqual(replay["summary"]["setup_count"], 1)
        self.assertEqual(replay["summary"]["entered_setup_count"], 1)
        self.assertEqual(len(replay["timeline"]), 2)
        self.assertEqual(replay["timeline"][-1]["symbol_state"], "live_position")
        self.assertEqual(replay["trades"][0]["pnl"], 24.5)


class DashboardSetupOpsEndpointTests(unittest.TestCase):
    def test_setup_replay_and_mode_report_endpoints(self):
        with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
             patch("src.ai.setup_replay.build_setup_replay", return_value={"summary": {"setup_count": 2}}), \
             patch("src.ai.trade_history.get_mode_confusion_report", return_value={"day": "2026-03-24", "setup_count": 2}):
            client = TestClient(dashboard_module.app)

            replay_resp = client.get("/api/setup-replay?symbol=PTLE&token=secret-token")
            self.assertEqual(replay_resp.status_code, 200)
            self.assertEqual(replay_resp.json()["summary"]["setup_count"], 2)

            report_resp = client.get("/api/mode-report?day=2026-03-24&token=secret-token")
            self.assertEqual(report_resp.status_code, 200)
            self.assertEqual(report_resp.json()["setup_count"], 2)
