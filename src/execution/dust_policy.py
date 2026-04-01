"""
Dust and partial fill policy — formalized rules for handling residual
positions, partial fills, and protection resizing.
"""

from __future__ import annotations

from typing import Dict

from loguru import logger


DUST_RULES = {
    "min_tradable_notional": 10.0,
    "auto_liquidate_threshold": 5.0,
    "residual_blocks_new_entry": True,
    "partial_fill_min_pct": 0.5,
    "protection_resize_on_partial": True,
}


def is_dust(position: Dict) -> bool:
    """True if position is below minimum tradable threshold."""
    notional = _notional(position)
    return 0 < notional < DUST_RULES["min_tradable_notional"]


def should_auto_liquidate(position: Dict) -> bool:
    """True if position should be automatically closed."""
    notional = _notional(position)
    return 0 < notional < DUST_RULES["auto_liquidate_threshold"]


def residual_blocks_entry(symbol: str, positions: Dict[str, Dict]) -> bool:
    """True if a dust residual for this symbol blocks new entry."""
    if not DUST_RULES["residual_blocks_new_entry"]:
        return False
    pos = positions.get(symbol)
    if pos and is_dust(pos):
        return True
    return False


def is_partial_fill(intended_qty: float, actual_qty: float) -> bool:
    """True if fill was partial (less than configured minimum %)."""
    if intended_qty <= 0:
        return False
    fill_pct = actual_qty / intended_qty
    return fill_pct < DUST_RULES["partial_fill_min_pct"]


def should_resize_protection(intended_qty: float, actual_qty: float) -> bool:
    """True if protection orders should be resized to match actual fill."""
    if not DUST_RULES["protection_resize_on_partial"]:
        return False
    return actual_qty > 0 and actual_qty != intended_qty


def _notional(position: Dict) -> float:
    for key in ("actual_notional", "notional", "market_value"):
        try:
            val = float(position.get(key, 0) or 0)
            if val:
                return abs(val)
        except Exception:
            pass
    try:
        price = float(position.get("entry_price", 0) or 0)
        qty = float(position.get("quantity", position.get("qty", 0)) or 0)
        return abs(price * qty)
    except Exception:
        return 0.0
