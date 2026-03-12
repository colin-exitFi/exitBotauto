"""
Shared strategy playbook metadata and gating helpers.
"""

from copy import deepcopy
from typing import Dict, Iterable, Optional, Set


_DEFAULT_PLAYBOOK = {
    "label": "Unknown",
    "live": False,
    "allowed_regimes": ["mixed"],
    "options_mode": "off",
    "thesis_mode": "intraday",
    "min_signal_age_seconds": 0,
    "requires_uw_confirmation": False,
    "watchlist_only": False,
}

_PLAYBOOKS = {
    "momentum_long": {
        "label": "Momentum Long",
        "live": True,
        "allowed_regimes": ["risk_on", "mixed"],
        "options_mode": "off",
        "thesis_mode": "required",
        "min_signal_age_seconds": 90,
    },
    "momentum_short": {
        "label": "Momentum Short",
        "live": True,
        "allowed_regimes": ["risk_off", "mixed"],
        "options_mode": "off",
        "thesis_mode": "required",
        "min_signal_age_seconds": 90,
    },
    "social_momentum_long": {
        "label": "Social Momentum Long",
        "live": True,
        "allowed_regimes": ["risk_on"],
        "options_mode": "off",
        "thesis_mode": "required",
        "min_signal_age_seconds": 120,
    },
    "social_momentum_short": {
        "label": "Social Momentum Short",
        "live": True,
        "allowed_regimes": ["risk_off"],
        "options_mode": "off",
        "thesis_mode": "required",
        "min_signal_age_seconds": 120,
    },
    "pharma_catalyst": {
        "label": "Pharma Catalyst",
        "live": True,
        "allowed_regimes": ["risk_on", "mixed"],
        "options_mode": "off",
        "thesis_mode": "required",
        "min_signal_age_seconds": 60,
    },
    "fade_short": {
        "label": "Fade Short",
        "live": True,
        "allowed_regimes": ["risk_off", "mixed"],
        "options_mode": "off",
        "thesis_mode": "intraday",
        "min_signal_age_seconds": 60,
    },
    "copy_trader_long": {
        "label": "Copy Trader Long",
        "live": True,
        "allowed_regimes": ["risk_on", "mixed"],
        "options_mode": "off",
        "thesis_mode": "intraday",
        "min_signal_age_seconds": 0,
    },
    "copy_trader_short": {
        "label": "Copy Trader Short",
        "live": True,
        "allowed_regimes": ["risk_off", "mixed"],
        "options_mode": "off",
        "thesis_mode": "intraday",
        "min_signal_age_seconds": 0,
    },
    "watchlist_long": {
        "label": "Watchlist Long",
        "live": True,
        "allowed_regimes": ["risk_on", "mixed"],
        "options_mode": "off",
        "thesis_mode": "required",
        "watchlist_only": True,
        "min_signal_age_seconds": 0,
    },
    "watchlist_short": {
        "label": "Watchlist Short",
        "live": True,
        "allowed_regimes": ["risk_off", "mixed"],
        "options_mode": "off",
        "thesis_mode": "required",
        "watchlist_only": True,
        "min_signal_age_seconds": 0,
    },
    "uw_flow_long": {
        "label": "UW Flow Long",
        "live": True,
        "allowed_regimes": ["risk_on", "mixed"],
        "options_mode": "prefer",
        "thesis_mode": "intraday",
        "requires_uw_confirmation": True,
        "min_signal_age_seconds": 0,
    },
    "uw_flow_short": {
        "label": "UW Flow Short",
        "live": True,
        "allowed_regimes": ["risk_off", "mixed"],
        "options_mode": "prefer",
        "thesis_mode": "intraday",
        "requires_uw_confirmation": True,
        "min_signal_age_seconds": 0,
    },
}

_BULLISH_BIAS_WORDS: Set[str] = {
    "bullish",
    "risk_on",
    "long",
    "calls",
    "call",
    "uptrend",
    "positive",
}
_BEARISH_BIAS_WORDS: Set[str] = {
    "bearish",
    "risk_off",
    "short",
    "puts",
    "put",
    "downtrend",
    "negative",
}


def get_playbook(strategy_tag: str) -> Dict:
    tag = str(strategy_tag or "").strip()
    profile = deepcopy(_DEFAULT_PLAYBOOK)
    profile.update(deepcopy(_PLAYBOOKS.get(tag, {})))
    profile["strategy_tag"] = tag or "unknown"
    return profile


def normalize_bias_label(value: Optional[str]) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return "unknown"
    if any(word in text for word in ("unknown", "unclear", "n/a")):
        return "unknown"
    if any(word in text for word in _BULLISH_BIAS_WORDS):
        return "bullish"
    if any(word in text for word in _BEARISH_BIAS_WORDS):
        return "bearish"
    if any(word in text for word in ("mixed", "neutral", "balanced", "range", "choppy")):
        return "mixed"
    return text


def bias_matches_direction(bias: Optional[str], direction: Optional[str]) -> bool:
    normalized = normalize_bias_label(bias)
    dir_norm = str(direction or "").strip().upper()
    if normalized in ("unknown", "mixed", ""):
        return True
    if dir_norm == "SHORT":
        return normalized == "bearish"
    return normalized == "bullish"


def extract_watchlist_symbols(thesis: Optional[Dict]) -> Set[str]:
    symbols: Set[str] = set()
    if not isinstance(thesis, dict):
        return symbols
    for item in thesis.get("watchlist", []) or []:
        if isinstance(item, dict):
            symbol = str(item.get("symbol") or item.get("ticker") or "").strip().upper()
        else:
            symbol = str(item or "").strip().upper()
        if symbol:
            symbols.add(symbol)
    return symbols


def score_directional_biases(values: Iterable[Optional[str]]) -> Dict[str, int]:
    bullish = 0
    bearish = 0
    for value in values:
        normalized = normalize_bias_label(value)
        if normalized == "bullish":
            bullish += 1
        elif normalized == "bearish":
            bearish += 1
    return {"bullish": bullish, "bearish": bearish}


def annotate_candidate(candidate: Dict, direction: Optional[str] = None) -> Dict:
    annotated = dict(candidate or {})
    strategy_tag = str(
        annotated.get("strategy_tag")
        or annotated.get("playbook_strategy_tag")
        or "unknown"
    ).strip()
    if strategy_tag == "unknown" and direction is not None:
        annotated["strategy_tag"] = strategy_tag
    profile = get_playbook(strategy_tag)
    annotated["playbook"] = deepcopy(profile)
    annotated["playbook_label"] = profile["label"]
    annotated["playbook_live"] = bool(profile["live"])
    annotated["playbook_allowed_regimes"] = list(profile["allowed_regimes"])
    annotated["playbook_options_mode"] = str(profile["options_mode"])
    annotated["playbook_thesis_mode"] = str(profile["thesis_mode"])
    annotated["playbook_min_signal_age_seconds"] = int(profile["min_signal_age_seconds"])
    annotated["playbook_requires_uw_confirmation"] = bool(profile["requires_uw_confirmation"])
    annotated["playbook_watchlist_only"] = bool(profile["watchlist_only"])
    return annotated
