"""
Consensus Engine - Multi-AI Jury System.
Claude + GPT must both agree on direction (BUY or SHORT). Perplexity breaks ties.
"""

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from loguru import logger

import httpx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings

# ── Data Classes ──────────────────────────────────────────────────

@dataclass
class ModelVote:
    model: str
    decision: str  # "BUY", "SHORT", or "SKIP"
    confidence: int  # 0-100
    reasoning: str = ""
    target_price: Optional[float] = None
    stop_price: Optional[float] = None
    error: Optional[str] = None


@dataclass
class ConsensusResult:
    symbol: str
    final_decision: str  # "BUY", "SHORT", or "SKIP"
    size_modifier: float  # 1.0 = full, 0.75 = reduced, 0.0 = no trade
    avg_confidence: float
    claude_vote: Optional[ModelVote] = None
    gpt_vote: Optional[ModelVote] = None
    perplexity_vote: Optional[ModelVote] = None
    reasoning: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        def _vote(v):
            if not v:
                return None
            return {"model": v.model, "decision": v.decision, "confidence": v.confidence,
                    "reasoning": v.reasoning[:200], "error": v.error}
        return {
            "symbol": self.symbol, "final_decision": self.final_decision,
            "size_modifier": self.size_modifier, "avg_confidence": self.avg_confidence,
            "claude": _vote(self.claude_vote), "gpt": _vote(self.gpt_vote),
            "perplexity": _vote(self.perplexity_vote), "reasoning": self.reasoning,
            "timestamp": self.timestamp,
        }


# ── Prompt ────────────────────────────────────────────────────────

ANALYSIS_PROMPT = """You are a SHORT-TERM MOMENTUM TRADER inside Velox, an autonomous trading engine.
Mission: grow $25K to $5M through capital velocity. This portfolio represents a family's financial future.
Every position has a 3% trailing stop — our downside is capped. Dead capital is the real enemy.
Be AGGRESSIVE on entries when momentum is clear. The trailing stop handles risk — your job is to find runners.

Your job is to find stocks likely to move 2-5% in the next few hours — UP or DOWN.

CRITICAL CONTEXT: We have a TRAILING STOP at 3% protecting every position. If we're wrong, we lose 3% max. If we're right and it runs 20%, the trailing stop locks in the profit. Our ONLY job is to pick stocks more likely to go UP than DOWN in the near term. We are NOT evaluating this as a long-term investment.

CURRENT DATE/TIME: {current_datetime}

SYMBOL: {symbol}
CURRENT PRICE: ${price:.2f}
TODAY'S CHANGE: {change_pct:+.2f}%

VOLUME: {volume_spike:.1f}x average (higher = more momentum)

SOCIAL SENTIMENT:
- StockTwits: {sentiment_score:.2f} (range -1 to +1, positive = bullish crowd)
- Trending on StockTwits: {trending}
- Twitter/X buzz: {twitter_volume}

TECHNICALS:
- RSI: {rsi}
- vs VWAP: {vwap_relation}
- ATR: {atr}

NEWS: {news}

DECISION FRAMEWORK:
- BUY if: momentum is strong, sentiment is positive, volume is elevated, stock has room to run
- BUY if: social buzz is high and price is still moving up (momentum intact)
- BUY if: news catalyst + volume spike (early stage of a move)
- SHORT if: stock ran 30%+ yesterday and is showing weakness/profit-taking today (fade the runner)
- SHORT if: sentiment is turning very bearish, volume is spiking on the downside, breaking key support
- SHORT if: bad news catalyst (earnings miss, FDA rejection, SEC investigation) with heavy selling
- SKIP ONLY if: no clear direction, low volume, or mixed signals that don't favor either side
- DO NOT skip just because a stock already moved today — momentum stocks keep running
- DO NOT evaluate as a long-term hold — we're in and out within hours
- Remember: 3% trailing stop protects us. The question is NOT "is this safe?" but "is this likely to go higher?"

Respond with ONLY valid JSON (no markdown):
{{"decision": "BUY" or "SHORT" or "SKIP", "confidence": 0-100, "reasoning": "brief explanation", "target_price": number or null, "stop_price": number or null}}"""

PERPLEXITY_PROMPT = """Search for the latest news about {symbol} ({company}) in the last 4 hours.

We're a SHORT-TERM MOMENTUM TRADER deciding whether to BUY (long) or SHORT this stock for a few hours, with a trailing stop protecting us.

The question is simple: Based on current news, is this stock more likely to go UP or DOWN in the next few hours?

- If news is positive, neutral, or there's a catalyst driving the move UP → BUY
- If news reveals fraud, SEC investigation, earnings miss, FDA rejection → SHORT (profit from the drop)
- If stock ran huge yesterday and is fading today → SHORT (fade the runner)
- If no news found, default to BUY if the stock has upward momentum (we have a trailing stop)
- SKIP only if truly no clear direction

Respond with ONLY valid JSON (no markdown):
{{"decision": "BUY" or "SHORT" or "SKIP", "confidence": 0-100, "reasoning": "what the latest news says"}}"""


# ── Engine ────────────────────────────────────────────────────────

class ConsensusEngine:
    """Multi-AI jury: Claude + GPT must agree. Perplexity breaks ties."""

    TIMEOUT = 10  # seconds per model call

    def __init__(self):
        self._cache: Dict[str, ConsensusResult] = {}  # symbol -> result
        self._call_timestamps: List[float] = []
        self._history: List[Dict] = []  # last N decisions for dashboard
        self._api_calls = {"claude": 0, "gpt": 0, "perplexity": 0}

    # ── Public API ────────────────────────────────────────────────

    async def evaluate(self, symbol: str, price: float, signals_data: Dict) -> ConsensusResult:
        """Run consensus evaluation. Returns cached result if fresh."""
        if not getattr(settings, 'CONSENSUS_ENABLED', True):
            return ConsensusResult(symbol=symbol, final_decision="BUY",
                                   size_modifier=1.0, avg_confidence=100,
                                   reasoning="Consensus disabled")

        # Check cache
        cached = self._cache.get(symbol)
        # SKIP decisions expire fast (90s) so we re-evaluate with fresh data
        # BUY/SHORT decisions cache longer (5 min) since they're actionable
        base_ttl = getattr(settings, 'CONSENSUS_CACHE_SECONDS', 300)
        if cached:
            ttl = 90 if cached.final_decision == "SKIP" else base_ttl
            if (time.time() - cached.timestamp) < ttl:
                logger.debug(f"Consensus cache hit for {symbol}")
                return cached

        # Rate limit — higher during market hours (4AM-8PM ET)
        from datetime import datetime as _dt
        try:
            import zoneinfo
            _et_hour = _dt.now(zoneinfo.ZoneInfo("US/Eastern")).hour
        except Exception:
            _et_hour = 12
        default_limit = 60 if 4 <= _et_hour < 20 else 20
        max_per_hour = getattr(settings, 'CONSENSUS_MAX_CALLS_PER_HOUR', default_limit)
        now = time.time()
        self._call_timestamps = [t for t in self._call_timestamps if now - t < 3600]
        if len(self._call_timestamps) >= max_per_hour:
            logger.warning(f"Consensus rate limit reached ({max_per_hour}/hr)")
            return ConsensusResult(symbol=symbol, final_decision="SKIP",
                                   size_modifier=0.0, avg_confidence=0,
                                   reasoning="Rate limit reached")
        self._call_timestamps.append(now)

        # Build prompt data
        prompt = self._build_prompt(symbol, price, signals_data)

        # Run Claude + GPT in parallel
        claude_task = self._call_claude(prompt)
        gpt_task = self._call_gpt(prompt)
        claude_vote, gpt_vote = await asyncio.gather(claude_task, gpt_task)

        # Consensus logic
        result = await self._resolve(symbol, price, signals_data, claude_vote, gpt_vote)

        # Cache & record
        self._cache[symbol] = result
        self._history.append(result.to_dict())
        self._history = self._history[-50:]

        logger.info(
            f"🗳️ Consensus for {symbol}: {result.final_decision} "
            f"(Claude={claude_vote.decision if not claude_vote.error else 'ERR'}, "
            f"GPT={gpt_vote.decision if not gpt_vote.error else 'ERR'}"
            f"{', Perplexity=' + result.perplexity_vote.decision if result.perplexity_vote else ''}) "
            f"conf={result.avg_confidence:.0f}% size={result.size_modifier:.0%}"
        )
        return result

    def get_history(self) -> List[Dict]:
        return list(self._history)

    def get_stats(self) -> Dict:
        if not self._history:
            return {"total": 0, "agreement_rate": 0, "api_calls": self._api_calls}
        agreements = sum(1 for h in self._history if h.get("perplexity") is None
                        and h["final_decision"] != "SKIP")
        total = len(self._history)
        # Count how often Claude+GPT agreed (no tie-breaker needed)
        no_tiebreak = sum(1 for h in self._history if h.get("perplexity") is None)
        return {
            "total": total,
            "agreement_rate": no_tiebreak / total if total else 0,
            "buys": sum(1 for h in self._history if h["final_decision"] == "BUY"),
            "skips": sum(1 for h in self._history if h["final_decision"] == "SKIP"),
            "api_calls": self._api_calls,
            "estimated_cost": round(
                self._api_calls["claude"] * 0.003 +
                self._api_calls["gpt"] * 0.005 +
                self._api_calls["perplexity"] * 0.005, 2
            ),
        }

    # ── Prompt Builder ────────────────────────────────────────────

    def _build_prompt(self, symbol: str, price: float, signals: Dict) -> str:
        news_list = signals.get("news_headlines", signals.get("news", []))
        if isinstance(news_list, list):
            news_str = "\n".join(f"- {h}" for h in news_list[:5]) or "No recent news available"
        else:
            news_str = str(news_list) or "No recent news available"

        from datetime import datetime
        try:
            import zoneinfo
            now_et = datetime.now(zoneinfo.ZoneInfo("US/Eastern")).strftime("%Y-%m-%d %H:%M ET (%A)")
        except Exception:
            now_et = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")

        return ANALYSIS_PROMPT.format(
            current_datetime=now_et,
            symbol=symbol,
            price=price,
            change_pct=signals.get("change_pct", 0),
            volume_spike=signals.get("volume_spike", 1.0),
            sentiment_score=signals.get("sentiment_score", 0),
            trending=signals.get("trending", "unknown"),
            twitter_volume=signals.get("twitter_volume", "unknown"),
            rsi=signals.get("rsi", "N/A"),
            vwap_relation=signals.get("vwap_relation", "N/A"),
            atr=signals.get("atr", "N/A"),
            news=news_str,
        )

    # ── Model Calls ───────────────────────────────────────────────

    async def _call_claude(self, prompt: str) -> ModelVote:
        if not settings.ANTHROPIC_API_KEY:
            return ModelVote(model="claude", decision="SKIP", confidence=0, error="No API key")

        model = getattr(settings, 'CLAUDE_MODEL', 'claude-sonnet-4-5-20250929')
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                resp = await client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": settings.ANTHROPIC_API_KEY,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                self._api_calls["claude"] += 1
                text = resp.json()["content"][0]["text"]
                data = _parse_json(text)
                return ModelVote(
                    model="claude",
                    decision=data.get("decision", "SKIP").upper(),
                    confidence=int(data.get("confidence", 0)),
                    reasoning=data.get("reasoning", ""),
                    target_price=data.get("target_price"),
                    stop_price=data.get("stop_price"),
                )
        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return ModelVote(model="claude", decision="SKIP", confidence=0, error=str(e))

    async def _call_gpt(self, prompt: str) -> ModelVote:
        if not settings.OPENAI_API_KEY:
            return ModelVote(model="gpt", decision="SKIP", confidence=0, error="No API key")

        model = getattr(settings, 'OPENAI_MODEL', 'gpt-4o')
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                self._api_calls["gpt"] += 1
                text = resp.json()["choices"][0]["message"]["content"]
                data = _parse_json(text)
                return ModelVote(
                    model="gpt",
                    decision=data.get("decision", "SKIP").upper(),
                    confidence=int(data.get("confidence", 0)),
                    reasoning=data.get("reasoning", ""),
                    target_price=data.get("target_price"),
                    stop_price=data.get("stop_price"),
                )
        except Exception as e:
            logger.error(f"GPT API error: {e}")
            return ModelVote(model="gpt", decision="SKIP", confidence=0, error=str(e))

    async def _call_perplexity(self, symbol: str, signals: Dict) -> ModelVote:
        if not settings.PERPLEXITY_API_KEY:
            return ModelVote(model="perplexity", decision="SKIP", confidence=0, error="No API key")

        model = getattr(settings, 'PERPLEXITY_MODEL', 'sonar-pro')
        prompt = PERPLEXITY_PROMPT.format(
            symbol=symbol,
            company=signals.get("company_name", symbol),
        )
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                resp = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.PERPLEXITY_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 500,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                self._api_calls["perplexity"] += 1
                text = resp.json()["choices"][0]["message"]["content"]
                data = _parse_json(text)
                return ModelVote(
                    model="perplexity",
                    decision=data.get("decision", "SKIP").upper(),
                    confidence=int(data.get("confidence", 0)),
                    reasoning=data.get("reasoning", ""),
                )
        except Exception as e:
            logger.error(f"Perplexity API error: {e}")
            return ModelVote(model="perplexity", decision="SKIP", confidence=0, error=str(e))

    # ── Consensus Resolution ──────────────────────────────────────

    async def _resolve(self, symbol: str, price: float, signals: Dict,
                       claude: ModelVote, gpt: ModelVote) -> ConsensusResult:
        claude_ok = claude.error is None
        gpt_ok = gpt.error is None

        # Both failed → NO TRADE
        if not claude_ok and not gpt_ok:
            return ConsensusResult(
                symbol=symbol, final_decision="SKIP", size_modifier=0.0,
                avg_confidence=0, claude_vote=claude, gpt_vote=gpt,
                reasoning="Both AI models failed — never trade blind")

        # One failed → use the other (slight size reduction, but trust the working model)
        if not claude_ok:
            conf = gpt.confidence * 0.85  # slight penalty for single-model
            buy = gpt.decision == "BUY" and conf >= 40
            return ConsensusResult(
                symbol=symbol, final_decision="BUY" if buy else "SKIP",
                size_modifier=0.75 if buy else 0.0, avg_confidence=conf,
                claude_vote=claude, gpt_vote=gpt,
                reasoning=f"Claude failed; GPT says {'BUY' if buy else 'SKIP'} conf={conf:.0f}%")

        if not gpt_ok:
            conf = claude.confidence * 0.85
            buy = claude.decision == "BUY" and conf >= 40
            return ConsensusResult(
                symbol=symbol, final_decision="BUY" if buy else "SKIP",
                size_modifier=0.75 if buy else 0.0, avg_confidence=conf,
                claude_vote=claude, gpt_vote=gpt,
                reasoning=f"GPT failed; Claude says {'BUY' if buy else 'SKIP'} conf={conf:.0f}%")

        avg_conf = (claude.confidence + gpt.confidence) / 2

        # Both SKIP
        if claude.decision == "SKIP" and gpt.decision == "SKIP":
            return ConsensusResult(
                symbol=symbol, final_decision="SKIP", size_modifier=0.0,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt,
                reasoning="Both models say SKIP")

        # Both agree on direction (BUY or SHORT)
        if claude.decision == gpt.decision and claude.decision in ("BUY", "SHORT"):
            direction = claude.decision
            if avg_conf >= 60:
                return ConsensusResult(
                    symbol=symbol, final_decision=direction, size_modifier=1.0,
                    avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt,
                    reasoning=f"Both {direction}, strong ({avg_conf:.0f}%) — full size")
            elif avg_conf >= 40:
                return ConsensusResult(
                    symbol=symbol, final_decision=direction, size_modifier=0.75,
                    avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt,
                    reasoning=f"Both {direction}, moderate ({avg_conf:.0f}%) — 75% size")
            else:
                return ConsensusResult(
                    symbol=symbol, final_decision=direction, size_modifier=0.5,
                    avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt,
                    reasoning=f"Both {direction}, low confidence ({avg_conf:.0f}%) — 50% size")

        # Disagreement → Perplexity tie-breaker
        logger.info(f"🔀 Tie-breaker needed for {symbol} (Claude={claude.decision}, GPT={gpt.decision})")
        pplx = await self._call_perplexity(symbol, signals)

        if pplx.error:
            return ConsensusResult(
                symbol=symbol, final_decision="SKIP", size_modifier=0.0,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt,
                perplexity_vote=pplx,
                reasoning="Tie-breaker failed — conservative SKIP")

        if pplx.decision in ("BUY", "SHORT"):
            return ConsensusResult(
                symbol=symbol, final_decision=pplx.decision, size_modifier=0.75,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt,
                perplexity_vote=pplx,
                reasoning=f"Tie-break: Perplexity says {pplx.decision} — reduced size")
        else:
            return ConsensusResult(
                symbol=symbol, final_decision="SKIP", size_modifier=0.0,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt,
                perplexity_vote=pplx,
                reasoning=f"Tie-break: Perplexity says SKIP — no trade")


# ── Helpers ───────────────────────────────────────────────────────

def _parse_json(text: str) -> dict:
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
        return {"decision": "SKIP", "confidence": 0, "reasoning": f"Failed to parse: {text[:100]}"}
