import unittest
from unittest.mock import AsyncMock, patch

from src.agents import base_agent


class BaseAgentRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for provider in ("claude", "gpt", "grok", "perplexity"):
            base_agent._provider_timestamps[provider] = []
            base_agent._provider_backoff_until.pop(provider, None)
            base_agent._provider_backoff_seconds[provider] = 0
            base_agent._api_calls[provider] = 0
            usage = base_agent._api_token_usage[provider]
            usage["prompt_tokens"] = 0.0
            usage["completion_tokens"] = 0.0
            usage["reasoning_tokens"] = 0.0
            usage["cost_usd"] = 0.0
        base_agent._api_usage_day = ""

    async def test_await_rate_limit_slot_uses_exponential_backoff(self):
        with patch.object(
            base_agent,
            "_check_rate_limit",
            side_effect=[(False, 2.0), (False, 4.0), (True, 0.0)],
        ), patch("src.agents.base_agent.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            allowed = await base_agent._await_rate_limit_slot("gpt", max_attempts=5)

        self.assertTrue(allowed)
        self.assertEqual([call.args[0] for call in sleep_mock.await_args_list], [2.0, 4.0])

    def test_after_hours_limit_is_not_cut_to_thirty(self):
        self.assertEqual(base_agent._get_provider_hourly_limit("gpt", et_hour=22), 150)
        self.assertEqual(base_agent._get_provider_hourly_limit("perplexity", et_hour=22), 45)

    def test_check_rate_limit_backoff_escalates(self):
        now = 10_000.0
        limit = base_agent._get_provider_hourly_limit("gpt", et_hour=10)
        base_agent._provider_timestamps["gpt"] = [now - 10] * limit

        allowed, wait = base_agent._check_rate_limit("gpt", now=now, et_hour=10)
        self.assertFalse(allowed)
        self.assertEqual(wait, 2.0)

        allowed, wait = base_agent._check_rate_limit("gpt", now=now + 2.1, et_hour=10)
        self.assertFalse(allowed)
        self.assertEqual(wait, 4.0)

        allowed, wait = base_agent._check_rate_limit("gpt", now=now + 6.2, et_hour=10)
        self.assertFalse(allowed)
        self.assertEqual(wait, 8.0)

    def test_record_api_usage_tracks_estimated_cost(self):
        payload = {
            "usage": {
                "prompt_tokens": 1200,
                "completion_tokens": 300,
                "completion_tokens_details": {"reasoning_tokens": 100},
            }
        }
        with patch.object(base_agent, "_current_cost_day", return_value="2026-03-09"):
            base_agent._record_api_usage("gpt", payload)
            base_agent._api_calls["gpt"] = 1
            stats = base_agent.get_api_cost_stats()

        self.assertEqual(stats["day"], "2026-03-09")
        self.assertEqual(stats["per_provider"]["gpt"]["prompt_tokens"], 1200)
        self.assertEqual(stats["per_provider"]["gpt"]["completion_tokens"], 300)
        self.assertEqual(stats["per_provider"]["gpt"]["reasoning_tokens"], 100)
        self.assertGreater(stats["per_provider"]["gpt"]["estimated_cost_usd"], 0.0)

    def test_grok_prefers_exact_cost_ticks_when_present(self):
        payload = {
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 500,
                "cost_in_usd_ticks": 250000,
            }
        }
        with patch.object(base_agent, "_current_cost_day", return_value="2026-03-09"):
            base_agent._record_api_usage("grok", payload)
            base_agent._api_calls["grok"] = 1
            stats = base_agent.get_api_cost_stats()

        self.assertAlmostEqual(stats["per_provider"]["grok"]["estimated_cost_usd"], 0.25, places=6)


if __name__ == "__main__":
    unittest.main()
