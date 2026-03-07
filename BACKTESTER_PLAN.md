# Velox Backtester — Codex Build Plan

## Objective

Build a backtesting integration layer using **VectorBT Pro** (or vectorbt open-source) that:
1. Pulls historical OHLCV data from Polygon.io
2. Runs custom indicator functions (translated from TradingView Pine Script) against that data
3. Simulates trades with realistic conditions (slippage, fees, position sizing)
4. Scores and ranks strategies by Sharpe ratio, profit factor, max drawdown, and edge stability
5. Outputs ranked results that feed into Velox's technical agent as validated signal sources

## Architecture

```
exitBotauto/
├── backtester/
│   ├── __init__.py
│   ├── data_loader.py          # Polygon historical data fetcher + local cache
│   ├── indicators/
│   │   ├── __init__.py
│   │   ├── registry.py         # Indicator registry + standard interface
│   │   ├── ema_crossover.py    # EMA cross (9/21, 12/26, etc.)
│   │   ├── vwap_bands.py       # VWAP + standard deviation bands
│   │   ├── rsi_divergence.py   # RSI with divergence detection
│   │   ├── macd_histogram.py   # MACD histogram momentum
│   │   ├── bollinger_squeeze.py # Bollinger Band squeeze + breakout
│   │   ├── volume_profile.py   # Volume-weighted price levels
│   │   ├── supertrend.py       # Supertrend indicator
│   │   ├── hull_ma.py          # Hull Moving Average crossover
│   │   ├── keltner_channel.py  # Keltner Channel breakout
│   │   └── stoch_rsi.py        # Stochastic RSI
│   ├── engine.py               # VectorBT backtest runner
│   ├── scorer.py               # Strategy scoring + ranking
│   ├── universe.py             # Stock universe builder (what tickers to test)
│   ├── report.py               # Generate results report (JSON + markdown)
│   └── cli.py                  # Command-line entry point
├── data/
│   └── backtest_cache/         # Cached OHLCV data (don't re-fetch)
└── tests/
    ├── test_data_loader.py
    ├── test_indicators.py
    ├── test_engine.py
    └── test_scorer.py
```

## Dependencies

Add to `requirements.txt`:
```
vectorbt>=0.26.0
```

We already have in the project: `numpy`, `pandas`, `httpx` (for Polygon API).

VectorBT pulls in: `numba`, `plotly`, `scipy` — all fine.

## Component Specs

### 1. `data_loader.py` — Historical Data from Polygon

```python
"""
Fetch and cache OHLCV minute/daily bars from Polygon.io.
Cache locally to data/backtest_cache/{symbol}_{timeframe}_{start}_{end}.parquet
so we never re-fetch the same data twice.
"""
```

**Interface:**
```python
class DataLoader:
    def __init__(self, polygon_api_key: str, cache_dir: str = "data/backtest_cache"):
        ...

    def get_bars(
        self,
        symbol: str,
        timeframe: str,        # "1min", "5min", "15min", "1hour", "1day"
        start: str,            # "2024-01-01"
        end: str,              # "2026-03-07"
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """
        Returns DataFrame with columns: open, high, low, close, volume, vwap
        Index: DatetimeIndex (UTC)
        
        Checks cache first. If cache miss, fetches from Polygon REST API:
        GET /v2/aggs/ticker/{symbol}/range/{multiplier}/{timespan}/{from}/{to}
        
        Polygon free tier: 5 calls/min. Implement rate limiting with sleep.
        Polygon aggregates endpoint returns max 50,000 bars per request.
        For minute data over long periods, paginate by month.
        """
        ...

    def get_universe_bars(
        self,
        symbols: List[str],
        timeframe: str,
        start: str,
        end: str,
    ) -> Dict[str, pd.DataFrame]:
        """Fetch bars for multiple symbols. Rate-limited, cached."""
        ...
```

**Polygon API details:**
- Base URL: `https://api.polygon.io`
- Endpoint: `/v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{from}/{to}`
- Auth: `apiKey` query parameter
- Key is in `config/settings.py` as `POLYGON_API_KEY`
- Free tier: 5 API calls/min, 2 years of history, minute bars available
- Response shape: `{"results": [{"o": open, "h": high, "l": low, "c": close, "v": volume, "vw": vwap, "t": timestamp_ms}]}`
- Pagination: if `next_url` is present in response, follow it

**Caching:**
- Use parquet format (fast, columnar, small)
- Cache key: `{symbol}_{timeframe}_{start}_{end}.parquet`
- On cache hit, read from disk. On miss, fetch + save.
- `force_refresh=True` bypasses cache.

### 2. `indicators/registry.py` — Standard Indicator Interface

Every indicator must implement this interface:

```python
from abc import ABC, abstractmethod
import pandas as pd
from dataclasses import dataclass
from typing import List, Dict, Any

@dataclass
class IndicatorSignal:
    """Standardized output from every indicator."""
    entries: pd.Series       # Boolean series: True where indicator says BUY
    exits: pd.Series         # Boolean series: True where indicator says SELL
    signal_strength: pd.Series  # Float 0-1: confidence of each signal
    side: str                # "long", "short", or "both"
    name: str                # Human-readable name
    params: Dict[str, Any]   # Parameters used

class BaseIndicator(ABC):
    """All indicators inherit from this."""
    
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this indicator."""
        ...

    @abstractmethod
    def generate_signals(self, df: pd.DataFrame) -> IndicatorSignal:
        """
        Input: OHLCV DataFrame (columns: open, high, low, close, volume, vwap)
        Output: IndicatorSignal with entry/exit boolean series
        """
        ...

    @abstractmethod
    def param_grid(self) -> List[Dict[str, Any]]:
        """
        Return list of parameter combinations to test.
        Example: [{"fast": 9, "slow": 21}, {"fast": 12, "slow": 26}]
        Used by the engine to sweep parameter space.
        """
        ...


class IndicatorRegistry:
    """Discover and instantiate all indicators."""
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
```

### 3. Indicator Implementations (10 Starter Indicators)

Each indicator file follows the same pattern. Here are the specs:

**`ema_crossover.py`** — EMA Cross
- Fast EMA crosses above slow EMA → BUY
- Fast EMA crosses below slow EMA → SELL  
- Param grid: `[(9,21), (12,26), (5,13), (8,21), (20,50)]`
- Signal strength: distance between EMAs as % of price

**`vwap_bands.py`** — VWAP Standard Deviation Bands
- Price crosses above VWAP + 1σ → BUY (momentum breakout)
- Price crosses below VWAP - 1σ → SELL
- Param grid: `[1.0σ, 1.5σ, 2.0σ]` band widths
- Requires intraday data (VWAP resets daily)

**`rsi_divergence.py`** — RSI with Divergence
- RSI crosses above 30 from below → BUY (oversold bounce)
- RSI crosses below 70 from above → SELL (overbought fade)
- Divergence: price makes new low but RSI doesn't → strong BUY
- Param grid: `[period: 7, 14, 21]`

**`macd_histogram.py`** — MACD Histogram Momentum
- Histogram goes positive (crosses zero line) → BUY
- Histogram goes negative → SELL
- Param grid: `[(12,26,9), (8,17,9), (5,13,6)]` (fast, slow, signal)

**`bollinger_squeeze.py`** — Bollinger Band Squeeze
- Bandwidth contracts to < 4% → squeeze detected
- Price breaks above upper band after squeeze → BUY
- Price breaks below lower band after squeeze → SELL
- Param grid: `[period: 14, 20, 30]` × `[std: 1.5, 2.0, 2.5]`

**`volume_profile.py`** — Volume-Weighted Price Levels
- Price breaks above high-volume node with increasing volume → BUY
- Price breaks below with volume → SELL
- Param grid: `[lookback: 20, 50, 100 bars]`

**`supertrend.py`** — Supertrend
- Classic ATR-based trend indicator
- Supertrend flips from above to below price → BUY
- Flips from below to above → SELL
- Param grid: `[period: 7, 10, 14]` × `[multiplier: 2.0, 3.0, 4.0]`

**`hull_ma.py`** — Hull Moving Average
- HMA direction changes from down to up → BUY
- Direction changes from up to down → SELL
- Param grid: `[period: 9, 16, 25, 49]`

**`keltner_channel.py`** — Keltner Channel Breakout
- Price closes above upper channel → BUY
- Price closes below lower channel → SELL
- Param grid: `[ema: 20, 30]` × `[atr_mult: 1.5, 2.0, 2.5]` × `[atr_period: 10, 14, 20]`

**`stoch_rsi.py`** — Stochastic RSI
- StochRSI crosses above 20 → BUY
- StochRSI crosses below 80 → SELL
- Param grid: `[rsi_period: 14]` × `[stoch_period: 14]` × `[k_smooth: 3]` × `[d_smooth: 3]`

### 4. `engine.py` — VectorBT Backtest Runner

```python
"""
Core backtest engine. Takes indicator signals + price data,
runs vectorbt simulation with realistic execution modeling.
"""

class BacktestEngine:
    def __init__(
        self,
        initial_capital: float = 25_000.0,
        commission_pct: float = 0.0,       # Alpaca: zero commission on stocks
        sec_fee_per_dollar: float = 0.0000278,  # SEC fee: $27.80 per $1M sold
        slippage_pct: float = 0.05,        # 5 bps average slippage
        max_position_pct: float = 0.10,    # Max 10% of portfolio per position
        max_positions: int = 10,
    ):
        ...

    def run_single(
        self,
        price_data: pd.DataFrame,
        signal: IndicatorSignal,
        symbol: str,
    ) -> BacktestResult:
        """
        Run one indicator on one symbol.
        
        Uses vectorbt.Portfolio.from_signals():
        - entries = signal.entries
        - exits = signal.exits
        - size = max_position_pct of portfolio
        - fees = commission + SEC fee
        - slippage = slippage_pct
        - freq = inferred from price_data index
        
        Returns BacktestResult with all metrics.
        """
        ...

    def run_batch(
        self,
        universe: Dict[str, pd.DataFrame],  # symbol -> OHLCV
        indicators: List[BaseIndicator],
    ) -> List[BacktestResult]:
        """
        Run ALL indicators × ALL parameter combos × ALL symbols.
        VectorBT is vectorized so this is fast even with thousands of combos.
        Returns flat list of results for scoring.
        """
        ...

    def run_parameter_sweep(
        self,
        price_data: pd.DataFrame,
        indicator: BaseIndicator,
        symbol: str,
    ) -> List[BacktestResult]:
        """
        Run one indicator across all its param_grid combos on one symbol.
        Returns list of results, one per param combo.
        """
        ...


@dataclass
class BacktestResult:
    symbol: str
    indicator_name: str
    params: Dict[str, Any]
    side: str                    # long/short/both
    
    # Core metrics
    total_trades: int
    win_rate: float              # 0-1
    profit_factor: float         # gross_profit / gross_loss
    total_pnl: float
    total_return_pct: float
    
    # Risk metrics
    sharpe_ratio: float
    sortino_ratio: float
    max_drawdown_pct: float
    avg_drawdown_pct: float
    calmar_ratio: float          # annual_return / max_drawdown
    
    # Trade metrics
    avg_win_pct: float
    avg_loss_pct: float
    avg_hold_bars: float
    best_trade_pct: float
    worst_trade_pct: float
    
    # Edge stability
    win_rate_first_half: float   # Win rate on first half of data
    win_rate_second_half: float  # Win rate on second half of data
    pnl_first_half: float
    pnl_second_half: float
    regime_stability: float      # How consistent across bull/bear/chop
    
    # Metadata
    start_date: str
    end_date: str
    total_bars: int
    timeframe: str
```

### 5. `scorer.py` — Strategy Ranking

```python
"""
Score and rank backtest results.
Composite score weights multiple factors — not just returns.
"""

class StrategyScorer:
    # Weights for composite score (sum to 1.0)
    WEIGHTS = {
        "sharpe_ratio": 0.25,        # Risk-adjusted return
        "profit_factor": 0.20,       # Quality of wins vs losses
        "win_rate": 0.15,            # Consistency
        "edge_stability": 0.20,      # Does it work across time periods?
        "max_drawdown_penalty": 0.10, # Penalize deep drawdowns
        "trade_frequency": 0.10,     # Prefer active strategies (capital velocity)
    }

    def score(self, result: BacktestResult) -> float:
        """
        Composite score 0-100.
        
        - sharpe_ratio: normalize to 0-1 (cap at 3.0)
        - profit_factor: normalize to 0-1 (cap at 3.0)
        - win_rate: direct (already 0-1)
        - edge_stability: abs(win_rate_first_half - win_rate_second_half) inverted
          Lower difference = more stable = higher score
        - max_drawdown_penalty: 1 - (max_drawdown / 50%) clamped to 0-1
        - trade_frequency: normalize total_trades relative to available bars
        
        Minimum filters (result scores 0 if ANY fail):
        - total_trades >= 30 (statistical significance)
        - win_rate >= 0.40
        - profit_factor >= 1.0
        - sharpe_ratio >= 0.0
        """
        ...

    def rank(self, results: List[BacktestResult]) -> List[Dict]:
        """
        Score all results, sort descending, return ranked list with scores.
        Output: [{"rank": 1, "score": 87.3, "result": BacktestResult, ...}]
        """
        ...
```

### 6. `universe.py` — What Tickers to Test

```python
"""
Build a test universe of stocks that match Velox's trading style:
mid-cap momentum stocks that move 5-40% on high volume.
"""

class UniverseBuilder:
    def __init__(self, polygon_api_key: str):
        ...

    def build_momentum_universe(
        self,
        lookback_months: int = 12,
        min_price: float = 5.0,
        max_price: float = 500.0,
        min_avg_volume: int = 500_000,
        min_days_with_big_moves: int = 10,  # Days with >5% move in lookback
        max_symbols: int = 100,
    ) -> List[str]:
        """
        Find stocks that Velox would actually trade.
        
        Approach:
        1. Start with Polygon's stock list (or a curated mid-cap list)
        2. Filter by price range and average volume
        3. Rank by number of high-momentum days (>5% intraday range)
        4. Return top N symbols
        
        If Polygon ticker list is too expensive API-wise, use a hardcoded
        curated list of ~200 known momentum names (TSLA, NVDA, AMD, MARA,
        COIN, PLTR, SOFI, RIVN, LCID, etc.) and filter from there.
        """
        ...

    @staticmethod
    def get_default_universe() -> List[str]:
        """
        Hardcoded starter universe of 50 known momentum stocks.
        Good enough to validate indicators before building dynamic universe.
        """
        return [
            "TSLA", "NVDA", "AMD", "MARA", "COIN", "PLTR", "SOFI", "RIVN",
            "LCID", "NIO", "PLUG", "DKNG", "RBLX", "SNAP", "SQ", "SHOP",
            "ROKU", "CRWD", "NET", "DDOG", "ZS", "MDB", "SNOW", "ABNB",
            "DASH", "PINS", "U", "HOOD", "AFRM", "UPST", "PATH", "IONQ",
            "SMCI", "ARM", "CELH", "HIMS", "DUOL", "CAVA", "BIRK", "ONON",
            "MSTR", "RIOT", "CLSK", "HUT", "BITF", "CORZ", "IREN",
            "XBI", "ARKK", "QQQ",
        ]
```

### 7. `report.py` — Results Output

```python
"""
Generate ranked results as JSON + readable markdown.
"""

class ReportGenerator:
    def generate(
        self,
        ranked_results: List[Dict],
        output_dir: str = "data/backtest_results",
    ) -> Dict[str, str]:
        """
        Outputs:
        1. data/backtest_results/results.json — full machine-readable results
        2. data/backtest_results/REPORT.md — human-readable summary
        3. data/backtest_results/top_indicators.json — top 10 indicators
           formatted for Velox technical agent consumption
        
        REPORT.md format:
        
        # Backtest Results — {date}
        
        ## Top 10 Indicators by Composite Score
        
        | Rank | Indicator | Params | Score | Win Rate | Sharpe | PF | Max DD | Trades |
        |------|-----------|--------|-------|----------|--------|----|--------|--------|
        | 1    | SuperTrend | p=10,m=3 | 87.3 | 58% | 1.82 | 2.1 | -12% | 342 |
        
        ## Edge Stability Analysis
        (first half vs second half performance)
        
        ## Worst Performers (avoid these)
        
        ## Recommended Velox Integration
        (which indicators to wire into the technical agent)
        """
        ...
```

### 8. `cli.py` — Command-Line Entry Point

```python
"""
CLI for running backtests.

Usage:
    # Run all indicators on default universe, daily bars, 1 year
    python -m backtester.cli --timeframe 1day --start 2025-03-01 --end 2026-03-07

    # Run specific indicator on specific symbol
    python -m backtester.cli --indicator supertrend --symbol TSLA --timeframe 5min --start 2025-06-01

    # Run full sweep with minute data (slower, more realistic)
    python -m backtester.cli --timeframe 1min --start 2026-01-01 --end 2026-03-07 --universe momentum

    # Just score existing results
    python -m backtester.cli --score-only
"""

import argparse

def main():
    parser = argparse.ArgumentParser(description="Velox Backtester")
    parser.add_argument("--timeframe", default="1day", choices=["1min", "5min", "15min", "1hour", "1day"])
    parser.add_argument("--start", default="2025-03-01")
    parser.add_argument("--end", default="2026-03-07")
    parser.add_argument("--indicator", default=None, help="Run specific indicator only")
    parser.add_argument("--symbol", default=None, help="Run specific symbol only")
    parser.add_argument("--universe", default="default", choices=["default", "momentum"])
    parser.add_argument("--capital", type=float, default=25000.0)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--output", default="data/backtest_results")
    args = parser.parse_args()

    # 1. Build universe
    # 2. Fetch data (cached)
    # 3. Run indicators
    # 4. Score + rank
    # 5. Generate report

if __name__ == "__main__":
    main()
```

## Integration with Velox (Post-Backtest)

Once we have ranked results, the top indicators get wired into Velox:

### File: `data/backtest_results/top_indicators.json`
```json
[
    {
        "name": "supertrend",
        "params": {"period": 10, "multiplier": 3.0},
        "score": 87.3,
        "win_rate": 0.58,
        "sharpe": 1.82,
        "profit_factor": 2.1,
        "side": "long",
        "recommended_weight": 0.85
    }
]
```

### Integration point: `src/agents/technical_agent.py`

The technical agent currently computes basic technicals (RSI, MACD, EMA). After backtesting:
1. Load `top_indicators.json` on startup
2. Run the top 3-5 validated indicators on each candidate
3. Include their signals in the brief sent to the Jury
4. Weight the signals by their backtest composite score

This means the Jury sees something like:
```
VALIDATED INDICATORS:
- SuperTrend (score=87.3): BUY signal, strength=0.82
- Hull MA (score=74.1): BUY signal, strength=0.65
- Bollinger Squeeze (score=71.8): NEUTRAL, squeeze not active
```

Instead of just raw RSI/MACD/EMA values.

## Testing

### `test_data_loader.py`
- Test cache hit/miss
- Test Polygon API response parsing
- Test rate limiting (mock API, verify sleep between calls)
- Test pagination for large date ranges

### `test_indicators.py`
- Test each indicator produces valid IndicatorSignal
- Test entries/exits are boolean Series aligned with input index
- Test param_grid returns non-empty list
- Test known pattern: feed synthetic data with obvious trend, verify BUY signal fires

### `test_engine.py`
- Test single backtest returns valid BacktestResult
- Test commission/slippage math: known trade → known P&L
- Test position sizing: never exceeds max_position_pct
- Test batch run processes all indicators × symbols

### `test_scorer.py`
- Test minimum filters (< 30 trades → score 0)
- Test ranking order (higher Sharpe → higher rank)
- Test edge stability calculation

## Execution Order

1. Implement `data_loader.py` + test — need data before anything else
2. Implement `indicators/registry.py` + 3 starter indicators (ema_crossover, supertrend, rsi_divergence) + tests
3. Implement `engine.py` + test — can now run backtests
4. Implement `scorer.py` + test — can now rank results
5. Implement remaining 7 indicators
6. Implement `universe.py`, `report.py`, `cli.py`
7. Run full backtest, generate report
8. Wire top indicators into `src/agents/technical_agent.py`

## Environment

- Python 3.9+ (same as Velox)
- Virtual env: `exitBotauto/.venv/`
- Polygon API key: already in `config/settings.py` as `POLYGON_API_KEY`
- All output goes to `data/backtest_results/`
- Tests go in `tests/` alongside existing Velox tests

## Important Constraints

- **Polygon free tier rate limit: 5 calls/min.** Data fetching MUST sleep between calls. For minute data across 50 symbols over 1 year, this means ~hours of fetching on first run. Cache aggressively.
- **No look-ahead bias.** Indicators must only use data available at the time of the signal. VectorBT handles this correctly with `from_signals()` but custom indicator code must not peek forward.
- **Realistic fills.** Use `slippage=0.05%` minimum. For low-volume stocks, increase slippage proportional to volume.
- **Out-of-sample testing.** The scorer splits data into first half / second half. An indicator that only works on one half is unreliable.
- **Don't overfit.** If a parameter combo only works on one symbol, it's noise. Require that the same params work across at least 5 symbols in the universe.
