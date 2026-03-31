"""Helpers for validating captured intraday bar payloads."""

from typing import Dict


def latest_bar_timestamp(bar_context: Dict) -> int:
    latest = 0
    payload = dict(bar_context or {})
    for key in ("bars_1m", "bars_5m"):
        for row in payload.get(key, []) or []:
            if not isinstance(row, dict):
                continue
            try:
                ts = int(row.get("timestamp", 0) or 0)
            except Exception:
                ts = 0
            latest = max(latest, ts)
    return latest


def bar_context_is_stale(bar_context: Dict, now_ts: float, max_age_seconds: float = 900.0) -> bool:
    latest = latest_bar_timestamp(bar_context)
    if latest <= 0:
        return True
    latest_seconds = latest / 1000.0 if latest > 10_000_000_000 else float(latest)
    return max(0.0, float(now_ts or 0.0) - latest_seconds) > float(max_age_seconds or 0.0)
