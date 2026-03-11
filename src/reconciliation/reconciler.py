"""Broker-vs-internal reconciliation helpers."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from src import persistence
from src.ai import trade_history


DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
RECONCILIATION_FILE = DATA_DIR / "reconciliation_state.json"


class Reconciler:
    def __init__(self, alpaca_client, entry_manager=None, options_engine=None):
        self.alpaca_client = alpaca_client
        self.entry_manager = entry_manager
        self.options_engine = options_engine

    def snapshot(self, trade_date: Optional[str] = None) -> Dict:
        previous = self._load_json(RECONCILIATION_FILE)
        broker = self.get_broker_truth(trade_date=trade_date)
        internal = self.get_internal_analytics(trade_date=trade_date or broker.get("date"))
        reconciliation = self.classify_mismatch(broker, internal)
        canaries = self.build_canaries(broker, internal, reconciliation, previous)
        trust = self.build_trust_flags(reconciliation)
        payload = {
            "as_of": time.time(),
            "date": trade_date or broker.get("date") or time.strftime("%Y-%m-%d"),
            "broker": broker,
            "internal": internal,
            "reconciliation": reconciliation,
            "canaries": canaries,
            "trust": trust,
        }
        self._save(payload)
        if reconciliation.get("status") != "healthy":
            logger.warning(
                "BROKER TRUTH:\n"
                f"equity={broker.get('equity')} last_equity={broker.get('last_equity')} "
                f"day_pnl={broker.get('day_pnl')} open_unrealized={broker.get('current_open_unrealized')} "
                f"overnight_gap={broker.get('overnight_gap_pnl')}\n"
                "INTERNAL ANALYTICS:\n"
                f"pnl_state_realized={internal.get('pnl_state_realized')} "
                f"trade_history_realized={internal.get('trade_history_realized')} "
                f"game_film_realized={internal.get('game_film_realized')} "
                f"trade_count={internal.get('trade_history_trade_count')}\n"
                "RECONCILIATION:\n"
                f"status={reconciliation.get('status')} "
                f"broker_vs_pnl_state={reconciliation.get('broker_vs_pnl_state_diff')} "
                f"broker_vs_trade_history={reconciliation.get('broker_vs_trade_history_diff')} "
                f"reasons={','.join(reconciliation.get('reasons', []))} "
                f"canaries={','.join(c.get('code', '') for c in canaries)}"
            )
        return payload

    def get_broker_truth(self, trade_date: Optional[str] = None) -> Dict:
        account = self.alpaca_client.get_account() if self.alpaca_client else {}
        positions = self.alpaca_client.get_positions() if self.alpaca_client else []
        activities = self.alpaca_client.get_account_activities(activity_types="FILL", date=trade_date) if self.alpaca_client else []
        portfolio_history = self.alpaca_client.get_portfolio_history(period="1D", timeframe="15Min") if self.alpaca_client else {}

        equity = float(account.get("equity", 0) or 0)
        last_equity = float(account.get("last_equity", 0) or 0)
        cash = float(account.get("cash", 0) or 0)
        long_mv = float(account.get("long_market_value", 0) or 0)
        short_mv = float(account.get("short_market_value", 0) or 0)
        position_mv = long_mv + abs(short_mv)
        current_open_unrealized = round(sum(float(p.get("unrealized_pnl", 0) or 0) for p in positions), 2)
        day_pnl = round(equity - last_equity, 2)
        day_pnl_pct = round((day_pnl / last_equity * 100.0), 2) if last_equity else 0.0

        timestamps = list(portfolio_history.get("timestamp") or []) if isinstance(portfolio_history, dict) else []
        equities = list(portfolio_history.get("equity") or []) if isinstance(portfolio_history, dict) else []
        pnl_series = list(portfolio_history.get("profit_loss") or []) if isinstance(portfolio_history, dict) else []
        overnight_gap_pnl = round(float(pnl_series[0]), 2) if pnl_series else None
        intraday_change_from_open = round(float(equities[-1]) - float(equities[0]), 2) if len(equities) >= 2 else None

        carryover_symbols = []
        carryover_fragment_symbols = []
        intraday_symbols = []
        today_key = trade_date or time.strftime("%Y-%m-%d")
        if self.entry_manager:
            tracked_positions = getattr(self.entry_manager, "positions", {}) or {}
            for symbol, pos in tracked_positions.items():
                qty = float(pos.get("quantity", 0) or 0)
                entry_time = pos.get("entry_time")
                entry_source = pos.get("entry_time_source") or ""
                if entry_source == "broker_fallback":
                    carryover_symbols.append(symbol)
                    if qty < 1:
                        carryover_fragment_symbols.append(symbol)
                    continue
                if entry_time:
                    try:
                        day = self._trade_day_key(float(entry_time))
                    except Exception:
                        day = today_key
                    if day == today_key:
                        intraday_symbols.append(symbol)
                    else:
                        carryover_symbols.append(symbol)
                        if qty < 1:
                            carryover_fragment_symbols.append(symbol)

        broker_open_symbols = sorted({
            str(p.get("symbol", "") or "").upper()
            for p in positions
            if str(p.get("symbol", "") or "").strip()
        })
        symbols_with_broker_activity = sorted({
            str(a.get("symbol", "") or "").upper()
            for a in activities
            if str(a.get("symbol", "") or "").strip()
        })
        broker_closed_symbols = sorted(
            set(symbols_with_broker_activity) - set(broker_open_symbols)
        )
        if not trade_date:
            trade_date = str(account.get("balance_asof") or today_key)

        return {
            "date": trade_date,
            "equity": round(equity, 2),
            "last_equity": round(last_equity, 2),
            "day_pnl": day_pnl,
            "day_pnl_pct": day_pnl_pct,
            "cash": round(cash, 2),
            "long_market_value": round(long_mv, 2),
            "short_market_value": round(short_mv, 2),
            "position_market_value": round(position_mv, 2),
            "current_open_unrealized": current_open_unrealized,
            "overnight_gap_pnl": overnight_gap_pnl,
            "intraday_change_from_open": intraday_change_from_open,
            "intraday_realized_estimate": None,
            "fill_count": len(activities),
            "symbols_with_broker_activity": symbols_with_broker_activity,
            "broker_open_symbols": broker_open_symbols,
            "broker_closed_symbols": broker_closed_symbols,
            "carryover_symbols": sorted(set(carryover_symbols)),
            "carryover_fragment_symbols": sorted(set(carryover_fragment_symbols)),
            "intraday_symbols": sorted(set(intraday_symbols)),
            "broker_history_available": bool(timestamps and equities),
            "broker_positions": {
                str(p.get("symbol", "") or "").upper(): {
                    "qty": float(p.get("qty", 0) or 0),
                    "side": str(p.get("side", "") or "").lower(),
                    "avg_entry_price": float(p.get("avg_entry_price", 0) or 0),
                    "market_value": float(p.get("market_value", 0) or 0),
                }
                for p in positions
                if str(p.get("symbol", "") or "").strip()
            },
        }

    def get_internal_analytics(self, trade_date: Optional[str] = None) -> Dict:
        pnl_state = persistence.load_pnl_state()
        analytics = trade_history.get_analytics()
        history = trade_history.load_all()
        target_day = trade_date or time.strftime("%Y-%m-%d")
        today_history = [
            t for t in history
            if self._trade_day_key_from_trade(t) == target_day
        ]
        today_realized = round(sum(float(t.get("pnl", 0) or 0) for t in today_history), 2)
        today_trade_count = len(today_history)
        today_wins = len([t for t in today_history if float(t.get("pnl", 0) or 0) > 0])
        today_win_rate_pct = round(today_wins / max(1, today_trade_count) * 100.0, 2) if today_trade_count else 0.0
        today_symbols = sorted({
            str(t.get("symbol", "") or "").upper()
            for t in today_history
            if str(t.get("symbol", "") or "").strip()
        })
        game_film = self._load_json(DATA_DIR / "game_film.json")
        internal_positions = getattr(self.entry_manager, "positions", {}) if self.entry_manager else {}
        return {
            "pnl_state_realized": round(float(pnl_state.get("total_realized_pnl", 0) or 0), 2),
            "pnl_state_today_realized": round(float(pnl_state.get("today_realized_pnl", 0) or 0), 2),
            "pnl_state_trade_count": int(pnl_state.get("total_trades", 0) or 0),
            "trade_history_realized": today_realized,
            "trade_history_trade_count": today_trade_count,
            "trade_history_win_rate_pct": today_win_rate_pct,
            "game_film_realized": round(float(game_film.get("total_pnl", 0) or 0), 2) if isinstance(game_film, dict) else 0.0,
            "game_film_trade_count": int(game_film.get("total_trades", 0) or 0) if isinstance(game_film, dict) else 0,
            "game_film_win_rate_pct": round(float(game_film.get("overall_win_rate_pct", 0) or 0), 2) if isinstance(game_film, dict) else 0.0,
            "symbols_in_trade_history": today_symbols,
            "symbols_in_game_film": sorted((game_film.get("by_symbol", {}) or {}).keys()) if isinstance(game_film, dict) else [],
            "analytics_total_realized_all_time": round(float(analytics.get("total_pnl", 0) or 0), 2),
            "internal_live_positions": {
                str(symbol).upper(): {
                    "qty": float(pos.get("quantity", 0) or 0),
                    "side": str(pos.get("side", "") or "").lower(),
                    "entry_price": float(pos.get("entry_price", 0) or 0),
                }
                for symbol, pos in (internal_positions or {}).items()
            },
        }

    def classify_mismatch(self, broker: Dict, internal: Dict) -> Dict:
        equity = float(broker.get("equity", 0) or 0)
        broker_day_pnl = float(broker.get("day_pnl", 0) or 0)
        pnl_state_realized = float(internal.get("pnl_state_today_realized", 0) or 0)
        trade_history_realized = float(internal.get("trade_history_realized", 0) or 0)
        overnight_gap = float(broker.get("overnight_gap_pnl", 0) or 0)
        current_open_unrealized = float(broker.get("current_open_unrealized", 0) or 0)
        broker_closed_trade_estimate = round(broker_day_pnl - overnight_gap - current_open_unrealized, 2)
        diff_pnl_state = round(broker_closed_trade_estimate - pnl_state_realized, 2)
        diff_trade_history = round(broker_closed_trade_estimate - trade_history_realized, 2)
        effective_diff = max(abs(diff_pnl_state), abs(diff_trade_history))

        reasons: List[str] = []
        if broker.get("overnight_gap_pnl") is not None and abs(float(broker.get("overnight_gap_pnl") or 0)) > 25:
            reasons.append("carryover_gap")
        if abs(pnl_state_realized - trade_history_realized) > 10:
            reasons.append("internal_ledgers_diverge")
        if broker.get("broker_closed_symbols"):
            missing = sorted(set(broker.get("broker_closed_symbols") or []) - set(internal.get("symbols_in_trade_history") or []))
            if missing:
                reasons.append("broker_symbols_missing_from_internal")
            internal_missing = sorted(set(internal.get("symbols_in_trade_history") or []) - set(broker.get("symbols_with_broker_activity") or []))
            if internal_missing:
                reasons.append("internal_symbols_missing_from_broker_day_bundle")
        if broker.get("carryover_fragment_symbols"):
            reasons.append("residual_position_drift")
        if effective_diff > 10:
            reasons.append("internal_closed_trade_subset_only")
        if not broker.get("broker_history_available"):
            reasons.append("broker_history_unavailable")

        status = "healthy"
        severity = "healthy"
        threshold = max(25.0, 0.005 * equity) if equity > 0 else 25.0
        if not broker.get("broker_history_available"):
            status = "minor_mismatch"
            severity = "warning"
        elif effective_diff > threshold:
            status = "critical_mismatch"
            severity = "critical"
            reasons.append("broker_truth_canary_triggered")
        elif effective_diff > 5:
            status = "minor_mismatch"
            severity = "warning"
        elif reasons:
            status = "minor_mismatch"
            severity = "warning"

        return {
            "broker_vs_pnl_state_diff": diff_pnl_state,
            "broker_vs_trade_history_diff": diff_trade_history,
            "broker_closed_trade_estimate": broker_closed_trade_estimate,
            "status": status,
            "severity": severity,
            "reasons": sorted(set(reasons)),
        }

    def build_canaries(self, broker: Dict, internal: Dict, reconciliation: Dict, previous: Dict) -> List[Dict]:
        previous_canaries = {
            f"{c.get('code','')}::{c.get('symbol','')}": c
            for c in (previous.get("canaries", []) or [])
            if isinstance(c, dict)
        }
        canaries: List[Dict] = []
        now_ts = time.time()

        def add_canary(code: str, severity: str, magnitude: float = 0.0, symbol: str = "", recommended_action: str = ""):
            key = f"{code}::{symbol}"
            prior = previous_canaries.get(key, {})
            canaries.append(
                {
                    "code": code,
                    "symbol": symbol or None,
                    "severity": severity,
                    "first_seen": float(prior.get("first_seen", now_ts) or now_ts),
                    "current_magnitude": round(float(magnitude or 0.0), 4),
                    "recommended_action": recommended_action,
                }
            )

        broker_positions = broker.get("broker_positions", {}) or {}
        internal_positions = internal.get("internal_live_positions", {}) or {}
        broker_symbols = set(broker_positions.keys())
        internal_symbols = set(internal_positions.keys())

        for symbol in sorted(broker_symbols - internal_symbols):
            add_canary(
                "broker_position_missing_internal",
                "critical",
                symbol=symbol,
                recommended_action="Sync live positions from broker before trusting internal exposure.",
            )
        for symbol in sorted(internal_symbols - broker_symbols):
            add_canary(
                "internal_position_missing_broker",
                "critical",
                symbol=symbol,
                recommended_action="Drop or repair the orphaned internal position state.",
            )
        for symbol in sorted(broker_symbols & internal_symbols):
            broker_qty = float((broker_positions.get(symbol, {}) or {}).get("qty", 0) or 0)
            internal_qty = float((internal_positions.get(symbol, {}) or {}).get("qty", 0) or 0)
            if abs(broker_qty - internal_qty) > 0.001:
                add_canary(
                    "position_qty_mismatch",
                    "critical",
                    magnitude=abs(broker_qty - internal_qty),
                    symbol=symbol,
                    recommended_action="Use broker quantity as canonical and repair internal position sizing.",
                )

        pnl_gap = max(
            abs(float(reconciliation.get("broker_vs_pnl_state_diff", 0) or 0)),
            abs(float(reconciliation.get("broker_vs_trade_history_diff", 0) or 0)),
        )
        if pnl_gap > 5:
            severity = "critical" if reconciliation.get("status") == "critical_mismatch" else "warning"
            add_canary(
                "realized_pnl_mismatch",
                severity,
                magnitude=pnl_gap,
                recommended_action="Rebuild internal closed-trade accounting from Alpaca fills/orders.",
            )

        for symbol in sorted(set(broker.get("broker_closed_symbols", []) or []) - set(internal.get("symbols_in_trade_history", []) or [])):
            add_canary(
                "broker_activity_missing_internal_history",
                "critical",
                symbol=symbol,
                recommended_action="Backfill the missing broker close into internal trade history.",
            )

        if broker.get("overnight_gap_pnl") is not None and abs(float(broker.get("overnight_gap_pnl", 0) or 0)) > 25:
            add_canary(
                "overnight_carryover_gap",
                "warning",
                magnitude=float(broker.get("overnight_gap_pnl", 0) or 0),
                recommended_action="Split prior-session carry from same-day realized performance.",
            )

        if broker.get("carryover_fragment_symbols"):
            add_canary(
                "residual_position_drift",
                "warning",
                magnitude=len(broker.get("carryover_fragment_symbols", []) or []),
                recommended_action="Flatten or explicitly classify broker residual fragments.",
            )

        return canaries

    @staticmethod
    def build_trust_flags(reconciliation: Dict) -> Dict:
        status = reconciliation.get("status", "minor_mismatch")
        broker_only = status == "critical_mismatch"
        degraded = status != "healthy"
        return {
            "topline_source": "broker",
            "positions_source": "broker",
            "exposure_source": "broker",
            "internal_analytics_trusted": not degraded,
            "internal_analytics_degraded": degraded,
            "broker_only_mode": broker_only,
            "show_internal_stats": not broker_only,
            "dim_internal_stats": degraded and not broker_only,
            "allow_closed_trade_analytics": not broker_only,
            "allow_ai_summaries": not broker_only,
        }

    @staticmethod
    def _trade_day_key(ts: float) -> str:
        from datetime import datetime
        try:
            import zoneinfo
            et = zoneinfo.ZoneInfo("US/Eastern")
        except Exception:
            from pytz import timezone as tz
            et = tz("US/Eastern")
        return datetime.fromtimestamp(ts, et).strftime("%Y-%m-%d")

    @staticmethod
    def _load_json(path: Path):
        try:
            if path.exists():
                return json.loads(path.read_text())
        except Exception:
            return {}
        return {}

    @classmethod
    def _trade_day_key_from_trade(cls, trade: Dict) -> str:
        ts = float(trade.get("exit_time", trade.get("recorded_at", 0)) or 0)
        if ts <= 0:
            return ""
        return cls._trade_day_key(ts)

    @staticmethod
    def _save(payload: Dict):
        try:
            DATA_DIR.mkdir(exist_ok=True)
            RECONCILIATION_FILE.write_text(json.dumps(payload, indent=2, default=str))
        except Exception:
            pass
