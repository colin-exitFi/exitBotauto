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
                with patch("src.ai.trade_history.get_analytics", return_value={
                    "unusual_whales": {
                        "overall": {"trades": 3, "pnl": 12.5},
                        "stream_assisted": {"trades": 1, "pnl": 4.0},
                    }
                }):
                    resp = client.get("/api/intelligence?token=secret-token")
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertIn("unusual_whales_api", payload)
                self.assertEqual(payload["unusual_whales_api"]["daily_request_count"], 21)
                self.assertEqual(payload["unusual_whales_api"]["budget_mode"], "normal")
                self.assertIn("unusual_whales_focus", payload)
                self.assertEqual(payload["unusual_whales_focus"][0]["symbol"], "NVDA")
                self.assertIn("unusual_whales_trade_analytics", payload)
                self.assertEqual(payload["unusual_whales_trade_analytics"]["overall"]["trades"], 3)
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

    def test_status_endpoint_reports_options_execution_state(self):
        class _Risk:
            def get_status(self):
                return {}

        class _Entry:
            def get_positions(self):
                return []
            def is_market_open(self):
                return True

        class _Bot:
            running = True
            paused = False
            start_time = 0
            risk_manager = _Risk()
            entry_manager = _Entry()
            options_engine = object()

        dashboard_module.set_bot(_Bot())
        try:
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
                 patch.object(dashboard_module.settings, "OPTIONS_ENABLED", True), \
                 patch.object(dashboard_module.settings, "OPTIONS_PILOT_ENABLED", False):
                client = TestClient(dashboard_module.app)
                resp = client.get("/api/status?token=secret-token")
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertTrue(payload["options_enabled"])
                self.assertTrue(payload["options_execution_enabled"])
                self.assertFalse(payload["options_entry_enabled"])
                self.assertFalse(payload["options_pilot_enabled"])
        finally:
            dashboard_module.set_bot(None)

    def test_status_and_metrics_recover_restart_safe_day_stats(self):
        class _Risk:
            def get_status(self):
                return {
                    "daily_pnl": 0.0,
                    "daily_pnl_pct": 0.0,
                    "total_trades": 0,
                    "winning_trades": 0,
                    "losing_trades": 0,
                    "win_rate": 0.0,
                    "equity": 26800.0,
                }

        class _Entry:
            def get_positions(self):
                return []

            def is_market_open(self):
                return True

        class _Bot:
            running = True
            paused = False
            start_time = 0
            risk_manager = _Risk()
            entry_manager = _Entry()
            options_engine = None
            ai_layers = {}
            reconciler = object()
            alpaca_client = None

        dashboard_module.set_bot(_Bot())
        try:
            reconciliation_state = {
                "broker": {
                    "equity": 26827.98,
                    "last_equity": 27093.43,
                    "day_pnl": -265.45,
                    "day_pnl_pct": -0.98,
                },
                "internal": {
                    "trade_history_realized": -265.45,
                    "trade_history_trade_count": 41,
                    "trade_history_win_rate_pct": 22.5,
                },
                "trust": {"internal_analytics_degraded": True},
                "reconciliation": {"status": "minor_mismatch"},
            }
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
                 patch.object(dashboard_module, "_get_reconciliation_state", return_value=reconciliation_state), \
                 patch.object(dashboard_module, "_get_cached_alpaca_terminal_snapshot", return_value={
                     "equity": 26827.98,
                     "last_equity": 27093.43,
                     "day_pnl": -265.45,
                     "day_pnl_pct": -0.98,
                 }), \
                 patch("src.ai.trade_history.get_analytics", return_value={
                     "total_trades": 181,
                     "wins": 83,
                     "losses": 96,
                 }):
                client = TestClient(dashboard_module.app)
                status_resp = client.get("/api/status?token=secret-token")
                metrics_resp = client.get("/api/metrics?token=secret-token")

                self.assertEqual(status_resp.status_code, 200)
                self.assertEqual(metrics_resp.status_code, 200)

                status_payload = status_resp.json()
                metrics_payload = metrics_resp.json()

                self.assertEqual(status_payload["daily_pnl"], -265.45)
                self.assertEqual(status_payload["total_trades"], 181)
                self.assertEqual(status_payload["today_trade_count"], 41)
                self.assertAlmostEqual(status_payload["today_win_rate_pct"], 22.5)
                self.assertAlmostEqual(status_payload["win_rate"], 45.86, places=2)

                self.assertEqual(metrics_payload["daily_pnl"], -265.45)
                self.assertEqual(metrics_payload["total_trades"], 181)
                self.assertAlmostEqual(metrics_payload["win_rate"], 45.86, places=2)
        finally:
            dashboard_module.set_bot(None)
