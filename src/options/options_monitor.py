"""Options monitor loop and execution helpers."""

import asyncio
from datetime import datetime, timezone
from typing import Dict

from loguru import logger

from config import settings
from src import persistence
from src.dashboard.dashboard import log_activity


class OptionsMonitor:
    """Runs options lifecycle monitoring and executes rule-based exits."""

    def __init__(self, bot):
        self.bot = bot

    @staticmethod
    def is_regular_market_hours() -> bool:
        """Options trade only during regular session (9:30 AM - 4:00 PM ET)."""
        try:
            import zoneinfo

            et = datetime.now(zoneinfo.ZoneInfo("US/Eastern"))
        except Exception:
            try:
                import pytz

                et = datetime.now(pytz.timezone("US/Eastern"))
            except Exception:
                et = datetime.now(timezone.utc)
        if et.weekday() >= 5:
            return False
        open_min = 9 * 60 + 30
        close_min = 16 * 60
        now_min = et.hour * 60 + et.minute
        return open_min <= now_min < close_min

    async def close_paired_options(self, underlying_symbol: str, reason: str = "underlying_exit"):
        """Close all linked options when the underlying share position exits."""
        options_engine = getattr(self.bot, "options_engine", None)
        if not options_engine:
            return
        try:
            results = await asyncio.get_event_loop().run_in_executor(
                None, options_engine.close_paired_options, underlying_symbol, reason
            )
            for item in results or []:
                if item.get("pnl") is not None:
                    self.bot._record_realized_exit(item)
                    log_activity(
                        "options",
                        f"🔗 PAIRED OPTIONS EXIT: {item.get('symbol')} ({item.get('reason')}) P&L ${item.get('pnl', 0):.2f}",
                    )
                else:
                    log_activity(
                        "options",
                        f"🔗 PAIRED OPTIONS CLOSE SUBMITTED: {item.get('symbol')} ({item.get('reason', reason)})",
                    )
        except Exception as e:
            logger.error(f"Paired options close error for {underlying_symbol}: {e}")

    async def execute_exit_action(self, contract_symbol: str, action: Dict) -> bool:
        """Execute an options exit action returned by OptionsEngine.check_exit_rules()."""
        options_engine = getattr(self.bot, "options_engine", None)
        if not options_engine or not action:
            return False

        action_type = action.get("action")
        reason = action.get("reason", "options_rule")
        if action_type == "tighten_trail":
            log_activity(
                "options",
                f"🎯 {contract_symbol}: profit target hit, tightened trail to protect gains",
            )
            return False
        if action_type not in ("close", "partial_take_profit"):
            return False

        qty = int(action.get("qty", 0) or 0)
        if qty < 1:
            return False

        order = await asyncio.get_event_loop().run_in_executor(
            None, options_engine.close_option_position, contract_symbol, qty, reason
        )
        if not order:
            return False

        exit_premium = float(action.get("current_premium", 0) or 0)
        status = str(order.get("status", "") or "").lower()
        try:
            default_filled_qty = int(float(order.get("filled_qty", 0) or 0))
        except Exception:
            default_filled_qty = 0
        if default_filled_qty <= 0 and status in ("filled", "partially_filled"):
            default_filled_qty = int(qty)
        try:
            default_fill_price = float(order.get("filled_avg_price", exit_premium) or exit_premium)
        except Exception:
            default_fill_price = exit_premium
        fill_details = {
            "status": status,
            "filled_qty": default_filled_qty,
            "fill_price": default_fill_price,
        }
        if hasattr(options_engine, "extract_order_fill"):
            fill_details = await asyncio.get_event_loop().run_in_executor(
                None, options_engine.extract_order_fill, order, qty, exit_premium
            )
        filled_qty = int(fill_details.get("filled_qty", 0) or 0)
        if filled_qty < 1:
            log_activity(
                "options",
                f"⏳ OPTIONS EXIT SUBMITTED: {contract_symbol} {reason} awaiting fill",
            )
            return False

        if getattr(options_engine, "reconcile_exit_required", False):
            log_activity(
                "options",
                f"⏳ OPTIONS EXIT FILLED: {contract_symbol} {reason} awaiting broker reconciliation",
            )
            return True

        fill_price = float(fill_details.get("fill_price", exit_premium) or exit_premium)
        trade_record = await asyncio.get_event_loop().run_in_executor(
            None, options_engine.finalize_exit, contract_symbol, filled_qty, fill_price, reason, order
        )
        if not trade_record:
            return False

        self.bot._record_realized_exit(trade_record)
        log_activity(
            "options",
            f"🧾 OPTIONS EXIT: {contract_symbol} {reason} pnl=${trade_record.get('pnl', 0):.2f}",
        )
        return True

    async def monitor_once(self):
        """Single options monitor pass for premium-based exits and broker reconciliation."""
        options_engine = getattr(self.bot, "options_engine", None)
        if not options_engine:
            return

        for pos in list(options_engine.get_options_positions()):
            contract_symbol = pos.get("contract_symbol") or pos.get("symbol")
            if not contract_symbol:
                continue
            action = await asyncio.get_event_loop().run_in_executor(
                None, options_engine.check_exit_rules, contract_symbol
            )
            if action:
                await self.execute_exit_action(contract_symbol, action)

        # Keep local options state in sync with broker snapshot.
        recon = await asyncio.get_event_loop().run_in_executor(None, options_engine.reconcile_with_broker)
        for trade_record in recon.get("reconciled_trades", []) if isinstance(recon, dict) else []:
            self.bot._record_realized_exit(trade_record)
            log_activity(
                "options",
                f"🧾 OPTIONS RECONCILED EXIT: {trade_record.get('symbol')} pnl=${trade_record.get('pnl', 0):.2f}",
            )
        for removed_pos in recon.get("removed_positions", []) if isinstance(recon, dict) else []:
            trade_record = options_engine.build_external_close_trade(
                removed_pos, reason="options_reconcile_closed"
            )
            if trade_record:
                self.bot._record_realized_exit(trade_record)
                log_activity(
                    "options",
                    f"🔄 EXTERNAL OPTIONS CLOSE: {trade_record.get('symbol')} pnl=${trade_record.get('pnl', 0):.2f}",
                )
        if getattr(self.bot, "risk_manager", None):
            self.bot.risk_manager.update_options_exposure(options_engine.get_options_positions())
        persistence.save_options_positions(options_engine.positions)

    async def monitor_loop(self):
        """Background options lifecycle monitor (premium trail, stop loss, DTE exits)."""
        interval = max(15, int(getattr(settings, "OPTIONS_MONITOR_INTERVAL_SECONDS", 30)))
        while getattr(self.bot, "running", False):
            try:
                if not self.is_regular_market_hours():
                    await asyncio.sleep(interval)
                    continue
                await self.monitor_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Options monitor error: {e}")
            await asyncio.sleep(interval)
