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
from backtester.indicators.alphatrend import AlphaTrendIndicator
from backtester.indicators.wavetrend import WaveTrendIndicator
from backtester.indicators.ichimoku import IchimokuIndicator
from backtester.indicators.smi import SMIIndicator
from backtester.indicators.rsi_ema_combo import RSIEMAComboIndicator

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
    "AlphaTrendIndicator",
    "WaveTrendIndicator",
    "IchimokuIndicator",
    "SMIIndicator",
    "RSIEMAComboIndicator",
]
