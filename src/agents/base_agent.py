"""
Base Agent - Common infrastructure for all specialized agents.
Handles AI calls, JSON parsing, error handling, rate limiting.
"""

import asyncio
import json
import time
from typing import Dict, Optional, Tuple
from loguru import logger
import httpx

from config import settings


# ── Per-provider rate limiters ─────────────────────────────────────
_provider_timestamps: Dict[str, list] = {
    "claude": [],
    "gpt": [],
    "grok": [],
    "perplexity": [],
}
_api_calls: Dict[str, int] = {"claude": 0, "gpt": 0, "grok": 0, "perplexity": 0}
_provider_backoff_until: Dict[str, float] = {}
_provider_backoff_seconds: Dict[str, int] = {}

# Per-provider limits per hour (conservative defaults under actual API limits)
_PROVIDER_LIMITS: Dict[str, int] = {
    "claude": 200,   # Anthropic: ~1000 RPM on paid tier
    "gpt": 200,      # OpenAI: 500 RPM on paid tier
    "grok": 60,      # xAI: lower tier, reasoning model is slow anyway
    "perplexity": 60, # Perplexity: moderate tier
}

TIMEOUT = 45


def get_api_stats() -> Dict:
    return dict(_api_calls)


def _get_eastern_hour() -> int:
    from datetime import datetime

    try:
        import zoneinfo

        return datetime.now(zoneinfo.ZoneInfo("US/Eastern")).hour
    except Exception:
        return 12


def _get_provider_hourly_limit(provider: str, et_hour: Optional[int] = None) -> int:
    """Reduce after-hours limits, but not so aggressively that agents go blind."""
    if et_hour is None:
        et_hour = _get_eastern_hour()

    base_limit = int(_PROVIDER_LIMITS.get(provider, 60))
    if 4 <= et_hour < 20:
        return base_limit

    # After hours still needs meaningful scans; keep 75% of regular limit with a sane floor.
    return max(45, int((base_limit * 3 + 3) / 4))


def _check_rate_limit(
    provider: str,
    now: Optional[float] = None,
    et_hour: Optional[int] = None,
) -> Tuple[bool, float]:
    """Returns (allowed, wait_seconds) for the given provider."""
    if now is None:
        now = time.time()

    backoff_until = float(_provider_backoff_until.get(provider, 0.0) or 0.0)
    if backoff_until > now:
        return False, max(0.0, backoff_until - now)

    limit = _get_provider_hourly_limit(provider, et_hour=et_hour)

    timestamps = _provider_timestamps.setdefault(provider, [])
    timestamps[:] = [t for t in timestamps if now - t < 3600]
    if len(timestamps) >= limit:
        last_backoff = int(_provider_backoff_seconds.get(provider, 0) or 0)
        next_backoff = min(30, max(2, last_backoff * 2 if last_backoff else 2))
        _provider_backoff_seconds[provider] = next_backoff
        _provider_backoff_until[provider] = now + next_backoff
        return False, float(next_backoff)

    timestamps.append(now)
    _provider_backoff_seconds[provider] = 0
    _provider_backoff_until.pop(provider, None)
    return True, 0.0


async def _await_rate_limit_slot(provider: str, max_attempts: int = 5) -> bool:
    """Wait a bounded amount of time before giving up on a provider call."""
    for attempt in range(1, max_attempts + 1):
        allowed, wait_seconds = _check_rate_limit(provider)
        if allowed:
            return True

        wait_seconds = max(0.5, float(wait_seconds or 0.0))
        logger.warning(
            f"Rate limit reached for {provider}; backing off {wait_seconds:.1f}s "
            f"(attempt {attempt}/{max_attempts})"
        )
        await asyncio.sleep(wait_seconds)

    logger.warning(f"Rate limit remained active for {provider}; skipping call after backoff")
    return False


def parse_json(text: str) -> dict:
    """Parse JSON from AI response, handling markdown fences."""
    text = text.strip()
    if "```" in text:
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0]
        else:
            text = text.split("```")[1].split("```")[0]
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            return json.loads(text[start:end])
        return {}


async def call_claude(prompt: str, max_tokens: int = 600) -> Optional[Dict]:
    """Call Claude Sonnet and return parsed JSON."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    if not await _await_rate_limit_slot("claude"):
        return None
    model = getattr(settings, 'CLAUDE_MODEL', 'claude-sonnet-4-5-20250929')
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            _api_calls["claude"] += 1
            text = resp.json()["content"][0]["text"]
            return parse_json(text)
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


async def call_claude_text(prompt: str, max_tokens: int = 900) -> Optional[str]:
    """Call Claude Sonnet and return plain text."""
    if not settings.ANTHROPIC_API_KEY:
        return None
    if not await _await_rate_limit_slot("claude"):
        return None
    model = getattr(settings, 'CLAUDE_MODEL', 'claude-sonnet-4-5-20250929')
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": settings.ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            _api_calls["claude"] += 1
            return str(resp.json()["content"][0]["text"]).strip()
    except Exception as e:
        logger.error(f"Claude API error: {e}")
        return None


async def call_gpt(prompt: str, max_tokens: int = 600) -> Optional[Dict]:
    """Call GPT-5.2 and return parsed JSON."""
    if not settings.OPENAI_API_KEY:
        return None
    if not await _await_rate_limit_slot("gpt"):
        return None
    model = getattr(settings, 'OPENAI_MODEL', 'gpt-5.4')
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_completion_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            _api_calls["gpt"] += 1
            text = resp.json()["choices"][0]["message"]["content"]
            return parse_json(text)
    except Exception as e:
        logger.error(f"GPT API error: {e}")
        return None


async def call_gpt_text(prompt: str, max_tokens: int = 900) -> Optional[str]:
    """Call GPT and return plain text."""
    if not settings.OPENAI_API_KEY:
        return None
    if not await _await_rate_limit_slot("gpt"):
        return None
    model = getattr(settings, 'OPENAI_MODEL', 'gpt-5.4')
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_completion_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            _api_calls["gpt"] += 1
            return str(resp.json()["choices"][0]["message"]["content"]).strip()
    except Exception as e:
        logger.error(f"GPT API error: {e}")
        return None


async def call_grok(prompt: str, max_tokens: int = 600) -> Optional[Dict]:
    """Call Grok-4 via xAI and return parsed JSON."""
    if not settings.XAI_API_KEY:
        return None
    if not await _await_rate_limit_slot("grok"):
        return None
    model = getattr(settings, 'XAI_MODEL', 'grok-4-0709')
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.XAI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "temperature": 0.3,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            _api_calls["grok"] += 1
            text = resp.json()["choices"][0]["message"]["content"]
            return parse_json(text)
    except Exception as e:
        logger.error(f"Grok API error: {e}")
        return None


async def call_perplexity(prompt: str, max_tokens: int = 600) -> Optional[Dict]:
    """Call Perplexity sonar-pro and return parsed JSON."""
    if not settings.PERPLEXITY_API_KEY:
        return None
    if not await _await_rate_limit_slot("perplexity"):
        return None
    model = getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro')
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            _api_calls["perplexity"] += 1
            text = resp.json()["choices"][0]["message"]["content"]
            return parse_json(text)
    except Exception as e:
        logger.error(f"Perplexity API error: {e}")
        return None


async def call_perplexity_text(prompt: str, max_tokens: int = 900) -> Optional[str]:
    """Call Perplexity and return plain text."""
    if not settings.PERPLEXITY_API_KEY:
        return None
    if not await _await_rate_limit_slot("perplexity"):
        return None
    model = getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro')
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            resp = await client.post(
                "https://api.perplexity.ai/chat/completions",
                headers={
                    "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )
            resp.raise_for_status()
            _api_calls["perplexity"] += 1
            return str(resp.json()["choices"][0]["message"]["content"]).strip()
    except Exception as e:
        logger.error(f"Perplexity API error: {e}")
        return None
