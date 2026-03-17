"""
Risk Agent 🛡️ - Deterministic portfolio safety and size caps for Velox v2.
"""

from typing import Dict, List

from loguru import logger

from config import settings
from src.risk.risk_manager import SECTOR_MAP


DEFAULT_BRIEF = {
    "can_trade": True,
    "size_cap_pct": 1.0,
    "reasoning": "Risk agent neutral fallback — reduced size only",
    "portfolio_heat": "medium",
    "constraint_flags": ["risk_agent_fallback"],
    "error": False,
}


def _heat_bucket(heat_pct: float) -> str:
    if heat_pct >= 70:
        return "high"
    if heat_pct >= 35:
        return "medium"
    return "low"


async def analyze(
    symbol: str,
    price: float,
    signals: Dict,
    risk_manager=None,
    positions: List[Dict] = None,
    direction: str = "BUY",
) -> Dict:
    """Return deterministic risk sizing and hard portfolio constraints."""
    try:
        if not risk_manager:
            return {**DEFAULT_BRIEF, "symbol": symbol, "reasoning": "No risk manager available"}

        pos_list = positions or []
        status = risk_manager.get_status() or {}
        tier = risk_manager.get_risk_tier() or {}
        equity = float(status.get("equity", getattr(risk_manager, "equity", 0)) or 0)
        heat_pct = float(status.get("heat_pct", 0) or 0)
        consecutive_losses = int(status.get("consecutive_losses", 0) or 0)
        tier_size_pct = max(0.0, min(5.0, float(tier.get("size_pct", 1.0) or 1.0)))
        size_cap_pct = tier_size_pct
        constraint_flags: List[str] = []
        can_trade = True

        if risk_manager.is_wash_sale(symbol):
            can_trade = False
            constraint_flags.append("wash_sale")

        if not risk_manager.can_trade():
            can_trade = False
            constraint_flags.append("trading_halted")

        max_positions = int(tier.get("max_positions", 5) or 5)
        if len(pos_list) >= max_positions:
            can_trade = False
            constraint_flags.append("max_positions")

        sector = SECTOR_MAP.get(str(symbol or "").upper(), "unknown")
        sector_notional = sum(
            float(p.get("entry_price", 0) or 0) * float(p.get("quantity", 0) or 0)
            for p in pos_list
            if SECTOR_MAP.get(str(p.get("symbol", "")).upper(), "unknown") == sector
        )
        sector_exposure = ((sector_notional / equity) * 100.0) if equity > 0 else 0.0
        sector_cap = float(getattr(settings, "MAX_SECTOR_PCT", 40.0) or 40.0)
        if sector_exposure >= sector_cap:
            can_trade = False
            constraint_flags.append("sector_cap")
        elif sector_exposure >= sector_cap * 0.75:
            size_cap_pct *= 0.5
            constraint_flags.append("sector_near_cap")

        if heat_pct >= 100.0:
            can_trade = False
            constraint_flags.append("gross_heat")
        elif heat_pct >= 70.0:
            size_cap_pct *= 0.5
            constraint_flags.append("size_reduced_heat")
        elif heat_pct >= 50.0:
            size_cap_pct *= 0.7
            constraint_flags.append("size_reduced_warm_heat")

        if consecutive_losses >= 5:
            size_cap_pct *= 0.35
            constraint_flags.append("size_reduced_loss_streak")
        elif consecutive_losses >= 3:
            size_cap_pct *= 0.5
            constraint_flags.append("size_reduced_consecutive_losses")
        elif consecutive_losses >= 2:
            size_cap_pct *= 0.75
            constraint_flags.append("size_reduced_recent_losses")

        signal_tier = str((signals or {}).get("signal_tier", "tier_2") or "tier_2").lower()
        if signal_tier == "tier_3":
            size_cap_pct *= 0.5
            constraint_flags.append("size_reduced_tier3")
        elif signal_tier == "tier_2":
            size_cap_pct *= 0.85
            constraint_flags.append("size_reduced_tier2")

        spread_pct = float((signals or {}).get("spread_pct", 0) or 0)
        if spread_pct >= 1.5:
            can_trade = False
            constraint_flags.append("execution_safety_failure")
        elif spread_pct >= 0.8:
            size_cap_pct *= 0.7
            constraint_flags.append("size_reduced_spread")

        extended = bool((signals or {}).get("extended_hours") or (signals or {}).get("extended_hours_entry"))
        if extended and signal_tier != "tier_1" and bool(getattr(settings, "EXTENDED_HOURS_TIER1_ONLY", True)):
            can_trade = False
            constraint_flags.append("extended_hours_tier_block")
        elif extended:
            size_cap_pct *= float(getattr(settings, "EXTENDED_HOURS_SIZE_MULT", 0.5) or 0.5)
            constraint_flags.append("size_reduced_extended_hours")

        size_cap_pct = max(0.0, min(5.0, round(size_cap_pct, 3)))
        if can_trade and size_cap_pct <= 0.0:
            size_cap_pct = 0.25
            constraint_flags.append("size_floor_applied")

        hard_flags = [flag for flag in constraint_flags if flag in {
            "wash_sale",
            "trading_halted",
            "max_positions",
            "gross_heat",
            "execution_safety_failure",
            "extended_hours_tier_block",
            "sector_cap",
        }]
        if hard_flags:
            can_trade = False

        reasoning = "hard constraints active" if hard_flags else "portfolio capacity available"
        if constraint_flags:
            reasoning += f" ({', '.join(constraint_flags[:4])})"

        brief = {
            "symbol": symbol,
            "can_trade": bool(can_trade),
            "size_cap_pct": size_cap_pct,
            "reasoning": reasoning,
            "portfolio_heat": _heat_bucket(heat_pct),
            "constraint_flags": constraint_flags,
            "sector": sector,
            "sector_exposure_pct": round(sector_exposure, 2),
            "tier_size_pct": round(tier_size_pct, 3),
            "direction": direction,
            "error": False,
        }
        logger.debug(
            f"🛡️ Risk {symbol}: can_trade={brief['can_trade']} "
            f"size={brief['size_cap_pct']}% flags={brief['constraint_flags']}"
        )
        return brief
    except Exception as e:
        logger.error(f"Risk agent error for {symbol}: {e}")
        return {**DEFAULT_BRIEF, "symbol": symbol, "reasoning": f"Risk fallback after error: {e}"}
