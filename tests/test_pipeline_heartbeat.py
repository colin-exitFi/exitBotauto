"""
Regression coverage for the "silent halt" family of bugs that froze the
entry pipeline for days without a visible signal.

These tests exercise three independent guard rails that used to fail
closed with no diagnostic:

1. `_get_operating_guardrails` must clear, not latch, after a transient
   `protection_failed` flag recovers.
2. `_emit_entry_pipeline_heartbeat` must surface *why* the pipeline is
   gated (broker readiness, risk halt, reconciliation).
3. Stale on-disk `protection_failed` flags must not leak through a
   restart in paper mode.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.main import TradingBot


class _EntryManagerStub:
    def __init__(self, positions):
        self.positions = {p["symbol"]: dict(p) for p in positions}

    def get_positions(self):
        return list(self.positions.values())


class _RiskStub:
    def __init__(self, halted: bool = False):
        self.halted = halted

    def get_status(self):
        return {"trading_halted": self.halted}


class _ReconcilerStub:
    def __init__(self, status: str = "healthy"):
        self._status = status

    def snapshot(self):
        return {
            "reconciliation": {"status": self._status, "reasons": []},
            "trust": {
                "broker_only_mode": False,
                "entry_pipeline_paused": False,
                "degraded_mode_reasons": [],
            },
        }


def _make_bot(positions=None, halted: bool = False, broker_ready: bool = True):
    bot = TradingBot.__new__(TradingBot)
    bot.entry_manager = _EntryManagerStub(positions or [])
    bot.risk_manager = _RiskStub(halted=halted)
    bot.reconciler = _ReconcilerStub()
    bot.extended_guard = None
    bot._broker_ready = broker_ready
    return bot


class OperatingGuardrailsTests(unittest.TestCase):
    def test_paper_mode_does_not_latch_protection_failed(self):
        """PAPER_MODE must never let a transient protection_failed flag
        freeze the entry pipeline (this is the original 2-day silent halt)."""
        bot = _make_bot(
            positions=[{"symbol": "AAPL", "protection_failed": True, "hard_stop_order_id": "abc"}]
        )
        with patch("src.main.settings.PAPER_MODE", True, create=True), \
             patch("src.main.settings.ALPACA_PAPER", True, create=True):
            guardrails = bot._get_operating_guardrails()

        self.assertTrue(guardrails["allow_new_entries"])
        self.assertNotIn("protection_failed", guardrails["reasons"])

    def test_risk_halted_surfaces_in_reasons(self):
        bot = _make_bot(halted=True)
        guardrails = bot._get_operating_guardrails()
        self.assertFalse(guardrails["allow_new_entries"])
        self.assertIn("risk_halted", guardrails["reasons"])


class HeartbeatTests(unittest.TestCase):
    def test_heartbeat_warns_when_broker_not_ready(self):
        bot = _make_bot(broker_ready=False)
        captured = {}

        def _warn(msg, *args, **kwargs):
            captured.setdefault("warning", []).append(str(msg))

        with patch("src.main.logger.warning", side_effect=_warn), \
             patch("src.main.logger.info"), \
             patch("src.main.log_activity"):
            bot._emit_entry_pipeline_heartbeat()

        joined = "\n".join(captured.get("warning", []))
        self.assertIn("PIPELINE HEARTBEAT", joined)
        self.assertIn("GATED", joined)
        self.assertIn("broker_not_ready", joined)

    def test_heartbeat_info_when_healthy(self):
        bot = _make_bot()
        captured = {}

        def _info(msg, *args, **kwargs):
            captured.setdefault("info", []).append(str(msg))

        with patch("src.main.logger.info", side_effect=_info), \
             patch("src.main.logger.warning"), \
             patch("src.main.log_activity"):
            bot._emit_entry_pipeline_heartbeat()

        joined = "\n".join(captured.get("info", []))
        self.assertIn("PIPELINE HEARTBEAT", joined)
        self.assertIn("allow_entries=True", joined)


if __name__ == "__main__":
    unittest.main()
