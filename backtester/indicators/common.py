"""Shared indicator math helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd

from backtester.indicators.registry import IndicatorSignal


def ema(series: pd.Series, period: int) -> pd.Series:
    return pd.Series(series, dtype=float).ewm(span=max(1, int(period)), adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    return pd.Series(series, dtype=float).rolling(max(1, int(period))).mean()


def wma(series: pd.Series, period: int) -> pd.Series:
    period = max(1, int(period))
    weights = np.arange(1, period + 1, dtype=float)
    return pd.Series(series, dtype=float).rolling(period).apply(lambda x: np.dot(x, weights) / weights.sum(), raw=True)


def hull_ma(series: pd.Series, period: int) -> pd.Series:
    period = max(2, int(period))
    half = max(1, period // 2)
    sqrt_period = max(1, int(np.sqrt(period)))
    return wma((2 * wma(series, half)) - wma(series, period), sqrt_period)


def true_range(df: pd.DataFrame) -> pd.Series:
    high = pd.Series(df["high"], dtype=float)
    low = pd.Series(df["low"], dtype=float)
    close = pd.Series(df["close"], dtype=float)
    prev_close = close.shift(1)
    return pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    return true_range(df).rolling(max(1, int(period))).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    series = pd.Series(series, dtype=float)
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / max(1, int(period)), adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / max(1, int(period)), adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).fillna(50.0)


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_line = ema(series, fast)
    slow_line = ema(series, slow)
    macd_line = fast_line - slow_line
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger(series: pd.Series, period: int = 20, std_mult: float = 2.0):
    mid = sma(series, period)
    std = pd.Series(series, dtype=float).rolling(max(1, int(period))).std().fillna(0.0)
    upper = mid + std * float(std_mult)
    lower = mid - std * float(std_mult)
    bandwidth = ((upper - lower) / mid.replace(0, np.nan)).fillna(0.0)
    return mid, upper, lower, bandwidth


def stochastic_rsi(series: pd.Series, rsi_period: int = 14, stoch_period: int = 14, k_smooth: int = 3, d_smooth: int = 3):
    rsi_series = rsi(series, rsi_period)
    min_rsi = rsi_series.rolling(max(1, int(stoch_period))).min()
    max_rsi = rsi_series.rolling(max(1, int(stoch_period))).max()
    stoch = ((rsi_series - min_rsi) / (max_rsi - min_rsi).replace(0, np.nan) * 100.0).fillna(0.0)
    k = stoch.rolling(max(1, int(k_smooth))).mean().fillna(0.0)
    d = k.rolling(max(1, int(d_smooth))).mean().fillna(0.0)
    return rsi_series, k, d


def crossed_above(left: pd.Series, right: pd.Series) -> pd.Series:
    left = pd.Series(left, dtype=float)
    right = pd.Series(right, dtype=float)
    return (left > right) & (left.shift(1) <= right.shift(1))


def crossed_below(left: pd.Series, right: pd.Series) -> pd.Series:
    left = pd.Series(left, dtype=float)
    right = pd.Series(right, dtype=float)
    return (left < right) & (left.shift(1) >= right.shift(1))


def daily_vwap(df: pd.DataFrame) -> pd.Series:
    prices = ((pd.Series(df["high"], dtype=float) + pd.Series(df["low"], dtype=float) + pd.Series(df["close"], dtype=float)) / 3.0)
    volume = pd.Series(df["volume"], dtype=float).fillna(0.0)
    if not isinstance(df.index, pd.DatetimeIndex):
        return (prices * volume).cumsum() / volume.replace(0, np.nan).cumsum()
    dates = df.index.tz_convert("UTC").normalize() if df.index.tz is not None else df.index.normalize()
    pv = prices * volume
    return pv.groupby(dates).cumsum() / volume.groupby(dates).cumsum().replace(0, np.nan)


def finalize_signal(index: pd.Index, entries, exits, strength, side: str, name: str, params: dict) -> IndicatorSignal:
    entries = pd.Series(entries, index=index).fillna(False).astype(bool)
    exits = pd.Series(exits, index=index).fillna(False).astype(bool)
    strength = pd.Series(strength, index=index, dtype=float).fillna(0.0).clip(lower=0.0, upper=1.0)
    return IndicatorSignal(entries=entries, exits=exits, signal_strength=strength, side=side, name=name, params=dict(params or {}))
