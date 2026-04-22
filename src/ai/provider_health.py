"""
Provider degradation policy — ensures Velox degrades gracefully when
AI providers (Claude, GPT, Grok) go down.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

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
        self._last_latency_ms: Dict[str, int] = {}
        self._last_error: Dict[str, str] = {}

    def record_success(self, provider: str, latency_ms: Optional[float] = None):
        p = provider.lower()
        self._last_success[p] = time.time()
        self._total_calls[p] = self._total_calls.get(p, 0) + 1
        if latency_ms is not None:
            self._last_latency_ms[p] = int(max(0, float(latency_ms or 0)))
        self._last_error.pop(p, None)

    def record_failure(
        self,
        provider: str,
        error: Optional[str] = None,
        latency_ms: Optional[float] = None,
    ):
        p = provider.lower()
        self._failure_counts[p] = self._failure_counts.get(p, 0) + 1
        self._total_calls[p] = self._total_calls.get(p, 0) + 1
        if latency_ms is not None:
            self._last_latency_ms[p] = int(max(0, float(latency_ms or 0)))
        if error:
            self._last_error[p] = str(error)

    def _enabled_providers(self) -> tuple:
        """Return the subset of PROVIDERS enabled via *_API_ENABLED flags.
        Prevents disabled providers from counting as 'failed' in the degradation policy."""
        try:
            from config import settings as _settings
        except Exception:  # pragma: no cover
            return self.PROVIDERS
        flag_map = {
            "claude": True,  # Claude is always required
            "gpt": bool(getattr(_settings, "OPENAI_API_ENABLED", True)),
            "grok": bool(getattr(_settings, "XAI_API_ENABLED", True)),
        }
        return tuple(p for p in self.PROVIDERS if flag_map.get(p, True))

    def healthy_count(self) -> int:
        now = time.time()
        count = 0
        for p in self._enabled_providers():
            last = self._last_success.get(p, 0)
            if (now - last) < self.HEALTH_WINDOW:
                count += 1
        return count

    def get_policy(self) -> Dict:
        enabled = self._enabled_providers()
        count = self.healthy_count()
        # Degradation is relative to enabled providers, not the hard-coded 3.
        # If only Claude is enabled and it's healthy, that's normal mode, not
        # "no_discretionary" like the old code assumed.
        if len(enabled) == 0:
            policy = dict(DEGRADATION_POLICY[0])
        elif count == len(enabled):
            policy = dict(DEGRADATION_POLICY[3])  # all enabled providers healthy
        elif count >= max(1, len(enabled) - 1):
            policy = dict(DEGRADATION_POLICY[2])  # one down out of enabled set
        elif count >= 1:
            policy = dict(DEGRADATION_POLICY[1])
        else:
            policy = dict(DEGRADATION_POLICY[0])
        policy["healthy_providers"] = count
        policy["enabled_providers"] = list(enabled)
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
                "last_latency_ms": self._last_latency_ms.get(p),
                "last_error": self._last_error.get(p, ""),
            }
        return {
            "providers": providers,
            "healthy_count": self.healthy_count(),
            "policy": self.get_policy(),
        }

    def get_dashboard_status(self) -> Dict[str, Dict]:
        # Lazy import to avoid a circular ref at module load time.
        try:
            from config import settings as _settings
        except Exception:  # pragma: no cover
            _settings = None

        provider_enable_flag = {
            "claude": True,  # Claude is required for Velox to run at all
            "gpt": bool(getattr(_settings, "OPENAI_API_ENABLED", True)) if _settings else True,
            "grok": bool(getattr(_settings, "XAI_API_ENABLED", True)) if _settings else True,
        }

        providers = (self.get_snapshot() or {}).get("providers", {}) or {}
        status: Dict[str, Dict] = {}
        for name, row in providers.items():
            enabled = bool(provider_enable_flag.get(name, True))
            healthy = bool(row.get("healthy"))
            last_success_seconds_ago = row.get("last_success_seconds_ago")
            detail = ""
            if not enabled:
                detail = "disabled_in_config"
            elif not healthy:
                if row.get("last_error"):
                    detail = str(row.get("last_error") or "")
                elif last_success_seconds_ago is None:
                    detail = "no_recent_success"
                else:
                    detail = f"stale:{last_success_seconds_ago}s"
            status[name] = {
                "ok": healthy,
                "enabled": enabled,
                "disabled": not enabled,
                "latency_ms": row.get("last_latency_ms"),
                "error": detail,
                "failure_count": int(row.get("failure_count", 0) or 0),
                "total_calls": int(row.get("total_calls", 0) or 0),
            }
        return status


_shared_tracker = ProviderHealthTracker()


def get_provider_health_tracker() -> ProviderHealthTracker:
    return _shared_tracker
