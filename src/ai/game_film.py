"""
Game Film (runs every 60 minutes).
Analyzes all historical trades from data/trade_history.json.
Calculates: win rate by symbol, time of day, hold duration, entry type.
Identifies patterns: what works, what doesn't.
Feeds analysis to advisor and tuner.
Saves to data/game_film.json.
"""

import asyncio
import json
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from loguru import logger

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from .trade_history import load_all, get_analytics

DATA_DIR = Path(__file__).parent.parent.parent / "data"
GAME_FILM_FILE = DATA_DIR / "game_film.json"


class GameFilm:
    """Analyzes trade history to find what works and what doesn't."""

    INTERVAL = 3600  # 60 minutes

    def __init__(self):
        self._last_run = 0.0
        self._last_output: Optional[Dict] = None
        DATA_DIR.mkdir(exist_ok=True)

    async def run(self, bot=None) -> Optional[Dict]:
        """Run game film analysis."""
        now = time.time()
        if now - self._last_run < self.INTERVAL:
            return None
        self._last_run = now

        try:
            history = load_all()
            if len(history) < 5:
                logger.debug("Game film: not enough trades yet")
                return None

            insights = self._analyze(history)
            self._last_output = insights
            self._save(insights)
            logger.info(
                f"🎬 Game film: {insights['total_trades']} trades, "
                f"{insights['overall_win_rate_pct']:.0f}% win rate, "
                f"${insights['total_pnl']:.2f} P&L"
            )
            return insights

        except Exception as e:
            logger.error(f"Game film analysis failed: {e}")
            return None

    def get_last_output(self) -> Optional[Dict]:
        return self._last_output

    def _analyze(self, history: List[Dict]) -> Dict:
        """Full analysis across all dimensions."""
        wins = [t for t in history if t.get("pnl", 0) > 0]
        losses = [t for t in history if t.get("pnl", 0) <= 0]
        total_pnl = sum(t.get("pnl", 0) for t in history)

        insights = {
            "generated_at": time.time(),
            "total_trades": len(history),
            "total_wins": len(wins),
            "total_losses": len(losses),
            "overall_win_rate_pct": round(len(wins) / max(1, len(history)) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_win": round(sum(t.get("pnl", 0) for t in wins) / max(1, len(wins)), 2),
            "avg_loss": round(sum(t.get("pnl", 0) for t in losses) / max(1, len(losses)), 2),
        }

        # By symbol
        insights["by_symbol"] = self._aggregate(history, lambda t: t.get("symbol", "?"))

        # By hour of day (entry time)
        def _hour(t):
            et = t.get("entry_time", t.get("recorded_at", 0))
            try:
                return datetime.fromtimestamp(et).strftime("%H") if et else "?"
            except Exception:
                return "?"
        insights["by_hour"] = self._aggregate(history, _hour)

        # By hold duration bucket
        def _hold_bucket(t):
            secs = t.get("hold_seconds", 0)
            mins = secs / 60 if secs else 0
            if mins < 5: return "<5min"
            elif mins < 30: return "5-30min"
            elif mins < 120: return "30min-2h"
            elif mins < 240: return "2-4h"
            else: return ">4h"
        insights["by_hold_duration"] = self._aggregate(history, _hold_bucket)

        # By exit reason
        insights["by_exit_reason"] = self._aggregate(history, lambda t: t.get("reason", "unknown"))

        # By risk tier at entry
        insights["by_risk_tier"] = self._aggregate(history, lambda t: t.get("risk_tier", "unknown"))

        # By conviction level
        insights["by_conviction"] = self._aggregate(history, lambda t: t.get("conviction_level", "normal"))

        # By day of week
        def _dow(t):
            et = t.get("entry_time", t.get("recorded_at", 0))
            try:
                return datetime.fromtimestamp(et).strftime("%A") if et else "?"
            except Exception:
                return "?"
        insights["by_day_of_week"] = self._aggregate(history, _dow)

        # Streak analysis
        max_win_streak = max_loss_streak = cur_win = cur_loss = 0
        for t in history:
            if t.get("pnl", 0) > 0:
                cur_win += 1
                cur_loss = 0
                max_win_streak = max(max_win_streak, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                max_loss_streak = max(max_loss_streak, cur_loss)
        insights["max_win_streak"] = max_win_streak
        insights["max_loss_streak"] = max_loss_streak

        # Winner vs loser hold times
        winner_holds = [t.get("hold_seconds", 0) / 60 for t in wins if t.get("hold_seconds")]
        loser_holds = [t.get("hold_seconds", 0) / 60 for t in losses if t.get("hold_seconds")]
        insights["avg_winner_hold_min"] = round(sum(winner_holds) / max(1, len(winner_holds)), 1) if winner_holds else 0
        insights["avg_loser_hold_min"] = round(sum(loser_holds) / max(1, len(loser_holds)), 1) if loser_holds else 0

        # Recent performance (last 20)
        recent = history[-20:]
        recent_wins = len([t for t in recent if t.get("pnl", 0) > 0])
        insights["recent_20_win_rate_pct"] = round(recent_wins / max(1, len(recent)) * 100, 1)
        insights["recent_20_pnl"] = round(sum(t.get("pnl", 0) for t in recent), 2)

        # Recommendations
        insights["recommendations"] = self._generate_recommendations(insights)

        return insights

    def _aggregate(self, history: List[Dict], key_fn) -> Dict:
        """Aggregate trades by a key function."""
        buckets = defaultdict(lambda: {"trades": 0, "wins": 0, "pnl": 0.0})
        for t in history:
            k = key_fn(t)
            b = buckets[k]
            b["trades"] += 1
            b["pnl"] += t.get("pnl", 0)
            if t.get("pnl", 0) > 0:
                b["wins"] += 1
        result = {}
        for k, b in buckets.items():
            b["win_rate_pct"] = round(b["wins"] / max(1, b["trades"]) * 100, 1)
            b["avg_pnl"] = round(b["pnl"] / max(1, b["trades"]), 2)
            b["pnl"] = round(b["pnl"], 2)
            result[k] = b
        return dict(sorted(result.items(), key=lambda x: x[1]["pnl"], reverse=True))

    def _generate_recommendations(self, insights: Dict) -> Dict:
        """Auto-generate recommendations from the data."""
        recs = {}

        # Best and worst symbols
        by_sym = insights.get("by_symbol", {})
        profitable_syms = [(k, v) for k, v in by_sym.items() if v["trades"] >= 3 and v["avg_pnl"] > 0]
        losing_syms = [(k, v) for k, v in by_sym.items() if v["trades"] >= 3 and v["avg_pnl"] < 0]
        if profitable_syms:
            recs["best_symbols"] = [s[0] for s in profitable_syms[:5]]
        if losing_syms:
            recs["worst_symbols"] = [s[0] for s in sorted(losing_syms, key=lambda x: x[1]["avg_pnl"])[:5]]

        # Best hours
        by_hour = insights.get("by_hour", {})
        best_hours = [(k, v) for k, v in by_hour.items() if v["trades"] >= 3 and v["avg_pnl"] > 0]
        if best_hours:
            recs["best_hours"] = [h[0] for h in sorted(best_hours, key=lambda x: x[1]["avg_pnl"], reverse=True)[:3]]

        # Hold time insight
        if insights["avg_winner_hold_min"] > 0 and insights["avg_loser_hold_min"] > 0:
            if insights["avg_loser_hold_min"] > insights["avg_winner_hold_min"] * 2:
                recs["holding_losers_too_long"] = True
                recs["suggested_max_hold_min"] = round(insights["avg_winner_hold_min"] * 1.5, 0)

        # Best exit reasons
        by_reason = insights.get("by_exit_reason", {})
        profitable_reasons = [(k, v) for k, v in by_reason.items() if v["avg_pnl"] > 0]
        if profitable_reasons:
            recs["best_exit_reasons"] = [r[0] for r in sorted(profitable_reasons, key=lambda x: x[1]["avg_pnl"], reverse=True)[:3]]

        return recs

    def _save(self, insights: Dict):
        try:
            GAME_FILM_FILE.write_text(json.dumps(insights, indent=2))
        except Exception as e:
            logger.warning(f"Failed to save game film: {e}")
