"""
Persistent pending setups for wait-for-trigger play states.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

from loguru import logger

from src.data.entry_controls import load_pending_setups_store, save_pending_setups_store


def _load() -> List[Dict]:
    store = load_pending_setups_store()
    rows = list(store.values()) if isinstance(store, dict) else []
    cleaned: List[Dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "") or "").upper().strip()
        mode = str(row.get("mode", "") or "").strip().lower()
        if not symbol or not mode:
            continue
        row["symbol"] = symbol
        row["mode"] = mode
        cleaned.append(row)
    return cleaned


def _save(rows: List[Dict]) -> None:
    payload = {}
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol", "") or "").upper().strip()
        mode = str(row.get("mode", "") or "").strip().lower()
        if not symbol or not mode:
            continue
        payload[f"{symbol}:{mode}"] = row
    save_pending_setups_store(payload)


def prune_expired(now_ts: Optional[float] = None) -> List[Dict]:
    now_ts = float(now_ts or time.time())
    rows = _load()
    fresh = [
        row
        for row in rows
        if float(row.get("expires_at", now_ts + 1) or (now_ts + 1)) > now_ts
    ]
    if len(fresh) != len(rows):
        _save(fresh)
    return fresh


def list_pending_setups(limit: Optional[int] = None) -> List[Dict]:
    rows = prune_expired()
    rows.sort(key=lambda row: float(row.get("created_at", 0) or 0), reverse=True)
    if isinstance(limit, int) and limit > 0:
        return rows[:limit]
    return rows


def upsert_pending_setup(setup: Dict) -> None:
    if not isinstance(setup, dict):
        return
    symbol = str(setup.get("symbol", "") or "").upper().strip()
    mode = str(setup.get("mode", "") or "").strip().lower()
    if not symbol or not mode:
        return

    rows = prune_expired()
    created_at = float(setup.get("created_at", time.time()) or time.time())
    updated = False
    for row in rows:
        if row.get("symbol") == symbol and row.get("mode") == mode:
            row.update(setup)
            row["symbol"] = symbol
            row["mode"] = mode
            row["updated_at"] = time.time()
            row.setdefault("created_at", created_at)
            updated = True
            break

    if not updated:
        payload = dict(setup)
        payload["symbol"] = symbol
        payload["mode"] = mode
        payload.setdefault("created_at", created_at)
        payload["updated_at"] = time.time()
        rows.append(payload)

    _save(rows)


def remove_pending_setup(symbol: str, mode: Optional[str] = None) -> None:
    symbol_key = str(symbol or "").upper().strip()
    mode_key = str(mode or "").strip().lower() if mode else None
    if not symbol_key:
        return
    rows = prune_expired()
    kept = []
    for row in rows:
        row_symbol = str(row.get("symbol", "") or "").upper().strip()
        row_mode = str(row.get("mode", "") or "").strip().lower()
        if row_symbol != symbol_key:
            kept.append(row)
            continue
        if mode_key and row_mode != mode_key:
            kept.append(row)
    if len(kept) != len(rows):
        _save(kept)


def get_pending_setup(symbol: str, mode: Optional[str] = None) -> Optional[Dict]:
    symbol_key = str(symbol or "").upper().strip()
    mode_key = str(mode or "").strip().lower() if mode else None
    if not symbol_key:
        return None
    for row in prune_expired():
        if str(row.get("symbol", "") or "").upper().strip() != symbol_key:
            continue
        if mode_key and str(row.get("mode", "") or "").strip().lower() != mode_key:
            continue
        return dict(row)
    return None


def clear_all() -> None:
    try:
        _save([])
    except Exception as e:
        logger.debug(f"Pending setup clear failed: {e}")
