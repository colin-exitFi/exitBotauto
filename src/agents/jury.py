"""
Jury 🗳️ - Synthesizes specialized agent briefs into a v2 entry decision.
Velox v2 keeps the jury focused on entries only. Exits are mechanical.
"""

import asyncio
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger

from config import settings
from src.agents.base_agent import call_claude, call_gpt, call_grok, provider_is_backing_off

_jury_gate_lock: Optional[asyncio.Lock] = None
_jury_last_slot_ts: float = 0.0


@dataclass
class JuryVerdict:
    symbol: str
    decision: str  # "BUY", "SHORT", "SKIP"
    size_pct: float
    trail_pct: float  # legacy compatibility; mirrors ratchet trail settings
    reasoning: str
    confidence: float = 0.0
    provider_used: str = ""
    briefs: Dict = field(default_factory=dict)
    consensus_detail: Dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> Dict:
        return {
            "symbol": self.symbol,
            "decision": self.decision,
            "size_pct": self.size_pct,
            "trail_pct": self.trail_pct,
            "reasoning": self.reasoning,
            "confidence": self.confidence,
            "provider_used": self.provider_used,
            "consensus_detail": self.consensus_detail,
            "timestamp": self.timestamp,
        }


PROMPT_TEMPLATE = """You are the JURY inside Velox v2.
{mission}

Your job is entry selection only.
- Risk decides whether the portfolio can trade and caps size.
- Exits are mechanical outside your authority: hard stop, profit ratchet, and portfolio circuit breaker.
- Missing data lowers confidence but does not justify panic or "system integrity" language.

SESSION: {session_label}
SYMBOL: {symbol} @ ${price:.2f}
TODAY'S MOVE: {change_pct:+.1f}%
VOLUME vs AVG: {volume_spike:.1f}x
SPREAD: {spread_pct}%
SETUP TAG: {strategy_tag}
SIGNAL TIER: {signal_tier}
HOLDING HORIZON: {holding_horizon}
ENTRY TIMING: {entry_quality} — stock is at {range_pct:.1f}% of day range
MARKET REGIME: {market_regime}
SIDE BIAS: {side_bias}
FADE CONTEXT: {fade_context}
ECONOMIC CALENDAR: {economic_calendar}
OVERNIGHT INDEX CONTEXT: {overnight_context}
HUMAN INTEL: {human_intel}
PRO TRADER CONTEXT: {copy_trader_context}
UW NEWS: {uw_news}
OPTION CHAIN CONFIRMATION: {option_chain_confirmation}
UW FLOW: {uw_flow_summary}
RETRO FEEDBACK: {retro_feedback}

AGENT BRIEFS:

TECHNICAL:
{technical}

SENTIMENT:
{sentiment}

CATALYST:
{catalyst}

RISK:
{risk}

MACRO:
{macro}

DECISION FRAMEWORK:
- Bias toward action when the setup is coherent, liquid, and aligned.
- BUY when long momentum, catalyst, or institutional flow is aligned.
- SHORT when fade / downside momentum / bearish institutional flow is aligned.
- Risk `can_trade=false` is a hard block. If `can_trade=true`, risk does not get to veto the thesis.
- Tier-1 institutional flow deserves serious weight; stronger UW confirmation can justify a smaller probe.
- Unanimous SKIP means skip.
- Do not invent exits or discretionary liquidation logic.

SIZING:
- size_pct should reflect conviction from 0.25 to 5.0.
- trail_pct is a legacy compatibility field only; default it near 2.0.

Respond with ONLY valid JSON:
{{"decision": "BUY" or "SHORT" or "SKIP", "size_pct": number, "trail_pct": number, "reasoning": "brief synthesis", "confidence": 0-100}}"""


async def deliberate(symbol: str, price: float, briefs: Dict, signals_data: Dict = None) -> JuryVerdict:
    """Synthesize agent briefs into a final trade decision."""
    try:
        await _await_jury_slot()

        def fmt(brief: Dict) -> str:
            if not brief:
                return "No data"
            lines = []
            for key, value in brief.items():
                if key in {"symbol"}:
                    continue
                lines.append(f"  {key}: {value}")
            return "\n".join(lines) if lines else "No data"

        from src.ai.mission import MISSION_SHORT

        sd = signals_data or {}
        side_bias = "SHORT" if str(sd.get("side", "")).strip().lower() == "short" else "LONG"
        if sd.get("fade_signal"):
            fade_context = (
                f"Ran {float(sd.get('fade_run_pct', 0) or 0):+.1f}% on the prior session; "
                f"now {float(sd.get('price_change_from_run', 0) or 0):+.1f}% vs run close; "
                f"RSI {float(sd.get('rsi', 0) or 0):.1f}; "
                f"day-2 volume {float(sd.get('volume', 0) or 0):,.0f} vs day-1 {float(sd.get('run_volume', 0) or 0):,.0f}"
            )
        else:
            fade_context = "None"

        try:
            from datetime import datetime as _dt
            import zoneinfo

            _et = _dt.now(zoneinfo.ZoneInfo("US/Eastern"))
            _h, _m = _et.hour, _et.minute
            if (_h == 9 and _m >= 30) or (10 <= _h < 16):
                session_label = "REGULAR HOURS"
            elif 4 <= _h < 9 or (_h == 9 and _m < 30):
                session_label = "PRE-MARKET"
            elif 16 <= _h < 20:
                session_label = "AFTER-HOURS"
            else:
                session_label = "OVERNIGHT"
        except Exception:
            session_label = "UNKNOWN"

        retro_feedback = _build_retro_feedback(symbol, sd)
        prompt = PROMPT_TEMPLATE.format(
            mission=MISSION_SHORT,
            session_label=session_label,
            symbol=symbol,
            price=price,
            change_pct=sd.get("change_pct", 0),
            volume_spike=sd.get("volume_spike", 0),
            spread_pct=sd.get("spread_pct", "N/A"),
            strategy_tag=sd.get("strategy_tag", "unknown"),
            signal_tier=sd.get("signal_tier", "tier_2"),
            holding_horizon=sd.get("holding_horizon", "intraday"),
            entry_quality=sd.get("entry_quality", "neutral"),
            range_pct=float(sd.get("range_pct", 50) or 50),
            market_regime=sd.get("market_regime", "mixed"),
            side_bias=side_bias,
            fade_context=fade_context,
            economic_calendar=sd.get("economic_calendar", "None"),
            overnight_context=sd.get("overnight_context", "Unavailable"),
            human_intel=sd.get("human_intel", "None"),
            copy_trader_context=sd.get("copy_trader_context", "None"),
            uw_news=sd.get("uw_news_summary", "None"),
            option_chain_confirmation=sd.get("uw_chain_summary", "None"),
            uw_flow_summary=sd.get("uw_flow_summary", "None"),
            retro_feedback=retro_feedback,
            technical=fmt(briefs.get("technical", {})),
            sentiment=fmt(briefs.get("sentiment", {})),
            catalyst=fmt(briefs.get("catalyst", {})),
            risk=fmt(briefs.get("risk", {})),
            macro=fmt(briefs.get("macro", {})),
        )

        provider_names = ["claude", "gpt", "grok"]
        results = await asyncio.gather(
            _safe_call("claude", call_claude, prompt),
            _safe_call("gpt", call_gpt, prompt),
            _safe_call("grok", call_grok, prompt),
            return_exceptions=True,
        )

        provider_results = []
        votes = []
        for provider_name, result in zip(provider_names, results):
            if isinstance(result, Exception):
                provider_results.append(
                    {
                        "provider": provider_name,
                        "result": None,
                        "rate_limited": _is_rate_limit_error(result),
                        "error": str(result),
                    }
                )
                continue
            if not isinstance(result, dict):
                provider_results.append(
                    {
                        "provider": provider_name,
                        "result": None,
                        "rate_limited": False,
                        "error": "invalid_result",
                    }
                )
                continue
            provider_results.append(result)
            normalized = _normalize_vote(provider_name, result.get("result"))
            if normalized:
                votes.append(normalized)

        verdict = _apply_consensus(symbol, votes, briefs, sd, provider_results)

        risk_brief = briefs.get("risk", {}) or {}
        risk_blocked = risk_brief.get("can_trade") is False or (
            "can_trade" not in risk_brief and risk_brief.get("approved") is False
        )
        if risk_blocked and verdict.decision in {"BUY", "SHORT"}:
            reasoning = str(risk_brief.get("reasoning", "") or "Risk hard gate blocked the trade")
            logger.info(f"🛡️ Jury hard-blocked by Risk Agent for {symbol}: {reasoning}")
            return JuryVerdict(
                symbol=symbol,
                decision="SKIP",
                size_pct=0.0,
                trail_pct=_default_trail_pct(),
                reasoning=f"Risk hard gate: {reasoning}",
                provider_used=verdict.provider_used,
                briefs=briefs,
                consensus_detail={**verdict.consensus_detail, "risk_override": True},
            )

        risk_cap = risk_brief.get("size_cap_pct", risk_brief.get("max_size_pct"))
        if risk_cap is not None:
            try:
                verdict.size_pct = min(float(verdict.size_pct or 0), float(risk_cap or 0))
            except Exception:
                pass
            if verdict.decision in {"BUY", "SHORT"} and float(verdict.size_pct or 0) <= 0:
                verdict.decision = "SKIP"
                verdict.reasoning = f"Risk size cap blocked action. {verdict.reasoning}".strip()
                verdict.confidence = 0.0

        logger.info(
            f"🗳️ Jury verdict for {symbol}: {verdict.decision} "
            f"size={verdict.size_pct}% trail={verdict.trail_pct}% "
            f"conf={verdict.confidence}% provider={verdict.provider_used or '?'} "
            f"votes={verdict.consensus_detail.get('votes', {})} — {verdict.reasoning[:120]}"
        )
        return verdict

    except Exception as e:
        logger.error(f"Jury error for {symbol}: {e}")
        return JuryVerdict(
            symbol=symbol,
            decision="SKIP",
            size_pct=0.0,
            trail_pct=_default_trail_pct(),
            reasoning=f"Jury exception: {e}",
            briefs=briefs,
        )


def _default_trail_pct() -> float:
    return round(float(getattr(settings, "PROFIT_RATCHET_TRAIL_PCT", 2.0) or 2.0), 3)


def _is_rate_limit_error(message: object) -> bool:
    text = str(message or "").lower()
    return (
        "rate limit" in text
        or "429" in text
        or "too many requests" in text
        or "exhausted all retries" in text
        or "backing off" in text
    )


async def _safe_call(provider_name: str, caller, prompt: str) -> Dict:
    if provider_is_backing_off(provider_name):
        return {
            "provider": provider_name,
            "result": None,
            "rate_limited": True,
            "error": "provider_backoff_active",
        }

    last_error = ""
    for attempt in range(1, 3):
        try:
            result = await caller(prompt, max_tokens=400)
            rate_limited = result is None and provider_is_backing_off(provider_name)
            if result is not None or rate_limited or attempt == 2:
                return {
                    "provider": provider_name,
                    "result": result,
                    "rate_limited": rate_limited,
                    "error": "rate_limited" if rate_limited else ("no_response" if result is None else last_error),
                }
            logger.warning(f"Jury {provider_name} returned no response; retrying once")
            await asyncio.sleep(0.75)
        except Exception as e:
            last_error = str(e)
            rate_limited = provider_is_backing_off(provider_name) or _is_rate_limit_error(e)
            if rate_limited or attempt == 2:
                logger.warning(f"Jury {provider_name} failed: {e}")
                return {
                    "provider": provider_name,
                    "result": None,
                    "rate_limited": rate_limited,
                    "error": str(e),
                }
            logger.warning(f"Jury {provider_name} transient failure; retrying once: {e}")
            await asyncio.sleep(0.75)
    return {
        "provider": provider_name,
        "result": None,
        "rate_limited": provider_is_backing_off(provider_name),
        "error": last_error or "no_response",
    }


async def _await_jury_slot():
    """Light global pacing gate to avoid burst-spiking provider calls."""
    global _jury_gate_lock, _jury_last_slot_ts
    spacing = max(0.0, float(getattr(settings, "JURY_MIN_SPACING_SECONDS", 0.35) or 0.35))
    if spacing <= 0:
        return
    if _jury_gate_lock is None:
        _jury_gate_lock = asyncio.Lock()
    async with _jury_gate_lock:
        now = time.time()
        wait = (_jury_last_slot_ts + spacing) - now
        if wait > 0:
            await asyncio.sleep(wait)
        _jury_last_slot_ts = time.time()


def _normalize_vote(provider_name: str, result: Dict) -> Optional[Dict]:
    if not result or not isinstance(result, dict):
        return None
    decision = str(result.get("decision", "SKIP") or "SKIP").upper()
    if decision not in {"BUY", "SHORT", "SKIP"}:
        decision = "SKIP"
    try:
        size_pct = max(0.0, min(5.0, float(result.get("size_pct", 0) or 0)))
    except Exception:
        size_pct = 0.0
    try:
        trail_pct = max(0.5, min(5.0, float(result.get("trail_pct", _default_trail_pct()) or _default_trail_pct())))
    except Exception:
        trail_pct = _default_trail_pct()
    try:
        confidence = max(0.0, min(100.0, float(result.get("confidence", 0) or 0)))
    except Exception:
        confidence = 0.0
    return {
        "provider": provider_name,
        "decision": decision,
        "size_pct": size_pct,
        "trail_pct": trail_pct,
        "confidence": confidence,
        "reasoning": str(result.get("reasoning", "") or "")[:220],
    }


def _build_retro_feedback(symbol: str, signals_data: Dict) -> str:
    if not bool(getattr(settings, "JURY_RETRO_ENABLED", True)):
        return "None"

    try:
        from src.ai import trade_history

        recent = trade_history.get_recent(int(getattr(settings, "JURY_RETRO_LOOKBACK_TRADES", 40) or 40))
    except Exception:
        return "None"

    if not recent:
        return "None"

    min_matches = max(2, int(getattr(settings, "JURY_RETRO_MIN_MATCHES", 3) or 3))
    strategy_tag = str((signals_data or {}).get("strategy_tag", "unknown") or "unknown")
    signal_sources = (signals_data or {}).get("signal_sources", []) or []
    if isinstance(signal_sources, str):
        signal_sources = [s.strip() for s in signal_sources.split(",") if s.strip()]
    source_set = {str(src).strip() for src in signal_sources if str(src).strip()}

    rows: List[str] = []

    if symbol:
        symbol_trades = [
            trade for trade in recent
            if str(trade.get("symbol", "")).upper() == str(symbol).upper()
        ]
        if len(symbol_trades) >= min_matches:
            rows.append(_format_retro_row(f"Recent {symbol}", symbol_trades))

    if strategy_tag and strategy_tag != "unknown":
        strategy_trades = [
            trade for trade in recent
            if str(trade.get("strategy_tag", "unknown") or "unknown") == strategy_tag
        ]
        if len(strategy_trades) >= min_matches:
            rows.append(_format_retro_row(f"Strategy {strategy_tag}", strategy_trades))
            high_conf = [
                trade for trade in strategy_trades
                if float(trade.get("decision_confidence", 0) or 0) >= 75.0
            ]
            if len(high_conf) >= min_matches:
                rows.append(_format_confidence_calibration(high_conf))

    if source_set:
        source_trades = []
        for trade in recent:
            sources = trade.get("signal_sources", []) or []
            if isinstance(sources, str):
                sources = [s.strip() for s in sources.split(",") if s.strip()]
            if source_set.intersection({str(src).strip() for src in sources if str(src).strip()}):
                source_trades.append(trade)
        if len(source_trades) >= min_matches:
            rows.append(_format_retro_row(f"Sources {', '.join(sorted(source_set))}", source_trades))

    if not rows:
        return "None"
    return "\n".join(f"- {row}" for row in rows[:4])


def _format_retro_row(label: str, trades: List[Dict]) -> str:
    pnl = sum(float(trade.get("pnl", 0) or 0) for trade in trades)
    wins = sum(1 for trade in trades if float(trade.get("pnl", 0) or 0) > 0)
    win_rate = wins / max(1, len(trades))
    return f"{label}: {len(trades)} trades, {win_rate:.0%} WR, ${pnl:.2f} P&L."


def _format_confidence_calibration(trades: List[Dict]) -> str:
    pnl = sum(float(trade.get("pnl", 0) or 0) for trade in trades)
    wins = sum(1 for trade in trades if float(trade.get("pnl", 0) or 0) > 0)
    win_rate = wins / max(1, len(trades))
    if win_rate <= 0.40 or pnl < 0:
        return (
            f"Calibration: recent high-confidence calls underperformed "
            f"({len(trades)} trades, {win_rate:.0%} WR, ${pnl:.2f})."
        )
    return (
        f"Calibration: recent high-confidence calls worked "
        f"({len(trades)} trades, {win_rate:.0%} WR, ${pnl:.2f})."
    )


def _apply_consensus(
    symbol: str,
    votes: List[Dict],
    briefs: Dict,
    signals_data: Dict,
    provider_results: Optional[List[Dict]] = None,
) -> JuryVerdict:
    provider_results = provider_results or []
    unavailable_providers = [
        str(item.get("provider", ""))
        for item in provider_results
        if not item.get("result")
    ]
    degraded_providers = [
        str(item.get("provider", ""))
        for item in provider_results
        if item.get("rate_limited")
    ]
    failed_providers = {
        str(item.get("provider", "")): str(item.get("error", "") or "")
        for item in provider_results
        if not item.get("result")
    }

    if not votes:
        return _skip_verdict(
            symbol=symbol,
            briefs=briefs,
            providers_used=[],
            vote_map={},
            agreement="no_votes",
            reasoning="No jury models returned a usable vote",
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
        )

    grouped = {"BUY": [], "SHORT": [], "SKIP": []}
    for vote in votes:
        grouped[vote["decision"]].append(vote)

    providers_used = [vote["provider"] for vote in votes]
    vote_map = {vote["provider"]: vote["decision"] for vote in votes}
    buy_votes = grouped["BUY"]
    short_votes = grouped["SHORT"]
    skip_votes = grouped["SKIP"]
    total_votes = len(votes)

    if total_votes == 1:
        if _allow_tier1_probe("BUY", buy_votes, short_votes, signals_data):
            return _decision_verdict(
                symbol=symbol,
                decision="BUY",
                agreeing_votes=buy_votes,
                opposing_votes=short_votes + skip_votes,
                providers_used=providers_used,
                vote_map=vote_map,
                briefs=briefs,
                agreement="tier1_probe",
                size_modifier=0.5,
                confidence_multiplier=0.8,
                unavailable_providers=unavailable_providers,
                rate_limited_providers=degraded_providers,
                failed_providers=failed_providers,
            )
        if _allow_tier1_probe("SHORT", short_votes, buy_votes, signals_data):
            return _decision_verdict(
                symbol=symbol,
                decision="SHORT",
                agreeing_votes=short_votes,
                opposing_votes=buy_votes + skip_votes,
                providers_used=providers_used,
                vote_map=vote_map,
                briefs=briefs,
                agreement="tier1_probe",
                size_modifier=0.5,
                confidence_multiplier=0.8,
                unavailable_providers=unavailable_providers,
                rate_limited_providers=degraded_providers,
                failed_providers=failed_providers,
            )
        return _skip_verdict(
            symbol=symbol,
            briefs=briefs,
            providers_used=providers_used,
            vote_map=vote_map,
            agreement="degraded_insufficient" if degraded_providers else "single_model_insufficient",
            reasoning="Single model response is insufficient for action",
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
            confidence_hint=_average_confidence(votes),
        )

    if total_votes == 3 and len(skip_votes) == 3:
        return _skip_verdict(
            symbol=symbol,
            briefs=briefs,
            providers_used=providers_used,
            vote_map=vote_map,
            agreement="unanimous_skip",
            reasoning="All jury models SKIPped",
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
            confidence_hint=_average_confidence(skip_votes),
        )

    if len(buy_votes) >= 2:
        if len(buy_votes) == 3:
            agreement = "unanimous"
        elif total_votes == 2:
            agreement = "degraded_unanimous" if degraded_providers else "majority_two_model"
        else:
            agreement = "majority_conflict" if short_votes else "majority"
        return _decision_verdict(
            symbol=symbol,
            decision="BUY",
            agreeing_votes=buy_votes,
            opposing_votes=short_votes + skip_votes,
            providers_used=providers_used,
            vote_map=vote_map,
            briefs=briefs,
            agreement=agreement,
            size_modifier=0.75 if agreement == "majority_conflict" else (0.85 if agreement == "degraded_unanimous" else 1.0),
            confidence_multiplier=1.0 if agreement == "unanimous" else 0.9,
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
        )

    if len(short_votes) >= 2:
        if len(short_votes) == 3:
            agreement = "unanimous"
        elif total_votes == 2:
            agreement = "degraded_unanimous" if degraded_providers else "majority_two_model"
        else:
            agreement = "majority_conflict" if buy_votes else "majority"
        return _decision_verdict(
            symbol=symbol,
            decision="SHORT",
            agreeing_votes=short_votes,
            opposing_votes=buy_votes + skip_votes,
            providers_used=providers_used,
            vote_map=vote_map,
            briefs=briefs,
            agreement=agreement,
            size_modifier=0.75 if agreement == "majority_conflict" else (0.85 if agreement == "degraded_unanimous" else 1.0),
            confidence_multiplier=1.0 if agreement == "unanimous" else 0.9,
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
        )

    if _allow_tier1_probe("BUY", buy_votes, short_votes, signals_data):
        return _decision_verdict(
            symbol=symbol,
            decision="BUY",
            agreeing_votes=buy_votes,
            opposing_votes=short_votes + skip_votes,
            providers_used=providers_used,
            vote_map=vote_map,
            briefs=briefs,
            agreement="tier1_probe",
            size_modifier=0.5,
            confidence_multiplier=0.8,
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
        )

    if _allow_tier1_probe("SHORT", short_votes, buy_votes, signals_data):
        return _decision_verdict(
            symbol=symbol,
            decision="SHORT",
            agreeing_votes=short_votes,
            opposing_votes=buy_votes + skip_votes,
            providers_used=providers_used,
            vote_map=vote_map,
            briefs=briefs,
            agreement="tier1_probe",
            size_modifier=0.5,
            confidence_multiplier=0.8,
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
        )

    if total_votes == 2 and len(skip_votes) == 2:
        return _skip_verdict(
            symbol=symbol,
            briefs=briefs,
            providers_used=providers_used,
            vote_map=vote_map,
            agreement="degraded_unanimous_skip" if degraded_providers else "two_model_skip",
            reasoning="Both responding jury models SKIPped",
            unavailable_providers=unavailable_providers,
            rate_limited_providers=degraded_providers,
            failed_providers=failed_providers,
            confidence_hint=_average_confidence(skip_votes),
        )

    return _skip_verdict(
        symbol=symbol,
        briefs=briefs,
        providers_used=providers_used,
        vote_map=vote_map,
        agreement="degraded_split" if degraded_providers else ("two_model_no_consensus" if total_votes == 2 else "split_vote"),
        reasoning="Jury did not reach a v2 executable consensus",
        unavailable_providers=unavailable_providers,
        rate_limited_providers=degraded_providers,
        failed_providers=failed_providers,
        confidence_hint=_average_confidence(votes),
    )


def _allow_tier1_probe(
    decision: str,
    agreeing_votes: List[Dict],
    opposing_directional_votes: List[Dict],
    signals_data: Dict,
) -> bool:
    # Disabled: 0% WR across all tier1_probe entries. Require 2-of-3 model agreement minimum.
    return False


def _average_confidence(votes: List[Dict]) -> float:
    if not votes:
        return 0.0
    return sum(float(vote.get("confidence", 0) or 0) for vote in votes) / max(1, len(votes))


def _decision_verdict(
    symbol: str,
    decision: str,
    agreeing_votes: List[Dict],
    opposing_votes: List[Dict],
    providers_used: List[str],
    vote_map: Dict[str, str],
    briefs: Dict,
    agreement: str,
    size_modifier: float,
    confidence_multiplier: float,
    unavailable_providers: Optional[List[str]] = None,
    rate_limited_providers: Optional[List[str]] = None,
    failed_providers: Optional[Dict[str, str]] = None,
) -> JuryVerdict:
    base_size = sum(vote["size_pct"] for vote in agreeing_votes) / max(1, len(agreeing_votes))
    base_trail = sum(vote["trail_pct"] for vote in agreeing_votes) / max(1, len(agreeing_votes))
    avg_conf = _average_confidence(agreeing_votes)
    size_pct = max(0.25, min(5.0, base_size * size_modifier))
    consensus_conf = max(0.0, min(100.0, avg_conf * confidence_multiplier))

    reasons = [
        f"{vote['provider']}={vote['decision']} ({vote['reasoning'][:70]})"
        for vote in agreeing_votes + opposing_votes
    ]
    reason_text = "; ".join(reasons[:3])

    summaries = {
        "unanimous": f"{decision} unanimous 3/3",
        "majority": f"{decision} 2-of-3 consensus",
        "majority_two_model": f"{decision} 2/2 consensus",
        "degraded_unanimous": f"{decision} degraded unanimous",
        "majority_conflict": f"{decision} 2-of-3 with direct opposition",
        "tier1_probe": f"{decision} tier-1 reduced-size probe",
    }
    summary = summaries.get(agreement, f"{decision} consensus")
    issue_suffix = _provider_issue_suffix(unavailable_providers, rate_limited_providers)
    if issue_suffix:
        summary = f"{summary}{issue_suffix}"

    return JuryVerdict(
        symbol=symbol,
        decision=decision,
        size_pct=round(size_pct, 3),
        trail_pct=round(base_trail if base_trail > 0 else _default_trail_pct(), 3),
        reasoning=f"{summary}. {reason_text}".strip(),
        confidence=round(consensus_conf, 2),
        provider_used=",".join(providers_used) if providers_used else "none",
        briefs=briefs,
        consensus_detail={
            "votes": vote_map,
            "total_models": len(providers_used),
            "agreement": agreement,
            "size_modifier": round(size_modifier, 3),
            "confidence": round(consensus_conf, 2),
            "base_size_pct": round(base_size, 3),
            "agreeing_models": [vote["provider"] for vote in agreeing_votes],
            "degraded": bool(rate_limited_providers),
            "unavailable_providers": list(unavailable_providers or []),
            "rate_limited_providers": list(rate_limited_providers or []),
            "failed_providers": dict(failed_providers or {}),
        },
    )


def _skip_verdict(
    symbol: str,
    briefs: Dict,
    providers_used: List[str],
    vote_map: Dict[str, str],
    agreement: str,
    reasoning: str,
    unavailable_providers: Optional[List[str]] = None,
    rate_limited_providers: Optional[List[str]] = None,
    failed_providers: Optional[Dict[str, str]] = None,
    confidence_hint: Optional[float] = None,
) -> JuryVerdict:
    baseline = {
        "unanimous_skip": 62.0,
        "degraded_unanimous_skip": 55.0,
        "two_model_skip": 55.0,
        "split_vote": 40.0,
        "two_model_no_consensus": 45.0,
        "single_model_insufficient": 30.0,
        "degraded_insufficient": 30.0,
        "degraded_split": 40.0,
        "no_votes": 25.0,
    }
    try:
        hinted = float(confidence_hint if confidence_hint is not None else 0.0)
    except Exception:
        hinted = 0.0
    consensus_conf = max(0.0, min(100.0, hinted if hinted > 0 else baseline.get(agreement, 35.0)))
    issue_suffix = _provider_issue_suffix(unavailable_providers, rate_limited_providers)
    if issue_suffix:
        reasoning = f"{reasoning}{issue_suffix}"

    return JuryVerdict(
        symbol=symbol,
        decision="SKIP",
        size_pct=0.0,
        trail_pct=_default_trail_pct(),
        reasoning=reasoning,
        confidence=round(consensus_conf, 2),
        provider_used=",".join(providers_used) if providers_used else "none",
        briefs=briefs,
        consensus_detail={
            "votes": vote_map,
            "total_models": len(providers_used),
            "agreement": agreement,
            "size_modifier": 0.0,
            "confidence": round(consensus_conf, 2),
            "degraded": bool(rate_limited_providers),
            "unavailable_providers": list(unavailable_providers or []),
            "rate_limited_providers": list(rate_limited_providers or []),
            "failed_providers": dict(failed_providers or {}),
        },
    )


def _provider_issue_suffix(
    unavailable_providers: Optional[List[str]],
    rate_limited_providers: Optional[List[str]],
) -> str:
    unavailable = [str(p) for p in (unavailable_providers or []) if str(p)]
    rate_limited = [str(p) for p in (rate_limited_providers or []) if str(p)]
    if not unavailable and not rate_limited:
        return ""
    missing = [p for p in unavailable if p not in rate_limited]
    parts = []
    if rate_limited:
        parts.append(f"rate-limited: {', '.join(rate_limited)}")
    if missing:
        parts.append(f"missing: {', '.join(missing)}")
    return f" ({'; '.join(parts)})" if parts else ""
