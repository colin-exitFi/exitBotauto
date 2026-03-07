"""Indicator interface and registry."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List

import pandas as pd


@dataclass
class IndicatorSignal:
    entries: pd.Series
    exits: pd.Series
    signal_strength: pd.Series
    side: str
    name: str
    params: Dict[str, Any]


class BaseIndicator(ABC):
    """Base class for all backtest indicators."""

    def __init__(self, **params):
        self.params = dict(params)

    @abstractmethod
    def name(self) -> str:
        ...

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        ...

    @abstractmethod
    def param_grid(self) -> List[Dict[str, Any]]:
        ...

    def with_params(self, **params):
        return self.__class__(**params)


class IndicatorRegistry:
    """Registry for all indicator classes."""

    _indicators: Dict[str, type] = {}

    @classmethod
    def register(cls, indicator_class: type):
        cls._indicators[indicator_class().name()] = indicator_class
        return indicator_class

    @classmethod
    def get_all(cls) -> Dict[str, type]:
        return dict(cls._indicators)

    @classmethod
    def get(cls, name: str) -> type:
        return cls._indicators[name]

    @classmethod
    def instantiate_all(cls) -> List[BaseIndicator]:
        return [indicator_class() for indicator_class in cls.get_all().values()]
