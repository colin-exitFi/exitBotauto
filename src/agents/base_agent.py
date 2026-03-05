"""
Base Agent - Common infrastructure for all specialized agents.
Handles AI calls, JSON parsing, error handling, rate limiting.
"""

import json
import time
from typing import Dict, Optional
from loguru import logger
import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings


# ── Shared rate limiter across all agents ─────────────────────────
_call_timestamps: list = []
_api_calls: Dict[str, int] = {"claude": 0, "gpt": 0, "grok": 0, "perplexity": 0}

TIMEOUT = 45


def get_api_stats() -> Dict:
    return dict(_api_calls)


def _check_rate_limit() -> bool:
    """Returns True if we're under the rate limit."""
    from datetime import datetime
    try:
        import zoneinfo
        _et_hour = datetime.now(zoneinfo.ZoneInfo("US/Eastern")).hour
    except Exception:
        _et_hour = 12
    default_limit = 200 if 4 <= _et_hour < 20 else 30
    max_per_hour = getattr(settings, 'CONSENSUS_MAX_CALLS_PER_HOUR', default_limit)
    now = time.time()
    _call_timestamps[:] = [t for t in _call_timestamps if now - t < 3600]
    if len(_call_timestamps) >= max_per_hour:
        return False
    _call_timestamps.append(now)
    return True


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
    if not _check_rate_limit():
        logger.warning("Rate limit reached — skipping Claude call")
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


async def call_gpt(prompt: str, max_tokens: int = 600) -> Optional[Dict]:
    """Call GPT-5.2 and return parsed JSON."""
    if not settings.OPENAI_API_KEY:
        return None
    if not _check_rate_limit():
        logger.warning("Rate limit reached — skipping GPT call")
        return None
    model = getattr(settings, 'OPENAI_MODEL', 'gpt-5.2')
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


async def call_grok(prompt: str, max_tokens: int = 600) -> Optional[Dict]:
    """Call Grok-4 via xAI and return parsed JSON."""
    if not settings.XAI_API_KEY:
        return None
    if not _check_rate_limit():
        logger.warning("Rate limit reached — skipping Grok call")
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
    if not _check_rate_limit():
        logger.warning("Rate limit reached — skipping Perplexity call")
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
