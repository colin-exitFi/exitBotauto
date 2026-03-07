import unittest
from unittest.mock import AsyncMock, patch

from src.signals.edgar import EdgarScanner


SAMPLE_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerTradingSymbol>BATL</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>SMITH JANE</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-03-06</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>12.5</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-03-06</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>250</value></transactionShares>
        <transactionPricePerShare><value>13.1</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


class EdgarForm4Tests(unittest.IsolatedAsyncioTestCase):
    def test_parse_form4_xml_extracts_directional_transactions(self):
        parsed = EdgarScanner._parse_form4_xml(SAMPLE_FORM4_XML)

        self.assertEqual(parsed["ticker"], "BATL")
        self.assertEqual(parsed["owner_name"], "SMITH JANE")
        self.assertEqual(len(parsed["transactions"]), 2)
        self.assertEqual(parsed["transactions"][0]["direction"], "buy")
        self.assertEqual(parsed["transactions"][0]["transaction_code"], "P")
        self.assertEqual(parsed["transactions"][1]["direction"], "sell")
        self.assertEqual(parsed["transactions"][1]["transaction_code"], "S")

    async def test_get_insider_trades_aggregates_open_market_signal(self):
        scanner = EdgarScanner()
        filings = [{"ticker": "BATL", "form_type": "4", "issuer_cik": "0001234567", "adsh": "0001234567-26-000001"}]
        parsed_payload = {
            "transactions": [
                {
                    "transaction_code": "P",
                    "direction": "buy",
                    "shares": 1000,
                    "value": 12_500,
                },
                {
                    "transaction_code": "A",
                    "direction": "acquire",
                    "shares": 500,
                    "value": 0,
                },
            ]
        }

        with patch.object(scanner, "_fetch_and_parse_form4", AsyncMock(return_value=parsed_payload)):
            result = await scanner.get_insider_trades("BATL", filings=filings)

        self.assertEqual(result["signal"], "bullish")
        self.assertEqual(result["open_market_buys"], 1)
        self.assertEqual(result["open_market_sells"], 0)
        self.assertGreater(result["buy_shares"], 1000)


if __name__ == "__main__":
    unittest.main()
