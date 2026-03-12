"""
Persistent Entry Controls — blacklist, cooldown, jury veto, tombstones.

Survives restarts. All entry paths must check this before opening positions.
Cooldowns anchor to broker-confirmed exit timestamps, not local removal time.
"""

import json
import time
from pathlib import Path
from typing import Dict, Optional
from loguru import logger

DATA_DIR = Path(__file__).parent.parent.parent / "data"
CONTROLS_FILE = DATA_DIR / "entry_controls.json"

_DEFAULT_COOLDOWN_SECONDS = 300
_DEFAULT_BLACKLIST_SECONDS = 86400
_DEFAULT_VETO_SECONDS = 3600


def _normalize(symbol: str) -> str:
    return str(symbol or "").upper().strip()


def _load() -> Dict:
    try:
        if CONTROLS_FILE.exists():
            with open(CONTROLS_FILE) as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.warning(f"Failed to load entry controls: {e}")
    return {"blacklist": {}, "cooldowns": {}, "jury_vetoes": {}, "tombstones": {}}


def _save(data: Dict):
    from src.persistence import atomic_write_json
    atomic_write_json(CONTROLS_FILE, data)


def _prune_expired(store: Dict, now: float) -> Dict:
    return {k: v for k, v in store.items()
            if float(v.get("expires_at", 0) or 0) > now}


def load_controls() -> Dict:
    return _load()


# ── Blacklist ────────────────────────────────────────────────────

def blacklist_symbol(symbol: str, duration_seconds: float = _DEFAULT_BLACKLIST_SECONDS,
                     reason: str = "", source: str = ""):
    sym = _normalize(symbol)
    if not sym:
        return
    data = _load()
    data.setdefault("blacklist", {})
    data["blacklist"][sym] = {
        "expires_at": time.time() + duration_seconds,
        "reason": reason,
        "source": source,
        "blacklisted_at": time.time(),
    }
    _save(data)
    logger.warning(f"BLACKLIST: {sym} for {duration_seconds/3600:.1f}h — {reason}")


def is_blacklisted(symbol: str) -> bool:
    sym = _normalize(symbol)
    data = _load()
    entry = data.get("blacklist", {}).get(sym)
    if not entry:
        return False
    return float(entry.get("expires_at", 0) or 0) > time.time()


# ── Cooldown ─────────────────────────────────────────────────────

def set_cooldown(symbol: str, exit_confirmed_at: Optional[float] = None,
                 cooldown_seconds: float = _DEFAULT_COOLDOWN_SECONDS):
    sym = _normalize(symbol)
    if not sym:
        return
    confirmed_at = exit_confirmed_at or time.time()
    data = _load()
    data.setdefault("cooldowns", {})
    data["cooldowns"][sym] = {
        "exit_confirmed_at": confirmed_at,
        "cooldown_until": confirmed_at + cooldown_seconds,
        "expires_at": confirmed_at + cooldown_seconds,
    }
    _save(data)


def is_in_cooldown(symbol: str) -> bool:
    sym = _normalize(symbol)
    data = _load()
    entry = data.get("cooldowns", {}).get(sym)
    if not entry:
        return False
    return float(entry.get("cooldown_until", 0) or 0) > time.time()


# ── Jury Veto ────────────────────────────────────────────────────

def record_jury_veto(symbol: str, ttl_seconds: float = _DEFAULT_VETO_SECONDS):
    sym = _normalize(symbol)
    if not sym:
        return
    data = _load()
    data.setdefault("jury_vetoes", {})
    data["jury_vetoes"][sym] = {
        "vetoed_at": time.time(),
        "expires_at": time.time() + ttl_seconds,
    }
    _save(data)


def clear_jury_veto(symbol: str):
    sym = _normalize(symbol)
    data = _load()
    data.get("jury_vetoes", {}).pop(sym, None)
    _save(data)


def is_jury_vetoed(symbol: str) -> bool:
    sym = _normalize(symbol)
    data = _load()
    entry = data.get("jury_vetoes", {}).get(sym)
    if not entry:
        return False
    return float(entry.get("expires_at", 0) or 0) > time.time()


# ── Tombstones ───────────────────────────────────────────────────

def tombstone_symbol(symbol: str, reason: str = ""):
    sym = _normalize(symbol)
    if not sym:
        return
    data = _load()
    data.setdefault("tombstones", {})
    data["tombstones"][sym] = {
        "tombstoned_at": time.time(),
        "reason": reason,
    }
    _save(data)


def is_tombstoned(symbol: str) -> bool:
    sym = _normalize(symbol)
    data = _load()
    return sym in data.get("tombstones", {})


# ── Unified Gate ─────────────────────────────────────────────────

def is_entry_blocked(symbol: str) -> tuple:
    """Check all persistent controls. Returns (blocked: bool, reason: str)."""
    sym = _normalize(symbol)
    if is_blacklisted(sym):
        return True, "blacklisted"
    if is_in_cooldown(sym):
        return True, "cooldown"
    if is_jury_vetoed(sym):
        return True, "jury_vetoed"
    if is_tombstoned(sym):
        return True, "tombstoned"
    return False, "ok"


def prune_expired():
    """Remove expired entries from all control categories."""
    now = time.time()
    data = _load()
    data["blacklist"] = _prune_expired(data.get("blacklist", {}), now)
    data["cooldowns"] = _prune_expired(data.get("cooldowns", {}), now)
    data["jury_vetoes"] = _prune_expired(data.get("jury_vetoes", {}), now)
    _save(data)
