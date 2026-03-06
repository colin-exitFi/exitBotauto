"""
Computed technical indicators for scanner + fast-path logic.
"""

import asyncio
import time
from functools import partial
from typing import Dict, List, Optional, Tuple


_TECHNICALS_CACHE: Dict[Tuple[str, int], Dict] = {}


def _bucket_now() -> int:
    return int(time.time() / 60)


def _prune_cache(current_bucket: int) -> None:
    stale = [k for k in _TECHNICALS_CACHE if k[1] < (current_bucket - 2)]
    for key in stale:
        _TECHNICALS_CACHE.pop(key, None)


def _ema(values: List[float], period: int) -> Optional[float]:
    if len(values) < period or period <= 0:
        return None
    mult = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    for value in values[period:]:
        ema = ((value - ema) * mult) + ema
    return ema


def _ema_series(values: List[float], period: int) -> List[Optional[float]]:
    if len(values) < period or period <= 0:
        return [None] * len(values)
    mult = 2.0 / (period + 1.0)
    ema = sum(values[:period]) / period
    series: List[Optional[float]] = [None] * (period - 1) + [ema]
    for value in values[period:]:
        ema = ((value - ema) * mult) + ema
        series.append(ema)
    return series


def _rsi_14(closes: List[float]) -> Optional[float]:
    if len(closes) < 15:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in deltas[:14]]
    losses = [max(-d, 0.0) for d in deltas[:14]]
    avg_gain = sum(gains) / 14.0
    avg_loss = sum(losses) / 14.0
    for delta in deltas[14:]:
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = ((avg_gain * 13.0) + gain) / 14.0
        avg_loss = ((avg_loss * 13.0) + loss) / 14.0
    if avg_loss == 0:
        if avg_gain == 0:
            return 50.0
        return 100.0
    rs = avg_gain / avg_loss
    return 100.0 - (100.0 / (1.0 + rs))


def _ema_cross_bars_ago(closes: List[float]) -> Optional[int]:
    ema9_series = _ema_series(closes, 9)
    ema20_series = _ema_series(closes, 20)
    diffs: List[float] = []
    for i in range(len(closes)):
        e9 = ema9_series[i]
        e20 = ema20_series[i]
        if e9 is None or e20 is None:
            continue
        diffs.append(e9 - e20)
    if not diffs:
        return None
    current = diffs[-1]
    if current == 0:
        return None
    current_sign = 1 if current > 0 else -1
    bars_ago = 0
    for value in reversed(diffs[:-1]):
        if value == 0:
            continue
        sign = 1 if value > 0 else -1
        if sign == current_sign:
            bars_ago += 1
            continue
        break
    return bars_ago


def _snapshot_day_high_low(snapshot: Optional[Dict], bars: List[Dict]) -> Tuple[float, float]:
    day_high = 0.0
    day_low = 0.0
    if snapshot:
        for high_key, low_key in (
            ("day_high", "day_low"),
            ("high", "low"),
            ("h", "l"),
        ):
            high_val = float(snapshot.get(high_key, 0) or 0)
            low_val = float(snapshot.get(low_key, 0) or 0)
            if high_val > 0 and low_val > 0:
                day_high = high_val
                day_low = low_val
                break
    if day_high <= 0 or day_low <= 0:
        highs = [float(b.get("high", 0) or 0) for b in bars if float(b.get("high", 0) or 0) > 0]
        lows = [float(b.get("low", 0) or 0) for b in bars if float(b.get("low", 0) or 0) > 0]
        if highs and lows:
            day_high = max(highs)
            day_low = min(lows)
    return day_high, day_low


async def compute_technicals(symbol: str, price: float, polygon_client, snapshot: dict = None) -> dict:
    """
    Returns computed indicators or empty dict if bars unavailable.
    snapshot: optional Alpaca/Polygon snapshot with day high/low for true intraday range.
    """
    if not symbol or not polygon_client:
        return {}

    bucket = _bucket_now()
    _prune_cache(bucket)

    key = (symbol, bucket)
    cached = _TECHNICALS_CACHE.get(key)
    if cached:
        return dict(cached)

    loop = asyncio.get_event_loop()
    bars = await loop.run_in_executor(
        None,
        partial(
            polygon_client.get_bars,
            symbol,
            timespan="minute",
            multiplier=5,
            limit=30,
        ),
    )
    if not bars:
        return {}

    closes: List[float] = []
    volumes: List[float] = []
    for bar in bars:
        close = float(bar.get("close", 0) or 0)
        if close <= 0:
            continue
        closes.append(close)
        volumes.append(float(bar.get("volume", 0) or 0))

    if not closes:
        return {}

    rsi_14 = _rsi_14(closes)
    ema_9 = _ema(closes, 9)
    ema_20 = _ema(closes, 20)
    ema_signal = "neutral"
    if ema_9 is not None and ema_20 is not None:
        if ema_9 > ema_20:
            ema_signal = "bullish"
        elif ema_9 < ema_20:
            ema_signal = "bearish"

    rolling_vwap = None
    total_vol = sum(max(v, 0.0) for v in volumes)
    if total_vol > 0:
        rolling_vwap = sum(closes[i] * max(volumes[i], 0.0) for i in range(len(closes))) / total_vol

    rolling_vwap_pct = None
    if rolling_vwap and rolling_vwap > 0 and price > 0:
        rolling_vwap_pct = ((price - rolling_vwap) / rolling_vwap) * 100.0

    day_high, day_low = _snapshot_day_high_low(snapshot, bars)
    range_pct = None
    if day_high > day_low and price > 0:
        range_pct = ((price - day_low) / (day_high - day_low)) * 100.0
        range_pct = max(0.0, min(100.0, range_pct))

    latest_volume = volumes[-1] if volumes else 0.0
    baseline_window = volumes[-21:-1] if len(volumes) >= 21 else volumes[:-1]
    if not baseline_window:
        baseline_window = volumes
    baseline_avg = (sum(baseline_window) / len(baseline_window)) if baseline_window else 0.0
    vol_accel = (latest_volume / baseline_avg) if baseline_avg > 0 else 0.0

    result = {
        "rsi_14": round(rsi_14, 2) if rsi_14 is not None else None,
        "rolling_vwap": round(rolling_vwap, 4) if rolling_vwap is not None else None,
        "rolling_vwap_pct": round(rolling_vwap_pct, 2) if rolling_vwap_pct is not None else None,
        "ema_9": round(ema_9, 4) if ema_9 is not None else None,
        "ema_20": round(ema_20, 4) if ema_20 is not None else None,
        "ema_signal": ema_signal,
        "ema_cross_bars_ago": _ema_cross_bars_ago(closes),
        "range_pct": round(range_pct, 2) if range_pct is not None else None,
        "day_high": round(day_high, 4) if day_high > 0 else None,
        "day_low": round(day_low, 4) if day_low > 0 else None,
        "vol_accel": round(vol_accel, 2),
    }
    _TECHNICALS_CACHE[key] = result
    return dict(result)


def get_cached_rsi(symbol: str) -> Optional[float]:
    if not symbol:
        return None
    current_bucket = _bucket_now()
    _prune_cache(current_bucket)
    for bucket in (current_bucket, current_bucket - 1, current_bucket - 2):
        data = _TECHNICALS_CACHE.get((symbol, bucket))
        if not data:
            continue
        rsi = data.get("rsi_14")
        if rsi is None:
            continue
        try:
            return float(rsi)
        except (TypeError, ValueError):
            continue
    return None

