# Jury Consensus Fix — URGENT

## Problem

The jury in `src/agents/jury.py` is implemented as a **fallback chain** (Claude → GPT → Grok), not a **consensus vote**. Only one model runs per candidate. This defeats the entire purpose of multi-AI consensus.

## Current Behavior (Wrong)

```python
provider_chain = [
    ("claude", call_claude),
    ("gpt", call_gpt),
    ("grok", call_grok),
]
for provider_name, caller in provider_chain:
    result = await caller(prompt, max_tokens=400)
    if result and "decision" in result:
        provider_used = provider_name
        break  # <-- First success wins, other models never called
```

Claude answers ~95% of the time. GPT and Grok almost never run.

## Desired Behavior (Consensus)

All three models vote in parallel. Require 2/3 agreement to act.

### Decision Logic

1. Call Claude, GPT, and Grok **concurrently** (`asyncio.gather`)
2. Collect all responses that return valid decisions
3. Apply consensus rules:

| Claude | GPT | Grok | Result |
|--------|-----|------|--------|
| BUY | BUY | BUY | **BUY** (unanimous, full size) |
| BUY | BUY | SKIP | **BUY** (2/3, full size) |
| BUY | BUY | SHORT | **BUY** (2/3, 75% size — conflicting signal) |
| BUY | SKIP | SKIP | **SKIP** (only 1/3 agree) |
| BUY | SHORT | SKIP | **SKIP** (no consensus) |
| SHORT | SHORT | SHORT | **SHORT** (unanimous, full size) |
| SHORT | SHORT | SKIP | **SHORT** (2/3, full size) |
| SHORT | SHORT | BUY | **SHORT** (2/3, 75% size) |
| SHORT | SKIP | SKIP | **SKIP** (only 1/3) |
| SKIP | SKIP | SKIP | **SKIP** (unanimous) |
| BUY | SHORT | * | **SKIP** (directly opposed) |

Simplified rules:
- **Unanimous (3/3):** Full size, highest confidence
- **Majority (2/3) same direction:** Full size
- **Majority (2/3) but one opposes (BUY vs SHORT):** 75% size (conflict discount)
- **Only 1 model agrees or split 3 ways:** SKIP
- **Only 1 model responds (others failed):** Use that model's decision but at 50% size
- **Only 2 models respond, they agree:** Full size
- **Only 2 models respond, they disagree:** SKIP
- **Zero models respond:** SKIP with error

### Size Modifier

The jury already returns `size_pct` in its verdict. Apply a consensus modifier:
- Unanimous agreement: `size_pct * 1.0`
- 2/3 majority, no opposition: `size_pct * 1.0`
- 2/3 majority, one opposes: `size_pct * 0.75`
- Single model fallback: `size_pct * 0.50`

### Trail Modifier

Average the `trail_pct` recommendations from agreeing models. If only one model, use its trail.

### Confidence Scoring

Add a `consensus_confidence` field to `JuryVerdict`:
- 3/3 agree: `confidence = avg(individual_confidences) * 1.0`
- 2/3 agree: `confidence = avg(agreeing_confidences) * 0.85`
- 1/3 fallback: `confidence = individual_confidence * 0.60`

## Implementation

### File: `src/agents/jury.py`

Replace the sequential fallback chain with:

```python
import asyncio

async def deliberate(symbol, price, briefs, signals_data=None):
    # ... existing prompt building code stays the same ...

    # Call all three models concurrently
    results = await asyncio.gather(
        _safe_call("claude", call_claude, prompt),
        _safe_call("gpt", call_gpt, prompt),
        _safe_call("grok", call_grok, prompt),
        return_exceptions=True,
    )

    # Collect valid votes
    votes = []
    for provider_name, result in zip(["claude", "gpt", "grok"], results):
        if isinstance(result, Exception) or result is None:
            continue
        if isinstance(result, dict) and "decision" in result:
            votes.append({"provider": provider_name, "result": result})

    # Apply consensus logic
    verdict = _apply_consensus(symbol, price, votes, briefs)
    return verdict


async def _safe_call(provider_name, caller, prompt):
    """Wrap a model call so exceptions don't kill the gather."""
    try:
        result = await caller(prompt, max_tokens=400)
        return result
    except Exception as e:
        logger.warning(f"Jury {provider_name} failed: {e}")
        return None


def _apply_consensus(symbol, price, votes, briefs):
    """
    Apply 2/3 consensus rules to collected votes.
    Returns JuryVerdict.
    """
    if not votes:
        return JuryVerdict(
            symbol=symbol, decision="SKIP", size_pct=0, trail_pct=3.0,
            reasoning="All jury models failed",
            provider_used="none", briefs=briefs,
        )

    # Count decisions
    decisions = [v["result"].get("decision", "SKIP").upper() for v in votes]
    buy_votes = [v for v, d in zip(votes, decisions) if d == "BUY"]
    short_votes = [v for v, d in zip(votes, decisions) if d == "SHORT"]
    skip_votes = [v for v, d in zip(votes, decisions) if d == "SKIP"]

    total = len(votes)
    providers_used = [v["provider"] for v in votes]

    # Determine consensus
    # ... implement the decision table above ...
    # ... compute size_modifier, trail_pct (averaged), confidence ...

    return JuryVerdict(
        symbol=symbol,
        decision=consensus_decision,
        size_pct=consensus_size,
        trail_pct=consensus_trail,
        reasoning=consensus_reasoning,  # Include each model's reasoning
        provider_used=",".join(providers_used),
        briefs=briefs,
        consensus_detail={  # NEW field
            "votes": {v["provider"]: d for v, d in zip(votes, decisions)},
            "total_models": total,
            "agreement": agreement_level,  # "unanimous", "majority", "single", "none"
            "size_modifier": size_modifier,
            "confidence": consensus_confidence,
        },
    )
```

### File: `src/agents/jury.py` — JuryVerdict dataclass

Add `consensus_detail` field:

```python
@dataclass
class JuryVerdict:
    symbol: str
    decision: str
    size_pct: float
    trail_pct: float
    reasoning: str
    provider_used: str = ""
    briefs: Dict = field(default_factory=dict)
    consensus_detail: Dict = field(default_factory=dict)  # NEW
```

### Rate Limiting

Currently we self-limit to 20 consensus calls/hour. With 3 models per call, that's 60 API calls/hour. Check that `_PROVIDER_LIMITS` in `base_agent.py` can handle this:

```python
_PROVIDER_LIMITS = {
    "claude": 60,   # Currently 60/hr — fine
    "gpt": 60,      # Currently 60/hr — fine
    "grok": 30,     # Currently 30/hr — may need bump to 60
    "perplexity": 20,
}
```

If Grok is at 30/hr limit and we're doing 20 consensus calls/hr, that's tight. Bump Grok to 60.

### Logging

Log every consensus decision with full detail:

```
🗳️ AAPL Jury: Claude=BUY, GPT=BUY, Grok=SKIP → BUY (2/3 majority, full size)
🗳️ NVDA Jury: Claude=SHORT, GPT=BUY, Grok=SHORT → SHORT (2/3, 75% size — conflict)
🗳️ TSLA Jury: Claude=BUY, GPT=SKIP, Grok=SKIP → SKIP (1/3, insufficient consensus)
```

### Dashboard

Update the dashboard's last consensus display to show all three votes, not just the single provider.

## Testing

### `tests/test_jury_consensus.py`

- Test unanimous BUY (3/3) → BUY at full size
- Test majority BUY (2/3, one SKIP) → BUY at full size
- Test majority BUY (2/3, one SHORT) → BUY at 75% size
- Test single BUY (1/3) → SKIP
- Test split (BUY/SHORT/SKIP) → SKIP
- Test all SKIP → SKIP
- Test only 1 model responds → use at 50% size
- Test only 2 respond, agree → full size
- Test only 2 respond, disagree → SKIP
- Test 0 respond → SKIP with error
- Test trail_pct is averaged across agreeing models
- Test confidence calculation
- Test consensus_detail is populated in verdict
- Test risk agent override still works after consensus

## Constraints

- **Do NOT remove Perplexity from base_agent.py.** It's used elsewhere (macro research, etc.). Just don't add it to the jury vote — three models is enough.
- **asyncio.gather with return_exceptions=True** — one model failing must not crash the others.
- **Rate limit backoff still applies per-model.** If Claude is rate-limited, the gather will still get GPT + Grok results (2 models is enough for consensus).
- **All existing jury tests must still pass** or be updated to account for the new consensus structure.
- **Keep the consensus call budget at ~20/hour.** That means 60 model calls/hour total across three providers.
