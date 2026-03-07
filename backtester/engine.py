"""Backtest engine with optional vectorbt support and deterministic fallback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd

from backtester.indicators.registry import BaseIndicator, IndicatorSignal

try:
    import vectorbt as vbt  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    vbt = None


@dataclass
class BacktestResult:
    symbol: str
    indicator_name: str
    params: Dict[str, Any]
    side: str
    total_trades: int
    win_rate: float
    profit_factor: float
    total_pnl: float
    total_return_pct: float
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    avg_drawdown_pct: float
    calmar_ratio: float
    avg_win_pct: float
    avg_loss_pct: float
    avg_hold_bars: float
    best_trade_pct: float
    worst_trade_pct: float
    win_rate_first_half: float
    win_rate_second_half: float
    pnl_first_half: float
    pnl_second_half: float
    regime_stability: float
    start_date: str
    end_date: str
    total_bars: int
    timeframe: str


class BacktestEngine:
    """Run signal-based strategy tests."""

    def __init__(
        self,
        initial_capital: float = 25_000.0,
        commission_pct: float = 0.0,
        sec_fee_per_dollar: float = 0.0000278,
        slippage_pct: float = 0.05,
        max_position_pct: float = 0.10,
        max_positions: int = 10,
    ):
        self.initial_capital = float(initial_capital or 25_000.0)
        self.commission_pct = float(commission_pct or 0.0)
        self.sec_fee_per_dollar = float(sec_fee_per_dollar or 0.0)
        self.slippage_pct = float(slippage_pct or 0.0)
        self.max_position_pct = float(max_position_pct or 0.10)
        self.max_positions = int(max_positions or 10)

    def run_single(self, price_data: pd.DataFrame, signal: IndicatorSignal, symbol: str) -> BacktestResult:
        df = price_data.copy()
        df = df[["close"]].copy() if "close" in df.columns else df.copy()
        close = pd.Series(df["close"], index=df.index, dtype=float).fillna(method="ffill").fillna(method="bfill")
        entries = pd.Series(signal.entries, index=close.index).fillna(False).astype(bool)
        exits = pd.Series(signal.exits, index=close.index).fillna(False).astype(bool)

        if vbt is not None and signal.side != "short":  # pragma: no cover - optional fast path
            try:
                portfolio = vbt.Portfolio.from_signals(
                    close,
                    entries=entries,
                    exits=exits,
                    init_cash=self.initial_capital,
                    size=self.max_position_pct,
                    size_type="valuepercent",
                    fees=self.commission_pct / 100.0,
                    slippage=self.slippage_pct / 100.0,
                )
                trades = self._trades_from_vectorbt(portfolio)
                equity_curve = portfolio.value()
                return self._build_result(symbol, signal, close, trades, equity_curve)
            except Exception:
                pass

        trades, equity_curve = self._simulate(close, entries, exits, signal.side)
        return self._build_result(symbol, signal, close, trades, equity_curve)

    def run_parameter_sweep(self, price_data: pd.DataFrame, indicator: BaseIndicator, symbol: str) -> List[BacktestResult]:
        results = []
        for params in indicator.param_grid():
            instance = indicator.__class__(**params)
            results.append(self.run_single(price_data, instance.generate_signals(price_data), symbol))
        return results

    def run_batch(self, universe: Dict[str, pd.DataFrame], indicators: List[BaseIndicator]) -> List[BacktestResult]:
        results: List[BacktestResult] = []
        for symbol, price_data in (universe or {}).items():
            if price_data is None or price_data.empty:
                continue
            for indicator in indicators or []:
                results.extend(self.run_parameter_sweep(price_data, indicator, symbol))
        return results

    def _simulate(self, close: pd.Series, entries: pd.Series, exits: pd.Series, side: str) -> Tuple[List[Dict], pd.Series]:
        side = str(side or "long").lower()
        active = False
        entry_idx = None
        entry_price = 0.0
        entry_equity = self.initial_capital
        equity = self.initial_capital
        trades: List[Dict] = []
        curve = []

        for i, (ts, price) in enumerate(close.items()):
            price = float(price or 0.0)
            if price <= 0:
                curve.append(equity)
                continue

            is_entry = bool(entries.iloc[i])
            is_exit = bool(exits.iloc[i])

            if not active and is_entry:
                active = True
                entry_idx = i
                entry_equity = equity
                entry_price = price * (1.0 + self.slippage_pct / 100.0) if side != "short" else price * (1.0 - self.slippage_pct / 100.0)
            elif active and (is_exit or i == len(close) - 1):
                exit_price = price * (1.0 - self.slippage_pct / 100.0) if side != "short" else price * (1.0 + self.slippage_pct / 100.0)
                notional = entry_equity * self.max_position_pct
                if side == "short":
                    gross_return = (entry_price - exit_price) / max(entry_price, 1e-9)
                    sell_notional = notional
                else:
                    gross_return = (exit_price - entry_price) / max(entry_price, 1e-9)
                    sell_notional = notional * max(exit_price, 0.0) / max(entry_price, 1e-9)
                fees = ((self.commission_pct / 100.0) * (notional + sell_notional)) + (self.sec_fee_per_dollar * max(sell_notional, 0.0))
                pnl = (gross_return * notional) - fees
                equity += pnl
                trades.append(
                    {
                        "entry_bar": int(entry_idx or 0),
                        "exit_bar": i,
                        "entry_time": close.index[int(entry_idx or 0)],
                        "exit_time": ts,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl": pnl,
                        "return_pct": gross_return * 100.0,
                        "hold_bars": i - int(entry_idx or 0),
                    }
                )
                active = False
                entry_idx = None
                entry_price = 0.0
            curve.append(equity)

        equity_curve = pd.Series(curve, index=close.index, dtype=float)
        return trades, equity_curve

    def _build_result(self, symbol: str, signal: IndicatorSignal, close: pd.Series, trades: List[Dict], equity_curve: pd.Series) -> BacktestResult:
        returns = np.array([float(t["return_pct"]) / 100.0 for t in trades], dtype=float)
        pnl_values = np.array([float(t["pnl"]) for t in trades], dtype=float)
        wins = returns[returns > 0]
        losses = returns[returns <= 0]
        total_trades = len(trades)
        win_rate = float(len(wins) / total_trades) if total_trades else 0.0
        gross_profit = float(pnl_values[pnl_values > 0].sum()) if total_trades else 0.0
        gross_loss = float(abs(pnl_values[pnl_values < 0].sum())) if total_trades else 0.0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else (gross_profit if gross_profit > 0 else 0.0)
        total_pnl = float(pnl_values.sum()) if total_trades else 0.0
        total_return_pct = ((float(equity_curve.iloc[-1]) - self.initial_capital) / self.initial_capital * 100.0) if len(equity_curve) else 0.0

        avg_hold_bars = float(np.mean([t["hold_bars"] for t in trades])) if trades else 0.0
        avg_win_pct = float(np.mean(wins) * 100.0) if len(wins) else 0.0
        avg_loss_pct = float(np.mean(losses) * 100.0) if len(losses) else 0.0
        best_trade_pct = float(np.max(returns) * 100.0) if len(returns) else 0.0
        worst_trade_pct = float(np.min(returns) * 100.0) if len(returns) else 0.0

        sharpe_ratio = self._sharpe(returns)
        sortino_ratio = self._sortino(returns)
        drawdown_series = self._drawdowns(equity_curve)
        max_drawdown_pct = float(abs(drawdown_series.min()) * 100.0) if len(drawdown_series) else 0.0
        avg_drawdown_pct = float(abs(drawdown_series[drawdown_series < 0].mean()) * 100.0) if (drawdown_series < 0).any() else 0.0
        annual_return = self._annualized_return(total_return_pct / 100.0, close)
        calmar_ratio = annual_return / (max_drawdown_pct / 100.0) if max_drawdown_pct > 0 else annual_return

        midpoint = len(close) // 2
        first_half = [t for t in trades if int(t["exit_bar"]) < midpoint]
        second_half = [t for t in trades if int(t["exit_bar"]) >= midpoint]
        win_rate_first_half, pnl_first_half = self._half_metrics(first_half)
        win_rate_second_half, pnl_second_half = self._half_metrics(second_half)
        regime_stability = self._regime_stability(win_rate_first_half, win_rate_second_half, pnl_first_half, pnl_second_half)

        return BacktestResult(
            symbol=symbol,
            indicator_name=signal.name,
            params=dict(signal.params or {}),
            side=signal.side,
            total_trades=total_trades,
            win_rate=round(win_rate, 4),
            profit_factor=round(float(profit_factor), 4),
            total_pnl=round(total_pnl, 2),
            total_return_pct=round(total_return_pct, 2),
            sharpe_ratio=round(sharpe_ratio, 4),
            sortino_ratio=round(sortino_ratio, 4),
            max_drawdown_pct=round(max_drawdown_pct, 2),
            avg_drawdown_pct=round(avg_drawdown_pct, 2),
            calmar_ratio=round(float(calmar_ratio), 4),
            avg_win_pct=round(avg_win_pct, 2),
            avg_loss_pct=round(avg_loss_pct, 2),
            avg_hold_bars=round(avg_hold_bars, 2),
            best_trade_pct=round(best_trade_pct, 2),
            worst_trade_pct=round(worst_trade_pct, 2),
            win_rate_first_half=round(win_rate_first_half, 4),
            win_rate_second_half=round(win_rate_second_half, 4),
            pnl_first_half=round(pnl_first_half, 2),
            pnl_second_half=round(pnl_second_half, 2),
            regime_stability=round(regime_stability, 4),
            start_date=str(close.index.min()),
            end_date=str(close.index.max()),
            total_bars=int(len(close)),
            timeframe=self._infer_timeframe(close.index),
        )

    @staticmethod
    def _trades_from_vectorbt(portfolio) -> List[Dict]:  # pragma: no cover - optional dependency
        records = []
        for row in portfolio.trades.records_readable.to_dict("records"):
            records.append(
                {
                    "entry_bar": int(row.get("Entry Index", 0) or 0),
                    "exit_bar": int(row.get("Exit Index", 0) or 0),
                    "entry_time": row.get("Entry Timestamp"),
                    "exit_time": row.get("Exit Timestamp"),
                    "entry_price": float(row.get("Avg Entry Price", 0) or 0),
                    "exit_price": float(row.get("Avg Exit Price", 0) or 0),
                    "pnl": float(row.get("PnL", 0) or 0),
                    "return_pct": float(row.get("Return", 0) or 0) * 100.0,
                    "hold_bars": int(row.get("Exit Index", 0) or 0) - int(row.get("Entry Index", 0) or 0),
                }
            )
        return records

    @staticmethod
    def _drawdowns(equity_curve: pd.Series) -> pd.Series:
        running_max = equity_curve.cummax()
        return (equity_curve / running_max) - 1.0

    @staticmethod
    def _sharpe(returns: np.ndarray, risk_free_rate: float = 0.0) -> float:
        if returns.size < 2:
            return 0.0
        std = float(np.std(returns, ddof=1))
        if std <= 0:
            return 0.0
        mean = float(np.mean(returns))
        return ((mean * 252.0) - risk_free_rate) / (std * math.sqrt(252.0))

    @staticmethod
    def _sortino(returns: np.ndarray, risk_free_rate: float = 0.0) -> float:
        if returns.size < 2:
            return 0.0
        downside = returns[returns < 0]
        if downside.size == 0:
            return 0.0
        downside_std = float(np.std(downside, ddof=1)) if downside.size > 1 else abs(float(downside[0]))
        if downside_std <= 0:
            return 0.0
        mean = float(np.mean(returns))
        return ((mean * 252.0) - risk_free_rate) / (downside_std * math.sqrt(252.0))

    @staticmethod
    def _annualized_return(total_return: float, close: pd.Series) -> float:
        if close.empty:
            return 0.0
        periods = max(1.0, float(len(close)))
        return (1.0 + float(total_return or 0.0)) ** (252.0 / periods) - 1.0

    @staticmethod
    def _half_metrics(trades: List[Dict]) -> Tuple[float, float]:
        if not trades:
            return 0.0, 0.0
        wins = sum(1 for trade in trades if float(trade.get("pnl", 0) or 0) > 0)
        pnl = sum(float(trade.get("pnl", 0) or 0) for trade in trades)
        return wins / len(trades), pnl

    @staticmethod
    def _regime_stability(win_rate_first: float, win_rate_second: float, pnl_first: float, pnl_second: float) -> float:
        stability = max(0.0, 1.0 - abs(float(win_rate_first or 0.0) - float(win_rate_second or 0.0)))
        if pnl_first < 0 < pnl_second or pnl_second < 0 < pnl_first:
            stability *= 0.75
        return stability

    @staticmethod
    def _infer_timeframe(index: pd.Index) -> str:
        if len(index) < 2:
            return "unknown"
        try:
            delta = pd.Series(index).sort_values().diff().dropna().median()
        except Exception:
            return "unknown"
        if delta is pd.NaT:
            return "unknown"
        seconds = float(delta.total_seconds())
        if seconds <= 60:
            return "1min"
        if seconds <= 300:
            return "5min"
        if seconds <= 900:
            return "15min"
        if seconds <= 3600:
            return "1hour"
        return "1day"
