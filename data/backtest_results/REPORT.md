# Backtest Results — 2026-03-07 18:18 UTC

## Top 10 Indicators by Composite Score

| Rank | Indicator | Params | Score | Win Rate | Sharpe | PF | Max DD | Trades |
|------|-----------|--------|-------|----------|--------|----|--------|--------|
| 1 | vwap_bands | std_mult=1.0 | 59.3 | 51.7% | 0.93 | 1.18 | 3.7% | 60 |
| 2 | vwap_bands | std_mult=1.5 | 59.3 | 51.7% | 0.93 | 1.18 | 3.7% | 60 |
| 3 | vwap_bands | std_mult=2.0 | 59.3 | 51.7% | 0.93 | 1.18 | 3.7% | 60 |
| 4 | vwap_bands | std_mult=1.0 | 54.7 | 51.6% | 0.82 | 1.14 | 3.0% | 64 |
| 5 | vwap_bands | std_mult=1.5 | 54.7 | 51.6% | 0.82 | 1.14 | 3.0% | 64 |
| 6 | vwap_bands | std_mult=2.0 | 54.7 | 51.6% | 0.82 | 1.14 | 3.0% | 64 |
| 7 | vwap_bands | std_mult=1.0 | 52.9 | 42.9% | 0.61 | 1.11 | 3.6% | 63 |
| 8 | vwap_bands | std_mult=1.5 | 52.9 | 42.9% | 0.61 | 1.11 | 3.6% | 63 |
| 9 | vwap_bands | std_mult=2.0 | 52.9 | 42.9% | 0.61 | 1.11 | 3.6% | 63 |
| 10 | vwap_bands | std_mult=1.0 | 48.5 | 45.3% | 0.22 | 1.03 | 3.1% | 64 |

## Edge Stability Analysis

Strategies are ranked on risk-adjusted returns and stability across time halves.

## Recommended Velox Integration

- vwap_bands (std_mult=1.0) score=59.3 weight=0.59
- vwap_bands (std_mult=1.5) score=59.3 weight=0.59
- vwap_bands (std_mult=2.0) score=59.3 weight=0.59
- rsi_divergence (period=21) score=0.0 weight=0.25
- wavetrend_c14_a21 (channel_length=14, avg_length=21, ob_level=53, os_level=-53) score=0.0 weight=0.25
- rsi_divergence (period=14) score=0.0 weight=0.25
- wavetrend_c9_a12 (channel_length=9, avg_length=12, ob_level=53, os_level=-53) score=0.0 weight=0.25
- keltner_channel (ema_period=20, atr_mult=2.0, atr_period=20) score=0.0 weight=0.25
- rsi_divergence (period=7) score=0.0 weight=0.25
- smi_k14_d3 (k_length=14, d_length=3, smooth_length=3, ob=40, os=-40) score=0.0 weight=0.25
