import unittest

from src.data.technicals import compute_technicals, get_cached_rsi, _TECHNICALS_CACHE


class FakePolygon:
    def __init__(self, bars):
        self._bars = bars
        self.calls = 0

    def get_bars(self, symbol: str, timespan="minute", multiplier=5, limit=30):
        self.calls += 1
        return list(self._bars)


def _bars(n=30, start=100.0, step=0.5):
    out = []
    price = start
    for i in range(n):
        close = price + (i * step)
        out.append(
            {
                "open": close - 0.3,
                "high": close + 0.7,
                "low": close - 0.8,
                "close": close,
                "volume": 1000 + (i * 20),
            }
        )
    return out


class TechnicalsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _TECHNICALS_CACHE.clear()

    async def test_compute_technicals_snapshot_range_and_ema_signal(self):
        polygon = FakePolygon(_bars())
        result = await compute_technicals(
            symbol="AAPL",
            price=110.0,
            polygon_client=polygon,
            snapshot={"day_high": 120.0, "day_low": 100.0},
        )
        self.assertEqual(polygon.calls, 1)
        self.assertIn("rsi_14", result)
        self.assertIn("rolling_vwap", result)
        self.assertIn("ema_9", result)
        self.assertIn("ema_20", result)
        self.assertEqual(result.get("range_pct"), 50.0)
        self.assertEqual(result.get("day_high"), 120.0)
        self.assertEqual(result.get("day_low"), 100.0)
        self.assertEqual(result.get("ema_signal"), "bullish")
        self.assertGreater(result.get("vol_accel", 0), 0)

    async def test_compute_technicals_returns_empty_when_bars_missing(self):
        polygon = FakePolygon([])
        result = await compute_technicals("AAPL", 100.0, polygon_client=polygon)
        self.assertEqual(result, {})

    async def test_technicals_cache_reuses_previous_compute_and_exposes_cached_rsi(self):
        polygon = FakePolygon(_bars())
        symbol = "MSFT"
        first = await compute_technicals(symbol=symbol, price=110.0, polygon_client=polygon)
        second = await compute_technicals(symbol=symbol, price=110.0, polygon_client=polygon)
        self.assertEqual(polygon.calls, 1)
        self.assertEqual(first, second)
        cached_rsi = get_cached_rsi(symbol)
        self.assertIsInstance(cached_rsi, float)

    async def test_day_range_falls_back_to_bar_window_without_snapshot(self):
        bars = _bars(n=30, start=50.0, step=1.0)
        polygon = FakePolygon(bars)
        result = await compute_technicals("NVDA", 60.0, polygon_client=polygon, snapshot=None)
        highs = [b["high"] for b in bars]
        lows = [b["low"] for b in bars]
        self.assertAlmostEqual(result.get("day_high"), max(highs), places=4)
        self.assertAlmostEqual(result.get("day_low"), min(lows), places=4)
        self.assertIsNotNone(result.get("range_pct"))


if __name__ == "__main__":
    unittest.main()
