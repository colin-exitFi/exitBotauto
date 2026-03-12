"""
Persistence — Atomic, locked state persistence for all Velox data files.

ALL critical state writes go through this module. No other module should open
data files for writing directly. Uses temp-file + fsync + atomic replace to
prevent corruption on crash.
"""

import json
import os
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

from src.data.trade_schema import normalize_trade_record

DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)

POSITIONS_FILE = DATA_DIR / "positions.json"
OPTIONS_POSITIONS_FILE = DATA_DIR / "options_positions.json"
TRADES_FILE = DATA_DIR / "trades.json"
TRADE_HISTORY_FILE = DATA_DIR / "trade_history.json"
PNL_FILE = DATA_DIR / "pnl_state.json"
AI_STATE_FILE = DATA_DIR / "ai_state.json"
BOT_STATE_FILE = DATA_DIR / "bot_state.json"
RISK_STATE_FILE = DATA_DIR / "risk_state.json"
RECONCILIATION_STATE_FILE = DATA_DIR / "reconciliation_state.json"
ENTRY_CONTROLS_FILE = DATA_DIR / "entry_controls.json"
TOMBSTONES_FILE = DATA_DIR / "tombstones.json"
SHUTDOWN_MARKER_FILE = DATA_DIR / "shutdown_marker.json"

_file_locks: Dict[str, threading.Lock] = {}


def _get_lock(path: Path) -> threading.Lock:
    key = str(path)
    if key not in _file_locks:
        _file_locks[key] = threading.Lock()
    return _file_locks[key]


def atomic_write_json(path: Path, data, indent: int = 2):
    """Atomic JSON write: temp file -> fsync -> os.replace."""
    lock = _get_lock(path)
    tmp_path = path.with_suffix(".tmp")
    with lock:
        try:
            with open(tmp_path, "w") as f:
                json.dump(data, f, indent=indent, default=str)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp_path), str(path))
        except Exception as e:
            logger.error(f"Atomic write failed for {path.name}: {e}")
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass


def safe_load_json(path: Path, default=None):
    """Load JSON with crash safety. Returns default on any error."""
    lock = _get_lock(path)
    with lock:
        try:
            if path.exists():
                with open(path) as f:
                    return json.load(f)
        except Exception as e:
            logger.error(f"Failed to load {path.name}: {e}")
    return default() if callable(default) else (default if default is not None else {})


# ── Positions ────────────────────────────────────────────────────

def save_positions(positions: Dict[str, Dict]):
    atomic_write_json(POSITIONS_FILE, positions)


def load_positions() -> Dict[str, Dict]:
    data = safe_load_json(POSITIONS_FILE, default=dict)
    if isinstance(data, dict):
        if data:
            logger.info(f"Restored {len(data)} positions from disk")
        return data
    return {}


def save_options_positions(positions: Dict[str, Dict]):
    atomic_write_json(OPTIONS_POSITIONS_FILE, positions)


def load_options_positions() -> Dict[str, Dict]:
    data = safe_load_json(OPTIONS_POSITIONS_FILE, default=dict)
    if isinstance(data, dict):
        if data:
            logger.info(f"Restored {len(data)} options positions from disk")
        return data
    return {}


# ── Trade History (canonical single ledger) ──────────────────────

def save_trades(trades: List[Dict]):
    """Append-merge new trades into the canonical trade history ledger."""
    lock = _get_lock(TRADE_HISTORY_FILE)
    with lock:
        existing = _load_trade_history_unlocked()
        normalized_new = [normalize_trade_record(t) for t in trades]
        existing_keys = {
            (t.get("symbol", ""), round(float(t.get("entry_time", 0) or 0), 3))
            for t in existing
        }
        for t in normalized_new:
            key = (t.get("symbol", ""), round(float(t.get("entry_time", 0) or 0), 3))
            if key not in existing_keys:
                existing.append(t)
                existing_keys.add(key)
        _write_trade_history_unlocked(existing)


def _load_trade_history_unlocked() -> List[Dict]:
    """Load trade history without acquiring the lock (caller holds lock)."""
    try:
        if TRADE_HISTORY_FILE.exists():
            with open(TRADE_HISTORY_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    try:
        if TRADES_FILE.exists():
            with open(TRADES_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
    except Exception:
        pass
    return []


def _write_trade_history_unlocked(data: List[Dict]):
    tmp = TRADE_HISTORY_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(str(tmp), str(TRADE_HISTORY_FILE))
    except Exception as e:
        logger.error(f"Trade history write failed: {e}")
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass


def load_trades() -> List[Dict]:
    lock = _get_lock(TRADE_HISTORY_FILE)
    with lock:
        return _load_trade_history_unlocked()


# ── P&L State ────────────────────────────────────────────────────

_PNL_DEFAULTS = {
    "total_realized_pnl": 0.0,
    "today_realized_pnl": 0.0,
    "today_date": "",
    "total_trades": 0,
    "winning_trades": 0,
    "losing_trades": 0,
    "best_trade": 0.0,
    "worst_trade": 0.0,
    "total_fees": 0.0,
    "starting_equity": 25000.0,
    "peak_equity": 25000.0,
    "options_total_realized_pnl": 0.0,
    "options_total_trades": 0,
    "options_winning_trades": 0,
    "options_losing_trades": 0,
}


def save_pnl_state(pnl_state: Dict):
    atomic_write_json(PNL_FILE, pnl_state)


def load_pnl_state() -> Dict:
    data = safe_load_json(PNL_FILE, default=dict)
    if not isinstance(data, dict):
        data = {}
    for k, v in _PNL_DEFAULTS.items():
        data.setdefault(k, v)
    return data


# ── AI State ─────────────────────────────────────────────────────

def save_ai_state(ai_layers: Dict):
    atomic_write_json(AI_STATE_FILE, ai_layers)


def load_ai_state() -> Dict:
    data = safe_load_json(AI_STATE_FILE, default=dict)
    if isinstance(data, dict) and data:
        logger.info("Restored AI layer state from disk")
        return data
    return {}


# ── Bot State ────────────────────────────────────────────────────

def save_bot_state(state: Dict):
    atomic_write_json(BOT_STATE_FILE, state)


def load_bot_state() -> Dict:
    return safe_load_json(BOT_STATE_FILE, default=dict)


# ── Risk State ───────────────────────────────────────────────────

def save_risk_state(state: Dict):
    atomic_write_json(RISK_STATE_FILE, state)


def load_risk_state() -> Dict:
    return safe_load_json(RISK_STATE_FILE, default=dict)


# ── Reconciliation State ─────────────────────────────────────────

def save_reconciliation_state(state: Dict):
    atomic_write_json(RECONCILIATION_STATE_FILE, state)


def load_reconciliation_state() -> Dict:
    return safe_load_json(RECONCILIATION_STATE_FILE, default=dict)


# ── Shutdown Marker ──────────────────────────────────────────────

def write_shutdown_marker(open_symbols: List[str]):
    atomic_write_json(SHUTDOWN_MARKER_FILE, {
        "timestamp": time.time(),
        "open_symbols": open_symbols,
        "reason": "graceful_shutdown",
    })


def load_shutdown_marker() -> Optional[Dict]:
    data = safe_load_json(SHUTDOWN_MARKER_FILE, default=None)
    return data if isinstance(data, dict) else None


def clear_shutdown_marker():
    try:
        SHUTDOWN_MARKER_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Tombstones ───────────────────────────────────────────────────

def save_tombstones(tombstones: Dict):
    atomic_write_json(TOMBSTONES_FILE, tombstones)


def load_tombstones() -> Dict:
    return safe_load_json(TOMBSTONES_FILE, default=dict)
