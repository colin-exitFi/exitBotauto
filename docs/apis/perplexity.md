# Perplexity API Reference

Base URL: `https://api.perplexity.ai`
Docs: `https://docs.perplexity.ai`

## Authentication

```
Authorization: Bearer {PERPLEXITY_API_KEY}
Content-Type: application/json
```

Setting: `PERPLEXITY_API_KEY` in `.env` and `config/settings.py`

## Endpoints Used by Velox

### Chat Completions

```
POST /chat/completions
```

Request body:
```json
{
  "model": "sonar-pro",
  "max_tokens": 600,
  "messages": [{"role": "user", "content": "..."}]
}
```

Response body (OpenAI-compatible):
```json
{
  "choices": [{"message": {"content": "..."}}]
}
```

## Models Used by Velox

- `sonar-pro` -- default for Catalyst Agent, overnight thesis, news scanning
- Configurable via `PERPLEXITY_MODEL` setting

## Velox Integration

Perplexity is used for real-time web search capabilities that other LLMs lack:
- Catalyst Agent (`src/agents/catalyst_agent.py`) via `call_perplexity()` in `base_agent.py`
- Overnight thesis building (`src/main.py` `_build_overnight_thesis()`)
- Overnight news scanning (`src/main.py` `_scan_overnight_news()`)
- Scanner news lookup (`src/scanner/scanner.py` `_get_news()`)

## Key Advantage

Perplexity has live web search built into its response generation. When asked "what news is moving AAPL today?", it searches the web in real-time. Claude, GPT, and Grok cannot do this.

## Rate Limits

- Velox internal limit: 60 calls/hour
- After-hours reduction to 30/hour
- Exponential backoff on rate limit hit
- Day 1 production: 903 rate limit skips

## Production Gotchas

- API format is OpenAI-compatible (same request/response shape)
- The `sonar-pro` model includes web search by default -- no special parameter needed
- Perplexity responses can include citations and source URLs in the text; the `parse_json()` function strips these when extracting JSON
