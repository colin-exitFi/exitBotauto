# Backtest Results — 2026-03-07 18:35 UTC

## Top 10 Indicators by Composite Score

| Rank | Indicator | Params | Score | Win Rate | Sharpe | PF | Max DD | Trades |
|------|-----------|--------|-------|----------|--------|----|--------|--------|
| 1 | vwap_bands | std_mult=1.0 | 83.3 | 53.4% | 3.36 | 1.73 | 2.9% | 58 |
| 2 | vwap_bands | std_mult=1.5 | 83.3 | 53.4% | 3.36 | 1.73 | 2.9% | 58 |
| 3 | vwap_bands | std_mult=2.0 | 83.3 | 53.4% | 3.36 | 1.73 | 2.9% | 58 |
| 4 | vwap_bands | std_mult=1.0 | 83.2 | 51.6% | 3.48 | 1.94 | 2.7% | 62 |
| 5 | vwap_bands | std_mult=1.5 | 83.2 | 51.6% | 3.48 | 1.94 | 2.7% | 62 |
| 6 | vwap_bands | std_mult=2.0 | 83.2 | 51.6% | 3.48 | 1.94 | 2.7% | 62 |
| 7 | vwap_bands | std_mult=1.0 | 82.4 | 53.6% | 3.14 | 1.76 | 1.6% | 69 |
| 8 | vwap_bands | std_mult=1.5 | 82.4 | 53.6% | 3.14 | 1.76 | 1.6% | 69 |
| 9 | vwap_bands | std_mult=2.0 | 82.4 | 53.6% | 3.14 | 1.76 | 1.6% | 69 |
| 10 | vwap_bands | std_mult=1.0 | 81.2 | 56.1% | 3.28 | 1.97 | 2.8% | 57 |

## Edge Stability Analysis

Strategies are ranked on risk-adjusted returns and stability across time halves.

## Recommended Velox Integration

- vwap_bands (std_mult=1.0) score=83.3 weight=0.83
- vwap_bands (std_mult=1.5) score=83.3 weight=0.83
- vwap_bands (std_mult=2.0) score=83.3 weight=0.83
- squeeze_mom_bb20_kc1.5 (bb_length=20, bb_mult=2.0, kc_length=20, kc_mult=1.5) score=0.0 weight=0.25
- wavetrend_c10_a21 (channel_length=10, avg_length=21, ob_level=53, os_level=-53) score=0.0 weight=0.25
- rsi_divergence (period=21) score=0.0 weight=0.25
- wavetrend_c14_a21 (channel_length=14, avg_length=21, ob_level=53, os_level=-53) score=0.0 weight=0.25
- mean_rev_z14_e2.0 (lookback=14, entry_z=-2.0, exit_z=0.0) score=0.0 weight=0.25
- atr_channel_e50_a20_m2.0 (ema_period=50, atr_period=20, atr_mult=2.0) score=0.0 weight=0.25
- atr_channel_e10_a10_m2.0 (ema_period=10, atr_period=10, atr_mult=2.0) score=0.0 weight=0.25
