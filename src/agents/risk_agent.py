"""
Risk Agent 🛡️ - Portfolio risk, position sizing, sector exposure, PDT, wash sales, drawdown.
Uses Claude Sonnet (analytical).
"""

from typing import Dict, List
from loguru import logger

from agents.base_agent import call_gpt


DEFAULT_BRIEF = {
    "approved": False,
    "max_size_pct": 0.0,
    "reasoning": "Risk agent unavailable — defaulting to block",
    "portfolio_heat": "high",
    "warnings": ["risk_agent_failed"],
    "error": True,
}

PROMPT_TEMPLATE = """You are a RISK MANAGEMENT specialist inside Velox, an autonomous momentum trading engine.
Your job: approve or deny new positions based on portfolio risk. Other agents handle the trade thesis.
Be the adult in the room — protect capital, but don't be so conservative you kill returns.
We use 3% trailing stops, so individual position risk is capped.

PROPOSED TRADE:
- Symbol: {symbol}
- Direction: {direction}
- Current price: ${price:.2f}

PORTFOLIO STATE:
- Equity: ${equity:,.2f}
- Risk tier: {tier_name} (size={tier_size_pct}%, max_positions={tier_max_pos})
- Open positions: {num_positions}/{tier_max_pos}
- Daily P&L: ${daily_pnl:.2f} ({daily_pnl_pct:+.2f}%)
- Portfolio heat: {heat_pct:.0f}% of daily loss budget
- Consecutive losses: {consec_losses}
- Win streak: {consec_wins}

CURRENT POSITIONS:
{positions_summary}

RISK FLAGS:
- PDT status: {pdt_status}
- Wash sale blocked: {wash_sale}
- Weekly P&L: ${weekly_pnl:.2f}
- Drawdown from ATH: {drawdown_pct:.1f}%
- Sector of new trade: {sector}
- Sector exposure: {sector_exposure:.1f}%

Should this trade be approved? If yes, what's the max position size as % of equity?

Respond with ONLY valid JSON:
{{"approved": true/false, "max_size_pct": 0.0-5.0, "reasoning": "brief explanation", "portfolio_heat": "low" or "medium" or "high", "warnings": ["list", "of", "concerns"]}}"""


async def analyze(symbol: str, price: float, signals: Dict,
                  risk_manager=None, positions: List[Dict] = None,
                  direction: str = "BUY") -> Dict:
    """Run risk assessment. Returns structured brief."""
    try:
        if not risk_manager:
            return {**DEFAULT_BRIEF, "reasoning": "No risk manager available"}

        status = risk_manager.get_status()
        tier = risk_manager.get_risk_tier()

        # Build positions summary
        pos_list = positions or []
        if pos_list:
            pos_lines = []
            for p in pos_list[:10]:
                sym = p.get("symbol", "?")
                entry = p.get("entry_price", 0)
                qty = p.get("quantity", 0)
                side = p.get("side", "long")
                pos_lines.append(f"  {sym}: {side} {qty} @ ${entry:.2f}")
            positions_summary = "\n".join(pos_lines)
        else:
            positions_summary = "  No open positions"

        # Sector info
        from risk.risk_manager import SECTOR_MAP
        sector = SECTOR_MAP.get(symbol, "Unknown")
        sector_notional = sum(
            p.get("entry_price", 0) * p.get("quantity", 0)
            for p in pos_list
            if SECTOR_MAP.get(p.get("symbol", ""), "Unknown") == sector
        )
        equity = status.get("equity", 1)
        sector_exposure = (sector_notional / equity * 100) if equity > 0 else 0

        # PDT check
        pdt_status = "SAFE"
        if equity < 25000:
            day_trades = risk_manager._count_recent_day_trades()
            pdt_status = f"{day_trades}/3 day trades used (equity < $25K)"

        prompt = PROMPT_TEMPLATE.format(
            symbol=symbol,
            direction=direction,
            price=price,
            equity=equity,
            tier_name=tier.get("name", "?"),
            tier_size_pct=tier.get("size_pct", 2),
            tier_max_pos=tier.get("max_positions", 5),
            num_positions=len(pos_list),
            daily_pnl=status.get("daily_pnl", 0),
            daily_pnl_pct=status.get("daily_pnl_pct", 0),
            heat_pct=status.get("heat_pct", 0),
            consec_losses=status.get("consecutive_losses", 0),
            consec_wins=status.get("consecutive_wins", 0),
            positions_summary=positions_summary,
            pdt_status=pdt_status,
            wash_sale="YES — BLOCKED" if risk_manager.is_wash_sale(symbol) else "No",
            weekly_pnl=risk_manager.weekly_pnl,
            drawdown_pct=status.get("drawdown_pct", 0),
            sector=sector,
            sector_exposure=sector_exposure,
        )

        result = await call_gpt(prompt, max_tokens=400)
        if not result or "approved" not in result:
            logger.warning(f"Risk agent failed for {symbol} — blocking by default")
            return {**DEFAULT_BRIEF, "symbol": symbol}

        brief = {
            "approved": bool(result.get("approved", False)),
            "max_size_pct": max(0.0, min(5.0, float(result.get("max_size_pct", 0)))),
            "reasoning": str(result.get("reasoning", ""))[:200],
            "portfolio_heat": result.get("portfolio_heat", "medium"),
            "warnings": result.get("warnings", []),
        }

        # Hard overrides — AI can't bypass these
        if risk_manager.is_wash_sale(symbol):
            brief["approved"] = False
            brief["warnings"].append("wash_sale_blocked")
            brief["reasoning"] = f"Wash sale: {symbol} sold at loss within 30 days"
        if not risk_manager.can_trade():
            brief["approved"] = False
            brief["warnings"].append("trading_halted")
        if len(pos_list) >= tier.get("max_positions", 5):
            brief["approved"] = False
            brief["warnings"].append("max_positions_reached")

        logger.debug(f"🛡️ Risk {symbol}: approved={brief['approved']} size={brief['max_size_pct']}% heat={brief['portfolio_heat']}")
        return brief

    except Exception as e:
        logger.error(f"Risk agent error for {symbol}: {e}")
        return {**DEFAULT_BRIEF, "symbol": symbol}
