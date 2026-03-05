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
