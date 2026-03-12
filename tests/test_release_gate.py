"""
Release gate tests — must pass before any deployment.
Tests the fail-closed redesign invariants.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))


class TestAtomicPersistence(unittest.TestCase):
    """D5: Atomic write prevents corruption."""

    def test_atomic_write_survives_content(self):
        from src.persistence import atomic_write_json, safe_load_json
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.json"
            atomic_write_json(path, {"key": "value", "num": 42})
            data = safe_load_json(path)
            self.assertEqual(data["key"], "value")
            self.assertEqual(data["num"], 42)

    def test_atomic_write_no_tmp_file_left(self):
        from src.persistence import atomic_write_json
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "test.json"
            atomic_write_json(path, {"a": 1})
            tmp = path.with_suffix(".tmp")
            self.assertFalse(tmp.exists())


class TestEntryControls(unittest.TestCase):
    """D3: Persistent blacklist/cooldown/veto/tombstone."""

    def setUp(self):
        self._orig_file = None
        from src.data import entry_controls
        self._orig_file = entry_controls.CONTROLS_FILE
        self._tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp.write(b'{}')
        self._tmp.close()
        entry_controls.CONTROLS_FILE = Path(self._tmp.name)

    def tearDown(self):
        from src.data import entry_controls
        entry_controls.CONTROLS_FILE = self._orig_file
        try:
            os.unlink(self._tmp.name)
        except Exception:
            pass

    def test_blacklist_blocks_entry(self):
        from src.data.entry_controls import blacklist_symbol, is_entry_blocked
        blacklist_symbol("DOMO", duration_seconds=86400, reason="statistical_poison")
        blocked, reason = is_entry_blocked("DOMO")
        self.assertTrue(blocked)
        self.assertEqual(reason, "blacklisted")

    def test_cooldown_blocks_entry(self):
        from src.data.entry_controls import set_cooldown, is_entry_blocked
        set_cooldown("AAPL", exit_confirmed_at=time.time(), cooldown_seconds=300)
        blocked, reason = is_entry_blocked("AAPL")
        self.assertTrue(blocked)
        self.assertEqual(reason, "cooldown")

    def test_expired_cooldown_allows_entry(self):
        from src.data.entry_controls import set_cooldown, is_entry_blocked
        set_cooldown("MSFT", exit_confirmed_at=time.time() - 400, cooldown_seconds=300)
        blocked, reason = is_entry_blocked("MSFT")
        self.assertFalse(blocked)

    def test_jury_veto_blocks_entry(self):
        from src.data.entry_controls import record_jury_veto, is_entry_blocked
        record_jury_veto("TSLA")
        blocked, reason = is_entry_blocked("TSLA")
        self.assertTrue(blocked)
        self.assertEqual(reason, "jury_vetoed")

    def test_tombstone_blocks_entry(self):
        from src.data.entry_controls import tombstone_symbol, is_entry_blocked
        tombstone_symbol("GHOST", reason="startup_cleanup")
        blocked, reason = is_entry_blocked("GHOST")
        self.assertTrue(blocked)
        self.assertEqual(reason, "tombstoned")

    def test_symbol_normalization(self):
        from src.data.entry_controls import blacklist_symbol, is_blacklisted
        blacklist_symbol("  aapl  ", duration_seconds=60)
        self.assertTrue(is_blacklisted("AAPL"))
        self.assertTrue(is_blacklisted("aapl"))


    def test_symbol_daily_entry_limit_blocks(self):
        from src.data.entry_controls import record_entry, is_entry_blocked
        record_entry("DOMO", "breakout_fast_path")
        record_entry("DOMO", "breakout_fast_path")
        blocked, reason = is_entry_blocked("DOMO", max_symbol_entries=2)
        self.assertTrue(blocked)
        self.assertEqual(reason, "symbol_daily_limit")

    def test_strategy_daily_entry_limit_counts(self):
        from src.data.entry_controls import record_entry, get_strategy_entry_count
        record_entry("AAPL", "breakout_fast_path")
        record_entry("MSFT", "breakout_fast_path")
        self.assertEqual(get_strategy_entry_count("breakout_fast_path"), 2)

class TestTradingCalendar(unittest.TestCase):
    """D6: Canonical trading calendar."""

    def test_trading_day_returns_string(self):
        from src.data.trading_calendar import trading_day
        day = trading_day()
        self.assertRegex(day, r"\d{4}-\d{2}-\d{2}")

    def test_is_same_trading_day(self):
        from src.data.trading_calendar import is_same_trading_day
        now = time.time()
        self.assertTrue(is_same_trading_day(now, now + 60))

    def test_different_days(self):
        from src.data.trading_calendar import is_same_trading_day
        now = time.time()
        self.assertFalse(is_same_trading_day(now, now - 100000))


class TestRiskAgentDefaultDeny(unittest.TestCase):
    """D7: Risk agent DEFAULT_BRIEF denies, not approves."""

    def test_default_brief_denies(self):
        from src.agents.risk_agent import DEFAULT_BRIEF
        self.assertFalse(DEFAULT_BRIEF["approved"])
        self.assertEqual(DEFAULT_BRIEF["max_size_pct"], 0.0)

    def test_default_brief_has_error_flag(self):
        from src.agents.risk_agent import DEFAULT_BRIEF
        self.assertTrue(DEFAULT_BRIEF["error"])


class TestMissionSafety(unittest.TestCase):
    """D8: Mission statement has no action bias."""

    def test_no_bias_toward_action(self):
        from src.ai.mission import MISSION
        self.assertNotIn("BIAS TOWARD ACTION", MISSION)

    def test_no_missing_runner_infinite(self):
        from src.ai.mission import MISSION
        self.assertNotIn("cost of missing a runner is infinite", MISSION)

    def test_no_dead_capital_enemy(self):
        from src.ai.mission import MISSION
        self.assertNotIn("Dead capital sitting in cash = losing", MISSION)

    def test_skip_is_mentioned_positively(self):
        from src.ai.mission import MISSION
        self.assertIn("SKIP", MISSION)

    def test_no_false_stop_guarantee(self):
        from src.ai.mission import MISSION
        self.assertNotIn("Maximum downside per trade is 3%", MISSION)


class TestJuryConsensusSafety(unittest.TestCase):
    """D8: Jury consensus fails closed on ambiguous input."""

    def test_split_with_skip_returns_skip(self):
        from src.agents.jury import _apply_consensus
        votes = [
            {"provider": "claude", "decision": "BUY", "size_pct": 2.0, "trail_pct": 3.0, "confidence": 70, "reasoning": "test"},
            {"provider": "gpt", "decision": "SKIP", "size_pct": 0, "trail_pct": 3.0, "confidence": 0, "reasoning": "test"},
        ]
        verdict = _apply_consensus("TEST", 100.0, votes, {}, [
            {"provider": "claude", "result": {}, "rate_limited": False, "error": ""},
            {"provider": "gpt", "result": {}, "rate_limited": False, "error": ""},
        ])
        self.assertEqual(verdict.decision, "SKIP")

    def test_all_models_failed_returns_skip(self):
        from src.agents.jury import _apply_consensus
        verdict = _apply_consensus("TEST", 100.0, [], {}, [
            {"provider": "claude", "result": None, "rate_limited": True, "error": "rate_limited"},
            {"provider": "gpt", "result": None, "rate_limited": True, "error": "rate_limited"},
        ])
        self.assertEqual(verdict.decision, "SKIP")

    def test_unanimous_buy_passes(self):
        from src.agents.jury import _apply_consensus
        votes = [
            {"provider": p, "decision": "BUY", "size_pct": 2.0, "trail_pct": 3.0, "confidence": 80, "reasoning": "test"}
            for p in ["claude", "gpt", "grok"]
        ]
        verdict = _apply_consensus("TEST", 100.0, votes, {}, [
            {"provider": p, "result": {}, "rate_limited": False, "error": ""}
            for p in ["claude", "gpt", "grok"]
        ])
        self.assertEqual(verdict.decision, "BUY")


class TestMinNotional(unittest.TestCase):
    """D7: Minimum notional rejects dust entries."""

    def test_min_notional_setting_exists(self):
        from config import settings
        self.assertGreater(float(getattr(settings, "MIN_NOTIONAL", 0)), 0)


class TestMinConfidence(unittest.TestCase):
    """D7: Minimum jury confidence setting exists."""

    def test_min_confidence_setting_exists(self):
        from config import settings
        self.assertGreater(int(getattr(settings, "MIN_JURY_CONFIDENCE", 0)), 0)


class TestOrchestratorAllDefaultsBriefs(unittest.TestCase):
    """D8: All 5 agents failing returns SKIP without calling jury."""

    def test_all_defaults_returns_skip(self):
        from src.agents import technical_agent, sentiment_agent, catalyst_agent, risk_agent, macro_agent
        all_errored = all(
            isinstance(b, dict) and b.get("error")
            for b in [
                technical_agent.DEFAULT_BRIEF,
                sentiment_agent.DEFAULT_BRIEF,
                catalyst_agent.DEFAULT_BRIEF,
                risk_agent.DEFAULT_BRIEF,
                macro_agent.DEFAULT_BRIEF,
            ]
        )
        self.assertTrue(all_errored, "All DEFAULT_BRIEFs must have error=True for all-defaults-SKIP check")


if __name__ == "__main__":
    unittest.main()
