"""
Shared symbol normalization and hygiene helpers.
"""

from __future__ import annotations

import re

_ALIASES = {
    "BRKA": "BRK.A",
    "BRKB": "BRK.B",
    "BFA": "BF.A",
    "BFB": "BF.B",
}

_EXCLUDED_SYMBOLS = {
    "RUT",
    "SPX",
    "SPXW",
    "VIX",
    "VIXY",
    "VVIX",
    "NDX",
    "DJI",
    "DJT",
    "OEX",
    "VXN",
}

_ALLOWED_DOTTED = {"BRK.A", "BRK.B", "BF.A", "BF.B"}
_PATTERN = re.compile(r"^[A-Z]{1,5}(?:\.[A-Z])?$")


def normalize_trade_symbol(symbol: object) -> str:
    text = str(symbol or "").upper().strip()
    if not text:
        return ""
    text = text.replace("/", ".")
    return _ALIASES.get(text, text)


def is_supported_trade_symbol(symbol: object) -> bool:
    text = normalize_trade_symbol(symbol)
    if not text or text in _EXCLUDED_SYMBOLS:
        return False
    if any(ch in text for ch in ("-", "^", "=", ":")):
        return False
    if "." in text and text not in _ALLOWED_DOTTED:
        return False
    return bool(_PATTERN.fullmatch(text))
