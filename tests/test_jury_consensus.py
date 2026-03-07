import unittest
from unittest.mock import patch

from src.agents import jury


def _vote(decision="BUY", confidence=80, size_pct=2.0, trail_pct=3.0, reasoning="ok"):
    return {
        "decision": decision,
        "confidence": confidence,
        "size_pct": size_pct,
        "trail_pct": trail_pct,
        "reasoning": reasoning,
    }


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


class JuryConsensusTests(unittest.IsolatedAsyncioTestCase):
    async def test_unanimous_buy_is_full_size(self):
        with patch.object(jury, "call_claude", new=_async_return(_vote("BUY", 90, 2.0, 2.5, "c"))), \
             patch.object(jury, "call_gpt", new=_async_return(_vote("BUY", 80, 2.0, 2.0, "g"))), \
             patch.object(jury, "call_grok", new=_async_return(_vote("BUY", 70, 2.0, 3.0, "x"))):
            verdict = await jury.deliberate("AAPL", 100.0, {}, {})

        self.assertEqual(verdict.decision, "BUY")
        self.assertEqual(verdict.size_pct, 2.0)
        self.assertEqual(verdict.consensus_detail["agreement"], "unanimous")
        self.assertEqual(verdict.consensus_detail["votes"]["claude"], "BUY")

    async def test_majority_buy_with_skip_is_full_size(self):
        with patch.object(jury, "call_claude", new=_async_return(_vote("BUY", 90, 2.0, 2.0))), \
             patch.object(jury, "call_gpt", new=_async_return(_vote("BUY", 70, 2.0, 4.0))), \
             patch.object(jury, "call_grok", new=_async_return(_vote("SKIP", 50, 0.0, 3.0))):
            verdict = await jury.deliberate("AAPL", 100.0, {}, {})

        self.assertEqual(verdict.decision, "BUY")
        self.assertEqual(verdict.size_pct, 2.0)
        self.assertAlmostEqual(verdict.trail_pct, 3.0, places=2)
        self.assertEqual(verdict.consensus_detail["agreement"], "majority")

    async def test_majority_buy_with_short_gets_conflict_discount(self):
        with patch.object(jury, "call_claude", new=_async_return(_vote("BUY", 90, 2.0, 2.0))), \
             patch.object(jury, "call_gpt", new=_async_return(_vote("BUY", 80, 2.0, 4.0))), \
             patch.object(jury, "call_grok", new=_async_return(_vote("SHORT", 75, 2.0, 3.0))):
            verdict = await jury.deliberate("AAPL", 100.0, {}, {})

        self.assertEqual(verdict.decision, "BUY")
        self.assertAlmostEqual(verdict.size_pct, 1.5, places=3)
        self.assertEqual(verdict.consensus_detail["agreement"], "majority_conflict")
        self.assertEqual(verdict.consensus_detail["size_modifier"], 0.75)

    async def test_one_buy_two_skip_is_skip(self):
        with patch.object(jury, "call_claude", new=_async_return(_vote("BUY", 90, 2.0, 2.0))), \
             patch.object(jury, "call_gpt", new=_async_return(_vote("SKIP", 50, 0.0, 3.0))), \
             patch.object(jury, "call_grok", new=_async_return(_vote("SKIP", 50, 0.0, 3.0))):
            verdict = await jury.deliberate("AAPL", 100.0, {}, {})

        self.assertEqual(verdict.decision, "SKIP")
        self.assertEqual(verdict.size_pct, 0)

    async def test_single_model_fallback_uses_half_size(self):
        with patch.object(jury, "call_claude", new=_async_return(_vote("BUY", 80, 2.0, 2.5))), \
             patch.object(jury, "call_gpt", new=_async_return(None)), \
             patch.object(jury, "call_grok", new=_async_return(None)):
            verdict = await jury.deliberate("AAPL", 100.0, {}, {})

        self.assertEqual(verdict.decision, "BUY")
        self.assertAlmostEqual(verdict.size_pct, 1.0, places=3)
        self.assertAlmostEqual(verdict.confidence, 48.0, places=2)
        self.assertEqual(verdict.consensus_detail["agreement"], "single")

    async def test_two_models_agree_full_size(self):
        with patch.object(jury, "call_claude", new=_async_return(None)), \
             patch.object(jury, "call_gpt", new=_async_return(_vote("SHORT", 70, 1.8, 2.0))), \
             patch.object(jury, "call_grok", new=_async_return(_vote("SHORT", 90, 2.2, 3.0))):
            verdict = await jury.deliberate("NVDA", 100.0, {}, {})

        self.assertEqual(verdict.decision, "SHORT")
        self.assertAlmostEqual(verdict.size_pct, 2.0, places=3)
        self.assertEqual(verdict.consensus_detail["agreement"], "majority_two_model")

    async def test_two_models_disagree_skip(self):
        with patch.object(jury, "call_claude", new=_async_return(None)), \
             patch.object(jury, "call_gpt", new=_async_return(_vote("BUY", 70, 2.0, 2.0))), \
             patch.object(jury, "call_grok", new=_async_return(_vote("SHORT", 90, 2.0, 3.0))):
            verdict = await jury.deliberate("NVDA", 100.0, {}, {})

        self.assertEqual(verdict.decision, "SKIP")
        self.assertEqual(verdict.consensus_detail["agreement"], "split")

    async def test_all_models_fail_is_skip(self):
        with patch.object(jury, "call_claude", new=_async_return(None)), \
             patch.object(jury, "call_gpt", new=_async_return(None)), \
             patch.object(jury, "call_grok", new=_async_return(None)):
            verdict = await jury.deliberate("TSLA", 100.0, {}, {})

        self.assertEqual(verdict.decision, "SKIP")
        self.assertEqual(verdict.provider_used, "none")
        self.assertEqual(verdict.consensus_detail["total_models"], 0)

    async def test_risk_override_still_blocks_trade(self):
        briefs = {"risk": {"approved": False, "reasoning": "portfolio too hot"}}
        with patch.object(jury, "call_claude", new=_async_return(_vote("BUY", 90, 2.0, 2.0))), \
             patch.object(jury, "call_gpt", new=_async_return(_vote("BUY", 80, 2.0, 2.0))), \
             patch.object(jury, "call_grok", new=_async_return(_vote("BUY", 70, 2.0, 2.0))):
            verdict = await jury.deliberate("AAPL", 100.0, briefs, {})

        self.assertEqual(verdict.decision, "SKIP")
        self.assertTrue(verdict.consensus_detail["risk_override"])


class JuryRetroContextTests(unittest.TestCase):
    def test_build_retro_feedback_summarizes_recent_matches(self):
        recent = [
            {
                "symbol": "AAPL",
                "strategy_tag": "fade_runner",
                "signal_sources": ["copy_trader"],
                "pnl": -25.0,
                "decision_confidence": 85,
            },
            {
                "symbol": "AAPL",
                "strategy_tag": "fade_runner",
                "signal_sources": ["copy_trader", "human_intel"],
                "pnl": -10.0,
                "decision_confidence": 82,
            },
            {
                "symbol": "TSLA",
                "strategy_tag": "fade_runner",
                "signal_sources": ["copy_trader"],
                "pnl": 5.0,
                "decision_confidence": 79,
            },
            {
                "symbol": "AAPL",
                "strategy_tag": "fade_runner",
                "signal_sources": ["copy_trader"],
                "pnl": -8.0,
                "decision_confidence": 77,
            },
        ]
        with patch("src.ai.trade_history.get_recent", return_value=recent):
            text = jury._build_retro_feedback(
                "AAPL",
                {"strategy_tag": "fade_runner", "signal_sources": ["copy_trader"]},
            )

        self.assertIn("Recent AAPL", text)
        self.assertIn("Strategy fade_runner", text)
        self.assertIn("Sources copy_trader", text)
        self.assertIn("Calibration:", text)

    def test_build_retro_feedback_can_be_disabled(self):
        with patch.object(jury.settings, "JURY_RETRO_ENABLED", False):
            text = jury._build_retro_feedback("AAPL", {"strategy_tag": "fade_runner"})

        self.assertEqual(text, "None")


if __name__ == "__main__":
    unittest.main()
