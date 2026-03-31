"""
Deterministic profit-ratchet logic for Velox v2 stabilization.
"""

from __future__ import annotations

import time
from typing import Dict, Optional

from config import settings


class ProfitRatchet:
    HARD_STOP_PCT = float(getattr(settings, "PROFIT_RATCHET_HARD_STOP_PCT", -3.0) or -3.0)
    AT_HIGHS_HARD_STOP_PCT = float(getattr(settings, "PROFIT_RATCHET_AT_HIGHS_HARD_STOP_PCT", -2.0) or -2.0)
    EXTENDED_HOURS_HARD_STOP_PCT = float(
        getattr(settings, "PROFIT_RATCHET_EXTENDED_HOURS_HARD_STOP_PCT", -2.25) or -2.25
    )
    OBSERVE_HARD_STOP_PCT = float(getattr(settings, "PROFIT_RATCHET_OBSERVE_HARD_STOP_PCT", -2.25) or -2.25)
    PROBATION_HARD_STOP_PCT = float(
        getattr(settings, "PROFIT_RATCHET_PROBATION_HARD_STOP_PCT", -2.0) or -2.0
    )
    STALLED_LOSER_HOURS = float(getattr(settings, "PROFIT_RATCHET_STALLED_LOSER_HOURS", 1.5) or 1.5)
    STALLED_LOSER_MAX_PEAK_PNL_PCT = float(
        getattr(settings, "PROFIT_RATCHET_STALLED_LOSER_MAX_PEAK_PNL_PCT", 0.5) or 0.5
    )
    STALLED_LOSER_MIN_PNL_PCT = float(
        getattr(settings, "PROFIT_RATCHET_STALLED_LOSER_MIN_PNL_PCT", -0.75) or -0.75
    )
    STALLED_LOSER_HARD_STOP_PCT = float(
        getattr(settings, "PROFIT_RATCHET_STALLED_LOSER_HARD_STOP_PCT", -1.75) or -1.75
    )
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
                "hard_stop_flags": [],
            }

        current_pnl_pct = cls.calc_pnl_pct(entry_price, current_price, side)
        peak_price = cls._compute_peak_price(position, current_price, side)
        peak_pnl_pct = cls.calc_pnl_pct(entry_price, peak_price, side)
        hold_seconds = max(0.0, now_ts - float(position.get("entry_time", now_ts) or now_ts))
        base_hard_stop_pct, hard_stop_flags = cls._effective_hard_stop_pct(
            position,
            current_pnl_pct=current_pnl_pct,
            peak_pnl_pct=peak_pnl_pct,
            hold_seconds=hold_seconds,
        )
        min_hold_active = (
            hold_seconds < horizon_profile["min_hold_seconds"]
            and current_pnl_pct > base_hard_stop_pct
        )
        dead_money = cls.is_dead_money(position, current_price, now=now_ts)
        hard_stop_pct = max(base_hard_stop_pct, cls.DEAD_MONEY_TIGHT_STOP_PCT) if dead_money else base_hard_stop_pct
        if dead_money and "dead_money" not in hard_stop_flags:
            hard_stop_flags.append("dead_money")
        hard_stop_price = cls.price_for_pnl(entry_price, hard_stop_pct, side)
        floor_pct = cls.compute_floor_pct(
            peak_pnl_pct,
            activation_pct=horizon_profile["activation_pct"],
            initial_floor_pct=horizon_profile["initial_floor_pct"],
            trail_pct=horizon_profile["trail_pct"],
        )
        floor_is_live = floor_pct is not None and not min_hold_active
        ratchet_active = floor_is_live
        live_floor_pct = floor_pct if floor_is_live else None
        target_exit_price = cls.price_for_pnl(entry_price, live_floor_pct, side) if live_floor_pct is not None else None
        prior_floor = cls._safe_float(position.get("ratchet_floor_pct"), None)
        giveback_pct = cls.compute_giveback_pct(peak_pnl_pct, current_pnl_pct)

        if current_pnl_pct <= hard_stop_pct:
            return {
                "action": "hard_stop",
                "reason": "dead_money_tight_stop_breached" if dead_money else "hard_stop_breached",
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "floor_pct": live_floor_pct,
                "target_exit_price": target_exit_price,
                "hard_stop_price": hard_stop_price,
                "hard_stop_pct": round(hard_stop_pct, 4),
                "hold_seconds": hold_seconds,
                "ratchet_active": ratchet_active,
                "min_hold_active": min_hold_active,
                "dead_money": dead_money,
                "holding_horizon": horizon_profile["holding_horizon"],
                "giveback_pct": giveback_pct,
                "hard_stop_flags": hard_stop_flags,
            }

        if live_floor_pct is not None and current_pnl_pct <= live_floor_pct:
            return {
                "action": "ratchet_exit",
                "reason": "ratchet_floor_breached",
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "floor_pct": round(live_floor_pct, 4),
                "target_exit_price": target_exit_price,
                "hard_stop_price": hard_stop_price,
                "hard_stop_pct": round(hard_stop_pct, 4),
                "hold_seconds": hold_seconds,
                "ratchet_active": True,
                "min_hold_active": min_hold_active,
                "dead_money": dead_money,
                "holding_horizon": horizon_profile["holding_horizon"],
                "giveback_pct": giveback_pct,
                "hard_stop_flags": hard_stop_flags,
            }

        if live_floor_pct is not None and (prior_floor is None or live_floor_pct > prior_floor + 1e-9):
            return {
                "action": "update_limit",
                "reason": "ratchet_floor_raised",
                "current_pnl_pct": round(current_pnl_pct, 4),
                "peak_pnl_pct": round(peak_pnl_pct, 4),
                "floor_pct": round(live_floor_pct, 4),
                "target_exit_price": target_exit_price,
                "hard_stop_price": hard_stop_price,
                "hard_stop_pct": round(hard_stop_pct, 4),
                "hold_seconds": hold_seconds,
                "ratchet_active": True,
                "min_hold_active": min_hold_active,
                "dead_money": dead_money,
                "holding_horizon": horizon_profile["holding_horizon"],
                "giveback_pct": giveback_pct,
                "hard_stop_flags": hard_stop_flags,
            }

        return {
            "action": "hold",
            "reason": "hold_zone" if min_hold_active else ("ratchet_active" if ratchet_active else "pre_activation"),
            "current_pnl_pct": round(current_pnl_pct, 4),
            "peak_pnl_pct": round(peak_pnl_pct, 4),
            "floor_pct": round(live_floor_pct, 4) if live_floor_pct is not None else None,
            "target_exit_price": target_exit_price,
            "hard_stop_price": hard_stop_price,
            "hard_stop_pct": round(hard_stop_pct, 4),
            "hold_seconds": hold_seconds,
            "ratchet_active": ratchet_active,
            "min_hold_active": min_hold_active,
            "dead_money": dead_money,
            "holding_horizon": horizon_profile["holding_horizon"],
            "giveback_pct": giveback_pct,
            "hard_stop_flags": hard_stop_flags,
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
        base_trail = float(trail_pct if trail_pct is not None else cls.RATCHET_TRAIL_PCT)
        if peak < activation:
            return None
        # Progressive ratchet: trail tightens as the trade proves itself
        # Peak 1-3%: full trail (4%)
        # Peak 3-6%: tighter trail (2.5%) -- the trade is working, protect more
        # Peak 6-10%: tight trail (2.0%) -- big winner, lock it in
        # Peak 10%+: very tight (1.5%) -- monster winner, don't give back
        if peak >= 10.0:
            trail = min(base_trail, 1.5)
        elif peak >= 6.0:
            trail = min(base_trail, 2.0)
        elif peak >= 3.0:
            trail = min(base_trail, 2.5)
        else:
            trail = base_trail
        floor = max(floor_base, peak - trail)
        return round(floor, 4)

    @classmethod
    def profile_for_position(cls, position: Dict) -> Dict:
        holding_horizon = str((position or {}).get("holding_horizon", "intraday") or "intraday").lower()
        trail_override = cls._position_trail_override(position)
        activation_override = cls._position_activation_override(position)
        floor_override = cls._position_floor_override(position)
        if holding_horizon == "swing":
            return {
                "holding_horizon": "swing",
                "activation_pct": activation_override if activation_override is not None else cls.SWING_RATCHET_ACTIVATION_PCT,
                "initial_floor_pct": floor_override if floor_override is not None else cls.INITIAL_FLOOR_PCT,
                "trail_pct": trail_override if trail_override is not None else cls.SWING_RATCHET_TRAIL_PCT,
                "min_hold_seconds": cls.SWING_RATCHET_MIN_HOLD_SECONDS,
                "dead_money_hours": cls.SWING_DEAD_MONEY_HOURS,
            }
        return {
            "holding_horizon": holding_horizon or "intraday",
            "activation_pct": activation_override if activation_override is not None else cls.RATCHET_ACTIVATION_PCT,
            "initial_floor_pct": floor_override if floor_override is not None else cls.INITIAL_FLOOR_PCT,
            "trail_pct": trail_override if trail_override is not None else cls.RATCHET_TRAIL_PCT,
            "min_hold_seconds": cls.MIN_HOLD_SECONDS,
            "dead_money_hours": cls.DEAD_MONEY_HOURS,
        }

    @classmethod
    def initial_hard_stop_profile(cls, position: Dict) -> tuple[float, list]:
        return cls._effective_hard_stop_pct(
            position,
            current_pnl_pct=0.0,
            peak_pnl_pct=0.0,
            hold_seconds=0.0,
        )

    @classmethod
    def _position_trail_override(cls, position: Dict) -> Optional[float]:
        if not isinstance(position, dict):
            return None

        base_trail = cls._safe_float(position.get("trail_pct"), None)
        tighten_trail = cls._safe_float(position.get("ratchet_tighten_suggestion_pct"), None)

        candidates = []
        if base_trail is not None and base_trail > 0:
            candidates.append(base_trail)
        if tighten_trail is not None and tighten_trail > 0:
            candidates.append(tighten_trail)
        if not candidates:
            return None

        return max(0.5, min(10.0, min(candidates)))

    @classmethod
    def _position_activation_override(cls, position: Dict) -> Optional[float]:
        if not isinstance(position, dict):
            return None
        override = cls._safe_float(position.get("ratchet_activation_override_pct"), None)
        if override is None or override <= 0:
            return None
        return max(0.1, min(override, 100.0))

    @classmethod
    def _position_floor_override(cls, position: Dict) -> Optional[float]:
        if not isinstance(position, dict):
            return None
        override = cls._safe_float(position.get("ratchet_initial_floor_override_pct"), None)
        if override is None:
            return None
        return max(cls.INITIAL_FLOOR_PCT, min(override, 100.0))

    @classmethod
    def _effective_hard_stop_pct(
        cls,
        position: Dict,
        current_pnl_pct: float,
        peak_pnl_pct: float,
        hold_seconds: float,
    ) -> tuple[float, list]:
        hard_stop_pct = cls.HARD_STOP_PCT
        flags = []

        manual_override = cls._safe_float((position or {}).get("hard_stop_override_pct"), None)
        if manual_override is not None and manual_override < 0:
            hard_stop_pct = max(hard_stop_pct, manual_override)
            flags.append("manual_override")

        allocator_status = str((position or {}).get("allocator_status", "") or "").strip().lower()
        allocator_action = str((position or {}).get("allocator_recommended_action", "") or "").strip().lower()
        allocator_control_state = str((position or {}).get("allocator_control_state", "") or "").strip().lower()
        if not (allocator_status or allocator_action or allocator_control_state):
            for code in list((position or {}).get("allocator_reason_codes", []) or []):
                code = str(code or "").strip().lower()
                if code.startswith("status_") and not allocator_status:
                    allocator_status = code[len("status_"):]
                elif code.startswith("action_") and not allocator_action:
                    allocator_action = code[len("action_"):]
                elif code.startswith("control_") and not allocator_control_state:
                    allocator_control_state = code[len("control_"):]

        if allocator_status == "disable" or allocator_action == "disable" or allocator_control_state in {
            "manual_disabled",
            "hard_disabled",
            "soft_disabled",
        }:
            hard_stop_pct = max(hard_stop_pct, cls.PROBATION_HARD_STOP_PCT)
            flags.append("disabled_book")
        elif allocator_status == "probation" or allocator_action == "probation" or allocator_control_state == "probation":
            hard_stop_pct = max(hard_stop_pct, cls.PROBATION_HARD_STOP_PCT)
            flags.append("probation_book")
        elif (
            allocator_status == "observe"
            or allocator_action == "observe"
            or allocator_control_state == "observe"
        ):
            hard_stop_pct = max(hard_stop_pct, cls.OBSERVE_HARD_STOP_PCT)
            flags.append("observe_book")

        entry_quality = str((position or {}).get("entry_quality", "neutral") or "neutral").strip().lower()
        if entry_quality == "at_highs":
            hard_stop_pct = max(hard_stop_pct, cls.AT_HIGHS_HARD_STOP_PCT)
            flags.append("at_highs_entry")

        if bool((position or {}).get("extended_hours_entry")):
            hard_stop_pct = max(hard_stop_pct, cls.EXTENDED_HOURS_HARD_STOP_PCT)
            flags.append("extended_hours_entry")

        holding_horizon = str((position or {}).get("holding_horizon", "intraday") or "intraday").strip().lower()
        if (
            holding_horizon != "swing"
            and hold_seconds >= cls.STALLED_LOSER_HOURS * 3600.0
            and peak_pnl_pct <= cls.STALLED_LOSER_MAX_PEAK_PNL_PCT
            and current_pnl_pct <= cls.STALLED_LOSER_MIN_PNL_PCT
        ):
            hard_stop_pct = max(hard_stop_pct, cls.STALLED_LOSER_HARD_STOP_PCT)
            flags.append("stalled_loser")

        return round(hard_stop_pct, 4), flags

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
