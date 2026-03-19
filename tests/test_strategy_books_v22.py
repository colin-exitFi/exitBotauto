import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.agents import risk_agent
from src.ai import trade_history
from src.dashboard import dashboard as dashboard_module
from src.entry.entry_manager import EntryManager
from src.exit.profit_ratchet import ProfitRatchet
from src.scanner.scanner import Scanner


class RiskAgentBookPolicyTests(unittest.TestCase):
    def test_uw_flow_long_is_hard_disabled(self):
        class _RiskManager:
            def get_status(self):
                return {"equity": 25000.0, "heat_pct": 12.0, "consecutive_losses": 0}

            def get_risk_tier(self):
                return {"size_pct": 2.0, "max_positions": 5}

            def is_wash_sale(self, symbol):
                return False

            def can_trade(self):
                return True

        brief = asyncio.run(
            risk_agent.analyze(
                symbol="SOFI",
                price=12.5,
                signals={"strategy_tag": "uw_flow_long", "signal_tier": "tier_1"},
                risk_manager=_RiskManager(),
                positions=[],
                direction="BUY",
            )
        )

        self.assertFalse(brief["can_trade"])
        self.assertEqual(brief["size_cap_pct"], 0.0)
        self.assertIn("strategy_disabled_uw_flow_long", brief["constraint_flags"])


class TradeHistoryBookAnalyticsTests(unittest.TestCase):
    def test_strategy_analytics_exclude_artifacts_and_compute_book_metrics(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            history_file = Path(tmp_dir) / "trade_history.json"
            rows = [
                {
                    "symbol": "ARTL",
                    "entry_price": 10.0,
                    "quantity": 10,
                    "pnl": 10.0,
                    "pnl_pct": 10.0,
                    "strategy_tag": "momentum_long",
                    "holding_horizon": "intraday",
                    "ratchet_peak_pnl_pct": 2.4,
                    "exit_time": 1_700_000_000,
                },
                {
                    "symbol": "MARA",
                    "entry_price": 20.0,
                    "quantity": 5,
                    "pnl": -4.0,
                    "pnl_pct": -4.0,
                    "strategy_tag": "momentum_long",
                    "holding_horizon": "intraday",
                    "ratchet_peak_pnl_pct": 0.3,
                    "exit_time": 1_700_000_600,
                },
                {
                    "symbol": "OLD1",
                    "entry_price": 10.0,
                    "quantity": 1,
                    "pnl": 100.0,
                    "pnl_pct": 100.0,
                    "strategy_tag": "carryover",
                    "exit_time": 1_700_001_200,
                },
                {
                    "symbol": "OLD2",
                    "entry_price": 10.0,
                    "quantity": 1,
                    "pnl": -50.0,
                    "pnl_pct": -50.0,
                    "strategy_tag": "broker_reconciled",
                    "exit_time": 1_700_001_800,
                },
            ]
            history_file.write_text(json.dumps(rows))

            with patch.object(trade_history, "HISTORY_FILE", history_file):
                analytics = trade_history.get_analytics()

        self.assertNotIn("carryover", analytics["by_strategy_tag"])
        self.assertNotIn("broker_reconciled", analytics["by_strategy_tag"])
        momentum = analytics["by_strategy_tag"]["momentum_long"]
        self.assertEqual(momentum["trades"], 2)
        self.assertEqual(momentum["avg_win"], 10.0)
        self.assertEqual(momentum["avg_loss"], -4.0)
        self.assertEqual(momentum["expectancy"], 3.0)
        self.assertEqual(momentum["ratchet_activation_rate_pct"], 50.0)


class DashboardBookEndpointsTests(unittest.TestCase):
    def test_shadow_and_book_scoreboard_endpoints(self):
        class _EntryManager:
            def get_positions(self):
                return [
                    {
                        "symbol": "ARTL",
                        "strategy_tag": "momentum_long",
                        "entry_price": 10.0,
                        "current_price": 11.5,
                        "quantity": 4,
                        "side": "long",
                    }
                ]

        class _Bot:
            entry_manager = _EntryManager()

            async def refresh_shadow_trades(self):
                return None

        with tempfile.TemporaryDirectory() as tmp_dir:
            shadow_file = Path(tmp_dir) / "shadow_trades.json"
            shadow_file.write_text(
                json.dumps(
                    [
                        {
                            "symbol": "SOFI",
                            "strategy_tag": "uw_flow_long",
                            "signal_tier": "tier_1",
                            "entry_quality": "at_highs",
                            "signal_price": 12.5,
                            "spread_pct": 0.4,
                            "range_pct": 97.0,
                            "timestamp": time.time(),
                            "price_1h": None,
                            "price_4h": None,
                            "price_eod": None,
                            "mfe": None,
                            "mae": None,
                        }
                    ]
                )
            )

            dashboard_module.set_bot(_Bot())
            try:
                with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
                     patch.object(dashboard_module, "_SHADOW_TRADES_FILE", shadow_file), \
                     patch("src.ai.trade_history.get_analytics", return_value={
                         "by_strategy_tag": {
                             "momentum_long": {
                                 "pnl": 125.5,
                                 "trades": 3,
                                 "win_rate_pct": 66.7,
                                 "avg_win": 48.0,
                                 "avg_loss": -19.5,
                                 "expectancy": 25.5,
                                 "ratchet_activation_rate_pct": 66.7,
                             }
                         }
                     }):
                    client = TestClient(dashboard_module.app)

                    shadow_resp = client.get("/api/shadow-trades?token=secret-token")
                    self.assertEqual(shadow_resp.status_code, 200)
                    shadow_payload = shadow_resp.json()
                    self.assertEqual(shadow_payload["count"], 1)
                    self.assertEqual(shadow_payload["trades"][0]["strategy_tag"], "uw_flow_long")

                    board_resp = client.get("/api/book-scoreboard?token=secret-token")
                    self.assertEqual(board_resp.status_code, 200)
                    board_payload = board_resp.json()
                    momentum = next(row for row in board_payload["books"] if row["strategy_tag"] == "momentum_long")
                    self.assertEqual(momentum["open_position_count"], 1)
                    self.assertEqual(momentum["unrealized_pnl"], 6.0)
                    self.assertEqual(momentum["realized_pnl"], 125.5)
                    self.assertEqual(momentum["expectancy"], 25.5)
            finally:
                dashboard_module.set_bot(None)


class EntryManagerStrategyTagSyncTests(unittest.TestCase):
    def test_broker_sync_sanitizes_carryover_strategy_tag(self):
        class _Broker:
            def get_orders(self, status="open"):
                return []

        manager = EntryManager.__new__(EntryManager)
        manager.positions = {}
        manager.broker = _Broker()
        manager._recently_removed_positions = {
            "RLMD": {
                "position": {
                    "strategy_tag": "carryover",
                }
            }
        }

        updates = manager.sync_positions_from_brokerage(
            [
                {
                    "symbol": "RLMD",
                    "quantity": 3.0,
                    "side": "long",
                    "average_price": 6.07,
                    "current_price": 6.13,
                }
            ]
        )

        self.assertEqual(updates, 1)
        self.assertEqual(manager.positions["RLMD"]["strategy_tag"], "unknown")


class ProfitRatchetStrategyBookTests(unittest.TestCase):
    def test_swing_dead_money_waits_for_eight_hours(self):
        now = time.time()
        position = {
            "entry_price": 100.0,
            "entry_time": now - (5 * 3600),
            "side": "long",
            "holding_horizon": "swing",
            "peak_price": 100.4,
        }
        self.assertFalse(ProfitRatchet.is_dead_money(position, current_price=98.5, now=now))

        position["entry_time"] = now - (9 * 3600)
        self.assertTrue(ProfitRatchet.is_dead_money(position, current_price=98.5, now=now))


class ScannerCongressFreshnessTests(unittest.TestCase):
    def test_recent_calendar_signal_helper(self):
        self.assertTrue(Scanner._is_recent_calendar_signal("2026-03-15", max_days=7))
        self.assertFalse(Scanner._is_recent_calendar_signal("2026-03-01", max_days=7))


if __name__ == "__main__":
    unittest.main()
