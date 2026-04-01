"""
SQLite-backed state store for durable runtime persistence.

Replaces fragile JSON file persistence for critical runtime state:
- Pending setups (survive restarts, queryable)
- Symbol state transitions (audit trail)
- Setup funnel events (conversion analytics)
- Session context snapshots (replay support)

No new infrastructure required — SQLite is a file on disk.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

DB_PATH = Path(__file__).parent.parent.parent / "data" / "velox_state.db"


def _dict_factory(cursor, row):
    return {col[0]: row[idx] for idx, col in enumerate(cursor.description)}


class StateStore:
    """
    SQLite-backed durable state for Velox runtime.

    Usage:
        store = StateStore()
        store.upsert_pending_setup({...})
        setups = store.get_active_pending_setups()
        store.record_state_transition("AAPL", "idle", "classified", "mode_classified", "setup-123")
        store.record_funnel_event("AAPL", "setup-123", "classified", "continuation_long")
    """

    def __init__(self, db_path: Optional[Path] = None):
        self._db_path = str(db_path or DB_PATH)
        self._conn: Optional[sqlite3.Connection] = None
        self._initialize()

    def _initialize(self):
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = _dict_factory
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()
        logger.info(f"StateStore initialized at {self._db_path}")

    def _create_tables(self):
        c = self._conn
        c.execute("""
            CREATE TABLE IF NOT EXISTS pending_setups (
                key TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                setup_id TEXT,
                mode TEXT,
                direction_constraint TEXT DEFAULT 'none',
                trigger_spec TEXT,
                invalidation_type TEXT,
                invalidation_params TEXT,
                created_at REAL,
                expires_at REAL,
                candidate_snapshot TEXT,
                shadow_mode INTEGER DEFAULT 0,
                source_priority TEXT DEFAULT 'normal',
                feature_snapshot_id TEXT,
                status TEXT DEFAULT 'pending',
                updated_at REAL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS state_transitions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                reason TEXT,
                setup_id TEXT,
                setup_mode TEXT,
                blocking_details TEXT,
                timestamp REAL NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS funnel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                setup_id TEXT,
                stage TEXT NOT NULL,
                mode TEXT,
                book TEXT,
                session_label TEXT,
                reason TEXT,
                blocking_details TEXT,
                timestamp REAL NOT NULL
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS session_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL NOT NULL,
                snapshot TEXT NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_symbol ON pending_setups(symbol)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_setups(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_transitions_symbol ON state_transitions(symbol)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_transitions_ts ON state_transitions(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_funnel_ts ON funnel_events(timestamp)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_funnel_setup ON funnel_events(setup_id)")
        c.commit()

    # ── Pending Setups ─────────────────────────────────────────

    def upsert_pending_setup(self, setup: Dict):
        symbol = str(setup.get("symbol", "") or "").upper()
        mode = str(setup.get("setup_mode", setup.get("mode", "unknown")) or "unknown")
        key = f"{symbol}:{mode}"
        now = time.time()
        self._conn.execute("""
            INSERT OR REPLACE INTO pending_setups
            (key, symbol, setup_id, mode, direction_constraint, trigger_spec,
             invalidation_type, invalidation_params, created_at, expires_at,
             candidate_snapshot, shadow_mode, source_priority, feature_snapshot_id,
             status, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            key, symbol,
            str(setup.get("setup_id", "") or ""),
            mode,
            str(setup.get("direction_constraint", "none") or "none"),
            json.dumps(setup.get("trigger_spec")) if setup.get("trigger_spec") else None,
            setup.get("invalidation_type"),
            json.dumps(setup.get("invalidation_params")) if setup.get("invalidation_params") else None,
            float(setup.get("created_at", now) or now),
            float(setup.get("expires_at", 0) or 0),
            json.dumps(setup.get("candidate_snapshot")) if setup.get("candidate_snapshot") else None,
            1 if setup.get("shadow_mode") else 0,
            str(setup.get("source_priority", "normal") or "normal"),
            setup.get("feature_snapshot_id"),
            str(setup.get("status", "pending") or "pending"),
            now,
        ))
        self._conn.commit()

    def get_active_pending_setups(self) -> List[Dict]:
        now = time.time()
        rows = self._conn.execute(
            "SELECT * FROM pending_setups WHERE status = 'pending' AND (expires_at <= 0 OR expires_at > ?)",
            (now,),
        ).fetchall()
        return [self._deserialize_pending(r) for r in rows]

    def get_pending_setup(self, symbol: str, mode: str) -> Optional[Dict]:
        key = f"{symbol.upper()}:{mode}"
        row = self._conn.execute(
            "SELECT * FROM pending_setups WHERE key = ?", (key,)
        ).fetchone()
        return self._deserialize_pending(row) if row else None

    def remove_pending_setup(self, symbol: str, mode: str, reason: str = "removed"):
        key = f"{symbol.upper()}:{mode}"
        self._conn.execute(
            "UPDATE pending_setups SET status = ?, updated_at = ? WHERE key = ?",
            (reason, time.time(), key),
        )
        self._conn.commit()

    def expire_stale_setups(self) -> int:
        now = time.time()
        cursor = self._conn.execute(
            "UPDATE pending_setups SET status = 'expired', updated_at = ? "
            "WHERE status = 'pending' AND expires_at > 0 AND expires_at <= ?",
            (now, now),
        )
        self._conn.commit()
        return cursor.rowcount

    def _deserialize_pending(self, row: Dict) -> Dict:
        result = dict(row)
        for json_field in ("trigger_spec", "invalidation_params", "candidate_snapshot"):
            val = result.get(json_field)
            if isinstance(val, str):
                try:
                    result[json_field] = json.loads(val)
                except Exception:
                    pass
        result["shadow_mode"] = bool(result.get("shadow_mode", 0))
        result["setup_mode"] = result.get("mode", "unknown")
        return result

    # ── State Transitions ──────────────────────────────────────

    def record_state_transition(
        self,
        symbol: str,
        from_state: str,
        to_state: str,
        reason: str,
        setup_id: Optional[str] = None,
        setup_mode: Optional[str] = None,
        blocking_details: Optional[Dict] = None,
    ):
        self._conn.execute("""
            INSERT INTO state_transitions
            (symbol, from_state, to_state, reason, setup_id, setup_mode, blocking_details, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol.upper(), from_state, to_state, reason,
            setup_id, setup_mode,
            json.dumps(blocking_details) if blocking_details else None,
            time.time(),
        ))
        self._conn.commit()

    def get_symbol_transitions(self, symbol: str, limit: int = 50) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM state_transitions WHERE symbol = ? ORDER BY timestamp DESC LIMIT ?",
            (symbol.upper(), limit),
        ).fetchall()
        return [self._deserialize_transition(r) for r in rows]

    def get_recent_transitions(self, limit: int = 100) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT * FROM state_transitions ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._deserialize_transition(r) for r in rows]

    def _deserialize_transition(self, row: Dict) -> Dict:
        result = dict(row)
        bd = result.get("blocking_details")
        if isinstance(bd, str):
            try:
                result["blocking_details"] = json.loads(bd)
            except Exception:
                pass
        return result

    # ── Funnel Events ──────────────────────────────────────────

    def record_funnel_event(
        self,
        symbol: str,
        setup_id: str,
        stage: str,
        mode: str = "",
        book: str = "",
        session_label: str = "",
        reason: str = "",
        blocking_details: Optional[Dict] = None,
    ):
        self._conn.execute("""
            INSERT INTO funnel_events
            (symbol, setup_id, stage, mode, book, session_label, reason, blocking_details, timestamp)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            symbol.upper(), setup_id, stage, mode, book, session_label, reason,
            json.dumps(blocking_details) if blocking_details else None,
            time.time(),
        ))
        self._conn.commit()

    def get_funnel_summary(self, since: float = 0.0) -> Dict:
        rows = self._conn.execute(
            "SELECT stage, mode, book, reason FROM funnel_events WHERE timestamp >= ?",
            (since,),
        ).fetchall()

        from collections import defaultdict
        counts = defaultdict(int)
        by_mode = defaultdict(lambda: defaultdict(int))
        by_book = defaultdict(lambda: defaultdict(int))
        block_reasons = defaultdict(int)

        for r in rows:
            stage = r["stage"]
            counts[stage] += 1
            if r["mode"]:
                by_mode[r["mode"]][stage] += 1
            if r["book"]:
                by_book[r["book"]][stage] += 1
            if stage == "blocked" and r["reason"]:
                block_reasons[r["reason"]] += 1

        return {
            "total": dict(counts),
            "by_mode": {k: dict(v) for k, v in by_mode.items()},
            "by_book": {k: dict(v) for k, v in by_book.items()},
            "block_reasons": dict(block_reasons),
        }

    # ── Session Snapshots ──────────────────────────────────────

    def save_session_snapshot(self, snapshot_dict: Dict):
        self._conn.execute(
            "INSERT INTO session_snapshots (timestamp, snapshot) VALUES (?, ?)",
            (time.time(), json.dumps(snapshot_dict)),
        )
        self._conn.commit()

    def get_session_snapshots(self, since: float = 0.0, limit: int = 50) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT timestamp, snapshot FROM session_snapshots WHERE timestamp >= ? ORDER BY timestamp DESC LIMIT ?",
            (since, limit),
        ).fetchall()
        results = []
        for r in rows:
            try:
                snap = json.loads(r["snapshot"])
                snap["_stored_at"] = r["timestamp"]
                results.append(snap)
            except Exception:
                pass
        return results

    # ── Cleanup ────────────────────────────────────────────────

    def cleanup_old_data(self, days: int = 30):
        cutoff = time.time() - (days * 86400)
        self._conn.execute("DELETE FROM state_transitions WHERE timestamp < ?", (cutoff,))
        self._conn.execute("DELETE FROM funnel_events WHERE timestamp < ?", (cutoff,))
        self._conn.execute("DELETE FROM session_snapshots WHERE timestamp < ?", (cutoff,))
        self._conn.execute(
            "DELETE FROM pending_setups WHERE status != 'pending' AND updated_at < ?",
            (cutoff,),
        )
        self._conn.commit()

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
