# xAI / Grok API Reference

Base URL: `https://api.x.ai`
Docs: `https://docs.x.ai/overview`

## Authentication

```
Authorization: Bearer {XAI_API_KEY}
Content-Type: application/json
```

Setting: `XAI_API_KEY` in `.env` and `config/settings.py`

## API Format

OpenAI-compatible chat completions. Same request/response shape as OpenAI.

### Chat Completions

```
POST /v1/chat/completions
```

Request body:
```json
{
  "model": "grok-4-fast-reasoning",
  "max_tokens": 600,
  "temperature": 0.3,
  "messages": [{"role": "user", "content": "..."}]
}
```

Response body:
```json
{
  "choices": [{"message": {"content": "..."}}]
}
```

## Models Used by Velox

- `grok-4-fast-reasoning` -- default for sentiment agent (has native X/Twitter data access)
- Configurable via `XAI_MODEL` setting

## Velox Integration

- Used by: Sentiment Agent (`src/agents/sentiment_agent.py`) via `call_grok()` in `base_agent.py`
- Also in jury fallback chain: Claude -> GPT -> Grok
- Rate limit: 60/hour (conservative, actual tier is higher)

## Key Advantage

Grok has native access to X/Twitter data that Claude and GPT do not. This makes it the right choice for the sentiment agent, which evaluates social momentum and retail crowd direction.

## Rate Limits

- Velox internal limit: 60 calls/hour (configurable in `base_agent.py`)
- After-hours reduction to 30/hour
- Exponential backoff on rate limit hit (2s, 4s, 8s, max 30s)
