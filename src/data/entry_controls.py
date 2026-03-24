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
_DEFAULT_SYMBOL_LOSS_LOCK_SECONDS = 86400
_DEFAULT_SYMBOL_CONSECUTIVE_LOSS_LIMIT = 2


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
    return {
        "blacklist": {},
        "cooldowns": {},
        "jury_vetoes": {},
        "tombstones": {},
        "pending_setups": {},
        "symbol_trade_state": {},
        "symbol_loss_locks": {},
    }


def _save(data: Dict):
    from src.persistence import atomic_write_json
    atomic_write_json(CONTROLS_FILE, data)


def _prune_expired(store: Dict, now: float) -> Dict:
    return {k: v for k, v in store.items()
            if float(v.get("expires_at", 0) or 0) > now}


def load_controls() -> Dict:
    return _load()


def load_pending_setups_store() -> Dict:
    data = _load()
    store = data.get("pending_setups", {})
    return store if isinstance(store, dict) else {}


def save_pending_setups_store(store: Dict):
    data = _load()
    data["pending_setups"] = store if isinstance(store, dict) else {}
    _save(data)


def get_symbol_trade_state(symbol: str) -> Dict:
    sym = _normalize(symbol)
    if not sym:
        return {}
    data = _load()
    state = dict((data.get("symbol_trade_state", {}) or {}).get(sym, {}) or {})
    lock = dict((data.get("symbol_loss_locks", {}) or {}).get(sym, {}) or {})
    if lock and float(lock.get("expires_at", 0) or 0) > time.time():
        state["active_lock"] = lock
    return state


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


# ── Same-Symbol Loss Locks ───────────────────────────────────────

def get_symbol_loss_lock(symbol: str) -> Dict:
    sym = _normalize(symbol)
    if not sym:
        return {}
    data = _load()
    entry = dict((data.get("symbol_loss_locks", {}) or {}).get(sym, {}) or {})
    if float(entry.get("expires_at", 0) or 0) <= time.time():
        return {}
    return entry


def is_symbol_loss_locked(symbol: str) -> bool:
    return bool(get_symbol_loss_lock(symbol))


def clear_symbol_loss_lock(symbol: str):
    sym = _normalize(symbol)
    if not sym:
        return
    data = _load()
    data.get("symbol_loss_locks", {}).pop(sym, None)
    _save(data)


def record_symbol_trade_result(
    symbol: str,
    pnl: float,
    *,
    exit_confirmed_at: Optional[float] = None,
    reason: str = "",
    setup_id: str = "",
    loss_limit: int = _DEFAULT_SYMBOL_CONSECUTIVE_LOSS_LIMIT,
    lock_seconds: float = _DEFAULT_SYMBOL_LOSS_LOCK_SECONDS,
) -> Dict:
    sym = _normalize(symbol)
    if not sym:
        return {}

    now_ts = float(exit_confirmed_at or time.time())
    loss_limit = max(1, int(loss_limit or _DEFAULT_SYMBOL_CONSECUTIVE_LOSS_LIMIT))
    lock_seconds = max(0.0, float(lock_seconds or 0.0))

    data = _load()
    state_store = data.setdefault("symbol_trade_state", {})
    lock_store = data.setdefault("symbol_loss_locks", {})
    state = dict(state_store.get(sym, {}) or {})
    recent_results = list(state.get("recent_results", []) or [])
    pnl_value = float(pnl or 0.0)

    if pnl_value < 0:
        outcome = "loss"
        consecutive_losses = int(state.get("consecutive_losses", 0) or 0) + 1
        consecutive_wins = 0
        attempts_since_win = int(state.get("attempts_since_win", 0) or 0) + 1
    elif pnl_value > 0:
        outcome = "win"
        consecutive_losses = 0
        consecutive_wins = int(state.get("consecutive_wins", 0) or 0) + 1
        attempts_since_win = 0
        lock_store.pop(sym, None)
    else:
        outcome = "flat"
        consecutive_losses = 0
        consecutive_wins = 0
        attempts_since_win = 0
        lock_store.pop(sym, None)

    recent_results.append(
        {
            "timestamp": now_ts,
            "pnl": round(pnl_value, 4),
            "outcome": outcome,
            "reason": str(reason or ""),
            "setup_id": str(setup_id or ""),
        }
    )
    recent_results = recent_results[-6:]

    state.update(
        {
            "symbol": sym,
            "last_exit_time": now_ts,
            "last_pnl": round(pnl_value, 4),
            "last_outcome": outcome,
            "last_reason": str(reason or ""),
            "last_setup_id": str(setup_id or ""),
            "consecutive_losses": consecutive_losses,
            "consecutive_wins": consecutive_wins,
            "attempts_since_win": attempts_since_win,
            "recent_results": recent_results,
            "updated_at": time.time(),
        }
    )
    state_store[sym] = state

    if outcome == "loss" and consecutive_losses >= loss_limit and lock_seconds > 0:
        lock_store[sym] = {
            "symbol": sym,
            "locked_at": now_ts,
            "expires_at": now_ts + lock_seconds,
            "reason": f"consecutive_losses_{consecutive_losses}",
            "last_trade_reason": str(reason or ""),
            "last_setup_id": str(setup_id or ""),
            "consecutive_losses": consecutive_losses,
            "attempts_since_win": attempts_since_win,
        }
        logger.warning(
            f"LOCK: {sym} for {lock_seconds/3600:.1f}h after {consecutive_losses} consecutive losses"
        )

    _save(data)
    return get_symbol_trade_state(sym)


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
    if is_symbol_loss_locked(sym):
        return True, "symbol_loss_lock"
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
    data["pending_setups"] = _prune_expired(data.get("pending_setups", {}), now)
    data["symbol_loss_locks"] = _prune_expired(data.get("symbol_loss_locks", {}), now)
    _save(data)
