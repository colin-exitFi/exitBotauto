"""
Shared signal attribution helpers.
Used by scanner ranking and trade attribution paths to avoid drift.
"""

from typing import Dict, List, Optional


def extract_signal_sources(candidate: Dict) -> List[str]:
    """Build normalized signal source tags from scanner candidate fields."""
    raw_source = str(candidate.get("source", "")).strip().lower()
    sources: List[str] = []

    if raw_source == "both":
        sources.extend(["polygon", "stocktwits"])
    elif raw_source:
        for part in raw_source.split("+"):
            tag = part.strip()
            if tag:
                sources.append(tag)

    if candidate.get("pharma_signal") and "pharma" not in sources:
        sources.append("pharma")
    if candidate.get("fade_signal") and "fade" not in sources:
        sources.append("fade")
    if candidate.get("grok_x_reason") and "grok_x" not in sources:
        sources.append("grok_x")
    if candidate.get("stocktwits_trending_score", 0) and "stocktwits" not in sources:
        sources.append("stocktwits")

    seen = set()
    deduped = []
    for src in sources:
        if src and src not in seen:
            seen.add(src)
            deduped.append(src)
    return deduped or ["unknown"]


def derive_strategy_tag(candidate: Dict, direction: Optional[str] = None) -> str:
    """
    Tag strategy from candidate/source context.
    direction may be BUY/SHORT for entry paths or omitted for scanner-side inference.
    """
    src = str(candidate.get("source", "")).lower()
    dir_raw = (direction or candidate.get("side", "long") or "long")
    dir_norm = str(dir_raw).strip().lower()
    is_short = dir_norm in ("short", "sell_short")

    if candidate.get("fade_signal") or "fade" in src:
        return "fade_short"
    if candidate.get("pharma_signal") or "pharma" in src:
        return "pharma_catalyst"
    if "grok_x" in src or "stocktwits" in src or src == "both":
        return "social_momentum_short" if is_short else "social_momentum_long"
    if is_short:
        return "momentum_short"
    return "momentum_long"

