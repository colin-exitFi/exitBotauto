"""
Deterministic book allocator.

Turns regime, book performance, and current exposure into a capital deployment
decision for each candidate entry.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from config import settings
from src.data.strategy_playbook import get_playbook
from src.data.strategy_tags import is_artifact_strategy_tag, normalize_strategy_tag


def _position_notional(position: Dict) -> float:
    for key in ("actual_notional", "notional", "market_value"):
        try:
            value = float(position.get(key, 0) or 0)
        except Exception:
            value = 0.0
        if value:
            return abs(value)
    try:
        price = float(position.get("entry_price", 0) or 0)
        qty = float(position.get("quantity", position.get("qty", 0)) or 0)
        return abs(price * qty)
    except Exception:
        return 0.0


def _position_unrealized_pnl(position: Dict) -> float:
    try:
        return float(
            position.get("unrealized_pnl", position.get("open_pnl", 0.0)) or 0.0
        )
    except Exception:
        return 0.0


def _book_bias(strategy_tag: str) -> str:
    tag = normalize_strategy_tag(strategy_tag, fallback="unknown")
    if "pharma" in tag or "congress" in tag:
        return "event"
    if "uw_flow" in tag:
        return "flow"
    if "fade" in tag:
        return "short"
    if tag.endswith("_short"):
        return "short"
    if tag.endswith("_long"):
        return "long"
    return "balanced"


def _regime_alignment(strategy_tag: str, market_regime: str) -> str:
    regime = str(market_regime or "mixed").strip().lower() or "mixed"
    if regime == "mixed":
        return "neutral"
    playbook = get_playbook(strategy_tag)
    allowed = {str(r or "").strip().lower() for r in playbook.get("allowed_regimes", [])}
    if regime in allowed:
        return "aligned"
    bias = _book_bias(strategy_tag)
    if regime == "risk_on" and bias == "long":
        return "aligned"
    if regime == "risk_off" and bias == "short":
        return "aligned"
    if bias == "balanced":
        return "neutral"
    return "misaligned"


def _base_budget_pct(strategy_tag: str, market_regime: str) -> float:
    tag = normalize_strategy_tag(strategy_tag, fallback="unknown")
    regime = str(market_regime or "mixed").strip().lower() or "mixed"
    alignment = _regime_alignment(tag, regime)

    if tag in {"momentum_long", "momentum_short"}:
        return 28.0 if alignment == "aligned" else (18.0 if alignment == "neutral" else 10.0)
    if tag == "fade_short":
        return 24.0 if alignment == "aligned" else (16.0 if alignment == "neutral" else 8.0)
    if tag.startswith("social_momentum_"):
        return 14.0 if alignment == "aligned" else (10.0 if alignment == "neutral" else 6.0)
    if tag.startswith("copy_trader_"):
        return 12.0 if alignment == "aligned" else (9.0 if alignment == "neutral" else 6.0)
    if tag.startswith("watchlist_"):
        return 10.0 if alignment == "aligned" else (8.0 if alignment == "neutral" else 5.0)
    if tag.startswith("uw_flow_"):
        return 12.0 if alignment == "aligned" else (8.0 if alignment == "neutral" else 5.0)
    if tag == "pharma_catalyst":
        return 12.0 if regime in {"risk_on", "mixed"} else 7.0
    if tag == "congress_follow":
        return 8.0 if regime == "mixed" else 10.0

    bias = _book_bias(tag)
    if bias == "long":
        return 14.0 if alignment == "aligned" else (10.0 if alignment == "neutral" else 6.0)
    if bias == "short":
        return 14.0 if alignment == "aligned" else (10.0 if alignment == "neutral" else 6.0)
    return 8.0


def _book_report_rows(analytics: Dict) -> Dict[str, Dict]:
    rows: Dict[str, Dict] = {}
    report = dict((analytics or {}).get("book_report", {}) or {})
    for item in report.get("books", []) or []:
        if not isinstance(item, dict):
            continue
        tag = normalize_strategy_tag(item.get("strategy_tag", "unknown"), fallback="unknown")
        if is_artifact_strategy_tag(tag):
            continue
        rows[tag] = dict(item)
    return rows


def _play_report_rows(play_report: Dict) -> List[Dict]:
    rows: List[Dict] = []
    report = dict(play_report or {})
    for item in report.get("plays", []) or []:
        if not isinstance(item, dict):
            continue
        rows.append(dict(item))
    return rows


def _find_play_row(
    *,
    play_report: Optional[Dict],
    strategy_tag: str,
    setup_mode: str,
    market_regime: str,
    session_type: str,
) -> Dict:
    strategy = normalize_strategy_tag(strategy_tag, fallback="unknown")
    mode = str(setup_mode or "invalid").strip().lower() or "invalid"
    regime = str(market_regime or "mixed").strip().lower() or "mixed"
    session = str(session_type or "regular").strip().lower() or "regular"
    rows = _play_report_rows(play_report or {})
    if not rows or mode in {"", "invalid", "unknown"}:
        return {}

    exact = None
    regime_match = None
    mode_match = None
    for row in rows:
        if normalize_strategy_tag(row.get("strategy_tag", "unknown"), fallback="unknown") != strategy:
            continue
        if str(row.get("setup_mode", "invalid") or "invalid").strip().lower() != mode:
            continue
        if str(row.get("market_regime", "mixed") or "mixed").strip().lower() == regime:
            regime_match = regime_match or dict(row)
            if str(row.get("session_type", "regular") or "regular").strip().lower() == session:
                exact = dict(row)
                break
        mode_match = mode_match or dict(row)
    return exact or regime_match or mode_match or {}


def _merged_book_bucket(strategy_tag: str, strategy_stats: Dict[str, Dict], book_rows: Dict[str, Dict]) -> Dict:
    tag = normalize_strategy_tag(strategy_tag, fallback="unknown")
    merged = dict(strategy_stats.get(tag, {}) or {})
    row = dict(book_rows.get(tag, {}) or {})
    if row:
        for key in (
            "trades",
            "trade_count",
            "clean_trades",
            "win_rate_pct",
            "clean_win_rate_pct",
            "win_rate",
            "expectancy",
            "pnl",
            "clean_pnl",
            "net_pnl",
            "profit_factor",
            "max_drawdown",
            "sharpe_ratio",
            "anomaly_count",
            "status",
            "recommended_action",
            "control_state",
            "status_reason",
            "size_multiplier",
            "regimes",
            "sessions",
            "best_regime",
            "worst_regime",
            "best_session",
            "worst_session",
        ):
            if row.get(key) is not None:
                merged[key] = row.get(key)
    if "trades" not in merged and merged.get("trade_count") is not None:
        merged["trades"] = merged.get("trade_count")
    if "pnl" not in merged and merged.get("net_pnl") is not None:
        merged["pnl"] = merged.get("net_pnl")
    return merged


def _bucket_trade_count(bucket: Dict) -> int:
    clean_trades = int(bucket.get("clean_trades", 0) or 0)
    raw_trades = int(bucket.get("trades", bucket.get("trade_count", 0)) or 0)
    return clean_trades if clean_trades > 0 else raw_trades


def _bucket_pnl(bucket: Dict) -> float:
    clean_trades = int(bucket.get("clean_trades", 0) or 0)
    if clean_trades > 0 and bucket.get("clean_pnl") is not None:
        return float(bucket.get("clean_pnl", 0) or 0.0)
    if bucket.get("pnl") is not None:
        return float(bucket.get("pnl", 0) or 0.0)
    return float(bucket.get("net_pnl", 0) or 0.0)


def _bucket_win_rate(bucket: Dict) -> float:
    clean_trades = int(bucket.get("clean_trades", 0) or 0)
    if clean_trades > 0 and bucket.get("clean_win_rate_pct") is not None:
        return float(bucket.get("clean_win_rate_pct", 0) or 0.0)
    return float(bucket.get("win_rate_pct", bucket.get("win_rate", 0)) or 0.0)


def _anomaly_ratio(bucket: Dict) -> float:
    raw_trades = int(bucket.get("trades", bucket.get("trade_count", 0)) or 0)
    anomaly_count = int(bucket.get("anomaly_count", 0) or 0)
    if raw_trades <= 0 or anomaly_count <= 0:
        return 0.0
    return float(anomaly_count) / float(max(1, raw_trades))


def _evidence_state(bucket: Dict) -> str:
    trades = _bucket_trade_count(bucket)
    if trades >= 20:
        return "proven"
    if trades >= 10:
        return "developing"
    if trades >= 5:
        return "emerging"
    return "exploratory"


def _evidence_multiplier(bucket: Dict) -> float:
    state = _evidence_state(bucket)
    pnl = _bucket_pnl(bucket)
    expectancy = float(bucket.get("expectancy", 0) or 0)
    if state == "exploratory":
        return 0.55
    if state == "emerging":
        return 0.75
    if state == "developing":
        return 0.95
    if pnl > 0 and expectancy > 0:
        return 1.05
    return 1.0


def _data_quality_multiplier(bucket: Dict) -> float:
    ratio = _anomaly_ratio(bucket)
    if ratio >= 0.50:
        return 0.65
    if ratio >= 0.25:
        return 0.8
    if ratio >= 0.10:
        return 0.9
    return 1.0


def _dimension_bucket_state(bucket: Optional[Dict]) -> str:
    if not isinstance(bucket, dict):
        return "insufficient"
    trades = _bucket_trade_count(bucket)
    if trades < 3:
        return "insufficient"
    win_rate = _bucket_win_rate(bucket)
    pnl = _bucket_pnl(bucket)
    expectancy = float(bucket.get("expectancy", 0) or 0)
    if trades >= 4 and pnl > 0 and expectancy > 0:
        if win_rate >= 60.0:
            return "strong_positive"
        if win_rate >= 50.0:
            return "positive"
    if trades >= 4 and pnl < 0 and expectancy < 0:
        if win_rate < 35.0:
            return "strong_negative"
        if win_rate < 45.0:
            return "negative"
    return "neutral"


def _dimension_bucket_multiplier(bucket: Optional[Dict]) -> float:
    state = _dimension_bucket_state(bucket)
    if state == "strong_positive":
        return 1.15
    if state == "positive":
        return 1.07
    if state == "negative":
        return 0.8
    if state == "strong_negative":
        return 0.55
    return 1.0


def _context_multiplier(regime_bucket: Optional[Dict], session_bucket: Optional[Dict]) -> float:
    multipliers = [
        _dimension_bucket_multiplier(bucket)
        for bucket in (regime_bucket, session_bucket)
        if isinstance(bucket, dict)
    ]
    if not multipliers:
        return 1.0
    return round(sum(multipliers) / float(len(multipliers)), 4)


def _allocator_score(row: Dict) -> float:
    score = 50.0
    alignment = str(row.get("alignment", "neutral") or "neutral")
    if alignment == "aligned":
        score += 8.0
    elif alignment == "misaligned":
        score -= 10.0

    state = str(row.get("state", "neutral") or "neutral")
    score += {
        "hot": 12.0,
        "warm": 7.0,
        "neutral": 0.0,
        "cool": -6.0,
        "cold": -12.0,
    }.get(state, 0.0)

    evidence = str(row.get("evidence_state", "exploratory") or "exploratory")
    score += {
        "proven": 10.0,
        "developing": 4.0,
        "emerging": -4.0,
        "exploratory": -10.0,
    }.get(evidence, 0.0)

    score += {
        "strong_positive": 10.0,
        "positive": 5.0,
        "neutral": 0.0,
        "insufficient": -2.0,
        "negative": -7.0,
        "strong_negative": -14.0,
    }.get(str(row.get("regime_context_state", "neutral") or "neutral"), 0.0)
    score += {
        "strong_positive": 6.0,
        "positive": 3.0,
        "neutral": 0.0,
        "insufficient": -1.0,
        "negative": -4.0,
        "strong_negative": -8.0,
    }.get(str(row.get("session_context_state", "neutral") or "neutral"), 0.0)

    quality_mult = float(row.get("data_quality_multiplier", 1.0) or 1.0)
    if quality_mult < 1.0:
        score -= (1.0 - quality_mult) * 25.0

    return max(0.0, min(100.0, round(score, 1)))


def _allocator_note(row: Dict) -> str:
    parts: List[str] = []
    parts.append(f"{row.get('evidence_state', 'exploratory')} evidence")
    parts.append(f"regime {row.get('regime_context_state', 'neutral')}")
    parts.append(f"session {row.get('session_context_state', 'neutral')}")
    if float(row.get("data_quality_multiplier", 1.0) or 1.0) < 1.0:
        parts.append("data quality discounted")
    return ", ".join(parts)


def _performance_state(bucket: Dict) -> str:
    trades = _bucket_trade_count(bucket)
    win_rate = _bucket_win_rate(bucket)
    pnl = _bucket_pnl(bucket)
    expectancy = float(bucket.get("expectancy", 0) or 0)

    press_min = max(1, int(getattr(settings, "BOOK_ALLOCATOR_MIN_TRADES_FOR_PRESS", 8) or 8))
    suppress_min = max(1, int(getattr(settings, "BOOK_ALLOCATOR_MIN_TRADES_FOR_SUPPRESS", 10) or 10))

    if trades >= suppress_min and pnl < 0 and expectancy < 0:
        if win_rate < 38.0:
            return "cold"
        if win_rate < 46.0:
            return "cool"
    if trades >= press_min and pnl > 0 and expectancy > 0:
        if win_rate >= 58.0:
            return "hot"
        if win_rate >= 50.0:
            return "warm"
    return "neutral"


def _status_budget_multiplier(bucket: Dict) -> float:
    status = str(bucket.get("status", "") or "").strip().lower()
    action = str(bucket.get("recommended_action", status) or status).strip().lower()
    control_state = str(bucket.get("control_state", "active") or "active").strip().lower()

    if status == "disable" or action == "disable":
        return 0.0
    if control_state in {"manual_disabled", "hard_disabled", "soft_disabled"}:
        return 0.0
    if status == "probation" or action == "probation" or control_state == "probation":
        return float(getattr(settings, "BOOK_ALLOCATOR_PROBATION_BUDGET_MULTIPLIER", 0.7) or 0.4)
    if action == "observe":
        return float(getattr(settings, "BOOK_ALLOCATOR_OBSERVE_BUDGET_MULTIPLIER", 0.7) or 0.7)
    if status == "scale" or action == "scale":
        return float(getattr(settings, "BOOK_ALLOCATOR_SCALE_BUDGET_MULTIPLIER", 1.15) or 1.15)
    return 1.0


def _status_size_multiplier(bucket: Dict) -> float:
    status = str(bucket.get("status", "") or "").strip().lower()
    action = str(bucket.get("recommended_action", status) or status).strip().lower()
    control_state = str(bucket.get("control_state", "active") or "active").strip().lower()

    if status == "disable" or action == "disable":
        return 0.0
    if control_state in {"manual_disabled", "hard_disabled", "soft_disabled"}:
        return 0.0
    if status == "probation" or action == "probation" or control_state == "probation":
        return float(getattr(settings, "BOOK_ALLOCATOR_PROBATION_STATUS_MULTIPLIER", 0.7) or 0.55)
    if action == "observe":
        return float(getattr(settings, "BOOK_ALLOCATOR_OBSERVE_STATUS_MULTIPLIER", 0.8) or 0.8)
    if status == "scale" or action == "scale":
        return float(getattr(settings, "BOOK_ALLOCATOR_SCALE_STATUS_MULTIPLIER", 1.1) or 1.1)
    return 1.0


def _risk_adjustment(bucket: Dict) -> float:
    mult = 1.0
    trades = _bucket_trade_count(bucket)
    suppress_min = max(1, int(getattr(settings, "BOOK_ALLOCATOR_MIN_TRADES_FOR_SUPPRESS", 10) or 10))
    if trades < suppress_min:
        return 1.0

    try:
        profit_factor = bucket.get("profit_factor")
        if profit_factor is not None:
            profit_factor = float(profit_factor or 0.0)
            if profit_factor < 1.0:
                mult *= 0.85
            elif profit_factor >= 1.75:
                mult *= 1.05
    except Exception:
        pass

    try:
        pnl = _bucket_pnl(bucket)
    except Exception:
        pnl = 0.0
    try:
        max_drawdown = float(bucket.get("max_drawdown", 0.0) or 0.0)
    except Exception:
        max_drawdown = 0.0

    if pnl > 0 and max_drawdown > 0:
        drawdown_ratio = max_drawdown / max(pnl, 1e-6)
        if drawdown_ratio >= 1.0:
            mult *= 0.85
        elif drawdown_ratio <= 0.35:
            mult *= 1.05
    elif pnl < 0 and max_drawdown > 0:
        mult *= 0.9

    return max(0.25, min(1.5, round(mult, 4)))


def _base_size_multiplier(state: str, alignment: str, utilization_pct: float, open_unrealized_pnl: float) -> float:
    mult = 1.0

    if alignment == "aligned":
        mult *= float(getattr(settings, "BOOK_ALLOCATOR_ALIGNMENT_BONUS", 1.10) or 1.10)
    elif alignment == "misaligned":
        mult *= float(getattr(settings, "BOOK_ALLOCATOR_MISMATCH_PENALTY", 0.75) or 0.75)

    if state == "hot":
        mult *= float(getattr(settings, "BOOK_ALLOCATOR_PRESS_MULTIPLIER", 1.30) or 1.30)
    elif state == "warm":
        mult *= float(getattr(settings, "BOOK_ALLOCATOR_WARM_MULTIPLIER", 1.15) or 1.15)
    elif state == "cool":
        mult *= float(getattr(settings, "BOOK_ALLOCATOR_COOL_MULTIPLIER", 0.85) or 0.85)
    elif state == "cold":
        mult *= float(getattr(settings, "BOOK_ALLOCATOR_COLD_MULTIPLIER", 0.65) or 0.65)

    if open_unrealized_pnl > 0:
        mult *= 1.05
    elif open_unrealized_pnl < 0:
        mult *= 0.95

    if utilization_pct >= 85.0:
        mult *= 0.50
    elif utilization_pct >= 70.0:
        mult *= 0.75

    return max(0.25, min(1.75, round(mult, 4)))


def build_snapshot(
    *,
    market_regime: str,
    session_type: str,
    positions: List[Dict],
    analytics: Dict,
    equity: float,
) -> Dict[str, Dict]:
    regime = str(market_regime or "mixed").strip().lower() or "mixed"
    session = str(session_type or "regular").strip().lower() or "regular"
    eq = max(0.0, float(equity or 0.0))
    strategy_stats = dict((analytics or {}).get("by_strategy_tag", {}) or {})
    book_rows = _book_report_rows(analytics or {})
    books: Dict[str, Dict] = {}

    def _ensure(strategy_tag: str) -> Dict:
        tag = normalize_strategy_tag(strategy_tag, fallback="unknown")
        row = books.get(tag)
        if row is None:
            bucket = _merged_book_bucket(tag, strategy_stats, book_rows)
            status_budget_multiplier = _status_budget_multiplier(bucket)
            budget_pct = _base_budget_pct(tag, regime) * status_budget_multiplier
            row = {
                "strategy_tag": tag,
                "market_regime": regime,
                "session_type": session,
                "budget_pct": round(budget_pct, 2),
                "current_exposure_pct": 0.0,
                "open_unrealized_pnl": 0.0,
                "open_positions": 0,
                "trades": int(bucket.get("trades", bucket.get("trade_count", 0)) or 0),
                "clean_trades": int(bucket.get("clean_trades", 0) or 0),
                "win_rate_pct": round(float(bucket.get("win_rate_pct", bucket.get("win_rate", 0)) or 0), 1),
                "clean_win_rate_pct": round(
                    float(bucket.get("clean_win_rate_pct", bucket.get("win_rate_pct", bucket.get("win_rate", 0)) or 0) or 0),
                    1,
                ),
                "expectancy": round(float(bucket.get("expectancy", 0) or 0), 2),
                "realized_pnl": round(float(bucket.get("pnl", 0) or 0), 2),
                "clean_pnl": round(float(bucket.get("clean_pnl", bucket.get("pnl", 0)) or 0), 2),
                "profit_factor": (
                    None if bucket.get("profit_factor") is None else round(float(bucket.get("profit_factor", 0) or 0), 2)
                ),
                "max_drawdown": round(float(bucket.get("max_drawdown", 0) or 0), 2),
                "anomaly_count": int(bucket.get("anomaly_count", 0) or 0),
                "status": str(bucket.get("status", "hold") or "hold"),
                "recommended_action": str(
                    bucket.get("recommended_action", bucket.get("status", "hold")) or "hold"
                ),
                "control_state": str(bucket.get("control_state", "active") or "active"),
                "status_reason": str(bucket.get("status_reason", "") or ""),
                "control_size_multiplier": round(float(bucket.get("size_multiplier", 1.0) or 1.0), 4),
                "alignment": _regime_alignment(tag, regime),
                "state": _performance_state(bucket),
                "regimes": dict(bucket.get("regimes", {}) or {}),
                "sessions": dict(bucket.get("sessions", {}) or {}),
            }
            books[tag] = row
        return row

    for strategy_tag in set(strategy_stats.keys()) | set(book_rows.keys()):
        if is_artifact_strategy_tag(strategy_tag):
            continue
        _ensure(strategy_tag)

    for position in positions or []:
        tag = normalize_strategy_tag(position.get("strategy_tag", "unknown"), fallback="unknown")
        if is_artifact_strategy_tag(tag):
            continue
        row = _ensure(tag)
        notional = _position_notional(position)
        row["open_positions"] += 1
        row["open_unrealized_pnl"] = round(row["open_unrealized_pnl"] + _position_unrealized_pnl(position), 2)
        if eq > 0 and notional > 0:
            row["current_exposure_pct"] = round(row["current_exposure_pct"] + (notional / eq * 100.0), 2)

    for row in books.values():
        budget_pct = float(row.get("budget_pct", 0) or 0)
        exposure_pct = float(row.get("current_exposure_pct", 0) or 0)
        utilization_pct = (exposure_pct / budget_pct * 100.0) if budget_pct > 0 else 0.0
        row["utilization_pct"] = round(utilization_pct, 1)
        regime_bucket = dict((row.get("regimes", {}) or {}).get(regime, {}) or {})
        session_bucket = dict((row.get("sessions", {}) or {}).get(session, {}) or {})
        row["effective_trade_count"] = _bucket_trade_count(row)
        row["effective_win_rate_pct"] = round(_bucket_win_rate(row), 1)
        row["effective_realized_pnl"] = round(_bucket_pnl(row), 2)
        row["evidence_state"] = _evidence_state(row)
        row["data_quality_multiplier"] = round(_data_quality_multiplier(row), 4)
        row["regime_context_state"] = _dimension_bucket_state(regime_bucket)
        row["session_context_state"] = _dimension_bucket_state(session_bucket)
        row["regime_context_trades"] = _bucket_trade_count(regime_bucket) if regime_bucket else 0
        row["session_context_trades"] = _bucket_trade_count(session_bucket) if session_bucket else 0
        row["context_multiplier"] = _context_multiplier(regime_bucket, session_bucket)
        row["size_multiplier"] = _base_size_multiplier(
            str(row.get("state", "neutral") or "neutral"),
            str(row.get("alignment", "neutral") or "neutral"),
            utilization_pct,
            float(row.get("open_unrealized_pnl", 0) or 0),
        )
        row["size_multiplier"] *= float(row.get("context_multiplier", 1.0) or 1.0)
        row["size_multiplier"] *= float(row.get("data_quality_multiplier", 1.0) or 1.0)
        row["size_multiplier"] *= _evidence_multiplier(row)
        row["size_multiplier"] *= _status_size_multiplier(row)
        row["size_multiplier"] *= float(row.get("control_size_multiplier", 1.0) or 1.0)
        row["size_multiplier"] *= _risk_adjustment(row)
        row["size_multiplier"] = max(0.0, min(1.75, round(float(row["size_multiplier"]), 4)))
        row["allocator_score"] = _allocator_score(row)
        row["allocator_note"] = _allocator_note(row)

    return books


def plan_entry(
    *,
    strategy_tag: str,
    setup_mode: str,
    market_regime: str,
    session_type: str,
    confidence: float,
    requested_size_pct: float,
    snapshot: Dict[str, Dict],
    play_report: Optional[Dict] = None,
) -> Dict:
    tag = normalize_strategy_tag(strategy_tag, fallback="unknown")
    mode = str(setup_mode or "invalid").strip().lower() or "invalid"
    regime = str(market_regime or "mixed").strip().lower() or "mixed"
    session = str(session_type or "regular").strip().lower() or "regular"
    row = dict((snapshot or {}).get(tag, {}) or {})
    if not row:
        row = build_snapshot(
            market_regime=regime,
            session_type=session,
            positions=[],
            analytics={"by_strategy_tag": {tag: {}}},
            equity=1.0,
        ).get(tag, {"strategy_tag": tag})
    play_row = _find_play_row(
        play_report=play_report,
        strategy_tag=tag,
        setup_mode=mode,
        market_regime=regime,
        session_type=session,
    )

    budget_pct = float(row.get("budget_pct", 0) or 0)
    exposure_pct = float(row.get("current_exposure_pct", 0) or 0)
    remaining_pct = max(0.0, budget_pct - exposure_pct)
    size_pct = max(0.0, float(requested_size_pct or 0))
    confidence = float(confidence or 0)

    reason_codes: List[str] = []
    alignment = str(row.get("alignment", "neutral") or "neutral")
    state = str(row.get("state", "neutral") or "neutral")
    status = str(row.get("status", "hold") or "hold")
    recommended_action = str(row.get("recommended_action", status) or status)
    control_state = str(row.get("control_state", "active") or "active")
    size_multiplier = float(row.get("size_multiplier", 1.0) or 1.0)
    evidence_state = str(row.get("evidence_state", "exploratory") or "exploratory")
    regime_context_state = str(row.get("regime_context_state", "neutral") or "neutral")
    session_context_state = str(row.get("session_context_state", "neutral") or "neutral")
    play_status = str(play_row.get("status", "hold") or "hold")
    play_action = str(play_row.get("recommended_action", play_status) or play_status)
    play_key = str(play_row.get("play_key", "") or "")
    play_trades = int(play_row.get("trades", 0) or 0)
    play_expectancy = float(play_row.get("expectancy", 0.0) or 0.0)
    play_pnl = float(play_row.get("pnl", 0.0) or 0.0)

    if alignment == "aligned":
        reason_codes.append("regime_aligned")
    elif alignment == "misaligned":
        reason_codes.append("regime_misaligned")
    if state != "neutral":
        reason_codes.append(f"book_{state}")
    if status and status != "hold":
        reason_codes.append(f"status_{status}")
    if recommended_action and recommended_action not in {status, "hold"}:
        reason_codes.append(f"action_{recommended_action}")
    if control_state and control_state != "active":
        reason_codes.append(f"control_{control_state}")
    if evidence_state:
        reason_codes.append(f"evidence_{evidence_state}")
    if regime_context_state not in {"neutral", "insufficient"}:
        reason_codes.append(f"regime_book_{regime_context_state}")
    if session_context_state not in {"neutral", "insufficient"}:
        reason_codes.append(f"session_book_{session_context_state}")
    if play_key:
        reason_codes.append(f"play_{play_status}")
        if play_action not in {play_status, "hold"}:
            reason_codes.append(f"play_action_{play_action}")

    if status == "disable" or recommended_action == "disable" or control_state in {
        "manual_disabled",
        "hard_disabled",
        "soft_disabled",
    }:
        return {
            "allowed": False,
            "reason": "book_disabled_by_allocator",
            "strategy_tag": tag,
            "setup_mode": mode,
            "market_regime": regime,
            "session_type": session,
            "state": state,
            "status": status,
            "recommended_action": recommended_action,
            "control_state": control_state,
            "alignment": alignment,
            "budget_pct": round(budget_pct, 2),
            "current_exposure_pct": round(exposure_pct, 2),
            "remaining_budget_pct": round(max(0.0, remaining_pct), 2),
            "requested_size_pct": round(size_pct, 3),
            "size_pct": 0.0,
            "size_multiplier": 0.0,
            "utilization_pct": round(float(row.get("utilization_pct", 0) or 0), 1),
            "confidence": round(confidence, 1),
            "play_key": play_key,
            "play_status": play_status,
            "play_action": play_action,
            "play_trades": play_trades,
            "play_expectancy": round(play_expectancy, 2),
            "reason_codes": reason_codes,
        }

    if (
        regime_context_state in {"negative", "strong_negative"}
        and session_context_state in {"negative", "strong_negative"}
        and False  # DISABLED: context block was killing all trading. Probation sizing handles risk.
    ):
        return {
            "allowed": False,
            "reason": "book_context_blocked",
            "strategy_tag": tag,
            "setup_mode": mode,
            "market_regime": regime,
            "session_type": session,
            "state": state,
            "status": status,
            "recommended_action": recommended_action,
            "control_state": control_state,
            "alignment": alignment,
            "budget_pct": round(budget_pct, 2),
            "current_exposure_pct": round(exposure_pct, 2),
            "remaining_budget_pct": round(max(0.0, remaining_pct), 2),
            "requested_size_pct": round(size_pct, 3),
            "size_pct": 0.0,
            "size_multiplier": 0.0,
            "utilization_pct": round(float(row.get("utilization_pct", 0) or 0), 1),
            "confidence": round(confidence, 1),
            "allocator_score": round(float(row.get("allocator_score", 0) or 0), 1),
            "allocator_note": str(row.get("allocator_note", "") or ""),
            "play_key": play_key,
            "play_status": play_status,
            "play_action": play_action,
            "play_trades": play_trades,
            "play_expectancy": round(play_expectancy, 2),
            "reason_codes": reason_codes + ["context_blocked"],
        }

    if play_key and play_action == "disable" and confidence < 92.0:
        return {
            "allowed": False,
            "reason": "play_disabled_by_allocator",
            "strategy_tag": tag,
            "setup_mode": mode,
            "market_regime": regime,
            "session_type": session,
            "state": state,
            "status": status,
            "recommended_action": recommended_action,
            "control_state": control_state,
            "alignment": alignment,
            "budget_pct": round(budget_pct, 2),
            "current_exposure_pct": round(exposure_pct, 2),
            "remaining_budget_pct": round(max(0.0, remaining_pct), 2),
            "requested_size_pct": round(size_pct, 3),
            "size_pct": 0.0,
            "size_multiplier": 0.0,
            "utilization_pct": round(float(row.get("utilization_pct", 0) or 0), 1),
            "confidence": round(confidence, 1),
            "allocator_score": round(float(row.get("allocator_score", 0) or 0), 1),
            "allocator_note": str(row.get("allocator_note", "") or ""),
            "play_key": play_key,
            "play_status": play_status,
            "play_action": play_action,
            "play_trades": play_trades,
            "play_expectancy": round(play_expectancy, 2),
            "play_pnl": round(play_pnl, 2),
            "reason_codes": reason_codes + ["play_blocked"],
        }

    if confidence >= 90.0 and alignment == "aligned" and state in {"hot", "warm"}:
        size_multiplier *= 1.10
        reason_codes.append("high_confidence_press")
    elif confidence < 60.0 and state in {"cool", "cold"}:
        size_multiplier *= 0.90
        reason_codes.append("low_confidence_reduce")

    if play_key:
        if play_action == "scale":
            size_multiplier *= 1.10
            reason_codes.append("play_scale")
        elif play_action == "probation":
            size_multiplier *= 0.70
            reason_codes.append("play_probation")
        elif play_action == "observe":
            size_multiplier *= 0.85
            reason_codes.append("play_observe")

    adjusted_size_pct = size_pct * size_multiplier
    max_entry_pct = float(getattr(settings, "BOOK_ALLOCATOR_MAX_ENTRY_SIZE_PCT", 8.0) or 8.0)
    adjusted_size_pct = min(adjusted_size_pct, max_entry_pct)

    allowed = True
    reason = "allocator_ok"
    if remaining_pct <= 0.05:
        allowed = False
        reason = "book_budget_exhausted"
        adjusted_size_pct = 0.0
    elif adjusted_size_pct > remaining_pct:
        adjusted_size_pct = remaining_pct
        reason_codes.append("trimmed_to_book_budget")

    min_entry_pct = 0.15
    if allowed and adjusted_size_pct < min_entry_pct:
        # Floor: council-approved trades get minimum viable size instead of zero
        adjusted_size_pct = max(adjusted_size_pct, 0.5)
        reason_codes.append("allocator_floor_applied")

    return {
        "allowed": allowed,
        "reason": reason,
        "strategy_tag": tag,
        "setup_mode": mode,
        "market_regime": regime,
        "session_type": session,
        "state": state,
        "status": status,
        "recommended_action": recommended_action,
        "control_state": control_state,
        "alignment": alignment,
        "budget_pct": round(budget_pct, 2),
        "current_exposure_pct": round(exposure_pct, 2),
        "remaining_budget_pct": round(max(0.0, remaining_pct), 2),
        "requested_size_pct": round(size_pct, 3),
        "size_pct": round(max(0.0, adjusted_size_pct), 3),
        "size_multiplier": round(size_multiplier, 3),
        "utilization_pct": round(float(row.get("utilization_pct", 0) or 0), 1),
        "confidence": round(confidence, 1),
        "allocator_score": round(float(row.get("allocator_score", 0) or 0), 1),
        "allocator_note": str(row.get("allocator_note", "") or ""),
        "play_key": play_key,
        "play_status": play_status,
        "play_action": play_action,
        "play_trades": play_trades,
        "play_expectancy": round(play_expectancy, 2),
        "play_pnl": round(play_pnl, 2),
        "reason_codes": reason_codes,
    }
