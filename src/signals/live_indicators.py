"""
Live Indicator Signals — Bridge from backtested indicators to real-time trading.

Loads the top backtest-validated indicators and computes live BUY/SELL/NEUTRAL
signals from Polygon minute bars. Results are passed to the technical agent
as `validated_indicator_signals` for AI-augmented analysis.
"""

import asyncio
import json
import time
from functools import partial
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
from loguru import logger


# ── Config ────────────────────────────────────────────────────────────
_TOP_INDICATORS_FILE = (
    Path(__file__).resolve().parent.parent.parent / "data" / "backtest_results" / "top_indicators.json"
)
_CACHE: Dict[str, tuple] = {}  # symbol -> (signals_list, timestamp)
_CACHE_TTL = 120  # 2 minutes — indicators don't flip faster than this on 5m bars
_MAX_INDICATORS = 5  # top N to compute live


def _load_top_indicators() -> List[Dict]:
    """Load validated indicator configs from backtest results."""
    try:
        if not _TOP_INDICATORS_FILE.exists():
            return []
        raw = json.loads(_TOP_INDICATORS_FILE.read_text())
        if not isinstance(raw, list):
            return []
        # Only use indicators with real scores (report filter should handle this,
        # but double-check in case of stale files)
        valid = [r for r in raw if float(r.get("score", 0) or 0) > 0]
        return valid[:_MAX_INDICATORS]
    except Exception as e:
        logger.debug(f"Failed to load top indicators: {e}")
        return []


# ── Indicator Implementations (lightweight, no pandas overhead from backtester) ──

def _compute_vwap_bands(df: pd.DataFrame, params: Dict) -> Dict[str, Any]:
    """VWAP Bands: price above upper band = bullish breakout."""
    std_mult = float(params.get("std_mult", 1.5))
    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)
    volume = df["volume"].astype(float).clip(lower=0)

    # Compute rolling VWAP
    typical = (high + low + close) / 3.0
    pv_cum = (typical * volume).cumsum()
    vol_cum = volume.cumsum().replace(0, np.nan)
    vwap = pv_cum / vol_cum

    # Rolling std of close
    std = close.rolling(min(20, len(close))).std().fillna(0)
    upper = vwap + std * std_mult
    lower = vwap - std * std_mult

    latest_close = close.iloc[-1]
    latest_vwap = vwap.iloc[-1]
    latest_upper = upper.iloc[-1]
    latest_lower = lower.iloc[-1]

    if pd.isna(latest_vwap) or pd.isna(latest_upper):
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    # Signal logic
    prev_close = close.iloc[-2] if len(close) > 1 else latest_close
    prev_upper = upper.iloc[-2] if len(upper) > 1 else latest_upper

    if latest_close > latest_upper and prev_close <= prev_upper:
        signal = "BUY"
        strength = min(100, int(((latest_close - latest_vwap) / latest_close) * 500))
    elif latest_close < latest_lower:
        signal = "SELL"
        strength = min(100, int(((latest_vwap - latest_close) / latest_close) * 500))
    elif latest_close > latest_vwap:
        signal = "BUY"
        strength = min(60, int(((latest_close - latest_vwap) / latest_close) * 300))
    else:
        signal = "NEUTRAL"
        strength = 0

    return {
        "signal": signal,
        "strength": strength,
        "detail": f"close=${latest_close:.2f} vwap=${latest_vwap:.2f} upper=${latest_upper:.2f}",
    }


def _compute_smi(df: pd.DataFrame, params: Dict) -> Dict[str, Any]:
    """Stochastic Momentum Index: smoothed stochastic oscillator."""
    k_length = int(params.get("k_length", 10))
    d_length = int(params.get("d_length", 3))
    smooth = int(params.get("smooth_length", 3))

    high = df["high"].astype(float)
    low = df["low"].astype(float)
    close = df["close"].astype(float)

    hh = high.rolling(k_length).max()
    ll = low.rolling(k_length).min()

    diff = close - (hh + ll) / 2
    range_hl = hh - ll

    diff_s = diff.ewm(span=d_length, adjust=False).mean().ewm(span=smooth, adjust=False).mean()
    range_s = range_hl.ewm(span=d_length, adjust=False).mean().ewm(span=smooth, adjust=False).mean()

    smi = (100 * diff_s / (range_s / 2).replace(0, np.nan)).fillna(0)
    smi_signal = smi.ewm(span=d_length, adjust=False).mean()

    if len(smi) < 2:
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    latest = smi.iloc[-1]
    prev = smi.iloc[-2]
    latest_sig = smi_signal.iloc[-1]
    prev_sig = smi_signal.iloc[-2]

    cross_up = latest > latest_sig and prev <= prev_sig
    cross_down = latest < latest_sig and prev >= prev_sig

    if cross_up and latest < 0:
        signal = "BUY"
        strength = min(100, int(abs(latest) * 1.5))
    elif cross_down and latest > 0:
        signal = "SELL"
        strength = min(100, int(abs(latest) * 1.5))
    elif latest > 40:
        signal = "SELL"
        strength = min(50, int((latest - 40) * 1.2))
    elif latest < -40:
        signal = "BUY"
        strength = min(50, int(abs(latest + 40) * 1.2))
    else:
        signal = "NEUTRAL"
        strength = 0

    return {
        "signal": signal,
        "strength": strength,
        "detail": f"SMI={latest:.1f} signal={latest_sig:.1f}",
    }


def _compute_obv_divergence(df: pd.DataFrame, params: Dict) -> Dict[str, Any]:
    """OBV Divergence: detect accumulation/distribution via volume flow."""
    lookback = int(params.get("lookback", 14))
    smooth = int(params.get("smooth", 5))

    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    obv = (volume * np.sign(close.diff())).fillna(0).cumsum()
    obv_smooth = obv.rolling(smooth).mean()

    if len(close) < lookback + 2:
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    price_low = close.rolling(lookback).min()
    obv_low = obv_smooth.rolling(lookback).min()
    obv_rising = obv_smooth > obv_smooth.shift(3)

    price_high = close.rolling(lookback).max()
    obv_high = obv_smooth.rolling(lookback).max()

    latest_close = close.iloc[-1]
    latest_obv = obv_smooth.iloc[-1]

    if pd.isna(latest_obv):
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    # Bullish divergence: price near low but OBV not
    at_low = latest_close <= price_low.iloc[-1] * 1.02
    obv_above_low = latest_obv > obv_low.iloc[-1] * 1.02
    is_rising = bool(obv_rising.iloc[-1])

    # Bearish divergence: price near high but OBV not
    at_high = latest_close >= price_high.iloc[-1] * 0.98
    obv_below_high = latest_obv < obv_high.iloc[-1] * 0.98

    vol_ratio = float(volume.iloc[-1] / volume.rolling(20).mean().iloc[-1]) if volume.rolling(20).mean().iloc[-1] > 0 else 1

    if at_low and obv_above_low and is_rising:
        signal = "BUY"
        strength = min(100, int(vol_ratio * 40))
    elif at_high and obv_below_high and not is_rising:
        signal = "SELL"
        strength = min(100, int(vol_ratio * 40))
    else:
        signal = "NEUTRAL"
        strength = 0

    return {
        "signal": signal,
        "strength": strength,
        "detail": f"OBV={latest_obv:.0f} vol_ratio={vol_ratio:.1f}x",
    }


def _compute_rsi_divergence(df: pd.DataFrame, params: Dict) -> Dict[str, Any]:
    """RSI with divergence detection."""
    period = int(params.get("period", 14))
    close = df["close"].astype(float)
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = (100.0 - (100.0 / (1.0 + rs))).fillna(50)

    if len(rsi) < period + 5:
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    latest_rsi = rsi.iloc[-1]
    latest_close = close.iloc[-1]

    # Simple oversold/overbought + trend
    rsi_rising = latest_rsi > rsi.iloc[-3]

    if latest_rsi < 30 and rsi_rising:
        signal = "BUY"
        strength = min(100, int((30 - latest_rsi) * 4 + 30))
    elif latest_rsi > 70 and not rsi_rising:
        signal = "SELL"
        strength = min(100, int((latest_rsi - 70) * 4 + 30))
    else:
        signal = "NEUTRAL"
        strength = 0

    return {
        "signal": signal,
        "strength": strength,
        "detail": f"RSI={latest_rsi:.1f}",
    }


def _compute_hull_ma(df: pd.DataFrame, params: Dict) -> Dict[str, Any]:
    """Hull Moving Average: direction change = trend reversal."""
    period = int(params.get("period", 16))
    close = df["close"].astype(float)

    def _wma(series, n):
        weights = np.arange(1, n + 1, dtype=float)
        return series.rolling(n).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)

    half = max(1, period // 2)
    sqrt_p = max(1, int(np.sqrt(period)))
    hma = _wma((2 * _wma(close, half)) - _wma(close, period), sqrt_p)

    if hma.isna().all() or len(hma.dropna()) < 3:
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    slope = hma.diff()
    latest_slope = slope.iloc[-1]
    prev_slope = slope.iloc[-2]

    if pd.isna(latest_slope) or pd.isna(prev_slope):
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    latest_close = close.iloc[-1]

    if latest_slope > 0 and prev_slope <= 0:
        signal = "BUY"
        strength = min(100, int(abs(latest_slope / latest_close) * 3000 + 40))
    elif latest_slope < 0 and prev_slope >= 0:
        signal = "SELL"
        strength = min(100, int(abs(latest_slope / latest_close) * 3000 + 40))
    elif latest_slope > 0:
        signal = "BUY"
        strength = min(50, int(abs(latest_slope / latest_close) * 2000))
    elif latest_slope < 0:
        signal = "SELL"
        strength = min(50, int(abs(latest_slope / latest_close) * 2000))
    else:
        signal = "NEUTRAL"
        strength = 0

    return {
        "signal": signal,
        "strength": strength,
        "detail": f"HMA slope={latest_slope:.3f} period={period}",
    }


def _compute_atr_channel(df: pd.DataFrame, params: Dict) -> Dict[str, Any]:
    """ATR Channel Breakout: Turtle Trading variant."""
    ema_period = int(params.get("ema_period", 20))
    atr_period = int(params.get("atr_period", 14))
    atr_mult = float(params.get("atr_mult", 2.0))

    close = df["close"].astype(float)
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    ema = close.ewm(span=ema_period, adjust=False).mean()

    tr = pd.concat([
        high - low,
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    atr = tr.rolling(atr_period).mean()

    upper = ema + atr * atr_mult
    lower = ema - atr * atr_mult

    if atr.isna().all():
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    latest_close = close.iloc[-1]
    latest_upper = upper.iloc[-1]
    latest_lower = lower.iloc[-1]
    latest_ema = ema.iloc[-1]
    latest_atr = atr.iloc[-1]

    if pd.isna(latest_upper):
        return {"signal": "NEUTRAL", "strength": 0, "detail": "insufficient data"}

    prev_close = close.iloc[-2] if len(close) > 1 else latest_close
    prev_upper = upper.iloc[-2] if len(upper) > 1 else latest_upper

    if latest_close > latest_upper and prev_close <= prev_upper:
        signal = "BUY"
        dist = (latest_close - latest_upper) / latest_atr if latest_atr > 0 else 0
        strength = min(100, int(dist * 30 + 60))
    elif latest_close > latest_upper:
        signal = "BUY"
        strength = min(60, int(((latest_close - latest_upper) / latest_atr) * 20 + 30) if latest_atr > 0 else 30)
    elif latest_close < latest_ema and prev_close >= latest_ema:
        signal = "SELL"
        strength = 50
    elif latest_close < latest_lower:
        signal = "SELL"
        strength = 70
    else:
        signal = "NEUTRAL"
        strength = 0

    return {
        "signal": signal,
        "strength": strength,
        "detail": f"close=${latest_close:.2f} upper=${latest_upper:.2f} ema=${latest_ema:.2f}",
    }


# ── Dispatcher ────────────────────────────────────────────────────────

_INDICATOR_FNS = {
    "vwap_bands": _compute_vwap_bands,
    "smi": _compute_smi,
    "obv_div": _compute_obv_divergence,
    "obv_divergence": _compute_obv_divergence,
    "rsi_divergence": _compute_rsi_divergence,
    "hull_ma": _compute_hull_ma,
    "atr_channel": _compute_atr_channel,
    "atr_channel_breakout": _compute_atr_channel,
}


def _compute_indicator(name: str, df: pd.DataFrame, params: Dict) -> Optional[Dict]:
    """Compute a single indicator signal."""
    # Map parameterized names back to base indicator
    base_name = name
    for known in _INDICATOR_FNS:
        if name.startswith(known):
            base_name = known
            break

    fn = _INDICATOR_FNS.get(base_name)
    if not fn:
        return None

    try:
        result = fn(df, params)
        result["name"] = name
        result["score"] = 0  # will be filled from backtest metadata
        return result
    except Exception as e:
        logger.debug(f"Live indicator {name} failed: {e}")
        return None


async def compute_live_signals(
    symbol: str,
    price: float,
    polygon_client,
) -> List[Dict]:
    """
    Compute live signals from top backtest-validated indicators.

    Returns list of dicts:
      [{"name": "vwap_bands", "signal": "BUY", "strength": 72, "score": 83.3, "detail": "..."}, ...]
    """
    # Check cache
    cached = _CACHE.get(symbol)
    if cached and (time.time() - cached[1]) < _CACHE_TTL:
        return cached[0]

    top = _load_top_indicators()
    if not top:
        return []

    # Fetch 5-minute bars (same as compute_technicals but more history for indicators)
    try:
        loop = asyncio.get_event_loop()
        bars = await loop.run_in_executor(
            None,
            partial(
                polygon_client.get_bars,
                symbol,
                timespan="minute",
                multiplier=5,
                limit=60,  # 5 hours of 5-min bars
            ),
        )
    except Exception as e:
        logger.debug(f"Live indicators: bar fetch failed for {symbol}: {e}")
        return []

    if not bars or len(bars) < 10:
        return []

    # Build DataFrame
    df = pd.DataFrame(bars)
    for col in ("open", "high", "low", "close", "volume"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0)
        else:
            df[col] = 0.0

    # Compute each indicator
    signals = []
    for indicator in top:
        name = indicator.get("name", "")
        params = indicator.get("params", {})
        backtest_score = float(indicator.get("score", 0) or 0)

        result = _compute_indicator(name, df, params)
        if result:
            result["score"] = backtest_score
            signals.append(result)

    # Cache results
    _CACHE[symbol] = (signals, time.time())

    if signals:
        active = [s for s in signals if s["signal"] != "NEUTRAL"]
        if active:
            summary = ", ".join(f"{s['name']}={s['signal']}" for s in active)
            logger.debug(f"📊 Live indicators {symbol}: {summary}")

    return signals


def get_consensus(signals: List[Dict]) -> Dict[str, Any]:
    """
    Aggregate live indicator signals into a consensus view.

    Returns:
      {"bias": "BUY"|"SELL"|"NEUTRAL", "strength": 0-100, "agreement": 0.0-1.0, "count": N}
    """
    if not signals:
        return {"bias": "NEUTRAL", "strength": 0, "agreement": 0.0, "count": 0}

    buy_weight = 0.0
    sell_weight = 0.0
    total_weight = 0.0

    for s in signals:
        # Weight by backtest score
        weight = max(0.25, float(s.get("score", 0) or 0) / 100.0)
        strength = float(s.get("strength", 0) or 0) / 100.0

        if s["signal"] == "BUY":
            buy_weight += weight * strength
        elif s["signal"] == "SELL":
            sell_weight += weight * strength
        total_weight += weight

    if total_weight == 0:
        return {"bias": "NEUTRAL", "strength": 0, "agreement": 0.0, "count": len(signals)}

    buy_pct = buy_weight / total_weight
    sell_pct = sell_weight / total_weight

    if buy_pct > sell_pct and buy_pct > 0.2:
        bias = "BUY"
        strength = int(buy_pct * 100)
        agreement = buy_pct
    elif sell_pct > buy_pct and sell_pct > 0.2:
        bias = "SELL"
        strength = int(sell_pct * 100)
        agreement = sell_pct
    else:
        bias = "NEUTRAL"
        strength = 0
        agreement = 1.0 - abs(buy_pct - sell_pct)

    return {
        "bias": bias,
        "strength": min(100, strength),
        "agreement": round(agreement, 2),
        "count": len(signals),
    }
