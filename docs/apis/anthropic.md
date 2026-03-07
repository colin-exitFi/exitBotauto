# Anthropic / Claude API Reference

Base URL: `https://api.anthropic.com`
Docs: `https://docs.anthropic.com`

## Authentication

```
x-api-key: {ANTHROPIC_API_KEY}
anthropic-version: 2023-06-01
content-type: application/json
```

Setting: `ANTHROPIC_API_KEY` in `.env` and `config/settings.py`

## Endpoints Used by Velox

### Messages

```
POST /v1/messages
```

Request body:
```json
{
  "model": "claude-sonnet-4-5-20250929",
  "max_tokens": 600,
  "messages": [{"role": "user", "content": "..."}],
  "system": "optional system prompt"
}
```

Response body:
```json
{
  "content": [{"type": "text", "text": "..."}]
}
```

Note: Response is `content[0].text`, NOT `choices[0].message.content` (different from OpenAI format).

## Models Used by Velox

- `claude-sonnet-4-5-20250929` -- default for Technical Agent, Jury, Observer, Advisor, Tuner, Game Film, Position Manager, Exit Agent
- Configurable via `CLAUDE_MODEL` setting

## Velox Integration

Claude is the primary AI provider across the system:
- Technical Agent (`src/agents/technical_agent.py`)
- Jury first choice (`src/agents/jury.py`) in the fallback chain: Claude -> GPT -> Grok
- Observer, Advisor, Tuner, Game Film, Position Manager (`src/ai/*.py`)
- Exit Agent (`src/agents/exit_agent.py`)

All calls go through `call_claude()` in `src/agents/base_agent.py`.

## Rate Limits

- Velox internal limit: 200 calls/hour during market hours
- After-hours reduction to 30/hour
- Exponential backoff on rate limit hit (2s, 4s, 8s, max 30s)
- Day 1 production: 2,781 rate limit skips -- indicates the 200/hour limit is too low for active scanning

## Production Gotchas

- The `anthropic-version` header is required. Without it, the API returns 400.
- Response format differs from OpenAI: `content[0].text` vs `choices[0].message.content`
- Claude is the most expensive provider per call. When rate-limited, the jury falls back to GPT then Grok.
