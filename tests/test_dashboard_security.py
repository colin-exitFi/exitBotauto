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
                {
                    "get_usage_stats": lambda self: {
                        "daily_request_count": 21,
                        "minute_remaining": 118,
                        "budget_mode": "normal",
                        "last_request_path": "/api/market/market-tide",
                    }
                },
            )()
            unusual_whales_stream = None
            scanner = type(
                "ScannerStub",
                (),
                {
                    "get_cached_candidates": lambda self: [
                        {
                            "symbol": "NVDA",
                            "uw_budget_mode": "normal",
                            "uw_news_summary": "2 major UW headlines; bias bullish",
                            "uw_chain_summary": "chain bias bullish; calls dominate",
                        }
                    ]
                },
            )()

        dashboard_module.set_bot(_Bot())
        try:
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"):
                client = TestClient(dashboard_module.app)
                resp = client.get("/api/intelligence?token=secret-token")
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertIn("unusual_whales_api", payload)
                self.assertEqual(payload["unusual_whales_api"]["daily_request_count"], 21)
                self.assertEqual(payload["unusual_whales_api"]["budget_mode"], "normal")
                self.assertIn("unusual_whales_focus", payload)
                self.assertEqual(payload["unusual_whales_focus"][0]["symbol"], "NVDA")
        finally:
            dashboard_module.set_bot(None)

    def test_pnl_endpoint_includes_clean_pnl_and_api_costs(self):
        class _EntryManager:
            def get_positions(self):
                return []

        class _Bot:
            pnl_state = {
                "total_realized_pnl": 25.0,
                "today_realized_pnl": 25.0,
                "starting_equity": 25000.0,
                "peak_equity": 25125.0,
                "total_trades": 2,
                "winning_trades": 1,
                "losing_trades": 1,
                "best_trade": 40.0,
                "worst_trade": -15.0,
            }
            alpaca_client = None
            entry_manager = _EntryManager()

        dashboard_module.set_bot(_Bot())
        try:
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
                 patch("src.ai.trade_history.get_analytics", return_value={
                     "overall": {"avg_signal_to_fill_ms": 220.0},
                     "today": {"raw_pnl": 18.0, "clean_pnl": 12.0, "anomaly_count": 1},
                 }), \
                 patch("src.dashboard.dashboard.get_api_cost_stats", return_value={
                     "estimated_cost_usd": 3.75,
                     "per_provider": {"claude": {"calls": 10}},
                 }):
                client = TestClient(dashboard_module.app)
                resp = client.get("/api/pnl?token=secret-token")
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertEqual(payload["clean_realized"], 12.0)
                self.assertEqual(payload["raw_realized_today"], 18.0)
                self.assertEqual(payload["today_anomaly_count"], 1)
                self.assertEqual(payload["api_cost_estimate_usd"], 3.75)
        finally:
            dashboard_module.set_bot(None)

    def test_pnl_endpoint_exposes_reconciliation_trust_flags(self):
        class _EntryManager:
            def get_positions(self):
                return []

        class _Reconciler:
            def snapshot(self):
                return {
                    "broker": {
                        "equity": 24000.0,
                        "last_equity": 25000.0,
                        "day_pnl": -1000.0,
                        "day_pnl_pct": -4.0,
                        "cash": 23000.0,
                        "current_open_unrealized": -50.0,
                    },
                    "internal": {
                        "game_film_realized": 120.0,
                        "trade_history_win_rate_pct": 70.0,
                    },
                    "reconciliation": {
                        "status": "critical_mismatch",
                        "severity": "critical",
                        "broker_vs_pnl_state_diff": -500.0,
                        "reasons": ["broker_truth_canary_triggered"],
                    },
                    "canaries": [
                        {
                            "code": "realized_pnl_mismatch",
                            "severity": "critical",
                            "first_seen": 1.0,
                            "current_magnitude": 500.0,
                            "recommended_action": "Rebuild from broker fills.",
                        }
                    ],
                    "trust": {
                        "broker_only_mode": True,
                        "internal_analytics_trusted": False,
                        "internal_analytics_degraded": True,
                        "show_internal_stats": False,
                    },
                }

        class _Bot:
            pnl_state = {
                "total_realized_pnl": 25.0,
                "today_realized_pnl": 25.0,
                "starting_equity": 25000.0,
                "peak_equity": 25125.0,
                "total_trades": 2,
                "winning_trades": 1,
                "losing_trades": 1,
                "best_trade": 40.0,
                "worst_trade": -15.0,
            }
            alpaca_client = None
            entry_manager = _EntryManager()
            reconciler = _Reconciler()

        dashboard_module.set_bot(_Bot())
        try:
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
                 patch("src.ai.trade_history.get_analytics", return_value={
                     "overall": {"avg_signal_to_fill_ms": 220.0},
                     "today": {"raw_pnl": 18.0, "clean_pnl": 12.0, "anomaly_count": 1},
                 }), \
                 patch("src.dashboard.dashboard.get_api_cost_stats", return_value={"estimated_cost_usd": 0.0}):
                client = TestClient(dashboard_module.app)
                resp = client.get("/api/pnl?token=secret-token")
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertEqual(payload["reconciliation_status"], "critical_mismatch")
                self.assertTrue(payload["trust_flags"]["broker_only_mode"])
                self.assertFalse(payload["trust_flags"]["show_internal_stats"])
                self.assertEqual(payload["reconciliation_canaries"][0]["code"], "realized_pnl_mismatch")
        finally:
            dashboard_module.set_bot(None)
