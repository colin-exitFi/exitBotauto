"""
Book-to-mode and mode-to-book mapping.

Mode = what kind of setup this is.
Book = which capital bucket owns it.
Play-state = whether it is actionable now.

These three axes never blur.
"""

from __future__ import annotations

from typing import Dict, Optional, Set


BOOK_MODE_MAP: Dict[str, Set[str]] = {
    "momentum_long": {"continuation_long", "general_momentum_long"},
    "momentum_short": {"continuation_short", "general_momentum_short", "exhaustion_fade_short"},
    "uw_flow_long": {"continuation_long", "swing_catalyst_long"},
    "uw_flow_short": {"continuation_short", "exhaustion_fade_short"},
    "congress_follow": {"swing_catalyst_long"},
    "pharma_catalyst": {"swing_catalyst_long"},
    "fade_runner": {"exhaustion_fade_short"},
}

MODE_DEFAULT_BOOK: Dict[str, str] = {
    "continuation_long": "momentum_long",
    "continuation_short": "momentum_short",
    "exhaustion_fade_short": "fade_runner",
    "swing_catalyst_long": "congress_follow",
    "general_momentum_long": "momentum_long",
    "general_momentum_short": "momentum_short",
}


def get_valid_modes_for_book(book: str) -> Set[str]:
    return BOOK_MODE_MAP.get(book, set())


def get_default_book_for_mode(mode: str) -> str:
    return MODE_DEFAULT_BOOK.get(mode, "momentum_long")


def validate_book_mode(book: str, mode: str) -> bool:
    """True if this mode is valid for this book."""
    valid = BOOK_MODE_MAP.get(book)
    if valid is None:
        return True
    return mode in valid


def resolve_book(
    strategy_tag: str,
    setup_mode: str,
    signal_source: str = "",
) -> str:
    """
    Resolve the correct book for a candidate.
    When strategy_tag from scanner conflicts with classifier mode,
    the classifier mode wins for analytics attribution.
    """
    tag = str(strategy_tag or "").lower()
    mode = str(setup_mode or "").lower()

    if tag and tag in BOOK_MODE_MAP:
        if validate_book_mode(tag, mode):
            return tag

    if "congress" in tag or "congress" in str(signal_source or "").lower():
        return "congress_follow"
    if "pharma" in tag or "fda" in tag:
        return "pharma_catalyst"
    if "uw" in tag or "unusual" in str(signal_source or "").lower():
        if "short" in mode:
            return "uw_flow_short"
        return "uw_flow_long"
    if "fade" in tag:
        return "fade_runner"

    return get_default_book_for_mode(mode)
