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
_api_token_usage: Dict[str, Dict[str, float]] = {
    "claude": {"prompt_tokens": 0.0, "completion_tokens": 0.0, "reasoning_tokens": 0.0, "cost_usd": 0.0},
    "gpt": {"prompt_tokens": 0.0, "completion_tokens": 0.0, "reasoning_tokens": 0.0, "cost_usd": 0.0},
    "grok": {"prompt_tokens": 0.0, "completion_tokens": 0.0, "reasoning_tokens": 0.0, "cost_usd": 0.0},
    "perplexity": {"prompt_tokens": 0.0, "completion_tokens": 0.0, "reasoning_tokens": 0.0, "cost_usd": 0.0},
}
_api_usage_day: str = ""

# Per-provider limits per hour (conservative defaults under actual API limits)
_PROVIDER_LIMITS: Dict[str, int] = {
    "claude": 200,   # Anthropic: ~1000 RPM on paid tier
    "gpt": 200,      # OpenAI: 500 RPM on paid tier
    "grok": 60,      # xAI: lower tier, reasoning model is slow anyway
    "perplexity": 60, # Perplexity: moderate tier
}

TIMEOUT = 45


def get_api_stats() -> Dict:
    _roll_usage_day_if_needed()
    return dict(_api_calls)


def get_api_cost_stats() -> Dict:
    _roll_usage_day_if_needed()
    per_provider = {}
    total_cost = 0.0
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_reasoning_tokens = 0
    total_calls = 0
    for provider in _api_calls:
        usage = _api_token_usage.get(provider, {})
        provider_cost = float(usage.get("cost_usd", 0.0) or 0.0)
        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
        reasoning_tokens = int(usage.get("reasoning_tokens", 0) or 0)
        calls = int(_api_calls.get(provider, 0) or 0)
        total_cost += provider_cost
        total_prompt_tokens += prompt_tokens
        total_completion_tokens += completion_tokens
        total_reasoning_tokens += reasoning_tokens
        total_calls += calls
        per_provider[provider] = {
            "calls": calls,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "reasoning_tokens": reasoning_tokens,
            "estimated_cost_usd": round(provider_cost, 6),
        }
    return {
        "day": _api_usage_day or _current_cost_day(),
        "total_calls": total_calls,
        "prompt_tokens": total_prompt_tokens,
        "completion_tokens": total_completion_tokens,
        "reasoning_tokens": total_reasoning_tokens,
        "estimated_cost_usd": round(total_cost, 6),
        "per_provider": per_provider,
    }


def _current_cost_day() -> str:
    from datetime import datetime

    try:
        import zoneinfo

        return datetime.now(zoneinfo.ZoneInfo("America/Chicago")).strftime("%Y-%m-%d")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d")


def _roll_usage_day_if_needed():
    global _api_usage_day
    current = _current_cost_day()
    if _api_usage_day == current:
        return
    _api_usage_day = current
    for provider in _api_calls:
        _api_calls[provider] = 0
        usage = _api_token_usage.setdefault(
            provider,
            {"prompt_tokens": 0.0, "completion_tokens": 0.0, "reasoning_tokens": 0.0, "cost_usd": 0.0},
        )
        usage["prompt_tokens"] = 0.0
        usage["completion_tokens"] = 0.0
        usage["reasoning_tokens"] = 0.0
        usage["cost_usd"] = 0.0


def _provider_cost_config(provider: str) -> Dict[str, float]:
    if provider == "claude":
        return {
            "input_per_mtok": float(getattr(settings, "CLAUDE_INPUT_COST_PER_MTOK", 0.0) or 0.0),
            "output_per_mtok": float(getattr(settings, "CLAUDE_OUTPUT_COST_PER_MTOK", 0.0) or 0.0),
            "request_cost_usd": 0.0,
        }
    if provider == "gpt":
        return {
            "input_per_mtok": float(getattr(settings, "OPENAI_INPUT_COST_PER_MTOK", 0.0) or 0.0),
            "output_per_mtok": float(getattr(settings, "OPENAI_OUTPUT_COST_PER_MTOK", 0.0) or 0.0),
            "request_cost_usd": 0.0,
        }
    if provider == "grok":
        return {
            "input_per_mtok": float(getattr(settings, "XAI_INPUT_COST_PER_MTOK", 0.0) or 0.0),
            "output_per_mtok": float(getattr(settings, "XAI_OUTPUT_COST_PER_MTOK", 0.0) or 0.0),
            "request_cost_usd": 0.0,
        }
    if provider == "perplexity":
        return {
            "input_per_mtok": float(getattr(settings, "PERPLEXITY_INPUT_COST_PER_MTOK", 0.0) or 0.0),
            "output_per_mtok": float(getattr(settings, "PERPLEXITY_OUTPUT_COST_PER_MTOK", 0.0) or 0.0),
            "request_cost_usd": float(getattr(settings, "PERPLEXITY_REQUEST_COST_USD", 0.0) or 0.0),
        }
    return {"input_per_mtok": 0.0, "output_per_mtok": 0.0, "request_cost_usd": 0.0}


def _extract_usage_metrics(provider: str, payload: Dict) -> Dict[str, float]:
    usage = payload.get("usage") or {}
    prompt_tokens = 0.0
    completion_tokens = 0.0
    reasoning_tokens = 0.0
    exact_cost_usd = 0.0

    if provider == "claude":
        prompt_tokens = float(usage.get("input_tokens", 0) or 0)
        completion_tokens = float(usage.get("output_tokens", 0) or 0)
        cache_creation = float(usage.get("cache_creation_input_tokens", 0) or 0)
        cache_read = float(usage.get("cache_read_input_tokens", 0) or 0)
        prompt_tokens += cache_creation + cache_read
    else:
        prompt_tokens = float(usage.get("prompt_tokens", usage.get("input_tokens", 0)) or 0)
        completion_tokens = float(usage.get("completion_tokens", usage.get("output_tokens", 0)) or 0)

    completion_details = usage.get("completion_tokens_details") or {}
    reasoning_tokens = float(
        completion_details.get("reasoning_tokens", usage.get("reasoning_tokens", 0)) or 0
    )

    if provider == "grok":
        cost_ticks = usage.get("cost_in_usd_ticks")
        if cost_ticks is not None:
            try:
                exact_cost_usd = float(cost_ticks) / 1_000_000.0
            except Exception:
                exact_cost_usd = 0.0

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "reasoning_tokens": reasoning_tokens,
        "exact_cost_usd": exact_cost_usd,
    }


def _record_api_usage(provider: str, payload: Dict):
    _roll_usage_day_if_needed()
    usage_metrics = _extract_usage_metrics(provider, payload)
    usage = _api_token_usage.setdefault(
        provider,
        {"prompt_tokens": 0.0, "completion_tokens": 0.0, "reasoning_tokens": 0.0, "cost_usd": 0.0},
    )
    usage["prompt_tokens"] += usage_metrics["prompt_tokens"]
    usage["completion_tokens"] += usage_metrics["completion_tokens"]
    usage["reasoning_tokens"] += usage_metrics["reasoning_tokens"]

    exact_cost_usd = float(usage_metrics.get("exact_cost_usd", 0.0) or 0.0)
    if exact_cost_usd > 0:
        usage["cost_usd"] += exact_cost_usd
        return

    config = _provider_cost_config(provider)
    estimated_cost = (
        usage_metrics["prompt_tokens"] / 1_000_000.0 * config["input_per_mtok"]
        + usage_metrics["completion_tokens"] / 1_000_000.0 * config["output_per_mtok"]
        + config["request_cost_usd"]
    )
    usage["cost_usd"] += estimated_cost


def provider_is_backing_off(provider: str, now: Optional[float] = None) -> bool:
    if now is None:
        now = time.time()
    return float(_provider_backoff_until.get(provider, 0.0) or 0.0) > now


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
            payload = resp.json()
            _roll_usage_day_if_needed()
            _api_calls["claude"] += 1
            _record_api_usage("claude", payload)
            text = payload["content"][0]["text"]
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
            payload = resp.json()
            _roll_usage_day_if_needed()
            _api_calls["claude"] += 1
            _record_api_usage("claude", payload)
            return str(payload["content"][0]["text"]).strip()
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
            payload = resp.json()
            _roll_usage_day_if_needed()
            _api_calls["gpt"] += 1
            _record_api_usage("gpt", payload)
            text = payload["choices"][0]["message"]["content"]
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
            payload = resp.json()
            _roll_usage_day_if_needed()
            _api_calls["gpt"] += 1
            _record_api_usage("gpt", payload)
            return str(payload["choices"][0]["message"]["content"]).strip()
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
            payload = resp.json()
            _roll_usage_day_if_needed()
            _api_calls["grok"] += 1
            _record_api_usage("grok", payload)
            text = payload["choices"][0]["message"]["content"]
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
            payload = resp.json()
            _roll_usage_day_if_needed()
            _api_calls["perplexity"] += 1
            _record_api_usage("perplexity", payload)
            text = payload["choices"][0]["message"]["content"]
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
            payload = resp.json()
            _roll_usage_day_if_needed()
            _api_calls["perplexity"] += 1
            _record_api_usage("perplexity", payload)
            return str(payload["choices"][0]["message"]["content"]).strip()
    except Exception as e:
        logger.error(f"Perplexity API error: {e}")
        return None
