import unittest
from unittest.mock import AsyncMock, patch

from src.agents import base_agent


class BaseAgentRateLimitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for provider in ("claude", "gpt", "grok", "perplexity"):
            base_agent._provider_timestamps[provider] = []
            base_agent._provider_backoff_until.pop(provider, None)
            base_agent._provider_backoff_seconds[provider] = 0

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


if __name__ == "__main__":
    unittest.main()
