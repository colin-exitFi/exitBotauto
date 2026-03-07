# Backtester Quickstart

Use this when you want to warm the Polygon cache first, then run actual ranking passes.

## 1. Install dependencies

```bash
source .venv/bin/activate
pip install -r requirements.txt
```

`vectorbt` is optional at runtime. If it is installed, the backtester uses it. If not, Velox falls back to the internal simulator.

## 2. Warm the cache first

Start small. Polygon minute-history requests are rate-limited.

Daily bars for a few names:

```bash
python -m backtester.cli \
  --fetch-only \
  --symbols TSLA,NVDA,PLTR,HOOD \
  --timeframe 1day \
  --start 2025-01-01 \
  --end 2026-03-07
```

Minute bars for one symbol:

```bash
python -m backtester.cli \
  --fetch-only \
  --symbol TSLA \
  --timeframe 5min \
  --start 2026-01-01 \
  --end 2026-03-07
```

This writes cache files into `data/backtest_cache/` and a manifest into `data/backtest_results/cache_manifest.json`.

## 3. See what indicators are available

```bash
python -m backtester.cli --list-indicators
```

## 4. Run a small backtest first

Single symbol, single indicator:

```bash
python -m backtester.cli \
  --symbol TSLA \
  --indicator supertrend \
  --timeframe 1day \
  --start 2025-01-01 \
  --end 2026-03-07
```

Small multi-symbol ranking pass:

```bash
python -m backtester.cli \
  --symbols TSLA,NVDA,PLTR,HOOD,COIN \
  --timeframe 1day \
  --start 2025-01-01 \
  --end 2026-03-07 \
  --min-symbols-profitable 3
```

This writes:

- `data/backtest_results/results.json`
- `data/backtest_results/REPORT.md`
- `data/backtest_results/top_indicators.json`

## 5. Score previously generated results again

```bash
python -m backtester.cli --score-only
```

## 6. Practical first run

If you want the least painful first pass:

1. Warm `1day` bars for 5-10 symbols.
2. Run a daily-bar ranking pass.
3. Inspect `top_indicators.json`.
4. Only then try `5min` on a smaller set.

## Notes

- `--force-refresh` bypasses the cache.
- `--max-symbols` limits the default or momentum universe size.
- Minute data over long windows will be slow on Polygon free/basic plans. Warm cache overnight if you want broad minute-bar tests.
