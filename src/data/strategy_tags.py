"""
Helpers for strategy-tag normalization and book-level analytics.
"""

from typing import Optional

ARTIFACT_STRATEGY_TAGS = {
    "carryover",
    "broker_reconciled",
}

PRIMARY_BOOKS = (
    "momentum_long",
    "momentum_short",
    "social_momentum_long",
    "social_momentum_short",
    "uw_flow_long",
    "uw_flow_short",
    "fade_short",
    "copy_trader_long",
    "copy_trader_short",
    "watchlist_long",
    "watchlist_short",
    "pharma_catalyst",
    "congress_follow",
)


def normalize_strategy_tag(
    tag: object,
    fallback: str = "unknown",
    allow_artifacts: bool = False,
) -> str:
    normalized = str(tag or "").strip().lower()
    if not normalized:
        return str(fallback or "unknown").strip().lower() or "unknown"
    if not allow_artifacts and normalized in ARTIFACT_STRATEGY_TAGS:
        return str(fallback or "unknown").strip().lower() or "unknown"
    return normalized


def is_artifact_strategy_tag(tag: object) -> bool:
    return normalize_strategy_tag(tag, fallback="", allow_artifacts=True) in ARTIFACT_STRATEGY_TAGS
