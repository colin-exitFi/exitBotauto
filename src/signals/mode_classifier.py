"""
Deterministic setup-mode classifier.

This layer runs before the jury so rich signal is interpreted as structured
features instead of prompt soup.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional


_VALID_MODES = {
    "continuation_long",
    "continuation_short",
    "exhaustion_fade_short",
    "swing_catalyst_long",
    "invalid",
}

_DIRECTION_CONSTRAINTS = {"long_only", "short_only", "none"}


def _to_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _to_int(value, default: int = 0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _normalize_session(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if value in {"regular", "pre", "after", "overnight"}:
        return value
    return "regular"


def _infer_sentiment_percentile(candidate: Dict) -> float:
    bullish = _to_float(candidate.get("st_bullish"), 0.0)
    bearish = _to_float(candidate.get("st_bearish"), 0.0)
    if bullish > 0 or bearish > 0:
        total = bullish + bearish
        if total > 0:
            return round((bullish / total) * 100.0, 2)

    score = _to_float(candidate.get("sentiment_score"), 0.0)
    # Sentiment analyzer scores are roughly centered around [-1, 1].
    return round(_clamp((score + 1.0) * 50.0, 0.0, 100.0), 2)


def _infer_uw_bias(candidate: Dict) -> str:
    for key in (
        "uw_net_premium_bias",
        "uw_options_volume_bias",
        "uw_chain_bias",
        "uw_news_bias",
        "market_tide_bias",
    ):
        value = str(candidate.get(key, "") or "").strip().lower()
        if value in {"bullish", "bearish", "neutral"}:
            return value

    side = str(candidate.get("side", "") or "").strip().lower()
    if side == "short":
        return "bearish"
    if side == "long":
        return "bullish"
    return "neutral"


def _parse_iso(value: object) -> Optional[float]:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _infer_catalyst_tag(candidate: Dict) -> Optional[str]:
    if candidate.get("pharma_signal") or candidate.get("pharma_catalyst_type"):
        return "fda"
    if candidate.get("earnings") or candidate.get("earnings_date"):
        return "earnings"
    if candidate.get("congress_trades"):
        return "congress"
    if candidate.get("insider_activity"):
        return "insider"
    if candidate.get("watchlist_reason") and str(candidate.get("holding_horizon", "")).lower() in {"swing", "multiday"}:
        return "watchlist"
    return None


def _infer_catalyst_age_hours(candidate: Dict, now_ts: Optional[float] = None) -> Optional[float]:
    now_ts = float(now_ts or time.time())
    for key in ("catalyst_timestamp", "signal_timestamp", "earnings_date", "catalyst_date"):
        raw = candidate.get(key)
        ts = None
        if isinstance(raw, (int, float)):
            ts = float(raw)
        else:
            ts = _parse_iso(raw)
        if ts is None or ts <= 0:
            continue
        age_hours = max(0.0, (now_ts - ts) / 3600.0)
        return round(age_hours, 2)
    return None


def _infer_rsi_divergence(candidate: Dict) -> Optional[float]:
    value = candidate.get("rsi_divergence")
    if isinstance(value, (int, float)):
        return float(value)

    rsi = _to_float(candidate.get("rsi_14", candidate.get("rsi")), 50.0)
    range_pct = _to_float(candidate.get("range_pct"), 50.0)
    daily_pct = _to_float(candidate.get("change_pct"), 0.0)
    if daily_pct >= 15.0 and range_pct >= 90.0 and rsi <= 70.0:
        return -0.5
    if daily_pct <= -15.0 and range_pct <= 10.0 and rsi >= 35.0:
        return 0.5
    return None


def _infer_macd_hist_slope(candidate: Dict) -> Optional[float]:
    value = candidate.get("macd_hist_slope")
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _reason_confidence(base: float, features: List[bool], step: float = 0.03, cap: float = 0.95) -> float:
    score = base + (sum(1 for flag in features if flag) * step)
    return round(_clamp(score, 0.0, cap), 2)


def _feature_missing_fields(candidate: Dict) -> List[str]:
    missing = []
    if candidate.get("price") is None:
        missing.append("price")
    if candidate.get("spread_pct") is None:
        missing.append("spread_pct")
    if candidate.get("range_pct") is None:
        missing.append("range_pct")
    if candidate.get("rolling_vwap_pct") is None:
        missing.append("vwap_distance_pct")
    if candidate.get("vol_accel") is None and candidate.get("volume_spike") is None:
        missing.append("volume_accel")
    if (
        candidate.get("st_bullish") is None
        and candidate.get("st_bearish") is None
        and candidate.get("sentiment_score") is None
    ):
        missing.append("sentiment_pct")
    if candidate.get("halt_count") is None and candidate.get("luld_count") is None:
        missing.append("halt_count")
    return missing


def _feature_quality(candidate: Dict, data_age_seconds: float, holding_horizon: str) -> tuple[float, str, List[str]]:
    missing = _feature_missing_fields(candidate)
    core_count = 7
    score = 1.0 - (len(missing) / float(core_count))
    stale_penalty = 0.0
    if holding_horizon in {"intraday"}:
        if data_age_seconds > 300:
            stale_penalty = 0.35
        elif data_age_seconds > 120:
            stale_penalty = 0.2
        elif data_age_seconds > 60:
            stale_penalty = 0.1
    elif data_age_seconds > 900:
        stale_penalty = 0.1
    score = _clamp(score - stale_penalty, 0.0, 1.0)
    if score >= 0.8:
        quality = "high"
    elif score >= 0.55:
        quality = "medium"
    else:
        quality = "low"
    return round(score, 2), quality, missing


def _apply_regime_overlay(confidence: float, mode: str, market_regime: str) -> tuple[float, Optional[str]]:
    conf = float(confidence or 0.0)
    regime = str(market_regime or "mixed").strip().lower()
    mode = str(mode or "").strip().lower()
    if regime == "risk_on":
        if mode == "continuation_long":
            return round(_clamp(conf + 0.04, 0.0, 0.95), 2), "regime_tailwind"
        if mode == "exhaustion_fade_short":
            return round(_clamp(conf - 0.03, 0.0, 0.95), 2), "regime_headwind"
        if mode == "swing_catalyst_long":
            return round(_clamp(conf + 0.02, 0.0, 0.95), 2), "regime_tailwind"
    if regime == "risk_off":
        if mode in {"exhaustion_fade_short", "continuation_short"}:
            return round(_clamp(conf + 0.04, 0.0, 0.95), 2), "regime_tailwind"
        if mode in {"continuation_long", "swing_catalyst_long"}:
            return round(_clamp(conf - 0.05, 0.0, 0.95), 2), "regime_headwind"
    return round(_clamp(conf, 0.0, 0.95), 2), None


@dataclass
class ModeFeatures:
    symbol: str
    price: float
    daily_pct: float
    range_pct: float
    spread_pct: float
    volume_rel: float
    volume_accel: float
    halt_count: int
    sentiment_pct: float
    rsi_5m: Optional[float]
    rsi_divergence: Optional[float]
    macd_hist_slope: Optional[float]
    vwap_distance_pct: Optional[float]
    reclaiming_vwap: bool
    losing_vwap: bool
    uw_bias: str
    uw_premium: float
    catalyst_tag: Optional[str]
    catalyst_age_hours: Optional[float]
    session: str
    entry_quality: str
    holding_horizon: str
    sector: Optional[str]
    market_regime: str
    created_at: float
    last_refreshed_at: float
    data_age_seconds: float
    feature_quality_score: float
    feature_quality: str
    missing_fields: List[str] = field(default_factory=list)
    minute_notional_liquidity: float = 0.0
    bar_context: Dict = field(default_factory=dict)
    anomaly_flags: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ModeClassification:
    mode: str
    direction_constraint: str
    classifier_confidence: float
    reason_codes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes or [])
        return payload


def build_mode_features(candidate: Dict, now_ts: Optional[float] = None) -> ModeFeatures:
    now_ts = float(now_ts or time.time())
    vol_ratio = _to_float(candidate.get("vol_accel", candidate.get("volume_spike")), 1.0)
    volume_accel = round(vol_ratio - 1.0, 4)
    vwap_distance_pct = candidate.get("rolling_vwap_pct")
    if not isinstance(vwap_distance_pct, (int, float)):
        vwap_distance_pct = None

    range_pct = _clamp(_to_float(candidate.get("range_pct"), 50.0), 0.0, 100.0)
    entry_quality = str(candidate.get("entry_quality", "neutral") or "neutral").lower()
    holding_horizon = str(candidate.get("holding_horizon", "intraday") or "intraday").lower()
    market_regime = str(candidate.get("market_regime", "mixed") or "mixed").lower()
    # Use the most recent timestamp available -- last_refreshed (from scanner) > signal_timestamp > created_at
    created_at = _to_float(
        candidate.get("last_refreshed")
        or candidate.get("last_refreshed_at")
        or candidate.get("enriched_at")
        or candidate.get("signal_timestamp")
        or candidate.get("created_at"),
        now_ts,
    )
    data_age_seconds = max(0.0, now_ts - created_at)
    session = _normalize_session(candidate.get("session_type") or candidate.get("session"))
    if candidate.get("extended_hours"):
        session = session if session in {"pre", "after"} else "after"

    reclaiming_vwap = isinstance(vwap_distance_pct, (int, float)) and float(vwap_distance_pct) >= 0.05
    losing_vwap = isinstance(vwap_distance_pct, (int, float)) and float(vwap_distance_pct) <= -0.05

    anomaly_flags = candidate.get("anomaly_flags", []) or []
    if isinstance(anomaly_flags, str):
        anomaly_flags = [flag.strip() for flag in anomaly_flags.split(",") if flag.strip()]
    if not isinstance(anomaly_flags, list):
        anomaly_flags = []
    feature_quality_score, feature_quality, missing_fields = _feature_quality(
        candidate,
        data_age_seconds=data_age_seconds,
        holding_horizon=holding_horizon,
    )
    minute_notional = _to_float(candidate.get("minute_vol"), 0.0) * _to_float(candidate.get("price"), 0.0)

    return ModeFeatures(
        symbol=str(candidate.get("symbol", "") or "").upper(),
        price=_to_float(candidate.get("price"), 0.0),
        daily_pct=_to_float(candidate.get("change_pct"), 0.0),
        range_pct=round(range_pct, 2),
        spread_pct=_to_float(candidate.get("spread_pct"), 0.0),
        volume_rel=_to_float(candidate.get("volume_spike"), vol_ratio),
        volume_accel=volume_accel,
        halt_count=_to_int(candidate.get("halt_count", candidate.get("luld_count")), 0),
        sentiment_pct=_infer_sentiment_percentile(candidate),
        rsi_5m=(
            round(_to_float(candidate.get("rsi_14", candidate.get("rsi")), 0.0), 2)
            if candidate.get("rsi_14", candidate.get("rsi")) is not None
            else None
        ),
        rsi_divergence=_infer_rsi_divergence(candidate),
        macd_hist_slope=_infer_macd_hist_slope(candidate),
        vwap_distance_pct=round(float(vwap_distance_pct), 2) if isinstance(vwap_distance_pct, (int, float)) else None,
        reclaiming_vwap=reclaiming_vwap,
        losing_vwap=losing_vwap,
        uw_bias=_infer_uw_bias(candidate),
        uw_premium=_to_float(candidate.get("uw_total_premium", candidate.get("premium")), 0.0),
        catalyst_tag=_infer_catalyst_tag(candidate),
        catalyst_age_hours=_infer_catalyst_age_hours(candidate, now_ts=now_ts),
        session=session,
        entry_quality=entry_quality,
        holding_horizon=holding_horizon,
        sector=str(candidate.get("sector", "") or "").upper() or None,
        market_regime=market_regime,
        created_at=created_at,
        last_refreshed_at=now_ts,
        data_age_seconds=round(data_age_seconds, 2),
        feature_quality_score=feature_quality_score,
        feature_quality=feature_quality,
        missing_fields=missing_fields,
        minute_notional_liquidity=round(minute_notional, 2),
        bar_context=dict(candidate.get("bar_context", {}) or {}),
        anomaly_flags=list(anomaly_flags),
    )


def mode_features_from_dict(payload: Dict) -> Optional[ModeFeatures]:
    if not isinstance(payload, dict):
        return None
    symbol = str(payload.get("symbol", "") or "").upper().strip()
    if not symbol:
        return None

    missing_fields = payload.get("missing_fields", []) or []
    if isinstance(missing_fields, str):
        missing_fields = [field.strip() for field in missing_fields.split(",") if field.strip()]
    if not isinstance(missing_fields, list):
        missing_fields = []

    anomaly_flags = payload.get("anomaly_flags", []) or []
    if isinstance(anomaly_flags, str):
        anomaly_flags = [flag.strip() for flag in anomaly_flags.split(",") if flag.strip()]
    if not isinstance(anomaly_flags, list):
        anomaly_flags = []

    return ModeFeatures(
        symbol=symbol,
        price=_to_float(payload.get("price"), 0.0),
        daily_pct=_to_float(payload.get("daily_pct"), 0.0),
        range_pct=_to_float(payload.get("range_pct"), 0.0),
        spread_pct=_to_float(payload.get("spread_pct"), 0.0),
        volume_rel=_to_float(payload.get("volume_rel"), 0.0),
        volume_accel=_to_float(payload.get("volume_accel"), 0.0),
        halt_count=_to_int(payload.get("halt_count"), 0),
        sentiment_pct=_to_float(payload.get("sentiment_pct"), 0.0),
        rsi_5m=_to_float(payload.get("rsi_5m"), 0.0) if payload.get("rsi_5m") is not None else None,
        rsi_divergence=(
            _to_float(payload.get("rsi_divergence"), 0.0) if payload.get("rsi_divergence") is not None else None
        ),
        macd_hist_slope=(
            _to_float(payload.get("macd_hist_slope"), 0.0)
            if payload.get("macd_hist_slope") is not None
            else None
        ),
        vwap_distance_pct=(
            _to_float(payload.get("vwap_distance_pct"), 0.0)
            if payload.get("vwap_distance_pct") is not None
            else None
        ),
        reclaiming_vwap=bool(payload.get("reclaiming_vwap", False)),
        losing_vwap=bool(payload.get("losing_vwap", False)),
        uw_bias=str(payload.get("uw_bias", "neutral") or "neutral"),
        uw_premium=_to_float(payload.get("uw_premium"), 0.0),
        catalyst_tag=str(payload.get("catalyst_tag", "") or "").lower() or None,
        catalyst_age_hours=(
            _to_float(payload.get("catalyst_age_hours"), 0.0)
            if payload.get("catalyst_age_hours") is not None
            else None
        ),
        session=_normalize_session(payload.get("session")),
        entry_quality=str(payload.get("entry_quality", "neutral") or "neutral").lower(),
        holding_horizon=str(payload.get("holding_horizon", "intraday") or "intraday").lower(),
        sector=str(payload.get("sector", "") or "").upper() or None,
        market_regime=str(payload.get("market_regime", "mixed") or "mixed").lower(),
        created_at=_to_float(payload.get("created_at"), time.time()),
        last_refreshed_at=_to_float(payload.get("last_refreshed_at"), time.time()),
        data_age_seconds=_to_float(payload.get("data_age_seconds"), 0.0),
        feature_quality_score=_to_float(payload.get("feature_quality_score"), 0.0),
        feature_quality=str(payload.get("feature_quality", "") or ""),
        missing_fields=list(missing_fields),
        minute_notional_liquidity=_to_float(payload.get("minute_notional_liquidity"), 0.0),
        bar_context=dict(payload.get("bar_context", {}) or {}),
        anomaly_flags=list(anomaly_flags),
    )


def classify_mode(features: ModeFeatures) -> ModeClassification:
    symbol = features.symbol or "?"
    if features.feature_quality_score < 0.25:
        return ModeClassification(
            mode="invalid",
            direction_constraint="none",
            classifier_confidence=0.9,
            reason_codes=["low_feature_quality", *[f"missing_{name}" for name in features.missing_fields[:4]]],
        )
    # Staleness is handled by feature_quality scoring (confidence haircut),
    # not as a hard block. Scanner actively refreshes candidates each cycle.
    if features.price <= 0 or features.price < 1.0:
        return ModeClassification(
            mode="invalid",
            direction_constraint="none",
            classifier_confidence=0.95,
            reason_codes=["price_too_low", f"symbol_{symbol}"],
        )
    if "halted" in {str(flag).lower() for flag in features.anomaly_flags}:
        return ModeClassification(
            mode="invalid",
            direction_constraint="none",
            classifier_confidence=0.9,
            reason_codes=["active_halt", f"symbol_{symbol}"],
        )

    exhaustion_flags = [
        features.daily_pct >= 20.0,
        features.volume_accel < 0.0,
        features.halt_count >= 2,
        features.sentiment_pct >= 75.0,
        features.range_pct >= 88.0,
        features.losing_vwap,
        (features.rsi_divergence or 0.0) < 0.0,
    ]
    if exhaustion_flags[0] and exhaustion_flags[1] and (exhaustion_flags[2] or exhaustion_flags[4] or exhaustion_flags[5]) and exhaustion_flags[3]:
        reasons = ["daily_extension_extreme", "volume_decelerating", "sentiment_extreme"]
        if features.halt_count >= 3:
            reasons.append("halts_elevated")
        if features.range_pct >= 92.0:
            reasons.append("range_extended")
        if features.losing_vwap:
            reasons.append("below_vwap")
        if (features.rsi_divergence or 0.0) < 0.0:
            reasons.append("rsi_bearish_divergence")
        confidence, regime_code = _apply_regime_overlay(
            _reason_confidence(0.76, exhaustion_flags),
            "exhaustion_fade_short",
            features.market_regime,
        )
        if regime_code:
            reasons.append(regime_code)
        return ModeClassification(
            mode="exhaustion_fade_short",
            direction_constraint="short_only",
            classifier_confidence=confidence,
            reason_codes=reasons,
        )

    if (
        features.catalyst_tag in {"congress", "insider", "fda", "earnings", "watchlist"}
        and features.holding_horizon in {"swing", "multiday"}
        and (features.catalyst_age_hours is None or features.catalyst_age_hours <= 168.0)
    ):
        catalyst_flags = [
            features.catalyst_tag in {"congress", "insider", "fda", "earnings", "watchlist"},
            features.holding_horizon in {"swing", "multiday"},
            features.spread_pct < 0.8,
            features.range_pct < 90.0,
            features.uw_bias != "bearish",
        ]
        reasons = ["fresh_catalyst", f"catalyst_{features.catalyst_tag}", f"horizon_{features.holding_horizon}"]
        if features.spread_pct < 0.8:
            reasons.append("spread_ok")
        if features.range_pct < 90.0:
            reasons.append("not_too_extended")
        confidence, regime_code = _apply_regime_overlay(
            _reason_confidence(0.66, catalyst_flags),
            "swing_catalyst_long",
            features.market_regime,
        )
        if regime_code:
            reasons.append(regime_code)
        return ModeClassification(
            mode="swing_catalyst_long",
            direction_constraint="long_only",
            classifier_confidence=confidence,
            reason_codes=reasons,
        )

    continuation_long_flags = [
        features.daily_pct >= 1.5,
        features.volume_accel >= 0.0,
        features.spread_pct < 2.0,
        features.entry_quality in {"pullback", "neutral"},
        features.range_pct < 95.0,
        features.reclaiming_vwap or (features.vwap_distance_pct or 0.0) > -0.5,
    ]
    if all(continuation_long_flags[:4]) and continuation_long_flags[4]:
        reasons = ["trend_intact", "volume_reaccelerating", "spread_ok", f"entry_quality_{features.entry_quality}"]
        if continuation_long_flags[5]:
            reasons.append("vwap_supported")
        if features.uw_bias == "bullish":
            reasons.append("uw_bias_bullish")
        confidence, regime_code = _apply_regime_overlay(
            _reason_confidence(0.63, continuation_long_flags),
            "continuation_long",
            features.market_regime,
        )
        if regime_code:
            reasons.append(regime_code)
        return ModeClassification(
            mode="continuation_long",
            direction_constraint="long_only",
            classifier_confidence=confidence,
            reason_codes=reasons,
        )

    continuation_short_flags = [
        features.daily_pct <= -1.5,
        features.volume_rel > 1.0,
        features.volume_accel >= 0.0,
        features.spread_pct < 2.0,
        features.sentiment_pct <= 45.0,
        features.losing_vwap or (features.vwap_distance_pct or 0.0) < 0.5,
    ]
    if all(continuation_short_flags[:4]):
        reasons = ["downtrend_intact", "volume_confirming", "spread_ok", "volume_rel_strong"]
        if continuation_short_flags[4]:
            reasons.append("sentiment_weak")
        if continuation_short_flags[5]:
            reasons.append("below_vwap")
        if features.uw_bias == "bearish":
            reasons.append("uw_bias_bearish")
        confidence, regime_code = _apply_regime_overlay(
            _reason_confidence(0.61, continuation_short_flags),
            "continuation_short",
            features.market_regime,
        )
        if regime_code:
            reasons.append(regime_code)
        return ModeClassification(
            mode="continuation_short",
            direction_constraint="short_only",
            classifier_confidence=confidence,
            reason_codes=reasons,
        )

    return ModeClassification(
        mode="invalid",
        direction_constraint="none",
        classifier_confidence=0.0,
        reason_codes=["no_clear_setup"],
    )


def normalize_mode(mode: object) -> str:
    value = str(mode or "").strip().lower()
    if value in _VALID_MODES:
        return value
    return "invalid"


def normalize_direction_constraint(value: object) -> str:
    text = str(value or "").strip().lower()
    if text in _DIRECTION_CONSTRAINTS:
        return text
    return "none"
