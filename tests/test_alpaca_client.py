import unittest
from unittest.mock import patch

from src.broker.alpaca_client import AlpacaClient


class _FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")


class AlpacaClientActivityTests(unittest.TestCase):
    def setUp(self):
        self.client = AlpacaClient()
        self.client._initialized = True

    def test_get_account_activities_paginates_until_short_page(self):
        calls = []
        page_one = [
            {"id": "page-1-a", "symbol": "SOXL"},
            {"id": "page-1-b", "symbol": "QNTM"},
        ]
        page_two = [
            {"id": "page-2-a", "symbol": "CVX"},
        ]

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(dict(params or {}))
            if params.get("page_token") == "page-1-b":
                return _FakeResponse(200, page_two)
            return _FakeResponse(200, page_one)

        with patch("src.broker.alpaca_client.requests.get", side_effect=fake_get):
            rows = self.client.get_account_activities(date="2026-03-30", page_size=2)

        self.assertEqual([row.get("id") for row in rows], ["page-1-a", "page-1-b", "page-2-a"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["date"], "2026-03-30")
        self.assertNotIn("page_token", calls[0])
        self.assertEqual(calls[1]["page_token"], "page-1-b")

    def test_get_account_activities_cache_is_scoped_by_date(self):
        calls = []

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(dict(params or {}))
            date = params.get("date")
            return _FakeResponse(200, [{"id": f"{date}-1", "symbol": date}])

        with patch("src.broker.alpaca_client.requests.get", side_effect=fake_get):
            day_one = self.client.get_account_activities(date="2026-03-30", page_size=100)
            day_two = self.client.get_account_activities(date="2026-03-31", page_size=100)

        self.assertEqual(day_one[0]["id"], "2026-03-30-1")
        self.assertEqual(day_two[0]["id"], "2026-03-31-1")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["date"], "2026-03-30")
        self.assertEqual(calls[1]["date"], "2026-03-31")
