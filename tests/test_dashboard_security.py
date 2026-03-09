import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.dashboard import dashboard as dashboard_module


class DashboardSecurityTests(unittest.TestCase):
    def test_docs_redoc_and_openapi_require_token(self):
        with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"):
            client = TestClient(dashboard_module.app)

            self.assertEqual(client.get("/docs").status_code, 401)
            self.assertEqual(client.get("/docs/oauth2-redirect").status_code, 401)
            self.assertEqual(client.get("/redoc").status_code, 401)
            self.assertEqual(client.get("/openapi.json").status_code, 401)

            self.assertEqual(client.get("/docs?token=secret-token").status_code, 200)
            self.assertEqual(client.get("/redoc?token=secret-token").status_code, 200)
            self.assertEqual(client.get("/openapi.json?token=secret-token").status_code, 200)

    def test_streams_endpoint_includes_unusual_whales_stats(self):
        class _Bot:
            market_stream = None
            trade_stream = None
            unusual_whales_stream = type(
                "UWStreamStub",
                (),
                {"get_stats": lambda self: {"connected": True, "mode": "auto", "recent_flow_count": 3}},
            )()

        dashboard_module.set_bot(_Bot())
        try:
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"):
                client = TestClient(dashboard_module.app)
                resp = client.get("/api/streams?token=secret-token")
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertIn("unusual_whales", payload)
                self.assertTrue(payload["unusual_whales"]["connected"])
        finally:
            dashboard_module.set_bot(None)

    def test_intelligence_endpoint_includes_unusual_whales_api_usage(self):
        class _Bot:
            unusual_whales = type(
                "UWClientStub",
                (),
                {"get_usage_stats": lambda self: {"daily_request_count": 21, "minute_remaining": 118}},
            )()
            unusual_whales_stream = None

        dashboard_module.set_bot(_Bot())
        try:
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"):
                client = TestClient(dashboard_module.app)
                resp = client.get("/api/intelligence?token=secret-token")
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertIn("unusual_whales_api", payload)
                self.assertEqual(payload["unusual_whales_api"]["daily_request_count"], 21)
        finally:
            dashboard_module.set_bot(None)
