# OpenAI / GPT API Reference

Base URL: `https://api.openai.com`
Docs: `https://platform.openai.com/docs`

## Authentication

```
Authorization: Bearer {OPENAI_API_KEY}
Content-Type: application/json
```

Setting: `OPENAI_API_KEY` in `.env` and `config/settings.py`

## Endpoints Used by Velox

### Chat Completions

```
POST /v1/chat/completions
```

Request body:
```json
{
  "model": "gpt-5.4",
  "max_completion_tokens": 600,
  "messages": [{"role": "user", "content": "..."}]
}
```

Response body:
```json
{
  "choices": [{"message": {"content": "..."}}]
}
```

Note: Uses `max_completion_tokens` (not `max_tokens`) per OpenAI's current API.

## Models Used by Velox

- `gpt-5.4` -- default for Risk Agent, Macro Agent
- Configurable via `OPENAI_MODEL` setting

## Velox Integration

- Risk Agent (`src/agents/risk_agent.py`) via `call_gpt()` in `base_agent.py`
- Macro Agent (`src/agents/macro_agent.py`) via `call_gpt()`
- Jury fallback chain position 2: Claude -> GPT -> Grok

## Rate Limits

- Velox internal limit: 200 calls/hour during market hours
- After-hours reduction to 30/hour
- Exponential backoff on rate limit hit
- Day 1 production: 1,934 rate limit skips

## Production Gotchas

- Use `max_completion_tokens` not `max_tokens` for newer models
- GPT is the second choice in the jury fallback chain (after Claude)
- When both Claude and GPT are rate-limited, Grok handles jury decisions alone
