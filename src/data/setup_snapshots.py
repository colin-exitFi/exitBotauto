"""
Feature snapshot persistence for every classified candidate.
"""

from __future__ import annotations

import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from src import persistence
from src.data.trade_schema import normalize_setup_snapshot

DATA_DIR = Path(__file__).parent.parent.parent / "data"
SETUP_SNAPSHOTS_FILE = DATA_DIR / "setup_snapshots.json"
MAX_SETUP_SNAPSHOTS = 5000


def _load() -> List[Dict]:
    rows = persistence.safe_load_json(SETUP_SNAPSHOTS_FILE, default=list)
    if not isinstance(rows, list):
        return []
    return [normalize_setup_snapshot(row) for row in rows]


def _save(rows: List[Dict]) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    persistence.atomic_write_json(SETUP_SNAPSHOTS_FILE, rows, indent=2)


def record_setup_snapshot(snapshot: Dict) -> str:
    rows = _load()
    snapshot_id = str(snapshot.get("snapshot_id") or uuid.uuid4().hex)
    payload = normalize_setup_snapshot(dict(snapshot))
    payload["snapshot_id"] = snapshot_id
    payload.setdefault("recorded_at", time.time())
    rows.append(payload)
    if len(rows) > MAX_SETUP_SNAPSHOTS:
        rows = rows[-MAX_SETUP_SNAPSHOTS:]
    _save(rows)
    return snapshot_id


def load_setup_snapshots(limit: Optional[int] = None) -> List[Dict]:
    rows = _load()
    rows.sort(key=lambda row: float(row.get("recorded_at", 0) or 0), reverse=True)
    if isinstance(limit, int) and limit > 0:
        return rows[:limit]
    return rows
