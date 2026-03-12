import time
import unittest

from src.agents.jury import JuryVerdict
from src.agents.orchestrator import Orchestrator


class OrchestratorHistoryTests(unittest.TestCase):
    def test_append_history_coalesces_adjacent_duplicate_verdicts(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        orchestrator._history = []

        verdict1 = JuryVerdict(
            symbol="LWLG",
            decision="SKIP",
            size_pct=0.0,
            trail_pct=3.0,
            reasoning="Two-model unanimous SKIP",
            confidence=0.0,
            provider_used="grok",
            timestamp=time.time(),
        )
        verdict2 = JuryVerdict(
            symbol="LWLG",
            decision="SKIP",
            size_pct=0.0,
            trail_pct=3.0,
            reasoning="Two-model unanimous SKIP",
            confidence=0.0,
            provider_used="grok",
            timestamp=verdict1.timestamp + 30,
        )

        orchestrator._append_history(verdict1)
        orchestrator._append_history(verdict2)

        history = orchestrator.get_history()
        self.assertEqual(len(history), 1)
        self.assertAlmostEqual(history[0]["timestamp"], verdict2.timestamp, places=6)

    def test_get_stats_uses_deduped_history(self):
        orchestrator = Orchestrator.__new__(Orchestrator)
        now = time.time()
        entry = {
            "symbol": "LWLG",
            "decision": "SKIP",
            "size_pct": 0.0,
            "trail_pct": 3.0,
            "reasoning": "Two-model unanimous SKIP",
            "confidence": 0.0,
            "provider_used": "grok",
            "timestamp": now,
        }
        duplicate = dict(entry)
        duplicate["timestamp"] = now + 20
        orchestrator._history = [entry, duplicate]

        from unittest.mock import patch

        with patch("src.agents.base_agent.get_api_stats", return_value={}), \
             patch("src.agents.base_agent.get_api_cost_stats", return_value={}):
            stats = orchestrator.get_stats()

        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["skips"], 1)


if __name__ == "__main__":
    unittest.main()
