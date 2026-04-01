"""
Symbol state machine — enforces valid transitions and maintains audit trail.

Every symbol moves through explicit states. No silent drop-offs allowed.
Every transition is logged with setup_id, reason, and timestamp.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from loguru import logger


PLAY_STATES = {
    "trade_now",
    "wait_for_trigger",
    "broker_blocked",
    "execution_unfavorable",
    "capital_blocked",
    "shadow_only",
    "mode_conflict",
    "data_insufficient",
    "expired",
}

SYMBOL_STATES = {
    "idle",
    "classified",
    "pending_trigger",
    "entry_submitted",
    "live_position",
    "cooldown",
    "shadow_only",
    "broker_blocked",
    "capital_blocked",
    "data_insufficient",
    "mode_conflict",
    "expired",
}

VALID_TRANSITIONS: Dict[str, set] = {
    "idle": {"classified", "shadow_only"},
    "classified": {
        "pending_trigger", "entry_submitted", "shadow_only",
        "broker_blocked", "capital_blocked", "data_insufficient",
        "mode_conflict", "expired", "execution_unfavorable",
    },
    "pending_trigger": {
        "entry_submitted", "expired", "shadow_only",
        "mode_conflict", "idle", "classified",
        "execution_unfavorable", "broker_blocked", "capital_blocked",
    },
    "entry_submitted": {"live_position", "expired", "broker_blocked", "idle"},
    "live_position": {"cooldown", "idle"},
    "cooldown": {"idle"},
    "shadow_only": {"idle", "classified"},
    "broker_blocked": {"idle", "classified"},
    "capital_blocked": {"idle", "classified"},
    "data_insufficient": {"idle", "classified"},
    "mode_conflict": {"idle", "classified"},
    "expired": {"idle"},
    "execution_unfavorable": {"idle", "classified"},
}


@dataclass
class StateTransition:
    symbol: str
    from_state: str
    to_state: str
    reason: str
    setup_id: Optional[str]
    setup_mode: Optional[str]
    timestamp: float
    blocking_details: Optional[Dict] = None

    def to_dict(self) -> Dict:
        d = {
            "symbol": self.symbol,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "reason": self.reason,
            "setup_id": self.setup_id,
            "setup_mode": self.setup_mode,
            "timestamp": self.timestamp,
        }
        if self.blocking_details:
            d["blocking_details"] = self.blocking_details
        return d


@dataclass
class SymbolState:
    symbol: str
    state: str = "idle"
    setup_id: Optional[str] = None
    setup_mode: Optional[str] = None
    entered_at: float = 0.0
    reason: str = ""
    flip_count: int = 0


MAX_HISTORY_PER_SYMBOL = 50
MAX_FLIPS_PER_DAY = 3


class SymbolStateTracker:
    """
    Tracks every symbol through its lifecycle with enforced transitions.
    Optionally backed by SQLite StateStore for durable persistence.

    Usage:
        tracker = SymbolStateTracker()
        ok = tracker.transition("AAPL", "classified", "mode_classified",
                                setup_id="AAPL:cont:123:abc")
        state = tracker.get_state("AAPL")
        history = tracker.get_history("AAPL")
    """

    def __init__(self, state_store=None):
        self._states: Dict[str, SymbolState] = {}
        self._history: Dict[str, List[StateTransition]] = {}
        self._daily_flip_counts: Dict[str, int] = {}
        self._store = state_store

    def transition(
        self,
        symbol: str,
        new_state: str,
        reason: str,
        setup_id: Optional[str] = None,
        setup_mode: Optional[str] = None,
        blocking_details: Optional[Dict] = None,
    ) -> bool:
        """
        Attempt a state transition. Returns True if valid and applied.
        Every candidate that enters the pipeline must exit with a logged state.
        """
        current = self._states.get(symbol)
        from_state = current.state if current else "idle"

        normalized = new_state.strip().lower()
        if normalized not in SYMBOL_STATES and normalized not in ("execution_unfavorable",):
            logger.warning(f"SymbolState: invalid state '{new_state}' for {symbol}")
            return False

        valid_next = VALID_TRANSITIONS.get(from_state, set())
        if normalized not in valid_next and from_state != normalized:
            logger.warning(
                f"SymbolState: invalid transition {symbol} {from_state}→{normalized} "
                f"(valid: {valid_next})"
            )
            return False

        now = time.time()
        transition = StateTransition(
            symbol=symbol,
            from_state=from_state,
            to_state=normalized,
            reason=reason,
            setup_id=setup_id or (current.setup_id if current else None),
            setup_mode=setup_mode or (current.setup_mode if current else None),
            timestamp=now,
            blocking_details=blocking_details,
        )

        self._states[symbol] = SymbolState(
            symbol=symbol,
            state=normalized,
            setup_id=setup_id or (current.setup_id if current else None),
            setup_mode=setup_mode or (current.setup_mode if current else None),
            entered_at=now,
            reason=reason,
        )

        history = self._history.setdefault(symbol, [])
        history.append(transition)
        if len(history) > MAX_HISTORY_PER_SYMBOL:
            self._history[symbol] = history[-MAX_HISTORY_PER_SYMBOL:]

        if self._store:
            try:
                self._store.record_state_transition(
                    symbol=symbol,
                    from_state=from_state,
                    to_state=normalized,
                    reason=reason,
                    setup_id=setup_id or (current.setup_id if current else None),
                    setup_mode=setup_mode or (current.setup_mode if current else None),
                    blocking_details=blocking_details,
                )
            except Exception as e:
                logger.debug(f"SymbolState: SQLite write failed: {e}")

        logger.debug(
            f"SymbolState: {symbol} {from_state}→{normalized} "
            f"reason={reason} setup={setup_id or '—'}"
        )
        return True

    def record_mode_flip(self, symbol: str) -> bool:
        """Record a mode flip. Returns False if max flips exceeded."""
        count = self._daily_flip_counts.get(symbol, 0) + 1
        self._daily_flip_counts[symbol] = count
        if count > MAX_FLIPS_PER_DAY:
            self.transition(symbol, "mode_conflict", "max_flips_exceeded")
            return False
        return True

    def get_state(self, symbol: str) -> SymbolState:
        return self._states.get(symbol, SymbolState(symbol=symbol))

    def get_history(self, symbol: str) -> List[StateTransition]:
        return list(self._history.get(symbol, []))

    def symbols_in_state(self, state: str) -> List[str]:
        return [s for s, st in self._states.items() if st.state == state]

    def is_occupied(self, symbol: str) -> bool:
        """True if symbol is in a non-idle state that blocks new setup creation."""
        state = self.get_state(symbol).state
        return state in ("entry_submitted", "live_position", "pending_trigger")

    def has_active_setup(self, symbol: str, mode: str) -> bool:
        """One active setup per symbol per mode rule."""
        current = self.get_state(symbol)
        if current.state in ("pending_trigger", "classified", "entry_submitted"):
            if current.setup_mode == mode:
                return True
        return False

    def reset_daily_counters(self):
        """Call at start of each trading day."""
        self._daily_flip_counts.clear()

    def get_blocking_summary(self) -> Dict[str, int]:
        """Count of symbols in each blocking state."""
        counts: Dict[str, int] = {}
        for st in self._states.values():
            if st.state != "idle":
                counts[st.state] = counts.get(st.state, 0) + 1
        return counts

    def get_transition_log(self, limit: int = 100) -> List[Dict]:
        """Recent transitions across all symbols for dashboard/review."""
        all_transitions: List[StateTransition] = []
        for history in self._history.values():
            all_transitions.extend(history)
        all_transitions.sort(key=lambda t: t.timestamp, reverse=True)
        return [t.to_dict() for t in all_transitions[:limit]]
