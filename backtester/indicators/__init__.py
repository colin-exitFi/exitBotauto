"""Indicator exports and registration side effects."""

from backtester.indicators.registry import BaseIndicator, IndicatorRegistry, IndicatorSignal

from backtester.indicators.ema_crossover import EMACrossoverIndicator
from backtester.indicators.vwap_bands import VWAPBandsIndicator
from backtester.indicators.rsi_divergence import RSIDivergenceIndicator
from backtester.indicators.macd_histogram import MACDHistogramIndicator
from backtester.indicators.bollinger_squeeze import BollingerSqueezeIndicator
from backtester.indicators.volume_profile import VolumeProfileIndicator
from backtester.indicators.supertrend import SupertrendIndicator
from backtester.indicators.hull_ma import HullMAIndicator
from backtester.indicators.keltner_channel import KeltnerChannelIndicator
from backtester.indicators.stoch_rsi import StochRSIIndicator

__all__ = [
    "BaseIndicator",
    "IndicatorRegistry",
    "IndicatorSignal",
    "EMACrossoverIndicator",
    "VWAPBandsIndicator",
    "RSIDivergenceIndicator",
    "MACDHistogramIndicator",
    "BollingerSqueezeIndicator",
    "VolumeProfileIndicator",
    "SupertrendIndicator",
    "HullMAIndicator",
    "KeltnerChannelIndicator",
    "StochRSIIndicator",
]
