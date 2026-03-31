import unittest
from unittest.mock import patch

from src.ai.advisor import Advisor, _parse_json
from src.agents.exit_agent import ExitAgent


class _RiskAllowsExit:
    def can_exit_position(self, position, reason="", log_block=True):
        return True


class _RiskBlocksExit:
    def can_exit_position(self, position, reason="", log_block=True):
        return False


class AdvisorActionTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_position_actions_filters_for_exit_and_trim(self):
        advisor = Advisor()
        advisor._last_output = {
            "timestamp": 123.0,
            "position_advice": [
                {"symbol": "NVDA", "action": "exit", "reason": "Thesis broken urgently", "urgency": "high"},
                {"symbol": "TSLA", "action": "trim", "reason": "Extended"},
                {"symbol": "AAPL", "action": "hold", "reason": "fine"},
            ],
        }

        actions = advisor.get_position_actions()

        self.assertEqual(len(actions), 2)
        self.assertEqual(actions[0]["symbol"], "NVDA")
        self.assertEqual(actions[1]["urgency"], "medium")

    async def test_exit_agent_processes_advisor_recommendation_once(self):
        agent = ExitAgent(risk_manager=_RiskAllowsExit())
        positions = [{"symbol": "NVDA", "trail_pct": 3.0, "quantity": 10, "halted": False}]
        advisor = Advisor()
        advisor._last_output = {
            "timestamp": 456.0,
            "position_advice": [{"symbol": "NVDA", "action": "trim", "reason": "Lock gains", "urgency": "medium"}],
        }
        applied = []

        async def _record(symbol, pos, action):
            applied.append((symbol, action["new_trail_pct"]))

        agent._execute_action = _record
        await agent._check_advisor_recommendations(positions, advisor)
        await agent._check_advisor_recommendations(positions, advisor)

        self.assertEqual(applied, [("NVDA", 1.5)])

    async def test_exit_agent_respects_swing_mode_rules(self):
        agent = ExitAgent(risk_manager=_RiskBlocksExit())
        positions = [{"symbol": "NVDA", "trail_pct": 3.0, "quantity": 10, "halted": False, "swing_only": True}]
        advisor = Advisor()
        advisor._last_output = {
            "timestamp": 789.0,
            "position_advice": [{"symbol": "NVDA", "action": "exit", "reason": "Reduce risk", "urgency": "high"}],
        }

        with patch.object(agent, "_execute_action") as execute_mock:
            applied = await agent._check_advisor_recommendations(positions, advisor)

        self.assertEqual(applied, [])
        execute_mock.assert_not_called()

    async def test_runtime_ready_requires_completed_scan_and_observer_output_during_warmup(self):
        bot = type(
            "Bot",
            (),
            {
                "start_time": 100.0,
                "scanner": type(
                    "Scanner",
                    (),
                    {"_last_scan_stats": {"status": "running", "last_completed_at": None}},
                )(),
            },
        )()

        self.assertFalse(Advisor._runtime_ready(bot, 160.0, observer_output=None))
        self.assertTrue(Advisor._runtime_ready(bot, 281.0, observer_output={"market_assessment": "stable"}))

    async def test_parse_json_unwraps_list_payload_into_last_dict(self):
        parsed = _parse_json(
            '[{"strategy":"stale","position_advice":[]},{"strategy":"fresh","position_advice":[{"symbol":"NVDA","action":"exit"}]}]'
        )

        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["strategy"], "fresh")
        self.assertEqual(parsed["position_advice"][0]["symbol"], "NVDA")


if __name__ == "__main__":
    unittest.main()
