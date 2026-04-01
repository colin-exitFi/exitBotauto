import unittest

from src.data.symbols import is_supported_trade_symbol, normalize_trade_symbol


class SymbolHygieneTests(unittest.TestCase):
    def test_normalizes_common_aliases(self):
        self.assertEqual(normalize_trade_symbol("brkb"), "BRK.B")
        self.assertEqual(normalize_trade_symbol("bfb"), "BF.B")

    def test_rejects_unsupported_symbols(self):
        self.assertFalse(is_supported_trade_symbol("RUT"))
        self.assertFalse(is_supported_trade_symbol("SPXW"))
        self.assertFalse(is_supported_trade_symbol("BA-A"))

    def test_allows_tradeable_equities_and_etfs(self):
        self.assertTrue(is_supported_trade_symbol("NVDA"))
        self.assertTrue(is_supported_trade_symbol("SPY"))
        self.assertTrue(is_supported_trade_symbol("BRKB"))


if __name__ == "__main__":
    unittest.main()
