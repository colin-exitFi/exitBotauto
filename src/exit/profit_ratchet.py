"""
Deterministic profit-ratchet logic for Velox v2 stabilization.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from config import settings


class ProfitRatchet:
    HARD_STOP_PCT = float(getattr(settings, "PROFIT_RATCHET_HARD_STOP_PCT", -3.0) or -3.0)
    RATCHET_ACTIVATION_PCT = float(getattr(settings, "PROFIT_RATCHET_ACTIVATION_PCT", 1.5) or 1.5)
    INITIAL_FLOOR_PCT = float(getattr(settings, "PROFIT_RATCHET_INITIAL_FLOOR_PCT", 0.25) or 0.25)
    RATCHET_TRAIL_PCT = float(getattr(settings, "PROFIT_RATCHET_TRAIL_PCT", 4.0) or 4.0)
    MIN_HOLD_SECONDS = int(getattr(settings, "PROFIT_RATCHET_MIN_HOLD_SECONDS", 900) or 900)
    SWING_RATCHET_ACTIVATION_PCT = float(getattr(settings, "SWING_RATCHET_ACTIVATION_PCT", 3.0) or 3.0)
    SWING_RATCHET_TRAIL_PCT = float(getattr(settings, "SWING_RATCHET_TRAIL_PCT", 6.0) or 6.0)
    SWING_RATCHET_MIN_HOLD_SECONDS = int(getattr(settings, "SWING_RATCHET_MIN_HOLD_SECONDS", 14400) or 14400)
    DEAD_MONEY_HOURS = float(getattr(settings, "DEAD_MONEY_HOURS", 4.0) or 4.0)
    SWING_DEAD_MONEY_HOURS = float(getattr(settings, "SWING_DEAD_MONEY_HOURS", 8.0) or 8.0)
    DEAD_MONEY_TIGHT_STOP_PCT = float(getattr(settings, "DEAD_MONEY_TIGHT_STOP_PCT", -1.5) or -1.5)
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
        horizon_profile = cls.profile_for_position(position)
        if entry_price <= 0 or current_price <= 0:
            return {
                "action": "hold",
                "reason": "invalid_price_context",
                "current_pnl_pct": 0.0,
                "peak_pnl_pct": 0.0,
                "floor_pct": None,
                "target_exit_price": None,
                "hard_stop_price": None,
                "hard_stop_pct": cls.HARD_STOP_PCT,
                "hold_seconds": 0.0,
                "ratchet_active": False,
                "min_hold_active": False,
                "dead_money": False,
                "holding_horizon": horizon_profile["holding_horizon"],
                "giveback_pct": None,
            }

        current_pnl_pct = cls.calc_pnl_pct(entry_price, current_price, side)
        peak_price = cls._compute_peak_price(position, current_price, side)
        peak_pnl_pct = cls.calc_pnl_pct(entry_price, peak_price, side)
        hold_seconds = max(0.0, now_ts - float(position.get("entry_time", now_ts) or now_ts))
        min_hold_active = (
            0.0 <= current_pnl_pct < horizon_profile["activation_pct"]
            and hold_seconds < horizon_profile["min_hold_seconds"]
        )
        dead_money = cls.is_dead_money(position, current_price, now=now_ts)
        hard_stop_pct = cls.DEAD_MONEY_TIGHT_STOP_PCT if dead_money else cls.HARD_STOP_PCT
        hard_stop_price = cls.price_for_pnl(entry_price, hard_stop_pct, side)
        floor_pct = cls.compute_floor_pct(
            peak_pnl_pct,
            activation_pct=horizon_profile["activation_pct"],
            initial_floor_pct=horizon_profile["initial_floor_pct"],
            trail_pct=horizon_profile["trail_pct"],
        )
        ratchet_active = floor_pct is not None
        target_exit_price = cls.price_for_pnl(entry_price, floor_pct, side) if floor_pct is not None else None
        prior_floor = cls._safe_float(position.get("ratchet_floor_pct"), None)
        giveback_pct = cls.compute_giveback_pct(peak_pnl_pct, current_pnl_pct)

        if current_pnl_pct <= hard_stop_pct:
            return {
                "action": "hard_stop",
                "reason": "dead_money_tight_stop_breached" if dead_money else "hard_stop_breached",
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "floor_pct": floor_pct,
                "target_exit_price": target_exit_price,
                "hard_stop_price": hard_stop_price,
                "hard_stop_pct": round(hard_stop_pct, 4),
                "hold_seconds": hold_seconds,
                "ratchet_active": ratchet_active,
                "min_hold_active": min_hold_active,
                "dead_money": dead_money,
                "holding_horizon": horizon_profile["holding_horizon"],
                "giveback_pct": giveback_pct,
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
                "hard_stop_pct": round(hard_stop_pct, 4),
                "hold_seconds": hold_seconds,
                "ratchet_active": True,
                "min_hold_active": min_hold_active,
                "dead_money": dead_money,
                "holding_horizon": horizon_profile["holding_horizon"],
                "giveback_pct": giveback_pct,
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
                "hard_stop_pct": round(hard_stop_pct, 4),
                "hold_seconds": hold_seconds,
                "ratchet_active": True,
                "min_hold_active": min_hold_active,
                "dead_money": dead_money,
                "holding_horizon": horizon_profile["holding_horizon"],
                "giveback_pct": giveback_pct,
            }

        return {
            "action": "hold",
            "reason": "hold_zone" if min_hold_active else ("ratchet_active" if ratchet_active else "pre_activation"),
            "current_pnl_pct": round(current_pnl_pct, 4),
            "peak_pnl_pct": round(peak_pnl_pct, 4),
            "floor_pct": round(floor_pct, 4) if floor_pct is not None else None,
            "target_exit_price": target_exit_price,
            "hard_stop_price": hard_stop_price,
            "hard_stop_pct": round(hard_stop_pct, 4),
            "hold_seconds": hold_seconds,
            "ratchet_active": ratchet_active,
            "min_hold_active": min_hold_active,
            "dead_money": dead_money,
            "holding_horizon": horizon_profile["holding_horizon"],
            "giveback_pct": giveback_pct,
        }

    @classmethod
    def compute_floor_pct(
        cls,
        peak_pnl_pct: float,
        activation_pct: Optional[float] = None,
        initial_floor_pct: Optional[float] = None,
        trail_pct: Optional[float] = None,
    ) -> Optional[float]:
        peak = float(peak_pnl_pct or 0.0)
        activation = float(activation_pct if activation_pct is not None else cls.RATCHET_ACTIVATION_PCT)
        floor_base = float(initial_floor_pct if initial_floor_pct is not None else cls.INITIAL_FLOOR_PCT)
        trail = float(trail_pct if trail_pct is not None else cls.RATCHET_TRAIL_PCT)
        if peak < activation:
            return None
        floor = max(floor_base, peak - trail)
        return round(floor, 4)

    @classmethod
    def profile_for_position(cls, position: Dict) -> Dict:
        holding_horizon = str((position or {}).get("holding_horizon", "intraday") or "intraday").lower()
        if holding_horizon == "swing":
            return {
                "holding_horizon": "swing",
                "activation_pct": cls.SWING_RATCHET_ACTIVATION_PCT,
                "initial_floor_pct": cls.INITIAL_FLOOR_PCT,
                "trail_pct": cls.SWING_RATCHET_TRAIL_PCT,
                "min_hold_seconds": cls.SWING_RATCHET_MIN_HOLD_SECONDS,
                "dead_money_hours": cls.SWING_DEAD_MONEY_HOURS,
            }
        return {
            "holding_horizon": holding_horizon or "intraday",
            "activation_pct": cls.RATCHET_ACTIVATION_PCT,
            "initial_floor_pct": cls.INITIAL_FLOOR_PCT,
            "trail_pct": cls.RATCHET_TRAIL_PCT,
            "min_hold_seconds": cls.MIN_HOLD_SECONDS,
            "dead_money_hours": cls.DEAD_MONEY_HOURS,
        }

    @classmethod
    def is_dead_money(
        cls,
        position: Dict,
        current_price: float,
        now: Optional[float] = None,
    ) -> bool:
        now_ts = float(now or time.time())
        hold_hours = max(0.0, now_ts - float(position.get("entry_time", now_ts) or now_ts)) / 3600.0
        entry_price = float(position.get("entry_price", 0) or 0)
        side = str(position.get("side", "long") or "long").lower()
        if entry_price <= 0 or current_price <= 0:
            return False
        peak_price = cls._compute_peak_price(position, current_price, side)
        peak_pnl_pct = cls.calc_pnl_pct(entry_price, peak_price, side)
        current_pnl_pct = cls.calc_pnl_pct(entry_price, current_price, side)
        profile = cls.profile_for_position(position)
        activation_reference = min(cls.RATCHET_ACTIVATION_PCT, profile["activation_pct"])
        dead_money_hours = float(profile.get("dead_money_hours", cls.DEAD_MONEY_HOURS) or cls.DEAD_MONEY_HOURS)
        return (
            hold_hours >= dead_money_hours
            and peak_pnl_pct < activation_reference
            and current_pnl_pct < 0
        )

    @staticmethod
    def compute_giveback_pct(peak_pnl_pct: float, realized_pnl_pct: float) -> Optional[float]:
        peak = float(peak_pnl_pct or 0.0)
        realized = float(realized_pnl_pct or 0.0)
        if peak <= 0:
            return None
        giveback = max(0.0, peak - min(peak, realized))
        return round((giveback / peak) * 100.0, 2)

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
        kind = "".join(ch for ch in str(order_kind or "").lower() if ch.isalnum())[:16] or "order"
        ts_ms = int(time.time() * 1000)
        return f"{sym}_{kind}_{ts_ms}"[:48]

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
