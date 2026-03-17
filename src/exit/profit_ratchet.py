"""
Deterministic profit-ratchet logic for Velox v2 stabilization.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from config import settings


class ProfitRatchet:
    HARD_STOP_PCT = float(getattr(settings, "PROFIT_RATCHET_HARD_STOP_PCT", -3.0) or -3.0)
    RATCHET_ACTIVATION_PCT = float(getattr(settings, "PROFIT_RATCHET_ACTIVATION_PCT", 1.0) or 1.0)
    INITIAL_FLOOR_PCT = float(getattr(settings, "PROFIT_RATCHET_INITIAL_FLOOR_PCT", 0.5) or 0.5)
    RATCHET_TRAIL_PCT = float(getattr(settings, "PROFIT_RATCHET_TRAIL_PCT", 2.0) or 2.0)
    MIN_HOLD_SECONDS = int(getattr(settings, "PROFIT_RATCHET_MIN_HOLD_SECONDS", 1800) or 1800)
    DAILY_CIRCUIT_BREAKER_PCT = -abs(float(getattr(settings, "MAX_DAILY_LOSS_PCT", 5.0) or 5.0))

    @classmethod
    def check_position(
        cls,
        position: Dict,
        current_price: float,
        now: Optional[float] = None,
    ) -> Dict:
        """
        Return the next deterministic action for a live position.

        Actions:
        - hold
        - update_limit
        - hard_stop
        - ratchet_exit
        """
        now_ts = float(now or time.time())
        entry_price = float(position.get("entry_price", 0) or 0)
        side = str(position.get("side", "long") or "long").lower()
        if entry_price <= 0 or current_price <= 0:
            return {
                "action": "hold",
                "reason": "invalid_price_context",
                "current_pnl_pct": 0.0,
                "peak_pnl_pct": 0.0,
                "floor_pct": None,
                "target_exit_price": None,
                "hard_stop_price": None,
                "hold_seconds": 0.0,
                "ratchet_active": False,
                "min_hold_active": False,
            }

        current_pnl_pct = cls.calc_pnl_pct(entry_price, current_price, side)
        peak_price = cls._compute_peak_price(position, current_price, side)
        peak_pnl_pct = cls.calc_pnl_pct(entry_price, peak_price, side)
        hold_seconds = max(0.0, now_ts - float(position.get("entry_time", now_ts) or now_ts))
        min_hold_active = 0.0 <= current_pnl_pct < cls.RATCHET_ACTIVATION_PCT and hold_seconds < cls.MIN_HOLD_SECONDS

        hard_stop_price = cls.price_for_pnl(entry_price, cls.HARD_STOP_PCT, side)
        floor_pct = cls.compute_floor_pct(peak_pnl_pct)
        ratchet_active = floor_pct is not None
        target_exit_price = cls.price_for_pnl(entry_price, floor_pct, side) if floor_pct is not None else None
        prior_floor = cls._safe_float(position.get("ratchet_floor_pct"), None)

        if current_pnl_pct <= cls.HARD_STOP_PCT:
            return {
                "action": "hard_stop",
                "reason": "hard_stop_breached",
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "floor_pct": floor_pct,
                "target_exit_price": target_exit_price,
                "hard_stop_price": hard_stop_price,
                "hold_seconds": hold_seconds,
                "ratchet_active": ratchet_active,
                "min_hold_active": min_hold_active,
            }

        if floor_pct is not None and current_pnl_pct <= floor_pct:
            return {
                "action": "ratchet_exit",
                "reason": "ratchet_floor_breached",
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "floor_pct": round(floor_pct, 4),
                "target_exit_price": target_exit_price,
                "hard_stop_price": hard_stop_price,
                "hold_seconds": hold_seconds,
                "ratchet_active": True,
                "min_hold_active": min_hold_active,
            }

        if floor_pct is not None and (prior_floor is None or floor_pct > prior_floor + 1e-9):
            return {
                "action": "update_limit",
                "reason": "ratchet_floor_raised",
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "floor_pct": round(floor_pct, 4),
                "target_exit_price": target_exit_price,
                "hard_stop_price": hard_stop_price,
                "hold_seconds": hold_seconds,
                "ratchet_active": True,
                "min_hold_active": min_hold_active,
            }

        return {
            "action": "hold",
            "reason": "hold_zone" if min_hold_active else ("ratchet_active" if ratchet_active else "pre_activation"),
            "current_pnl_pct": round(current_pnl_pct, 4),
            "peak_pnl_pct": round(peak_pnl_pct, 4),
            "floor_pct": round(floor_pct, 4) if floor_pct is not None else None,
            "target_exit_price": target_exit_price,
            "hard_stop_price": hard_stop_price,
            "hold_seconds": hold_seconds,
            "ratchet_active": ratchet_active,
            "min_hold_active": min_hold_active,
        }

    @classmethod
    def compute_floor_pct(cls, peak_pnl_pct: float) -> Optional[float]:
        peak = float(peak_pnl_pct or 0.0)
        if peak < cls.RATCHET_ACTIVATION_PCT:
            return None
        floor = max(cls.INITIAL_FLOOR_PCT, peak - cls.RATCHET_TRAIL_PCT)
        return round(floor, 4)

    @staticmethod
    def calc_pnl_pct(entry_price: float, current_price: float, side: str = "long") -> float:
        if entry_price <= 0:
            return 0.0
        if str(side or "long").lower() == "short":
            return ((entry_price - current_price) / entry_price) * 100.0
        return ((current_price - entry_price) / entry_price) * 100.0

    @staticmethod
    def price_for_pnl(entry_price: float, pnl_pct: Optional[float], side: str = "long") -> Optional[float]:
        if entry_price <= 0 or pnl_pct is None:
            return None
        if str(side or "long").lower() == "short":
            return round(entry_price * (1.0 - (float(pnl_pct) / 100.0)), 4)
        return round(entry_price * (1.0 + (float(pnl_pct) / 100.0)), 4)

    @staticmethod
    def make_client_order_id(symbol: str, order_kind: str, anchor: object) -> str:
        sym = "".join(ch for ch in str(symbol or "").upper() if ch.isalnum())[:8] or "UNK"
        kind = "".join(ch for ch in str(order_kind or "").lower() if ch.isalnum())[:12] or "order"
        anchor_str = str(anchor or "0").replace(".", "p").replace("-", "m")
        anchor_str = "".join(ch for ch in anchor_str if ch.isalnum())[:18] or "0"
        return f"veloxv2-{sym}-{kind}-{anchor_str}"[:48]

    @classmethod
    def _compute_peak_price(cls, position: Dict, current_price: float, side: str) -> float:
        peak_price = cls._safe_float(position.get("peak_price"), current_price) or current_price
        if str(side or "long").lower() == "short":
            return min(current_price, peak_price)
        return max(current_price, peak_price)

    @staticmethod
    def _safe_float(value: object, default: Optional[float] = 0.0) -> Optional[float]:
        try:
            if value is None:
                return default
            return float(value)
        except Exception:
            return default
