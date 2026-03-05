"""
Risk Manager - Dynamic position sizing engine that scales from $1K to $1M.
Risk tiers automatically tighten as the account grows.
"""

import json
import time
from pathlib import Path
from typing import Dict, List, Optional
from loguru import logger

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from config import settings

DATA_DIR = Path(__file__).parent.parent.parent / "data"

# ── Risk Tiers ─────────────────────────────────────────────────────
RISK_TIERS = {
    "SURVIVAL":  {"min": 0,      "max": 500,    "size_pct": 10.0, "max_positions": 3,  "stop_pct": 2.0, "daily_loss_pct": 8.0},
    "GROWTH":    {"min": 500,    "max": 2000,   "size_pct": 7.0,  "max_positions": 5,  "stop_pct": 1.5, "daily_loss_pct": 6.0},
    "SCALING":   {"min": 2000,   "max": 10000,  "size_pct": 4.0,  "max_positions": 8,  "stop_pct": 1.5, "daily_loss_pct": 5.0},
    "COMPOUND":  {"min": 10000,  "max": 50000,  "size_pct": 2.5,  "max_positions": 10, "stop_pct": 1.0, "daily_loss_pct": 4.0},
    "PROTECT":   {"min": 50000,  "max": 250000, "size_pct": 1.5,  "max_positions": 12, "stop_pct": 1.0, "daily_loss_pct": 3.0},
    "PRESERVE":  {"min": 250000, "max": 1000000,"size_pct": 0.8,  "max_positions": 15, "stop_pct": 0.8, "daily_loss_pct": 2.0},
}

MILESTONES = [1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000]

# ── Sector Mapping (top symbols) ───────────────────────────────
SECTOR_MAP = {
    "AAPL": "Technology", "MSFT": "Technology", "GOOGL": "Technology", "GOOG": "Technology",
    "META": "Technology", "NVDA": "Technology", "AMD": "Technology", "INTC": "Technology",
    "CRM": "Technology", "ADBE": "Technology", "ORCL": "Technology", "CSCO": "Technology",
    "AVGO": "Technology", "QCOM": "Technology", "TXN": "Technology", "NOW": "Technology",
    "IBM": "Technology", "MU": "Technology", "AMAT": "Technology", "LRCX": "Technology",
    "AMZN": "Consumer Discretionary", "TSLA": "Consumer Discretionary", "HD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary", "MCD": "Consumer Discretionary", "SBUX": "Consumer Discretionary",
    "TGT": "Consumer Discretionary", "LOW": "Consumer Discretionary", "BKNG": "Consumer Discretionary",
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials", "GS": "Financials",
    "MS": "Financials", "C": "Financials", "BLK": "Financials", "SCHW": "Financials",
    "V": "Financials", "MA": "Financials", "AXP": "Financials", "PYPL": "Financials",
    "JNJ": "Healthcare", "UNH": "Healthcare", "PFE": "Healthcare", "ABBV": "Healthcare",
    "MRK": "Healthcare", "LLY": "Healthcare", "TMO": "Healthcare", "ABT": "Healthcare",
    "BMY": "Healthcare", "AMGN": "Healthcare", "GILD": "Healthcare", "MDT": "Healthcare",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy", "EOG": "Energy",
    "PG": "Consumer Staples", "KO": "Consumer Staples", "PEP": "Consumer Staples",
    "WMT": "Consumer Staples", "COST": "Consumer Staples", "CL": "Consumer Staples",
    "DIS": "Communication Services", "NFLX": "Communication Services", "CMCSA": "Communication Services",
    "T": "Communication Services", "VZ": "Communication Services", "TMUS": "Communication Services",
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities", "D": "Utilities",
    "UNP": "Industrials", "BA": "Industrials", "HON": "Industrials", "CAT": "Industrials",
    "GE": "Industrials", "RTX": "Industrials", "DE": "Industrials", "LMT": "Industrials",
    "AMT": "Real Estate", "PLD": "Real Estate", "SPG": "Real Estate",
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials", "FCX": "Materials",
}


class RiskManager:
    """
    Dynamic risk engine:
      - Tiers auto-adjust sizing/stops based on portfolio equity
      - Streak detection (anti-martingale up, tilt protection down)
      - Heat tracking (total open risk vs daily loss budget)
      - Milestone tracking toward $1M
    """

    def __init__(self):
        # Config defaults (overridden by tier)
        self.base_position_pct = settings.POSITION_SIZE_PCT
        self.max_daily_loss_pct = settings.MAX_DAILY_LOSS_PCT

        # Live state
        self._equity = settings.TOTAL_CAPITAL
        self._starting_equity = settings.TOTAL_CAPITAL
        self._ath_equity = settings.TOTAL_CAPITAL
        self._start_date = time.time()
        self.daily_pnl = 0.0
        self.trading_halted = False

        # Streak tracking
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        self.trade_history: List[Dict] = []
        self._size_multiplier = 1.0

        # Weekly circuit breaker
        self.weekly_pnl = 0.0
        self.weekly_size_reduction = 1.0  # 1.0 = normal, 0.5 = reduced
        self._week_start_day = self._get_current_monday()

        # Heat tracking (total open risk in dollars)
        self._open_risk = 0.0  # sum of (entry_price * qty * stop_pct/100) for all positions

        # Wash sale tracking: {symbol: {"loss": float, "exit_time": float}}
        # If sold at a loss, can't rebuy within 30 days or loss is disallowed for taxes
        self._wash_sale_list: Dict[str, Dict] = {}

        # Round trip (day trade) tracking: [{symbol, entry_time, exit_time}]
        self._round_trips: List[Dict] = []

        # Load persisted state (including wash sales)
        self._load_state()

        tier = self.get_risk_tier(self._equity)
        logger.info(
            f"Risk manager initialized: tier={tier['name']}, equity=${self._equity:,.2f}, "
            f"size={tier['size_pct']}%, max_pos={tier['max_positions']}, stop={tier['stop_pct']}%"
        )

    # ── Equity & Tier ──────────────────────────────────────────────

    @property
    def equity(self) -> float:
        return self._equity

    def update_equity(self, equity: float):
        """Called every scan cycle with live Alpaca equity."""
        self._equity = equity
        if equity > self._ath_equity:
            self._ath_equity = equity
        self._save_state()

    def get_risk_tier(self, equity: float = None) -> Dict:
        """Returns current tier dict with name included."""
        eq = equity if equity is not None else self._equity
        for name, tier in RISK_TIERS.items():
            if tier["min"] <= eq < tier["max"]:
                return {**tier, "name": name}
        # Above $1M — use PRESERVE
        return {**RISK_TIERS["PRESERVE"], "name": "PRESERVE"}

    # ── Position Sizing ────────────────────────────────────────────

    def get_position_size(self, price: float, buying_power: float, conviction: str = "normal") -> float:
        """
        Calculate position size in DOLLARS (notional).
        Uses current tier's size_pct, adjusted by conviction.
        """
        if price <= 0 or buying_power <= 0:
            return 0.0

        tier = self.get_risk_tier()
        conviction_mult = {"high": 1.5, "normal": 1.0, "speculative": 0.5}.get(conviction, 1.0)

        # Base size from tier
        base_notional = self._equity * (tier["size_pct"] / 100.0) * conviction_mult * self._size_multiplier

        # Cap at buying power with buffer
        max_from_bp = buying_power * 0.95
        base_notional = min(base_notional, max_from_bp)

        # No single position > 25% of equity
        base_notional = min(base_notional, self._equity * 0.25)

        # Apply heat, streak, and weekly adjustments
        base_notional = self.adjust_for_heat(base_notional)
        base_notional = self.adjust_for_streak(base_notional)
        self._check_weekly_reset()
        self._check_weekly_circuit_breaker()
        base_notional *= self.weekly_size_reduction

        return max(0.0, round(base_notional, 2))

    def get_shares(self, price: float, notional: float) -> float:
        """Convert dollar amount to shares (supports fractional)."""
        if price <= 0 or notional <= 0:
            return 0.0
        return round(notional / price, 6)

    def adjust_for_heat(self, base_size: float) -> float:
        """Reduce size if portfolio heat is high."""
        tier = self.get_risk_tier()
        daily_loss_budget = self._equity * (tier["daily_loss_pct"] / 100.0)
        if daily_loss_budget <= 0:
            return 0.0

        # Heat = how much of daily loss budget is consumed by open risk + realized losses
        total_heat = self._open_risk + abs(min(0, self.daily_pnl))
        heat_pct = total_heat / daily_loss_budget

        if heat_pct > 0.75:
            return 0.0  # Too hot, no new positions
        elif heat_pct > 0.50:
            return base_size * 0.5
        return base_size

    def adjust_for_streak(self, base_size: float) -> float:
        """Anti-martingale: increase on wins, decrease on losses."""
        if self.consecutive_wins >= 3:
            return base_size * 1.25  # Hot streak
        elif self.consecutive_losses >= 5:
            return base_size * 0.5   # Tilt protection
        elif self.consecutive_losses >= 2:
            return base_size * 0.75  # Cooling off
        return base_size

    def update_open_risk(self, positions: List[Dict]):
        """Recalculate total open risk from position list."""
        tier = self.get_risk_tier()
        stop_pct = tier["stop_pct"]
        self._open_risk = sum(
            p.get("entry_price", 0) * p.get("quantity", 0) * (stop_pct / 100.0)
            for p in positions
        )

    def should_reduce_size(self) -> bool:
        """Check if conditions warrant reducing size. Placeholder for VIX integration."""
        # Future: check VIX via Alpaca data API
        return False

    # ── Pre-trade Checks ───────────────────────────────────────────

    def can_trade(self) -> bool:
        """Check if trading is allowed (circuit breakers)."""
        if self.trading_halted:
            logger.warning("Trading halted by circuit breaker")
            return False
        tier = self.get_risk_tier()
        daily_loss_pct = (self.daily_pnl / self._equity) * 100 if self._equity else 0
        if daily_loss_pct <= -tier["daily_loss_pct"]:
            logger.error(f"🚨 Daily loss limit hit: {daily_loss_pct:.2f}% (tier limit: -{tier['daily_loss_pct']}%)")
            self.trading_halted = True
            return False
        return True

    def is_wash_sale(self, symbol: str) -> bool:
        """Check if buying this symbol would trigger a wash sale (sold at loss within 30 days)."""
        self._clean_expired_wash_sales()
        if symbol in self._wash_sale_list:
            entry = self._wash_sale_list[symbol]
            days_ago = (time.time() - entry["exit_time"]) / 86400
            logger.warning(f"🧼 WASH SALE BLOCKED: {symbol} — lost ${entry['loss']:.2f}, exited {days_ago:.0f} days ago (need 31)")
            return True
        return False

    def _clean_expired_wash_sales(self):
        """Remove wash sale entries older than 31 days."""
        cutoff = time.time() - (31 * 86400)
        expired = [s for s, v in self._wash_sale_list.items() if v["exit_time"] < cutoff]
        for s in expired:
            logger.info(f"🧼 Wash sale cleared: {s} (31 days passed)")
            del self._wash_sale_list[s]

    def can_open_position(self, current_positions: List[Dict], symbol: str = None) -> bool:
        """Check if we can open a new position."""
        if not self.can_trade():
            return False
        # Wash sale check
        if symbol and self.is_wash_sale(symbol):
            return False
        # PDT protection: if equity < $25K, limit to 3 day trades per 5 business days
        if self._equity < 25000:
            day_trades = self._count_recent_day_trades()
            if day_trades >= 3:
                logger.error(f"🚨 PDT GUARD: {day_trades}/3 day trades used, equity ${self._equity:,.0f} < $25K — BLOCKED")
                return False
            elif day_trades >= 2:
                logger.warning(f"⚠️ PDT WARNING: {day_trades}/3 day trades used — 1 remaining")
        tier = self.get_risk_tier()
        if len(current_positions) >= tier["max_positions"]:
            logger.warning(f"Max positions for {tier['name']} tier: {len(current_positions)}/{tier['max_positions']}")
            return False
        return True

    def _count_recent_day_trades(self) -> int:
        """Count round trips (day trades) in the last 5 business days."""
        cutoff = time.time() - (5 * 24 * 3600)  # 5 calendar days (conservative)
        count = sum(1 for rt in self._round_trips if rt.get("exit_time", 0) > cutoff)
        # Also check trade_history for any not yet in round_trips
        for trade in self.trade_history:
            t = trade.get("exit_time") or trade.get("time", 0)
            if t > cutoff and trade.get("hold_seconds", 86400) < 86400:
                # Check not already counted in round_trips
                sym = trade.get("symbol")
                already = any(rt["symbol"] == sym and abs(rt["exit_time"] - t) < 60 for rt in self._round_trips)
                if not already:
                    count += 1
        return count

    # ── Post-trade Updates ─────────────────────────────────────────

    def record_trade(self, trade: Dict):
        """Record a completed trade and update streak/risk state."""
        pnl = trade.get("pnl", 0)
        self.daily_pnl += pnl
        self._check_weekly_reset()
        self.weekly_pnl += pnl
        self.total_trades += 1
        self.trade_history.append(trade)

        if pnl >= 0:
            self.winning_trades += 1
            self.consecutive_wins += 1
            self.consecutive_losses = 0
        else:
            self.losing_trades += 1
            self.consecutive_losses += 1
            self.consecutive_wins = 0

        # Update size multiplier (legacy compat)
        if self.consecutive_losses >= 3:
            self._size_multiplier = 0.5
        elif self.consecutive_wins >= 3:
            self._size_multiplier = 1.25
        else:
            self._size_multiplier = 1.0

        tier = self.get_risk_tier()
        daily_pct = (self.daily_pnl / self._equity) * 100 if self._equity else 0
        logger.info(
            f"Trade recorded: {trade.get('symbol')} pnl=${pnl:.2f} | "
            f"Daily: ${self.daily_pnl:.2f} ({daily_pct:+.2f}%) | "
            f"Tier: {tier['name']} | W/L: {self.winning_trades}/{self.losing_trades} | "
            f"Streak: {self.consecutive_wins}W/{self.consecutive_losses}L"
        )

        if daily_pct <= -tier["daily_loss_pct"]:
            logger.error(f"🚨 CIRCUIT BREAKER: Daily loss {daily_pct:.2f}% → HALTING")
            self.trading_halted = True

        # ── Wash sale tracking ──
        symbol = trade.get("symbol", "")
        if pnl < 0 and symbol:
            self._wash_sale_list[symbol] = {
                "loss": pnl,
                "exit_time": trade.get("exit_time", time.time()),
            }
            logger.warning(f"🧼 WASH SALE: {symbol} sold at ${pnl:.2f} loss — blocked for 30 days")

        # ── Round trip tracking ──
        if symbol:
            entry_t = trade.get("entry_time", 0)
            exit_t = trade.get("exit_time", time.time())
            hold = exit_t - entry_t if entry_t else 86400
            if hold < 86400:  # Same-day = round trip / day trade
                self._round_trips.append({
                    "symbol": symbol, "entry_time": entry_t, "exit_time": exit_t, "pnl": pnl
                })
                rt_count = self._count_recent_day_trades()
                logger.info(f"🔄 Round trip #{rt_count}: {symbol} (held {hold/3600:.1f}h, ${pnl:+.2f})")

    # ── Status & Milestones ────────────────────────────────────────

    def get_status(self) -> Dict:
        tier = self.get_risk_tier()
        daily_pct = (self.daily_pnl / self._equity) * 100 if self._equity else 0
        win_rate = self.winning_trades / self.total_trades if self.total_trades else 0
        drawdown = ((self._ath_equity - self._equity) / self._ath_equity * 100) if self._ath_equity > 0 else 0
        days_trading = max(1, (time.time() - self._start_date) / 86400)
        total_return = ((self._equity - self._starting_equity) / self._starting_equity * 100) if self._starting_equity > 0 else 0
        daily_avg_return = total_return / days_trading if days_trading > 0 else 0

        # Next milestone
        next_milestone = None
        for m in MILESTONES:
            if self._equity < m:
                next_milestone = m
                break

        # Heat
        daily_loss_budget = self._equity * (tier["daily_loss_pct"] / 100.0)
        total_heat = self._open_risk + abs(min(0, self.daily_pnl))
        heat_pct = (total_heat / daily_loss_budget * 100) if daily_loss_budget > 0 else 0

        return {
            # Tier
            "tier_name": tier["name"],
            "tier_size_pct": tier["size_pct"],
            "tier_max_positions": tier["max_positions"],
            "tier_stop_pct": tier["stop_pct"],
            "tier_daily_loss_pct": tier["daily_loss_pct"],
            # Performance
            "trading_halted": self.trading_halted,
            "daily_pnl": round(self.daily_pnl, 2),
            "daily_pnl_pct": round(daily_pct, 2),
            "total_trades": self.total_trades,
            "winning_trades": self.winning_trades,
            "losing_trades": self.losing_trades,
            "win_rate": round(win_rate, 4),
            "consecutive_wins": self.consecutive_wins,
            "consecutive_losses": self.consecutive_losses,
            "size_multiplier": self._size_multiplier,
            # Portfolio
            "equity": round(self._equity, 2),
            "starting_equity": round(self._starting_equity, 2),
            "ath_equity": round(self._ath_equity, 2),
            "drawdown_pct": round(drawdown, 2),
            "total_return_pct": round(total_return, 2),
            "daily_avg_return_pct": round(daily_avg_return, 4),
            "days_trading": round(days_trading, 1),
            # Milestones
            "next_milestone": next_milestone,
            "milestone_progress_pct": round(self._equity / next_milestone * 100, 1) if next_milestone else 100.0,
            # Heat
            "heat_pct": round(heat_pct, 1),
            "open_risk": round(self._open_risk, 2),
            # Legacy compat
            "max_positions": tier["max_positions"],
            "max_deployed": self._equity * (tier["size_pct"] / 100.0) * tier["max_positions"],
        }

    def reset_daily(self):
        """Reset daily stats (call at market open)."""
        self.daily_pnl = 0.0
        self.trading_halted = False
        self.consecutive_wins = 0
        self.consecutive_losses = 0
        self._size_multiplier = 1.0
        self.trade_history.clear()
        self.total_trades = 0
        self.winning_trades = 0
        self.losing_trades = 0
        logger.info("📊 Daily risk stats reset")

    def reset_weekly(self):
        """Reset weekly stats (call on Monday or when week changes)."""
        self.weekly_pnl = 0.0
        self.weekly_size_reduction = 1.0
        self._week_start_day = self._get_current_monday()
        logger.info("📊 Weekly risk stats reset")

    def _get_current_monday(self) -> str:
        """Return ISO date string of current week's Monday."""
        from datetime import datetime, timedelta
        today = datetime.now()
        monday = today - timedelta(days=today.weekday())
        return monday.strftime("%Y-%m-%d")

    def _check_weekly_reset(self):
        """Auto-reset weekly stats if we've crossed into a new week."""
        current_monday = self._get_current_monday()
        if current_monday != self._week_start_day:
            self.reset_weekly()

    def _check_weekly_circuit_breaker(self):
        """If weekly loss exceeds threshold, reduce position sizes."""
        if self._equity <= 0:
            return
        weekly_loss_pct = (self.weekly_pnl / self._equity) * 100
        if weekly_loss_pct <= -settings.WEEKLY_MAX_LOSS_PCT:
            if self.weekly_size_reduction == 1.0:
                logger.error(f"🚨 WEEKLY CIRCUIT BREAKER: {weekly_loss_pct:.2f}% → reducing sizes by 50%")
            self.weekly_size_reduction = 0.5
        else:
            self.weekly_size_reduction = 1.0

    def can_enter_sector(self, symbol: str, positions: List[Dict]) -> bool:
        """Check if adding this symbol would exceed sector concentration limit."""
        sector = SECTOR_MAP.get(symbol, "unknown")
        if sector == "unknown":
            return True  # Don't block unknown sectors

        # Calculate current sector exposure
        sector_notional = 0.0
        total_notional = 0.0
        for p in positions:
            pos_value = p.get("entry_price", 0) * p.get("quantity", 0)
            total_notional += pos_value
            if SECTOR_MAP.get(p.get("symbol", ""), "unknown") == sector:
                sector_notional += pos_value

        if self._equity <= 0:
            return True
        sector_pct = (sector_notional / self._equity) * 100
        if sector_pct >= settings.MAX_SECTOR_PCT:
            logger.warning(f"Sector limit: {sector} at {sector_pct:.1f}% (max {settings.MAX_SECTOR_PCT}%)")
            return False
        return True

    def get_buying_power_field(self, balances: Dict) -> float:
        """Return appropriate buying power based on cash vs margin account."""
        if settings.CASH_ACCOUNT:
            return balances.get("non_marginable_buying_power",
                                balances.get("cash", balances.get("buying_power", 0)))
        return balances.get("buying_power", 0)

    def halt(self):
        self.trading_halted = True
        logger.warning("🛑 Trading manually halted")

    def resume(self):
        tier = self.get_risk_tier()
        daily_pct = (self.daily_pnl / self._equity) * 100 if self._equity else 0
        if daily_pct <= -tier["daily_loss_pct"]:
            logger.error("Cannot resume: daily loss limit still exceeded")
            return False
        self.trading_halted = False
        logger.info("▶️ Trading resumed")
        return True

    # ── Persistence ────────────────────────────────────────────────

    def _save_state(self):
        """Save milestone tracking state."""
        DATA_DIR.mkdir(exist_ok=True)
        state = {
            "equity": self._equity,
            "starting_equity": self._starting_equity,
            "ath_equity": self._ath_equity,
            "start_date": self._start_date,
            "wash_sale_list": self._wash_sale_list,
            "round_trips": self._round_trips[-100:],  # Keep last 100
        }
        try:
            (DATA_DIR / "risk_state.json").write_text(json.dumps(state, indent=2))
        except Exception:
            pass

    def _load_state(self):
        """Load milestone tracking state."""
        state_file = DATA_DIR / "risk_state.json"
        if state_file.exists():
            try:
                state = json.loads(state_file.read_text())
                self._starting_equity = state.get("starting_equity", self._starting_equity)
                self._ath_equity = state.get("ath_equity", self._ath_equity)
                self._start_date = state.get("start_date", self._start_date)
                self._wash_sale_list = state.get("wash_sale_list", {})
                self._round_trips = state.get("round_trips", [])
                logger.info(f"Loaded risk state: ATH=${self._ath_equity:,.2f}, start=${self._starting_equity:,.2f}, wash_sales={len(self._wash_sale_list)}, round_trips={len(self._round_trips)}")
            except Exception:
                pass
