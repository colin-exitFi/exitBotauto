"""
Consensus Engine - Multi-AI Jury System.
3-model majority vote: Claude + GPT + Grok. 2-of-3 agreement = trade. Perplexity for research/tie-break.
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
    grok_vote: Optional[ModelVote] = None
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
            "grok": _vote(self.grok_vote),
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
- X/Twitter trending reason: {grok_x_reason}
- X/Twitter sentiment: {grok_x_sentiment}

TECHNICALS:
- RSI: {rsi}
- vs VWAP: {vwap_relation}
- ATR: {atr}
- Bid/Ask spread: {spread_pct}%

NEWS: {news}

ADDITIONAL SIGNALS:
- Pharma catalyst: {pharma_info}
- Fade signal (short setup): {fade_info}
- Earnings: {earnings_info}

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
    """Multi-AI jury: Claude + GPT + Grok — 2-of-3 majority wins. Perplexity for research."""

    TIMEOUT = 45  # seconds per model call (grok-4 reasoning can take 20-30s)

    def __init__(self):
        self._cache: Dict[str, ConsensusResult] = {}  # symbol -> result
        self._skip_cache: Dict[str, float] = {}  # symbol -> timestamp (cooldown for SKIPs)
        self._call_timestamps: List[float] = []
        self._history: List[Dict] = []  # last N decisions for dashboard
        self._api_calls = {"claude": 0, "gpt": 0, "grok": 0, "perplexity": 0}

    # ── Public API ────────────────────────────────────────────────

    async def evaluate(self, symbol: str, price: float, signals_data: Dict) -> ConsensusResult:
        """Run consensus evaluation. Returns cached result if fresh."""
        if not getattr(settings, 'CONSENSUS_ENABLED', True):
            return ConsensusResult(symbol=symbol, final_decision="BUY",
                                   size_modifier=1.0, avg_confidence=100,
                                   reasoning="Consensus disabled")

        # Skip cooldown — don't re-evaluate SKIPped tickers for 5 minutes
        skip_ts = self._skip_cache.get(symbol)
        if skip_ts and (time.time() - skip_ts) < 300:
            logger.debug(f"Skip cooldown active for {symbol} ({int(300 - (time.time() - skip_ts))}s left)")
            return ConsensusResult(symbol=symbol, final_decision="SKIP",
                                   size_modifier=0.0, avg_confidence=0,
                                   reasoning="Skip cooldown (5 min)")

        # Check cache
        cached = self._cache.get(symbol)
        base_ttl = getattr(settings, 'CONSENSUS_CACHE_SECONDS', 300)
        if cached:
            ttl = 300 if cached.final_decision == "SKIP" else base_ttl
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

        # Enrich with technicals + news if missing
        await self._enrich_signals(symbol, price, signals_data)

        # Log what data the AI is actually getting
        logger.info(f"📊 {symbol} signal data: price=${price:.2f} chg={signals_data.get('change_pct',0):+.1f}% "
                     f"vol={signals_data.get('volume_spike',0):.1f}x RSI={signals_data.get('rsi','N/A')} "
                     f"VWAP={signals_data.get('vwap_relation','N/A')} ATR={signals_data.get('atr','N/A')} "
                     f"news={len(signals_data.get('news_headlines',signals_data.get('news',[])) or [])} "
                     f"social={signals_data.get('sentiment_score',0):.2f} "
                     f"grok_x={signals_data.get('grok_x_reason','')[:50] or 'none'}")

        # Build prompt with enriched data
        prompt = self._build_prompt(symbol, price, signals_data)

        # Run all 3 jury members in parallel
        claude_task = self._call_claude(prompt)
        gpt_task = self._call_gpt(prompt)
        grok_task = self._call_grok(prompt)
        claude_vote, gpt_vote, grok_vote = await asyncio.gather(claude_task, gpt_task, grok_task)

        # 3-model majority vote resolution
        result = await self._resolve(symbol, price, signals_data, claude_vote, gpt_vote, grok_vote)

        # Track skip cooldown
        if result.final_decision == "SKIP":
            self._skip_cache[symbol] = time.time()

        # Cache & record
        self._cache[symbol] = result
        self._history.append(result.to_dict())
        self._history = self._history[-50:]

        logger.info(
            f"🗳️ Consensus for {symbol}: {result.final_decision} "
            f"(Claude={claude_vote.decision if not claude_vote.error else 'ERR'}, "
            f"GPT={gpt_vote.decision if not gpt_vote.error else 'ERR'}, "
            f"Grok={grok_vote.decision if not grok_vote.error else 'ERR'}) "
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
                self._api_calls["grok"] * 0.005 +
                self._api_calls["perplexity"] * 0.005, 2
            ),
        }

    # ── Prompt Builder ────────────────────────────────────────────

    async def _enrich_signals(self, symbol: str, price: float, signals: Dict):
        """Fetch technicals and news if not already present."""
        import httpx

        # ── Technicals from Polygon bars ──
        if signals.get("rsi") in (None, "N/A", 0):
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    # Get 20 bars for RSI calculation
                    # Use daily bars (more reliable, especially pre-market)
                    resp = await client.get(
                        f"https://api.polygon.io/v2/aggs/ticker/{symbol}/range/1/day/"
                        f"2026-02-10/2026-03-05?adjusted=true&sort=desc&limit=20"
                        f"&apiKey={settings.POLYGON_API_KEY}")
                    if resp.status_code == 200:
                        bars = resp.json().get("results", [])
                        if len(bars) >= 14:
                            # RSI calculation
                            closes = [b["c"] for b in reversed(bars)]
                            gains, losses = [], []
                            for i in range(1, len(closes)):
                                diff = closes[i] - closes[i-1]
                                gains.append(max(0, diff))
                                losses.append(max(0, -diff))
                            avg_gain = sum(gains[-14:]) / 14
                            avg_loss = sum(losses[-14:]) / 14
                            if avg_loss > 0:
                                rs = avg_gain / avg_loss
                                signals["rsi"] = round(100 - (100 / (1 + rs)), 1)
                            else:
                                signals["rsi"] = 100.0

                            # VWAP approximation
                            if price and bars:
                                vwap_sum = sum(b.get("vw", b["c"]) * b.get("v", 1) for b in bars)
                                vol_sum = sum(b.get("v", 1) for b in bars)
                                vwap = vwap_sum / vol_sum if vol_sum > 0 else price
                                diff_pct = ((price - vwap) / vwap) * 100
                                signals["vwap_relation"] = f"{'Above' if diff_pct > 0 else 'Below'} VWAP by {abs(diff_pct):.1f}%"

                            # ATR
                            if len(bars) >= 14:
                                trs = []
                                for i in range(1, min(15, len(bars))):
                                    h, l, pc = bars[i-1]["h"], bars[i-1]["l"], bars[i]["c"]
                                    trs.append(max(h-l, abs(h-pc), abs(l-pc)))
                                signals["atr"] = f"${sum(trs)/len(trs):.2f}"
            except Exception as e:
                logger.debug(f"Technicals fetch failed for {symbol}: {e}")

        # ── News from Polygon ──
        if not signals.get("news_headlines"):
            try:
                async with httpx.AsyncClient(timeout=8) as client:
                    resp = await client.get(
                        f"https://api.polygon.io/v2/reference/news?ticker={symbol}&limit=5"
                        f"&apiKey={settings.POLYGON_API_KEY}")
                    if resp.status_code == 200:
                        articles = resp.json().get("results", [])
                        headlines = [a.get("title", "") for a in articles if a.get("title")]
                        if headlines:
                            signals["news_headlines"] = headlines[:5]
                            logger.debug(f"Polygon news for {symbol}: {len(headlines)} headlines")
            except Exception as e:
                logger.debug(f"News fetch failed for {symbol}: {e}")

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
            grok_x_reason=signals.get("grok_x_reason", "Not trending on X") or "Not trending on X",
            grok_x_sentiment=signals.get("grok_x_sentiment", "N/A") or "N/A",
            rsi=signals.get("rsi", "N/A"),
            vwap_relation=signals.get("vwap_relation", "N/A"),
            atr=signals.get("atr", "N/A"),
            spread_pct=signals.get("spread_pct", "N/A"),
            news=news_str,
            pharma_info=signals.get("pharma_drug", "None") if signals.get("pharma_signal") else "None",
            fade_info=f"Ran +{signals.get('fade_run_pct',0):.0f}% yesterday — watching for weakness" if signals.get("fade_signal") else "None",
            earnings_info=signals.get("earnings_date", "None") or "None",
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

    _gpt_disabled_until: float = 0  # class-level circuit breaker for billing issues

    async def _call_gpt(self, prompt: str) -> ModelVote:
        if not settings.OPENAI_API_KEY:
            return ModelVote(model="gpt", decision="SKIP", confidence=0, error="No API key")

        # Circuit breaker: if billing issue detected, skip GPT for 15 min
        if time.time() < ConsensusEngine._gpt_disabled_until:
            remaining = int(ConsensusEngine._gpt_disabled_until - time.time())
            return ModelVote(model="gpt", decision="ERR", confidence=0,
                           error=f"GPT disabled (billing issue, retry in {remaining}s)")

        model = getattr(settings, 'OPENAI_MODEL', 'gpt-5.2')
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
                        "max_completion_tokens": 500,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                ConsensusEngine._gpt_disabled_until = 0  # reset circuit breaker on success
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
            err_str = str(e)
            # insufficient_quota = billing issue, not rate limit. Disable for 15 min.
            if "insufficient_quota" in err_str or ("429" in err_str and "quota" in err_str.lower()):
                logger.error(f"⚠️ GPT BILLING ISSUE (insufficient_quota) — disabling for 15 min. Add credits at platform.openai.com")
                ConsensusEngine._gpt_disabled_until = time.time() + 900
                return ModelVote(model="gpt", decision="ERR", confidence=0, error="insufficient_quota")
            # Real rate limit — retry with backoff
            if "429" in err_str:
                for attempt, wait in enumerate([5, 15], 1):
                    logger.warning(f"GPT rate limited, retry {attempt}/2 in {wait}s...")
                    await asyncio.sleep(wait)
                    try:
                        async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                            resp = await client.post(
                                "https://api.openai.com/v1/chat/completions",
                                headers={"Authorization": f"Bearer {settings.OPENAI_API_KEY}",
                                         "Content-Type": "application/json"},
                                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                                      "temperature": 0.3, "max_completion_tokens": 500},
                            )
                            resp.raise_for_status()
                            self._api_calls["gpt"] += 1
                            text = resp.json()["choices"][0]["message"]["content"]
                            data = _parse_json(text)
                            return ModelVote(model="gpt", decision=data.get("decision", "SKIP").upper(),
                                            confidence=int(data.get("confidence", 0)), reasoning=data.get("reasoning", ""))
                    except Exception as retry_e:
                        if "insufficient_quota" in str(retry_e):
                            ConsensusEngine._gpt_disabled_until = time.time() + 900
                            logger.error("⚠️ GPT BILLING ISSUE detected on retry — disabling for 15 min")
                            return ModelVote(model="gpt", decision="ERR", confidence=0, error="insufficient_quota")
                        if attempt == 2:
                            break
                        continue
            logger.error(f"GPT API error: {e}")
            return ModelVote(model="gpt", decision="ERR", confidence=0, error=err_str)

    async def _call_grok(self, prompt: str) -> ModelVote:
        if not settings.XAI_API_KEY:
            return ModelVote(model="grok", decision="SKIP", confidence=0, error="No API key")

        model = getattr(settings, 'XAI_MODEL', 'grok-4-0709')
        try:
            async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                resp = await client.post(
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {settings.XAI_API_KEY}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "max_tokens": 500,
                        "temperature": 0.3,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                self._api_calls["grok"] += 1
                text = resp.json()["choices"][0]["message"]["content"]
                data = _parse_json(text)
                return ModelVote(
                    model="grok",
                    decision=data.get("decision", "SKIP").upper(),
                    confidence=int(data.get("confidence", 0)),
                    reasoning=data.get("reasoning", ""),
                    target_price=data.get("target_price"),
                    stop_price=data.get("stop_price"),
                )
        except Exception as e:
            # Exponential backoff on 429
            if "429" in str(e):
                for attempt, wait in enumerate([5, 15, 30], 1):
                    logger.warning(f"Grok rate limited, retry {attempt}/3 in {wait}s...")
                    await asyncio.sleep(wait)
                    try:
                        async with httpx.AsyncClient(timeout=self.TIMEOUT) as client:
                            resp = await client.post(
                                "https://api.x.ai/v1/chat/completions",
                                headers={"Authorization": f"Bearer {settings.XAI_API_KEY}",
                                         "Content-Type": "application/json"},
                                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                                      "temperature": 0.3, "max_tokens": 500},
                            )
                            resp.raise_for_status()
                            self._api_calls["grok"] += 1
                            text = resp.json()["choices"][0]["message"]["content"]
                            data = _parse_json(text)
                            return ModelVote(model="grok", decision=data.get("decision", "SKIP").upper(),
                                            confidence=int(data.get("confidence", 0)), reasoning=data.get("reasoning", ""))
                    except Exception:
                        if attempt == 3:
                            break
                        continue
            err_msg = str(e) or repr(e) or type(e).__name__
            logger.error(f"Grok API error ({type(e).__name__}): {err_msg}")
            return ModelVote(model="grok", decision="ERR", confidence=0, error=err_msg)

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
                       claude: ModelVote, gpt: ModelVote, grok: ModelVote) -> ConsensusResult:
        """3-model majority vote: 2-of-3 agreeing on BUY or SHORT = trade."""
        votes = [claude, gpt, grok]
        working = [(v.model, v) for v in votes if v.error is None and v.decision != "ERR"]
        failed = [(v.model, v) for v in votes if v.error is not None or v.decision == "ERR"]

        if len(failed) > 0:
            logger.warning(f"Model failures for {symbol}: {[f[0] for f in failed]}")

        # All failed → NO TRADE
        if len(working) == 0:
            return ConsensusResult(
                symbol=symbol, final_decision="SKIP", size_modifier=0.0,
                avg_confidence=0, claude_vote=claude, gpt_vote=gpt, grok_vote=grok,
                reasoning="All 3 AI models failed — never trade blind")

        # Count votes by direction
        buy_votes = [v for _, v in working if v.decision == "BUY"]
        short_votes = [v for _, v in working if v.decision == "SHORT"]
        skip_votes = [v for _, v in working if v.decision == "SKIP"]

        all_confs = [v.confidence for _, v in working]
        avg_conf = sum(all_confs) / len(all_confs) if all_confs else 0

        # 3-of-3 agree on direction → full conviction
        if len(buy_votes) == 3:
            return ConsensusResult(
                symbol=symbol, final_decision="BUY", size_modifier=1.0,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt, grok_vote=grok,
                reasoning=f"Unanimous BUY (3/3) — full size, conf={avg_conf:.0f}%")
        if len(short_votes) == 3:
            return ConsensusResult(
                symbol=symbol, final_decision="SHORT", size_modifier=1.0,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt, grok_vote=grok,
                reasoning=f"Unanimous SHORT (3/3) — full size, conf={avg_conf:.0f}%")

        # 2-of-3 agree on direction → trade with reduced size
        if len(buy_votes) >= 2:
            size = 0.85 if avg_conf >= 60 else 0.65
            voters = [v.model for v in buy_votes]
            return ConsensusResult(
                symbol=symbol, final_decision="BUY", size_modifier=size,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt, grok_vote=grok,
                reasoning=f"Majority BUY (2/3: {', '.join(voters)}) — {size:.0%} size, conf={avg_conf:.0f}%")
        if len(short_votes) >= 2:
            size = 0.85 if avg_conf >= 60 else 0.65
            voters = [v.model for v in short_votes]
            return ConsensusResult(
                symbol=symbol, final_decision="SHORT", size_modifier=size,
                avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt, grok_vote=grok,
                reasoning=f"Majority SHORT (2/3: {', '.join(voters)}) — {size:.0%} size, conf={avg_conf:.0f}%")

        # Only 2 models working and they disagree, or all 3 split → Perplexity tie-break
        if len(working) == 2:
            w1, w2 = working[0], working[1]
            if w1[1].decision != w2[1].decision and w1[1].decision != "SKIP" and w2[1].decision != "SKIP":
                logger.info(f"🔀 2 models split for {symbol} ({w1[0]}={w1[1].decision}, {w2[0]}={w2[1].decision}) → Perplexity tie-break")
                pplx = await self._call_perplexity(symbol, signals)
                if not pplx.error and pplx.decision in ("BUY", "SHORT"):
                    return ConsensusResult(
                        symbol=symbol, final_decision=pplx.decision, size_modifier=0.6,
                        avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt, grok_vote=grok,
                        perplexity_vote=pplx,
                        reasoning=f"Tie-break: Perplexity says {pplx.decision} — 60% size")

        # All SKIP or no majority → SKIP
        return ConsensusResult(
            symbol=symbol, final_decision="SKIP", size_modifier=0.0,
            avg_confidence=avg_conf, claude_vote=claude, gpt_vote=gpt, grok_vote=grok,
            reasoning=f"No majority — BUY:{len(buy_votes)} SHORT:{len(short_votes)} SKIP:{len(skip_votes)}")


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
