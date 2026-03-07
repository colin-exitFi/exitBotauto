"""CLI entrypoint for the Velox backtester."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import List

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
    parser.add_argument("--symbols", default=None, help="Comma-separated symbol list")
    parser.add_argument("--universe", default="default", choices=["default", "momentum"])
    parser.add_argument("--max-symbols", type=int, default=10)
    parser.add_argument("--capital", type=float, default=25000.0)
    parser.add_argument("--score-only", action="store_true")
    parser.add_argument("--fetch-only", action="store_true")
    parser.add_argument("--list-indicators", action="store_true")
    parser.add_argument("--output", default="data/backtest_results")
    parser.add_argument("--cache-dir", default="data/backtest_cache")
    parser.add_argument("--min-symbols-profitable", type=int, default=5)
    parser.add_argument("--force-refresh", action="store_true")
    args = parser.parse_args()

    report = ReportGenerator()
    scorer = StrategyScorer(min_symbols_profitable=args.min_symbols_profitable)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.list_indicators:
        for name in sorted(IndicatorRegistry.get_all().keys()):
            print(name)
        return

    if args.score_only:
        existing = output_dir / "results.json"
        rows = json.loads(existing.read_text()) if existing.exists() else []
        print(report.generate(rows, output_dir=str(output_dir)))
        return

    universe_builder = UniverseBuilder(settings.POLYGON_API_KEY)
    symbols = _resolve_symbols(args, universe_builder)

    loader = DataLoader(settings.POLYGON_API_KEY, cache_dir=args.cache_dir)
    universe = {
        symbol: loader.get_bars(symbol, args.timeframe, args.start, args.end, force_refresh=args.force_refresh)
        for symbol in symbols
    }

    if args.fetch_only:
        manifest = {
            "timeframe": args.timeframe,
            "start": args.start,
            "end": args.end,
            "symbols": [
                {
                    "symbol": symbol,
                    "rows": int(len(df)),
                    "cache_dir": args.cache_dir,
                    "has_data": bool(df is not None and not df.empty),
                }
                for symbol, df in universe.items()
            ],
        }
        manifest_path = output_dir / "cache_manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))
        print({"cache_manifest": str(manifest_path), "symbols": len(universe)})
        return

    indicators = IndicatorRegistry.instantiate_all()
    if args.indicator:
        indicator_cls = IndicatorRegistry.get(args.indicator)
        indicators = [indicator_cls()]

    engine = BacktestEngine(initial_capital=args.capital)
    results = engine.run_batch(universe, indicators)
    ranked = scorer.rank(results)
    paths = report.generate(ranked, output_dir=str(output_dir))
    print(paths)


def _resolve_symbols(args, universe_builder: UniverseBuilder) -> List[str]:
    if args.symbol:
        return [str(args.symbol).upper()]
    if args.symbols:
        return [sym.strip().upper() for sym in str(args.symbols).split(",") if sym.strip()]
    if args.universe == "momentum":
        return universe_builder.build_momentum_universe(max_symbols=args.max_symbols)
    return universe_builder.get_default_universe()[: max(1, int(args.max_symbols or 1))]


if __name__ == "__main__":
    main()
