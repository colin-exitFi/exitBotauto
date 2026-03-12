"""
Canonical Trading Calendar — single source of truth for all timezone-dependent logic.

Replaces 6+ inconsistent implementations across risk_manager, trade_history,
reconciler, options_engine, options_monitor, and entry_manager.
"""

import time
from datetime import datetime, date, timedelta
from typing import Optional

try:
    import zoneinfo
    EASTERN = zoneinfo.ZoneInfo("US/Eastern")
except Exception:
    import pytz
    EASTERN = pytz.timezone("US/Eastern")


def now_eastern() -> datetime:
    return datetime.now(EASTERN)


def trading_day(ts: Optional[float] = None) -> str:
    """Return the trading day as YYYY-MM-DD in Eastern time."""
    if ts is None:
        dt = now_eastern()
    else:
        dt = datetime.fromtimestamp(ts, EASTERN)
    return dt.strftime("%Y-%m-%d")


def trading_week_start(ts: Optional[float] = None) -> str:
    """Return the Monday of the trading week as YYYY-MM-DD."""
    if ts is None:
        dt = now_eastern()
    else:
        dt = datetime.fromtimestamp(ts, EASTERN)
    monday = dt - timedelta(days=dt.weekday())
    return monday.strftime("%Y-%m-%d")


def is_same_trading_day(ts1: float, ts2: float) -> bool:
    return trading_day(ts1) == trading_day(ts2)


def is_market_hours(ts: Optional[float] = None) -> bool:
    """True during regular session (9:30 AM - 4:00 PM ET) on weekdays."""
    dt = now_eastern() if ts is None else datetime.fromtimestamp(ts, EASTERN)
    if dt.weekday() >= 5:
        return False
    t = dt.hour * 60 + dt.minute
    return 570 <= t < 960  # 9:30=570, 16:00=960


def is_extended_hours(ts: Optional[float] = None) -> bool:
    """True during extended session (4:00 AM - 9:30 AM or 4:00 PM - 8:00 PM ET)."""
    dt = now_eastern() if ts is None else datetime.fromtimestamp(ts, EASTERN)
    if dt.weekday() >= 5:
        return False
    t = dt.hour * 60 + dt.minute
    return (240 <= t < 570) or (960 <= t < 1200)


def is_regular_market_hours(ts: Optional[float] = None) -> bool:
    """Alias for is_market_hours for backward compat."""
    return is_market_hours(ts)


def market_open_today() -> bool:
    """True if today is a weekday (does not check holidays)."""
    return now_eastern().weekday() < 5


def seconds_since_market_open(ts: Optional[float] = None) -> float:
    """Seconds since 9:30 AM ET today. Negative if before open."""
    dt = now_eastern() if ts is None else datetime.fromtimestamp(ts, EASTERN)
    open_time = dt.replace(hour=9, minute=30, second=0, microsecond=0)
    return (dt - open_time).total_seconds()


def eastern_hour(ts: Optional[float] = None) -> int:
    dt = now_eastern() if ts is None else datetime.fromtimestamp(ts, EASTERN)
    return dt.hour
