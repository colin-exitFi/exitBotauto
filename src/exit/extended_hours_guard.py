"""
Extended-hours session helper for Velox v2.

Regular session:
- Broker-side hard stops and ratchet limit orders are maintained by main monitor logic.

Extended session:
- Protection is software-managed by the main profit-ratchet loop.
- No broker trailing stops are used.
- No new overnight entries.
"""

import time
from datetime import datetime
from typing import Dict, List

import pytz


ET = pytz.timezone("US/Eastern")


class ExtendedHoursGuard:
    """Lightweight session-state helper for the v2 protection model."""

    def __init__(self, alpaca_client, polygon_client):
        self.broker = alpaca_client
        self.polygon = polygon_client
        self._last_check = 0.0
        self._check_interval = 15.0
        self._last_session = None

    def session_label(self) -> str:
        now = datetime.now(ET)
        if now.weekday() >= 5:
            return "closed"
        hour, minute = now.hour, now.minute
        if (hour == 9 and minute >= 30) or (10 <= hour < 16):
            return "regular"
        if (4 <= hour < 9) or (hour == 9 and minute < 30):
            return "pre"
        if 16 <= hour < 20:
            return "after"
        return "overnight"

    def is_extended_hours(self) -> bool:
        return self.session_label() in {"pre", "after"}

    def is_regular_hours(self) -> bool:
        return self.session_label() == "regular"

    async def protect_positions(self, positions: List[Dict]) -> Dict[str, str]:
        """
        Surface session-state transitions only.
        Order placement/cancel/replace is owned by main monitor logic.
        """
        now = time.time()
        if now - self._last_check < self._check_interval:
            return {}
        self._last_check = now

        session = self.session_label()
        actions: Dict[str, str] = {}
        if session != self._last_session:
            for pos in positions or []:
                symbol = str(pos.get("symbol", "") or "").upper()
                if not symbol:
                    continue
                pos.setdefault("order_state", {})
                if session == "regular":
                    pos["order_state"]["session_protection"] = "broker_managed"
                    actions[symbol] = "regular_session_broker_protection"
                elif session in {"pre", "after"}:
                    pos["order_state"]["session_protection"] = "software_managed"
                    actions[symbol] = "extended_session_software_protection"
                else:
                    pos["order_state"]["session_protection"] = "overnight_monitor_only"
                    actions[symbol] = "overnight_monitor_only"
            self._last_session = session
        return actions
