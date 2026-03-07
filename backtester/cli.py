"""CLI entrypoint for the Velox backtester."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from config import settings
from backtester.data_loader import DataLoader
from backtester.engine import BacktestEngine
from backtester.indicators import IndicatorRegistry
from backtester.report import ReportGenerator
from backtester.scorer import StrategyScorer
from backtester.universe import UniverseBuilder


def main():
    parser = argparse.ArgumentParser(description="Velox Backtester")
    parser.add_argument("--timeframe", default="1day", choices=["1min", "5min", "15min", "1hour", "1day"])
    parser.add_argument("--start", default="2025-03-01")
    parser.add_argument("--end", default="2026-03-07")
    parser.add_argument("--indicator", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--universe", default="default", choices=["default", "momentum"])
    parser.add_argument("--capital", type=float, default=25000.0)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--output", default="data/backtest_results")
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()

    report = ReportGenerator()
    scorer = StrategyScorer()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.score_only:
        existing = output_dir / "results.json"
        rows = json.loads(existing.read_text()) if existing.exists() else []
        print(report.generate(rows, output_dir=str(output_dir)))
        return

    universe_builder = UniverseBuilder(settings.POLYGON_API_KEY)
    if args.symbol:
        symbols = [args.symbol.upper()]
    elif args.universe == "momentum":
        symbols = universe_builder.build_momentum_universe()
    else:
        symbols = universe_builder.get_default_universe()

    loader = DataLoader(settings.POLYGON_API_KEY)
    universe = {
        symbol: loader.get_bars(symbol, args.timeframe, args.start, args.end, force_refresh=args.force_refresh)
        for symbol in symbols
    }

    indicators = IndicatorRegistry.instantiate_all()
    if args.indicator:
        indicator_cls = IndicatorRegistry.get(args.indicator)
        indicators = [indicator_cls()]

    engine = BacktestEngine(initial_capital=args.capital)
    results = engine.run_batch(universe, indicators)
    ranked = scorer.rank(results)
    paths = report.generate(ranked, output_dir=str(output_dir))
    print(paths)


if __name__ == "__main__":
    main()
