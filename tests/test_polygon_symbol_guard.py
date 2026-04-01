import unittest
from types import SimpleNamespace

from src.data.polygon_client import PolygonClient


class PolygonSymbolGuardTests(unittest.TestCase):
    def test_get_quote_skips_unsupported_symbols(self):
        client = PolygonClient()
        calls = []
        client._rest_get = lambda path, params=None: calls.append(path) or None

        self.assertIsNone(client.get_quote("RUT"))
        self.assertEqual(calls, [])

    def test_get_quote_normalizes_class_share_aliases(self):
        client = PolygonClient()
        calls = []

        def _fake_rest_get(path, params=None):
            calls.append(path)
            return {"results": {"p": 123.45, "s": 10, "t": 1}}

        client._rest_get = _fake_rest_get

        quote = client.get_quote("BRKB")

        self.assertEqual(calls, ["/v2/last/trade/BRK.B"])
        self.assertEqual(quote["symbol"], "BRK.B")
        self.assertEqual(quote["price"], 123.45)

    def test_404_symbol_is_cached_as_unsupported(self):
        client = PolygonClient()
        calls = []

        class _Resp:
            status_code = 404

            def raise_for_status(self):
                raise RuntimeError("404")

        def _fake_get(url, params=None, timeout=10):
            calls.append(url)
            return _Resp()

        client._session = SimpleNamespace(get=_fake_get)

        self.assertIsNone(client.get_quote("PBRA"))
        self.assertEqual(client.get_quote("PBRA"), None)
        self.assertIn("PBRA", client._unsupported_symbols)
        self.assertEqual(len(calls), 1)

    def test_get_bars_requests_latest_results_and_returns_chronological_order(self):
        client = PolygonClient()
        captured = {}

        def _fake_rest_get(path, params=None):
            captured["path"] = path
            captured["params"] = dict(params or {})
            return {
                "results": [
                    {"t": 300, "o": 3, "h": 3, "l": 3, "c": 3, "v": 30, "vw": 3},
                    {"t": 200, "o": 2, "h": 2, "l": 2, "c": 2, "v": 20, "vw": 2},
                    {"t": 100, "o": 1, "h": 1, "l": 1, "c": 1, "v": 10, "vw": 1},
                ]
            }

        client._rest_get = _fake_rest_get

        bars = client.get_bars("AAPL", timespan="minute", multiplier=1, limit=3)

        self.assertEqual(captured["params"]["sort"], "desc")
        self.assertEqual(captured["params"]["limit"], "3")
        self.assertEqual([row["timestamp"] for row in bars], [100, 200, 300])


if __name__ == "__main__":
    unittest.main()
