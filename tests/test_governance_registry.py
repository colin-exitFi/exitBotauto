import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.ai import committee_memo
from src.data import governance_registry
from src.dashboard import dashboard as dashboard_module


class GovernanceRegistryTests(unittest.TestCase):
    def test_registry_loads_roles_and_artifacts(self):
        summary = governance_registry.get_governance_committee_summary(include_docs=False)

        self.assertEqual(summary["committee"]["name"], "Velox Governance Committee")
        self.assertIn("scaled live", summary["rollout_states"])
        self.assertIn("probation", summary["book_lifecycle"])
        self.assertGreaterEqual(len(summary["roles"]), 6)

        role_ids = {row["id"] for row in summary["roles"]}
        self.assertIn("quant_reviewer", role_ids)
        self.assertIn("portfolio_manager", role_ids)
        self.assertIn("risk_committee", role_ids)

        prompt_artifact = summary["committee"]["artifacts"]["committee_prompt"]
        self.assertTrue(prompt_artifact["path"].endswith("AGENT_GOVERNANCE_PROMPT.md"))
        self.assertTrue(prompt_artifact["abspath"].endswith("docs/governance/AGENT_GOVERNANCE_PROMPT.md"))

    def test_role_document_can_be_loaded(self):
        role = governance_registry.get_governance_role("portfolio_manager", include_doc=True)

        self.assertEqual(role["title"], "Portfolio Manager")
        self.assertIn("hedge fund portfolio manager", role["doc_markdown"].lower())


class GovernanceDashboardEndpointTests(unittest.TestCase):
    def test_governance_committee_endpoint_returns_registry(self):
        with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"):
            client = TestClient(dashboard_module.app)

            response = client.get("/api/governance/committee?token=secret-token")
            self.assertEqual(response.status_code, 200)
            payload = response.json()

            self.assertEqual(payload["committee"]["name"], "Velox Governance Committee")
            self.assertGreaterEqual(len(payload["roles"]), 6)
            self.assertIn("generated_at", payload)

            with_docs = client.get("/api/governance/committee?token=secret-token&include_docs=true")
            self.assertEqual(with_docs.status_code, 200)
            docs_payload = with_docs.json()
            quant_role = next(row for row in docs_payload["roles"] if row["id"] == "quant_reviewer")
            self.assertIn("statistical honesty", quant_role["doc_markdown"].lower())

    def test_governance_summary_and_weekly_memo_endpoints(self):
        analytics = {
            "total_trades": 42,
            "total_pnl": 123.45,
            "overall": {
                "win_rate_pct": 61.9,
                "avg_win": 18.0,
                "avg_loss": -9.0,
            },
            "book_report": {
                "summary": {"books": 3, "scale": 1, "hold": 1, "probation": 1, "disable": 0, "observe": 0},
                "books": [
                    {
                        "strategy_tag": "momentum_short",
                        "pnl": 80.0,
                        "expectancy": 6.0,
                        "status": "scale",
                        "best_regime": {"name": "risk_off"},
                        "worst_regime": {"name": "risk_on"},
                    },
                    {
                        "strategy_tag": "momentum_long",
                        "pnl": 35.0,
                        "expectancy": 2.0,
                        "status": "hold",
                        "best_regime": {"name": "risk_on"},
                        "worst_regime": {"name": "mixed"},
                    },
                    {
                        "strategy_tag": "uw_flow_long",
                        "pnl": -28.0,
                        "expectancy": -2.5,
                        "status": "probation",
                        "best_regime": {"name": "risk_on"},
                        "worst_regime": {"name": "risk_off"},
                    },
                ],
            },
        }
        recon_state = {
            "reconciliation": {"status": "healthy", "severity": "info", "reasons": []},
            "trust": {"broker_only_mode": False, "internal_analytics_degraded": False},
            "canaries": [],
        }
        controls = {
            "probation": {
                "uw_flow_long": {"status": "active", "reason": "negative expectancy"},
            },
            "hard_disabled": {},
            "soft_disabled": {},
            "manual_disabled": {},
            "manual_enabled": {},
            "size_reductions": {},
        }
        change_ledger = [
            {
                "title": "Tighten fade short stop",
                "rollout_mode": "shadow",
                "expected_benefit": "Reduce oversized tail losers",
                "recorded_at": 1_700_000_000,
            }
        ]

        with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
             patch("src.ai.committee_memo.trade_history.get_analytics", return_value=analytics), \
             patch("src.ai.committee_memo.persistence.load_reconciliation_state", return_value=recon_state), \
             patch("src.ai.committee_memo.strategy_controls.load_controls", return_value=controls), \
             patch("src.ai.committee_memo.persistence.load_change_ledger", return_value=change_ledger):
            client = TestClient(dashboard_module.app)

            summary_resp = client.get("/api/governance/summary?token=secret-token")
            self.assertEqual(summary_resp.status_code, 200)
            summary_payload = summary_resp.json()
            self.assertEqual(summary_payload["book_report_summary"]["books"], 3)
            self.assertEqual(summary_payload["top_winning_books"][0]["strategy_tag"], "momentum_short")
            self.assertEqual(summary_payload["probation_books"][0]["strategy_tag"], "uw_flow_long")
            self.assertIn("uw_flow_long", summary_payload["biggest_current_risk"])

            memo_resp = client.get("/api/governance/weekly-memo?token=secret-token")
            self.assertEqual(memo_resp.status_code, 200)
            memo_payload = memo_resp.json()
            self.assertEqual(memo_payload["executive_summary"]["total_trades"], 42)
            self.assertIn("Velox has 42 analytic trades", memo_payload["executive_summary"]["top_line"])
            self.assertEqual(memo_payload["recent_changes"][0]["title"], "Tighten fade short stop")
            self.assertIn("Weekly Committee Memo", memo_payload["markdown"])


class CommitteeMemoTests(unittest.TestCase):
    def test_build_governance_summary_prefers_reconciliation_risk_when_critical(self):
        analytics = {
            "overall": {"avg_win": 10.0, "avg_loss": -12.0},
            "book_report": {
                "summary": {"books": 1},
                "books": [
                    {"strategy_tag": "momentum_long", "pnl": 12.0, "expectancy": 1.2, "status": "hold"},
                ],
            },
        }
        recon_state = {
            "reconciliation": {"status": "critical_mismatch", "severity": "critical", "reasons": ["pnl_gap"]},
            "trust": {"broker_only_mode": True},
            "canaries": [{"code": "realized_pnl_mismatch", "severity": "critical"}],
        }

        with patch("src.ai.committee_memo.trade_history.get_analytics", return_value=analytics), \
             patch("src.ai.committee_memo.persistence.load_reconciliation_state", return_value=recon_state), \
             patch("src.ai.committee_memo.strategy_controls.load_controls", return_value={}), \
             patch("src.ai.committee_memo.persistence.load_change_ledger", return_value=[]):
            summary = committee_memo.build_governance_summary()

        self.assertEqual(summary["reconciliation"]["status"], "critical_mismatch")
        self.assertIn("Broker reconciliation is critical", summary["biggest_current_risk"])


if __name__ == "__main__":
    unittest.main()
