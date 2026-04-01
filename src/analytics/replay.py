"""
Setup Replay Harness — diagnostic tool for per-name pipeline analysis.

Replays a symbol through the full pipeline using historical data to inspect
every layer's decision. Essential for tuning without live production risk.

Not a full backtester.  The existing backtester/ handles strategy-level testing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger


DATA_DIR = Path(__file__).parent.parent.parent / "data"


@dataclass
class ReplayFrame:
    timestamp: float = 0.0
    price: float = 0.0
    volume: float = 0.0
    mode: str = ""
    classifier_confidence: float = 0.0
    play_state: str = ""
    trigger_type: str = ""
    trigger_live: bool = False
    data_quality_score: float = 0.0
    execution_verdict: str = ""
    execution_quality_score: float = 0.0
    spread_pct: float = 0.0
    liquidity_score: float = 0.0
    entry_decision: str = ""
    actual_outcome: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "timestamp": self.timestamp,
            "price": self.price,
            "volume": self.volume,
            "mode": self.mode,
            "classifier_confidence": self.classifier_confidence,
            "play_state": self.play_state,
            "trigger_type": self.trigger_type,
            "trigger_live": self.trigger_live,
            "data_quality_score": self.data_quality_score,
            "execution_verdict": self.execution_verdict,
            "execution_quality_score": self.execution_quality_score,
            "spread_pct": self.spread_pct,
            "liquidity_score": self.liquidity_score,
            "entry_decision": self.entry_decision,
            "actual_outcome": self.actual_outcome,
        }


class SetupReplay:
    """
    Replay a symbol's journey through the pipeline using stored snapshots.

    Usage:
        replay = SetupReplay()
        frames = replay.replay_from_snapshots("AAPL", "2026-04-01")
        for frame in frames:
            print(frame.to_dict())
    """

    def replay_from_snapshots(
        self,
        symbol: str,
        date_str: str,
        setup_snapshots: Optional[List[Dict]] = None,
        trade_history: Optional[List[Dict]] = None,
    ) -> List[ReplayFrame]:
        """
        Replay from stored setup_snapshots.json and trade_history.json.
        Returns chronological frames showing pipeline state at each snapshot.
        """
        if setup_snapshots is None:
            setup_snapshots = self._load_snapshots()
        if trade_history is None:
            trade_history = self._load_trade_history()

        sym = symbol.upper()
        symbol_snaps = [
            s for s in setup_snapshots
            if str(s.get("symbol", "") or "").upper() == sym
            and str(s.get("recorded_at", "") or "").startswith(date_str)
        ]
        if not symbol_snaps:
            symbol_snaps = [
                s for s in setup_snapshots
                if str(s.get("symbol", "") or "").upper() == sym
            ]

        actual_trades = [
            t for t in trade_history
            if str(t.get("symbol", "") or "").upper() == sym
        ]
        actual_outcome = None
        if actual_trades:
            latest = actual_trades[-1]
            pnl = float(latest.get("pnl", 0) or 0)
            actual_outcome = f"{'win' if pnl > 0 else 'loss'} ${pnl:.2f}"

        frames: List[ReplayFrame] = []
        for snap in sorted(symbol_snaps, key=lambda s: float(s.get("recorded_at", 0) or 0)):
            frame = ReplayFrame(
                timestamp=float(snap.get("recorded_at", 0) or 0),
                price=float(snap.get("price", 0) or 0),
                volume=float(snap.get("volume", 0) or 0),
                mode=str(snap.get("setup_mode", snap.get("mode", "")) or ""),
                classifier_confidence=float(snap.get("classifier_confidence", 0) or 0),
                play_state=str(snap.get("timing_state", snap.get("symbol_state", "")) or ""),
                trigger_type=str(snap.get("trigger_type", "") or ""),
                trigger_live=bool(snap.get("trigger_live", False)),
                data_quality_score=float(snap.get("feature_quality_score", snap.get("data_quality_score", 0)) or 0),
                spread_pct=float(snap.get("spread_pct", 0) or 0),
                entry_decision=str(snap.get("timing_state", "") or ""),
                actual_outcome=actual_outcome,
            )
            frames.append(frame)

        return frames

    def summarize(self, frames: List[ReplayFrame]) -> Dict:
        """Summarize a replay into key observations."""
        if not frames:
            return {"symbol": "", "frame_count": 0}

        modes_seen = list({f.mode for f in frames if f.mode})
        states_seen = list({f.play_state for f in frames if f.play_state})
        any_entered = any(f.play_state in ("trade_now", "entry_submitted") for f in frames)
        any_triggered = any(f.trigger_live for f in frames)

        return {
            "symbol": frames[0].to_dict().get("symbol", ""),
            "frame_count": len(frames),
            "modes_seen": modes_seen,
            "states_seen": states_seen,
            "entered": any_entered,
            "triggered": any_triggered,
            "final_state": frames[-1].play_state,
            "final_mode": frames[-1].mode,
            "actual_outcome": frames[-1].actual_outcome,
        }

    @staticmethod
    def _load_snapshots() -> List[Dict]:
        path = DATA_DIR / "setup_snapshots.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    @staticmethod
    def _load_trade_history() -> List[Dict]:
        path = DATA_DIR / "trade_history.json"
        if path.exists():
            try:
                with open(path) as f:
                    return json.load(f)
            except Exception:
                pass
        return []
