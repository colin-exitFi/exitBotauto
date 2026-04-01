"""
Provider degradation policy — ensures Velox degrades gracefully when
AI providers (Claude, GPT, Grok) go down.
"""

from __future__ import annotations

import time
from typing import Dict, List

from loguru import logger


DEGRADATION_POLICY = {
    3: {"mode": "normal", "auto_enter_allowed": True},
    2: {"mode": "normal", "auto_enter_allowed": True, "note": "one_provider_down"},
    1: {"mode": "reduced_trust", "auto_enter_allowed": False, "require_classifier_only": True},
    0: {"mode": "no_discretionary", "auto_enter_allowed": False, "classifier_shadow_only": True},
}


class ProviderHealthTracker:
    """
    Tracks AI provider availability and returns the appropriate degradation policy.

    Usage:
        tracker = ProviderHealthTracker()
        tracker.record_success("claude")
        tracker.record_failure("grok")
        policy = tracker.get_policy()
    """

    PROVIDERS = ("claude", "gpt", "grok")
    HEALTH_WINDOW = 300

    def __init__(self):
        self._last_success: Dict[str, float] = {}
        self._failure_counts: Dict[str, int] = {}
        self._total_calls: Dict[str, int] = {}

    def record_success(self, provider: str):
        p = provider.lower()
        self._last_success[p] = time.time()
        self._total_calls[p] = self._total_calls.get(p, 0) + 1

    def record_failure(self, provider: str):
        p = provider.lower()
        self._failure_counts[p] = self._failure_counts.get(p, 0) + 1
        self._total_calls[p] = self._total_calls.get(p, 0) + 1

    def healthy_count(self) -> int:
        now = time.time()
        count = 0
        for p in self.PROVIDERS:
            last = self._last_success.get(p, 0)
            if (now - last) < self.HEALTH_WINDOW:
                count += 1
        return count

    def get_policy(self) -> Dict:
        count = self.healthy_count()
        policy = dict(DEGRADATION_POLICY.get(count, DEGRADATION_POLICY[0]))
        policy["healthy_providers"] = count
        return policy

    def get_snapshot(self) -> Dict:
        now = time.time()
        providers = {}
        for p in self.PROVIDERS:
            last = self._last_success.get(p, 0)
            providers[p] = {
                "healthy": (now - last) < self.HEALTH_WINDOW if last > 0 else False,
                "last_success_seconds_ago": round(now - last, 1) if last > 0 else None,
                "failure_count": self._failure_counts.get(p, 0),
                "total_calls": self._total_calls.get(p, 0),
            }
        return {
            "providers": providers,
            "healthy_count": self.healthy_count(),
            "policy": self.get_policy(),
        }
