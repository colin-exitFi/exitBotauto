"""Broker-vs-internal reconciliation helpers."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

from loguru import logger

from config import settings
from src import persistence
from src.ai import trade_history
from src.data.trading_calendar import trading_session_day
from src.data.trade_schema import normalize_trade_record
from src.data.strategy_tags import normalize_strategy_tag


DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
RECONCILIATION_FILE = DATA_DIR / "reconciliation_state.json"


class Reconciler:
    def __init__(self, alpaca_client, entry_manager=None, options_engine=None):
        self.alpaca_client = alpaca_client
        self.entry_manager = entry_manager
        self.options_engine = options_engine
        self._last_warning_sig = ""
        self._last_warning_ts = 0.0
        self._recent_backfilled_trade_keys = set()

    @staticmethod
    def _looks_like_option_symbol(symbol: str) -> bool:
        return bool(re.match(r"^[A-Z]+\d{6}[CP]\d{8}$", str(symbol or "").upper().strip()))

    def snapshot(self, trade_date: Optional[str] = None) -> Dict:
        previous = persistence.load_reconciliation_state() or {}
        broker = self.get_broker_truth(trade_date=trade_date)
        self._backfill_broker_reconstructed_trades(broker)
        self._sync_internal_positions_with_broker(broker)
        self._cleanup_stale_pending_positions(broker)
        internal = self.get_internal_analytics(trade_date=trade_date or broker.get("date"), broker=broker)
        reconciliation = self.classify_mismatch(broker, internal)
        if self._repair_pnl_state_to_canonical_realized(broker, internal, reconciliation):
            internal = self.get_internal_analytics(trade_date=trade_date or broker.get("date"), broker=broker)
            reconciliation = self.classify_mismatch(broker, internal)
        canaries = self.build_canaries(broker, internal, reconciliation, previous)
        broker_api = self._broker_api_health()
        prev_meta = previous.get("meta", {}) if isinstance(previous, dict) else {}
        prev_consecutive_critical = int(prev_meta.get("consecutive_critical_mismatch", 0) or 0)
        consecutive_critical = prev_consecutive_critical + 1 if reconciliation.get("status") == "critical_mismatch" else 0
        threshold_critical = max(1, int(getattr(settings, "RECON_CRITICAL_CONSECUTIVE_ENTRY_BLOCK", 3) or 3))
        threshold_429 = max(1, int(getattr(settings, "RECON_BROKER_429_TRIPWIRE_5M", 8) or 8))
        entry_pause_due_to_critical = consecutive_critical >= threshold_critical
        entry_pause_due_to_429 = int(broker_api.get("recent_429_total", 0) or 0) >= threshold_429
        trust = self.build_trust_flags(
            reconciliation=reconciliation,
            consecutive_critical_mismatch=consecutive_critical,
            entry_pause_due_to_critical=entry_pause_due_to_critical,
            entry_pause_due_to_429=entry_pause_due_to_429,
            broker_api=broker_api,
        )
        payload = {
            "as_of": time.time(),
            "date": trade_date or broker.get("date") or trading_session_day(),
            "broker": broker,
            "internal": internal,
            "reconciliation": reconciliation,
            "canaries": canaries,
            "trust": trust,
            "broker_api": broker_api,
            "meta": {
                "consecutive_critical_mismatch": consecutive_critical,
                "entry_pause_due_to_critical": entry_pause_due_to_critical,
                "entry_pause_due_to_429": entry_pause_due_to_429,
                "critical_tripwire_threshold": threshold_critical,
                "broker_429_tripwire_threshold": threshold_429,
            },
        }
        persistence.save_reconciliation_state(payload)
        if reconciliation.get("status") != "healthy":
            warning_sig = "|".join(
                [
                    str(reconciliation.get("status", "")),
                    str(reconciliation.get("broker_vs_pnl_state_diff", "")),
                    str(reconciliation.get("broker_vs_trade_history_diff", "")),
                    ",".join(sorted(reconciliation.get("reasons", []) or [])),
                    ",".join(sorted(c.get("code", "") for c in canaries)),
                ]
            )
            now_ts = float(time.time())
            should_log = (
                warning_sig != self._last_warning_sig
                or (now_ts - float(self._last_warning_ts or 0.0)) >= 60.0
            )
            self._last_warning_sig = warning_sig
            self._last_warning_ts = now_ts
            if not should_log:
                return payload
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

    def _repair_pnl_state_to_canonical_realized(self, broker: Dict, internal: Dict, reconciliation: Dict) -> bool:
        if not broker.get("broker_history_available"):
            return False
        canonical_source = str(reconciliation.get("canonical_realized_source", "") or "")
        if not canonical_source.startswith("broker_day_estimate"):
            return False
        target_today_realized = round(float(reconciliation.get("canonical_realized_pnl", 0) or 0), 2)
        current_today_realized = round(float(internal.get("pnl_state_today_realized", 0) or 0), 2)
        if abs(target_today_realized - current_today_realized) <= 1.0:
            return False

        pnl_state = persistence.load_pnl_state() or {}
        live_today_realized = round(float(pnl_state.get("today_realized_pnl", current_today_realized) or 0), 2)
        delta = round(target_today_realized - live_today_realized, 2)
        if abs(delta) <= 1.0:
            return False

        live_total_realized = round(
            float(pnl_state.get("total_realized_pnl", internal.get("pnl_state_realized", 0)) or 0),
            2,
        )
        pnl_state["today_realized_pnl"] = target_today_realized
        pnl_state["total_realized_pnl"] = round(live_total_realized + delta, 2)
        try:
            persistence.save_pnl_state(pnl_state)
            logger.info(
                f"🧮 Reconciler anchored pnl_state today_realized -> {target_today_realized:.2f} "
                f"from {canonical_source} (delta={delta:+.2f})"
            )
            return True
        except Exception:
            return False

    def _backfill_broker_reconstructed_trades(self, broker: Dict):
        broker_fill_ledger = (broker or {}).get("broker_fill_ledger", {}) if isinstance(broker, dict) else {}
        trades = list(broker_fill_ledger.get("trades", []) or [])
        self._recent_backfilled_trade_keys = set()
        if not trades:
            return
        existing = trade_history.load_all()
        existing_keys = set()
        for t in existing:
            existing_keys.add(
                (
                    str(t.get("symbol", "")).upper(),
                    round(float(t.get("exit_time", 0) or 0), 3),
                    round(float(t.get("quantity", 0) or 0), 6),
                    round(float(t.get("pnl", 0) or 0), 2),
                    str(t.get("reason", "") or ""),
                )
            )
        added = 0
        skipped_duplicates = 0
        for t in trades:
            key = (
                str(t.get("symbol", "")).upper(),
                round(float(t.get("exit_time", 0) or 0), 3),
                round(float(t.get("quantity", 0) or 0), 6),
                round(float(t.get("pnl", 0) or 0), 2),
                str(t.get("reason", "") or ""),
            )
            if key in existing_keys:
                continue
            if self._find_recent_history_trade(existing, t, window_seconds=30.0):
                skipped_duplicates += 1
                continue
            if str(t.get("reason", "") or "").lower() == "broker_fill_reconstructed" and self._find_recent_history_trade(
                existing,
                t,
                window_seconds=60.0,
                reason_prefixes=("ratchet", "hard_stop"),
            ):
                skipped_duplicates += 1
                continue
            trade_history.record_trade(t)
            existing_keys.add(key)
            existing.append(t)
            self._recent_backfilled_trade_keys.add(key)
            added += 1
        if added > 0:
            logger.info(f"🧾 Backfilled {added} reconstructed broker trade(s) into history")
        if skipped_duplicates > 0:
            logger.info(f"🧾 Skipped {skipped_duplicates} duplicate reconstructed broker trade(s)")

    @staticmethod
    def _is_partial_trade(trade: Dict) -> bool:
        reason = str((trade or {}).get("reason", "") or "").lower()
        exit_scope = str((trade or {}).get("exit_scope", "") or "").lower()
        if exit_scope == "partial":
            return True
        return reason.endswith("_1") or reason.startswith("partial_")

    @staticmethod
    def _trade_identity_key(trade: Dict) -> tuple:
        return (
            str((trade or {}).get("symbol", "") or "").upper(),
            round(float((trade or {}).get("exit_time", 0) or 0), 3),
            round(float((trade or {}).get("quantity", 0) or 0), 6),
            round(float((trade or {}).get("pnl", 0) or 0), 2),
            str((trade or {}).get("reason", "") or ""),
        )

    def _find_recent_history_trade(
        self,
        history: List[Dict],
        trade: Dict,
        window_seconds: float = 30.0,
        reason_prefixes: Optional[tuple] = None,
    ) -> Optional[Dict]:
        symbol = str((trade or {}).get("symbol", "") or "").upper()
        asset_type = str((trade or {}).get("asset_type", "equity") or "equity").lower()
        trade_order_id = str(
            (trade or {}).get("exit_order_id")
            or (trade or {}).get("order_id")
            or ""
        ).strip()
        try:
            exit_time = float((trade or {}).get("exit_time", 0) or 0)
        except Exception:
            exit_time = 0.0
        if not symbol or exit_time <= 0 or self._is_partial_trade(trade):
            return None
        prefixes = tuple(str(prefix or "").lower() for prefix in (reason_prefixes or ()) if str(prefix or "").strip())
        for existing in reversed(history or []):
            if self._is_partial_trade(existing):
                continue
            if str(existing.get("asset_type", "equity") or "equity").lower() != asset_type:
                continue
            if str(existing.get("symbol", "") or "").upper() != symbol:
                continue
            existing_order_id = str(
                existing.get("exit_order_id")
                or existing.get("order_id")
                or ""
            ).strip()
            if trade_order_id and existing_order_id and trade_order_id == existing_order_id:
                return existing
            existing_reason = str(existing.get("reason", "") or "").lower()
            if prefixes and not any(existing_reason.startswith(prefix) for prefix in prefixes):
                continue
            try:
                existing_exit_time = float(existing.get("exit_time", 0) or 0)
            except Exception:
                continue
            if existing_exit_time <= 0:
                continue
            if abs(existing_exit_time - exit_time) <= float(window_seconds or 0.0):
                return existing
        return None

    def _lookup_position_metadata(self, symbol: str) -> Dict:
        symbol_key = str(symbol or "").upper()
        if not symbol_key or not self.entry_manager:
            return {}
        positions = getattr(self.entry_manager, "positions", {}) or {}
        current = positions.get(symbol_key) or positions.get(symbol) or {}
        if current:
            return dict(current)
        recent_removed = (getattr(self.entry_manager, "_recently_removed_positions", {}) or {}).get(symbol_key) or {}
        recent_wrapper = {}
        if isinstance(recent_removed, dict):
            recent_wrapper = {
                "last_exit_reason": recent_removed.get("last_exit_reason", ""),
                "exit_order_id": recent_removed.get("exit_order_id", ""),
                "removed_at": recent_removed.get("removed_at", 0),
                "reloaded_from_broker": True,
            }
        getter = getattr(self.entry_manager, "get_recently_removed_position", None)
        if callable(getter):
            recent = getter(symbol_key) or {}
            if recent:
                merged = dict(recent)
                for key, value in recent_wrapper.items():
                    if value not in ("", None, 0):
                        merged.setdefault(key, value)
                return merged
        recent_snapshot = recent_removed.get("position", {}) if isinstance(recent_removed, dict) else {}
        merged = dict(recent_snapshot or {})
        for key, value in recent_wrapper.items():
            if value not in ("", None, 0):
                merged.setdefault(key, value)
        return merged

    def _sync_internal_positions_with_broker(self, broker: Dict):
        if not self.entry_manager or not hasattr(self.entry_manager, "sync_positions_from_brokerage"):
            return
        broker_positions = (broker or {}).get("broker_positions", {}) or {}
        if not broker_positions:
            return
        normalized = []
        for symbol, pos in broker_positions.items():
            normalized_symbol = str(symbol or "").upper()
            if self._looks_like_option_symbol(normalized_symbol):
                continue
            normalized.append(
                {
                    "symbol": normalized_symbol,
                    "quantity": float((pos or {}).get("qty", 0) or 0),
                    "side": str((pos or {}).get("side", "") or "long").lower(),
                    "average_price": float((pos or {}).get("avg_entry_price", 0) or 0),
                    "current_price": float((pos or {}).get("avg_entry_price", 0) or 0),
                }
            )
        try:
            updates = int(self.entry_manager.sync_positions_from_brokerage(normalized) or 0)
            if updates > 0:
                logger.info(f"🔧 Reconciler synced {updates} live position(s) from broker truth")
        except Exception:
            pass

    def _broker_api_health(self) -> Dict:
        if not self.alpaca_client:
            return {"window_seconds": 300, "recent_429_total": 0, "endpoints": {}}
        getter = getattr(self.alpaca_client, "get_reliability_snapshot", None)
        if not callable(getter):
            return {"window_seconds": 300, "recent_429_total": 0, "endpoints": {}}
        try:
            snap = getter() or {}
            if not isinstance(snap, dict):
                return {"window_seconds": 300, "recent_429_total": 0, "endpoints": {}}
            return snap
        except Exception:
            return {"window_seconds": 300, "recent_429_total": 0, "endpoints": {}}

    def _cleanup_stale_pending_positions(self, broker: Dict):
        """
        Remove local ghost positions that never materialized at broker.
        We only purge long-lived `pending_new` states absent from broker symbols.
        """
        if not self.entry_manager:
            return
        positions = getattr(self.entry_manager, "positions", {}) or {}
        if not positions:
            return
        broker_symbols = set((broker or {}).get("broker_positions", {}).keys())
        now_ts = time.time()
        stale_threshold_s = 30 * 60

        for symbol, pos in list(positions.items()):
            status = str(pos.get("order_status", "") or "").lower()
            if status != "pending_new":
                continue
            if symbol in broker_symbols:
                continue
            entry_time = float(pos.get("entry_time", 0) or 0)
            age_s = (now_ts - entry_time) if entry_time > 0 else stale_threshold_s + 1
            if age_s < stale_threshold_s:
                continue
            logger.warning(
                f"🧹 Reconciler purging stale pending_new ghost: {symbol} "
                f"(age={int(age_s/60)}m, absent from broker positions)"
            )
            try:
                self.entry_manager.remove_position(symbol)
            except Exception:
                # Fallback: direct pop if remove helper is unavailable.
                positions.pop(symbol, None)

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
        current_open_unrealized = round(
            sum(
                float(
                    p.get(
                        "unrealized_pl",
                        p.get("unrealized_pnl", p.get("open_pnl", 0)),
                    )
                    or 0
                )
                for p in positions
            ),
            2,
        )
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
        today_key = trade_date or trading_session_day()
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
            trade_date = today_key
        broker_fill_ledger = self._build_broker_fill_ledger(
            trade_date=trade_date,
            activities=activities,
            end_positions=positions,
        )

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
            "broker_balance_asof": str(account.get("balance_asof") or ""),
            "broker_fill_ledger": broker_fill_ledger,
            "broker_positions": {
                str(p.get("symbol", "") or "").upper(): {
                    "qty": float(p.get("qty", p.get("quantity", 0)) or 0),
                    "side": str(p.get("side", "") or "").lower(),
                    "avg_entry_price": float(p.get("avg_entry_price", p.get("average_price", 0)) or 0),
                    "market_value": float(p.get("market_value", 0) or 0),
                }
                for p in positions
                if str(p.get("symbol", "") or "").strip()
                and not self._looks_like_option_symbol(str(p.get("symbol", "") or ""))
            },
        }

    def get_internal_analytics(self, trade_date: Optional[str] = None, broker: Optional[Dict] = None) -> Dict:
        pnl_state = persistence.load_pnl_state()
        analytics = trade_history.get_analytics()
        history = trade_history.load_all()
        target_day = trade_date or trading_session_day()
        today_history = [
            t for t in history
            if self._trade_day_key_from_trade(t) == target_day
        ]
        today_history_keys = {
            self._trade_identity_key(t)
            for t in today_history
        }
        today_history_keys |= set(getattr(self, "_recent_backfilled_trade_keys", set()) or set())
        broker_fill_ledger = (broker or {}).get("broker_fill_ledger", {}) if isinstance(broker, dict) else {}
        unresolved_broker_symbols = list(broker_fill_ledger.get("unresolved_symbols", []) or [])
        broker_day_pnl = float((broker or {}).get("day_pnl", 0) or 0)
        overnight_gap = float((broker or {}).get("overnight_gap_pnl", 0) or 0)
        current_open_unrealized = float((broker or {}).get("current_open_unrealized", 0) or 0)
        broker_closed_trade_estimate = round(broker_day_pnl - overnight_gap - current_open_unrealized, 2)
        broker_reconstructed_realized = round(float(broker_fill_ledger.get("realized_pnl", 0) or 0), 2)
        broker_fill_ledger_complete = int(broker_fill_ledger.get("trade_count", 0) or 0) > 0 and not unresolved_broker_symbols
        broker_fill_ledger_aligns = abs(broker_reconstructed_realized - broker_closed_trade_estimate) <= max(
            5.0,
            abs(broker_closed_trade_estimate) * 0.05,
        )
        allow_broker_supplementals = bool((broker or {}).get("broker_history_available", False)) and broker_fill_ledger_complete and broker_fill_ledger_aligns
        supplemental_trades = []
        if allow_broker_supplementals:
            for trade in (broker_fill_ledger.get("trades", []) or []):
                normalized_trade = normalize_trade_record(trade)
                if self._trade_identity_key(normalized_trade) in today_history_keys:
                    continue
                # Broker supplemental fills are only meant to patch missing internal history,
                # not double-count exits we already captured through the live exit paths.
                if self._find_recent_history_trade(today_history, normalized_trade, window_seconds=45.0):
                    continue
                supplemental_trades.append(normalized_trade)
        effective_today_trades = []
        seen_trade_keys = set()
        for trade in list(today_history) + supplemental_trades:
            trade_key = self._trade_identity_key(trade)
            if trade_key in seen_trade_keys:
                continue
            seen_trade_keys.add(trade_key)
            effective_today_trades.append(trade)
        today_realized = round(sum(float(t.get("pnl", 0) or 0) for t in effective_today_trades), 2)
        today_trade_count = len(effective_today_trades)
        today_wins = len([t for t in effective_today_trades if float(t.get("pnl", 0) or 0) > 0])
        today_win_rate_pct = round(today_wins / max(1, today_trade_count) * 100.0, 2) if today_trade_count else 0.0
        pnl_state_today_realized = round(float(pnl_state.get("today_realized_pnl", 0) or 0), 2)
        pnl_state_total_realized = round(float(pnl_state.get("total_realized_pnl", 0) or 0), 2)
        pnl_state_repaired = False
        # Only auto-repair from internal/history-derived trades when broker history
        # is unavailable, or when the broker fill ledger is both complete and aligned
        # with broker day truth. Otherwise internal/supplemental trades can drag
        # pnl_state away from the brokerage source of truth.
        allow_internal_repair = False
        if today_trade_count > 0:
            if not broker or not bool((broker or {}).get("broker_history_available", False)):
                allow_internal_repair = True
            elif broker_fill_ledger_complete and broker_fill_ledger_aligns:
                allow_internal_repair = True
        if allow_internal_repair and abs(pnl_state_today_realized - today_realized) > 1.0:
            delta = round(today_realized - pnl_state_today_realized, 2)
            pnl_state["today_realized_pnl"] = today_realized
            pnl_state["total_realized_pnl"] = round(pnl_state_total_realized + delta, 2)
            try:
                persistence.save_pnl_state(pnl_state)
                pnl_state_today_realized = round(float(pnl_state.get("today_realized_pnl", 0) or 0), 2)
                pnl_state_total_realized = round(float(pnl_state.get("total_realized_pnl", 0) or 0), 2)
                pnl_state_repaired = True
                logger.info(
                    f"🧮 Reconciler repaired pnl_state today_realized -> {today_realized:.2f} "
                    f"(delta={delta:+.2f})"
                )
            except Exception:
                pass
        effective_today_symbols = sorted({
            str(t.get("symbol", "") or "").upper()
            for t in effective_today_trades
            if str(t.get("symbol", "") or "").strip()
        })
        game_film = self._load_json(DATA_DIR / "game_film.json")
        internal_positions = getattr(self.entry_manager, "positions", {}) if self.entry_manager else {}
        return {
            "pnl_state_realized": pnl_state_total_realized,
            "pnl_state_today_realized": pnl_state_today_realized,
            "pnl_state_trade_count": int(pnl_state.get("total_trades", 0) or 0),
            "trade_history_realized": today_realized,
            "trade_history_trade_count": today_trade_count,
            "trade_history_win_rate_pct": today_win_rate_pct,
            "game_film_realized": round(float(game_film.get("total_pnl", 0) or 0), 2) if isinstance(game_film, dict) else 0.0,
            "game_film_trade_count": int(game_film.get("total_trades", 0) or 0) if isinstance(game_film, dict) else 0,
            "game_film_win_rate_pct": round(float(game_film.get("overall_win_rate_pct", 0) or 0), 2) if isinstance(game_film, dict) else 0.0,
            "symbols_in_trade_history": effective_today_symbols,
            "symbols_in_game_film": sorted((game_film.get("by_symbol", {}) or {}).keys()) if isinstance(game_film, dict) else [],
            "analytics_total_realized_all_time": round(
                float(analytics.get("raw_total_pnl", analytics.get("total_pnl", 0)) or 0),
                2,
            ),
            "broker_reconstructed_realized": round(float(broker_fill_ledger.get("realized_pnl", 0) or 0), 2),
            "broker_reconstructed_trade_count": int(broker_fill_ledger.get("trade_count", 0) or 0),
            "broker_reconstructed_unresolved_symbols": list(broker_fill_ledger.get("unresolved_symbols", []) or []),
            "broker_supplemental_trade_count": len(supplemental_trades),
            "pnl_state_repaired": pnl_state_repaired,
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
        broker_reconstructed_realized = float(internal.get("broker_reconstructed_realized", 0) or 0)
        broker_reconstructed_trade_count = int(internal.get("broker_reconstructed_trade_count", 0) or 0)
        unresolved_count = len(internal.get("broker_reconstructed_unresolved_symbols", []) or [])
        broker_supplemental_trade_count = int(internal.get("broker_supplemental_trade_count", 0) or 0)
        overnight_gap = float(broker.get("overnight_gap_pnl", 0) or 0)
        current_open_unrealized = float(broker.get("current_open_unrealized", 0) or 0)
        broker_closed_trade_estimate = round(broker_day_pnl - overnight_gap - current_open_unrealized, 2)
        canonical_realized = broker_closed_trade_estimate
        canonical_realized_source = "broker_day_estimate"
        broker_fill_ledger_complete = broker_reconstructed_trade_count > 0 and unresolved_count == 0
        broker_fill_ledger_aligns = abs(broker_reconstructed_realized - broker_closed_trade_estimate) <= max(5.0, abs(broker_closed_trade_estimate) * 0.05)
        if broker_fill_ledger_complete and broker_fill_ledger_aligns:
            canonical_realized = broker_reconstructed_realized
            canonical_realized_source = "broker_reconstructed"
        elif broker_fill_ledger_complete:
            canonical_realized_source = "broker_day_estimate_fill_ledger_mismatch"
        elif broker_reconstructed_trade_count > 0:
            canonical_realized_source = "broker_day_estimate_partial_fill_ledger"
        diff_pnl_state = round(canonical_realized - pnl_state_realized, 2)
        diff_trade_history = round(canonical_realized - trade_history_realized, 2)
        effective_diff = max(abs(diff_pnl_state), abs(diff_trade_history))
        if broker_supplemental_trade_count > 0 and abs(diff_trade_history) <= 5:
            effective_diff = abs(diff_trade_history)

        reasons: List[str] = []
        if broker.get("overnight_gap_pnl") is not None and abs(float(broker.get("overnight_gap_pnl") or 0)) > 25:
            reasons.append("carryover_gap")
        if abs(pnl_state_realized - trade_history_realized) > 10 and not (
            broker_supplemental_trade_count > 0 and abs(diff_trade_history) <= 5
        ):
            reasons.append("internal_ledgers_diverge")
        unresolved = set(internal.get("broker_reconstructed_unresolved_symbols") or [])
        if broker.get("broker_closed_symbols"):
            missing = sorted(
                set(broker.get("broker_closed_symbols") or [])
                - set(internal.get("symbols_in_trade_history") or [])
                - unresolved
            )
            if missing:
                reasons.append("broker_symbols_missing_from_internal")
            internal_missing = sorted(set(internal.get("symbols_in_trade_history") or []) - set(broker.get("symbols_with_broker_activity") or []))
            if internal_missing:
                reasons.append("internal_symbols_missing_from_broker_day_bundle")
        if broker.get("carryover_fragment_symbols"):
            reasons.append("residual_position_drift")
        if internal.get("broker_reconstructed_unresolved_symbols"):
            reasons.append("broker_fill_ledger_unresolved")
        if broker_fill_ledger_complete and not broker_fill_ledger_aligns:
            reasons.append("broker_fill_ledger_mismatch")
        if effective_diff > 10 and unresolved_count == 0:
            reasons.append("internal_closed_trade_subset_only")
        if not broker.get("broker_history_available"):
            reasons.append("broker_history_unavailable")

        status = "healthy"
        severity = "healthy"
        exposure_mismatch = self._has_live_exposure_mismatch(broker, internal)
        threshold = max(25.0, 0.005 * equity) if equity > 0 else 25.0
        if not broker.get("broker_history_available"):
            status = "minor_mismatch"
            severity = "warning"
        elif effective_diff > threshold:
            if exposure_mismatch:
                status = "critical_mismatch"
                severity = "critical"
                reasons.append("broker_truth_canary_triggered")
            else:
                # Large accounting divergence without live exposure drift should degrade
                # analytics but not force permanent hard-stop behavior.
                status = "minor_mismatch"
                severity = "warning"
                reasons.append("ledger_mismatch_no_live_exposure")
        elif effective_diff > 5:
            status = "minor_mismatch"
            severity = "warning"
        elif reasons:
            status = "minor_mismatch"
            severity = "warning"

        # If only benign carryover artifacts remain and realized ledgers align,
        # treat as healthy runtime state (with advisory reasons still attached).
        benign_reasons = {
            "residual_position_drift",
            "broker_fill_ledger_unresolved",
            "carryover_gap",
        }
        if (
            not exposure_mismatch
            and abs(diff_pnl_state) <= 1.0
            and abs(diff_trade_history) <= 1.0
            and reasons
            and set(reasons).issubset(benign_reasons)
        ):
            status = "healthy"
            severity = "healthy"

        return {
            "broker_vs_pnl_state_diff": diff_pnl_state,
            "broker_vs_trade_history_diff": diff_trade_history,
            "broker_closed_trade_estimate": broker_closed_trade_estimate,
            "canonical_realized_source": canonical_realized_source,
            "canonical_realized_pnl": round(canonical_realized, 2),
            "status": status,
            "severity": severity,
            "reasons": sorted(set(reasons)),
        }

    @staticmethod
    def _has_live_exposure_mismatch(broker: Dict, internal: Dict) -> bool:
        broker_positions = (broker or {}).get("broker_positions", {}) or {}
        internal_positions = (internal or {}).get("internal_live_positions", {}) or {}
        broker_symbols = set(str(s or "").upper() for s in broker_positions.keys())
        internal_symbols = set(str(s or "").upper() for s in internal_positions.keys())
        if broker_symbols != internal_symbols:
            return True
        for symbol in broker_symbols:
            b_qty = float((broker_positions.get(symbol, {}) or {}).get("qty", 0) or 0)
            i_qty = float((internal_positions.get(symbol, {}) or {}).get("qty", 0) or 0)
            if abs(b_qty - i_qty) > 0.001:
                return True
        return False

    def _build_broker_fill_ledger(self, trade_date: str, activities: List[Dict], end_positions: List[Dict]) -> Dict:
        grouped: Dict[str, List[Dict]] = {}
        end_signed_qty: Dict[str, float] = {}
        trusted_history = trade_history.load_all()
        for pos in end_positions or []:
            symbol = str(pos.get("symbol", "") or "").upper().strip()
            if not symbol:
                continue
            qty = float(pos.get("qty", pos.get("quantity", 0)) or 0)
            end_signed_qty[symbol] = qty
        for row in activities or []:
            symbol = str(row.get("symbol", "") or "").upper().strip()
            if not symbol:
                continue
            grouped.setdefault(symbol, []).append(dict(row))

        closed_trades: List[Dict] = []
        unresolved_symbols: List[str] = []
        reconstructable_closed_symbols: List[str] = []

        for symbol, rows in grouped.items():
            rows.sort(key=lambda r: self._parse_activity_ts(r))
            net_delta = sum(self._activity_signed_qty(r) for r in rows)
            starting_qty = end_signed_qty.get(symbol, 0.0) - net_delta
            if abs(starting_qty) > 1e-6:
                symbol_trades = self._reconstruct_intraday_trades(
                    symbol,
                    rows,
                    trade_date,
                    starting_qty=starting_qty,
                    seed_metadata=self._lookup_position_metadata(symbol),
                )
                if not symbol_trades:
                    symbol_trades = self._resolve_carryover_closes_from_history(
                        symbol,
                        rows,
                        trade_date,
                        starting_qty=starting_qty,
                        history=trusted_history,
                    )
                if symbol_trades:
                    reconstructable_closed_symbols.append(symbol)
                    closed_trades.extend(symbol_trades)
                    continue
                unresolved_symbols.append(symbol)
                continue
            symbol_trades = self._reconstruct_intraday_trades(symbol, rows, trade_date)
            if symbol_trades:
                reconstructable_closed_symbols.append(symbol)
                closed_trades.extend(symbol_trades)

        realized_pnl = round(sum(float(t.get("pnl", 0) or 0) for t in closed_trades), 2)
        return {
            "trade_count": len(closed_trades),
            "realized_pnl": realized_pnl,
            "closed_symbols": sorted({str(t.get("symbol", "") or "").upper() for t in closed_trades if str(t.get("symbol", "") or "").strip()}),
            "reconstructable_closed_symbols": sorted(set(reconstructable_closed_symbols)),
            "unresolved_symbols": sorted(set(unresolved_symbols)),
            "trades": closed_trades,
        }

    def _resolve_carryover_closes_from_history(
        self,
        symbol: str,
        rows: List[Dict],
        trade_date: str,
        *,
        starting_qty: float,
        history: List[Dict],
    ) -> List[Dict]:
        symbol_key = str(symbol or "").upper().strip()
        if not symbol_key or not rows:
            return []
        target_day = str(trade_date or "").strip()
        close_sign = -1 if float(starting_qty or 0) > 0 else 1
        close_rows = [
            row
            for row in rows
            if (1 if self._activity_signed_qty(row) > 0 else (-1 if self._activity_signed_qty(row) < 0 else 0)) == close_sign
        ]
        activity_exit_time = max(float(self._parse_activity_ts(row) or 0.0) for row in close_rows or rows)
        activity_qty = round(sum(abs(float(row.get("qty", 0) or 0)) for row in close_rows), 6)
        if activity_exit_time <= 0 or activity_qty <= 0:
            return []
        activity_order_ids = {
            str(row.get("order_id", "") or "").strip()
            for row in close_rows
            if str(row.get("order_id", "") or "").strip()
        }
        expected_side = "buy_to_cover" if float(starting_qty or 0) < 0 else "sell"
        candidates = []
        for trade in history or []:
            if self._is_partial_trade(trade):
                continue
            if str(trade.get("symbol", "") or "").upper() != symbol_key:
                continue
            if str(trade.get("asset_type", "equity") or "equity").lower() != "equity":
                continue
            if target_day and self._trade_day_key_from_trade(trade) != target_day:
                continue
            reason = str(trade.get("reason", "") or "").strip().lower()
            if reason in {"broker_fill_reconstructed", ""}:
                continue
            entry_price = float(trade.get("entry_price", 0) or 0)
            exit_price = float(trade.get("exit_price", 0) or 0)
            qty = round(float(trade.get("quantity", 0) or 0), 6)
            exit_time = float(trade.get("exit_time", 0) or 0)
            if entry_price <= 0 or exit_price <= 0 or qty <= 0 or exit_time <= 0:
                continue
            trade_side = str(trade.get("side", "") or "").lower()
            if trade_side and trade_side != expected_side:
                continue
            exit_order_id = str(
                trade.get("exit_order_id")
                or trade.get("order_id")
                or ""
            ).strip()
            order_match = bool(activity_order_ids and exit_order_id and exit_order_id in activity_order_ids)
            qty_gap = abs(qty - activity_qty)
            qty_match = qty_gap <= max(0.001, activity_qty * 0.02)
            dust_gap = qty_gap <= 0.25 and (qty_gap * exit_price) <= 25.0
            if not qty_match and not (order_match and dust_gap):
                continue
            time_match = abs(exit_time - activity_exit_time) <= 180.0
            if not order_match and not time_match:
                continue
            score = 1000 if order_match else 0
            score += max(0.0, 180.0 - abs(exit_time - activity_exit_time))
            candidates.append((score, normalize_trade_record(dict(trade))))
        if not candidates:
            return []
        candidates.sort(key=lambda item: item[0], reverse=True)
        return [candidates[0][1]]

    def _reconstruct_intraday_trades(
        self,
        symbol: str,
        rows: List[Dict],
        trade_date: str,
        *,
        starting_qty: float = 0.0,
        seed_metadata: Optional[Dict] = None,
    ) -> List[Dict]:
        trades: List[Dict] = []
        signed_qty = 0.0
        avg_cost = 0.0
        segment = None

        if abs(float(starting_qty or 0.0)) > 1e-6:
            metadata = dict(seed_metadata or {})
            entry_price = float(metadata.get("entry_price", 0) or 0)
            if entry_price <= 0:
                return []
            inferred_side = "short" if float(starting_qty) < 0 else "long"
            metadata_side = str(metadata.get("side", "") or "").lower()
            if metadata_side in {"long", "short"} and metadata_side != inferred_side:
                return []
            metadata_qty = abs(float(metadata.get("quantity", metadata.get("actual_qty", 0)) or 0))
            seeded_qty = abs(float(starting_qty))
            if metadata_qty > 0:
                qty_gap = abs(metadata_qty - seeded_qty)
                if qty_gap > max(0.01, seeded_qty * 0.25):
                    return []
            fallback_entry_time = self._parse_activity_ts(rows[0]) if rows else 0.0
            seeded_entry_time = float(
                metadata.get("entry_time")
                or metadata.get("signal_timestamp")
                or fallback_entry_time
            )
            signed_qty = float(starting_qty)
            avg_cost = entry_price
            segment = {
                "side": inferred_side,
                "opened_qty": seeded_qty,
                "open_notional": seeded_qty * entry_price,
                "closed_qty": 0.0,
                "close_notional": 0.0,
                "entry_time": seeded_entry_time,
                "order_ids": [],
            }

        def finalize_segment(exit_ts: float):
            nonlocal signed_qty, avg_cost, segment, trades
            if not segment:
                return
            open_qty = float(segment.get("opened_qty", 0) or 0)
            close_qty = float(segment.get("closed_qty", 0) or 0)
            if open_qty <= 0 or close_qty <= 0:
                segment = None
                return
            entry_price = float(segment.get("open_notional", 0) or 0) / open_qty
            exit_price = float(segment.get("close_notional", 0) or 0) / close_qty
            side = segment.get("side", "long")
            if side == "short":
                pnl = (entry_price - exit_price) * close_qty
                order_side = "buy_to_cover"
            else:
                pnl = (exit_price - entry_price) * close_qty
                order_side = "sell"
            pnl_pct = ((exit_price - entry_price) / entry_price * 100.0) if entry_price else 0.0
            if side == "short":
                pnl_pct = -pnl_pct
            metadata = self._lookup_position_metadata(symbol)
            signal_sources = metadata.get("signal_sources", ["broker_reconciliation"])
            if isinstance(signal_sources, str):
                signal_sources = [part.strip() for part in signal_sources.split(",") if part.strip()]
            anomaly_flags = list(metadata.get("anomaly_flags", []) or [])
            metadata_exit_order_id = str(metadata.get("exit_order_id", "") or "").strip()
            broker_order_ids = [
                str(order_id or "").strip()
                for order_id in (segment.get("order_ids", []) or [])
                if str(order_id or "").strip()
            ]
            confirmed_local_exit = bool(
                metadata_exit_order_id and metadata_exit_order_id in broker_order_ids
            )
            if confirmed_local_exit:
                anomaly_flags = [
                    flag
                    for flag in anomaly_flags
                    if str(flag or "").strip().lower()
                    not in {
                        "carryover_sync",
                        "broker_reloaded_after_local_removal",
                        "broker_reconstructed",
                    }
                ]
            elif "broker_reconstructed" not in anomaly_flags:
                anomaly_flags.append("broker_reconstructed")
            exit_reason = str(metadata.get("last_exit_reason", "") or "").strip()
            if not exit_reason:
                exit_reason = (
                    "broker_fill_confirmed_local_exit"
                    if confirmed_local_exit
                    else "broker_fill_reconstructed"
                )
            trade = normalize_trade_record({
                "symbol": symbol,
                "side": order_side,
                "entry_price": round(entry_price, 6),
                "exit_price": round(exit_price, 6),
                "quantity": round(close_qty, 6),
                "pnl": round(pnl, 2),
                "pnl_pct": round(pnl_pct, 2),
                "reason": exit_reason,
                "entry_time": segment.get("entry_time"),
                "exit_time": exit_ts,
                "hold_seconds": max(0.0, exit_ts - float(segment.get("entry_time", exit_ts) or exit_ts)),
                "asset_type": "equity",
                "strategy_tag": normalize_strategy_tag(metadata.get("strategy_tag", "unknown")),
                "signal_tier": metadata.get("signal_tier", "tier_2"),
                "holding_horizon": metadata.get("holding_horizon", "intraday"),
                "market_regime": metadata.get("market_regime", "mixed"),
                "entry_reason_code": metadata.get("entry_reason_code", "unknown"),
                "entry_model_votes": dict(metadata.get("entry_model_votes", {}) or {}),
                "risk_constraints_applied": list(metadata.get("risk_constraints_applied", []) or []),
                "entry_path": metadata.get("entry_path", "broker_reconciliation"),
                "signal_sources": signal_sources or ["broker_reconciliation"],
                "provider_used": metadata.get("provider_used", "alpaca_reconciler"),
                "decision_confidence": metadata.get("decision_confidence", 0),
                "anomaly_flags": anomaly_flags,
                "trade_date": trade_date,
                "exit_order_id": segment.get("order_ids", [""])[-1] if segment.get("order_ids") else "",
            })
            trades.append(trade)
            segment = None

        for row in rows:
            qty = abs(float(row.get("qty", 0) or 0))
            if qty <= 0:
                continue
            price = float(row.get("price", 0) or 0)
            if price <= 0:
                continue
            delta = self._activity_signed_qty(row)
            ts = self._parse_activity_ts(row)
            if signed_qty == 0:
                signed_qty = delta
                avg_cost = price
                segment = {
                    "side": "long" if delta > 0 else "short",
                    "opened_qty": qty,
                    "open_notional": qty * price,
                    "closed_qty": 0.0,
                    "close_notional": 0.0,
                    "entry_time": ts,
                    "order_ids": [str(row.get("order_id", "") or "")],
                }
                continue

            if signed_qty * delta > 0:
                total_qty = abs(signed_qty) + qty
                avg_cost = ((avg_cost * abs(signed_qty)) + (price * qty)) / max(1e-9, total_qty)
                signed_qty += delta
                if segment:
                    segment["opened_qty"] += qty
                    segment["open_notional"] += qty * price
                    segment["order_ids"].append(str(row.get("order_id", "") or ""))
                continue

            close_qty = min(abs(signed_qty), qty)
            if segment:
                segment["closed_qty"] += close_qty
                segment["close_notional"] += close_qty * price
                segment["order_ids"].append(str(row.get("order_id", "") or ""))
            if abs(delta) < abs(signed_qty) + 1e-9:
                signed_qty += delta
                if abs(signed_qty) <= 1e-9:
                    finalize_segment(ts)
                    signed_qty = 0.0
                    avg_cost = 0.0
                continue

            # Flip through zero: close the existing segment, then open the remainder.
            signed_qty = 0.0
            finalize_segment(ts)
            leftover = abs(qty - close_qty)
            if leftover > 1e-9:
                new_delta = leftover if delta > 0 else -leftover
                signed_qty = new_delta
                avg_cost = price
                segment = {
                    "side": "long" if new_delta > 0 else "short",
                    "opened_qty": leftover,
                    "open_notional": leftover * price,
                    "closed_qty": 0.0,
                    "close_notional": 0.0,
                    "entry_time": ts,
                    "order_ids": [str(row.get("order_id", "") or "")],
                }

        if abs(signed_qty) <= 1e-9 and segment:
            finalize_segment(float(segment.get("entry_time", 0) or 0))
        return trades

    @staticmethod
    def _parse_activity_ts(row: Dict) -> float:
        raw = row.get("transaction_time") or row.get("date") or row.get("created_at")
        if raw:
            try:
                from datetime import datetime
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            except Exception:
                pass
        return 0.0

    @staticmethod
    def _activity_signed_qty(row: Dict) -> float:
        qty = abs(float(row.get("qty", 0) or 0))
        side = str(row.get("side", "") or "").lower()
        return qty if side == "buy" else -qty

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
        for symbol in sorted(internal.get("broker_reconstructed_unresolved_symbols", []) or []):
            add_canary(
                "broker_fill_ledger_unresolved",
                "warning",
                symbol=symbol,
                recommended_action="Carryover cost basis is missing; reconcile with prior-session broker position basis.",
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
    def build_trust_flags(
        reconciliation: Dict,
        consecutive_critical_mismatch: int = 0,
        entry_pause_due_to_critical: bool = False,
        entry_pause_due_to_429: bool = False,
        broker_api: Optional[Dict] = None,
    ) -> Dict:
        status = reconciliation.get("status", "minor_mismatch")
        broker_only = status == "critical_mismatch"
        degraded = status != "healthy"
        degraded_reasons: List[str] = []
        if broker_only:
            degraded_reasons.append("critical_reconciliation")
        if entry_pause_due_to_critical:
            degraded_reasons.append("persistent_reconciliation_mismatch")
        if entry_pause_due_to_429:
            degraded_reasons.append("broker_api_rate_limited")
        entry_pipeline_paused = bool(broker_only or entry_pause_due_to_critical or entry_pause_due_to_429)
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
            "entry_pipeline_paused": entry_pipeline_paused,
            "degraded_mode_reasons": degraded_reasons,
            "consecutive_critical_mismatch": int(consecutive_critical_mismatch or 0),
            "broker_api_recent_429_total": int((broker_api or {}).get("recent_429_total", 0) or 0),
        }

    @staticmethod
    def _trade_day_key(ts: float) -> str:
        return trading_session_day(ts)

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
