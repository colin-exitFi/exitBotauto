import unittest

from src.risk import book_allocator


class BookAllocatorTests(unittest.TestCase):
    def test_hot_aligned_book_presses_size(self):
        analytics = {
            "by_strategy_tag": {
                "momentum_long": {
                    "trades": 18,
                    "win_rate_pct": 61.0,
                    "pnl": 240.0,
                    "expectancy": 13.5,
                }
            }
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        plan = book_allocator.plan_entry(
            strategy_tag="momentum_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=93.0,
            requested_size_pct=6.0,
            snapshot=snapshot,
        )

        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["state"], "hot")
        self.assertEqual(plan["alignment"], "aligned")
        self.assertGreater(plan["size_pct"], 6.0)
        self.assertLessEqual(plan["size_pct"], 8.0)
        self.assertIn("high_confidence_press", plan["reason_codes"])

    def test_book_budget_exhaustion_blocks_new_entry(self):
        analytics = {
            "by_strategy_tag": {
                "momentum_long": {
                    "trades": 12,
                    "win_rate_pct": 56.0,
                    "pnl": 80.0,
                    "expectancy": 6.0,
                }
            }
        }
        positions = [
            {
                "symbol": "AAPL",
                "strategy_tag": "momentum_long",
                "actual_notional": 4200.0,
                "unrealized_pnl": 20.0,
            },
            {
                "symbol": "NVDA",
                "strategy_tag": "momentum_long",
                "actual_notional": 3400.0,
                "unrealized_pnl": 25.0,
            },
        ]
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=positions,
            analytics=analytics,
            equity=27000.0,
        )

        plan = book_allocator.plan_entry(
            strategy_tag="momentum_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=88.0,
            requested_size_pct=5.0,
            snapshot=snapshot,
        )

        self.assertFalse(plan["allowed"])
        self.assertEqual(plan["reason"], "book_budget_exhausted")

    def test_cold_book_cuts_size(self):
        analytics = {
            "by_strategy_tag": {
                "uw_flow_short": {
                    "trades": 22,
                    "win_rate_pct": 34.0,
                    "pnl": -125.0,
                    "expectancy": -5.5,
                }
            }
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_off",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        plan = book_allocator.plan_entry(
            strategy_tag="uw_flow_short",
            setup_mode="continuation_short",
            market_regime="risk_off",
            session_type="regular",
            confidence=72.0,
            requested_size_pct=4.0,
            snapshot=snapshot,
        )

        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["state"], "cold")
        self.assertLess(plan["size_pct"], 4.0)
        self.assertIn("book_cold", plan["reason_codes"])

    def test_misaligned_book_is_penalized(self):
        analytics = {
            "by_strategy_tag": {
                "momentum_short": {
                    "trades": 14,
                    "win_rate_pct": 52.0,
                    "pnl": 60.0,
                    "expectancy": 4.0,
                }
            }
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        plan = book_allocator.plan_entry(
            strategy_tag="momentum_short",
            setup_mode="continuation_short",
            market_regime="risk_on",
            session_type="regular",
            confidence=84.0,
            requested_size_pct=5.0,
            snapshot=snapshot,
        )

        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["alignment"], "misaligned")
        self.assertLess(plan["size_pct"], 5.0)

    def test_book_report_scale_status_increases_budget_and_size(self):
        analytics = {
            "book_report": {
                "books": [
                    {
                        "strategy_tag": "momentum_long",
                        "trades": 22,
                        "trade_count": 22,
                        "win_rate_pct": 54.0,
                        "pnl": 180.0,
                        "net_pnl": 180.0,
                        "expectancy": 8.0,
                        "profit_factor": 1.8,
                        "max_drawdown": 40.0,
                        "status": "scale",
                        "recommended_action": "scale",
                        "control_state": "active",
                        "size_multiplier": 1.0,
                    }
                ]
            }
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        row = snapshot["momentum_long"]
        self.assertGreater(row["budget_pct"], 28.0)

        plan = book_allocator.plan_entry(
            strategy_tag="momentum_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=82.0,
            requested_size_pct=5.0,
            snapshot=snapshot,
        )

        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["status"], "scale")
        self.assertGreater(plan["size_pct"], 5.0)
        self.assertIn("status_scale", plan["reason_codes"])

    def test_probation_book_report_reduces_budget_and_size(self):
        analytics = {
            "book_report": {
                "books": [
                    {
                        "strategy_tag": "uw_flow_long",
                        "trades": 30,
                        "trade_count": 30,
                        "win_rate_pct": 47.0,
                        "pnl": 12.0,
                        "net_pnl": 12.0,
                        "expectancy": 0.4,
                        "profit_factor": 1.02,
                        "max_drawdown": 35.0,
                        "status": "probation",
                        "recommended_action": "probation",
                        "control_state": "probation",
                        "size_multiplier": 1.0,
                    }
                ]
            }
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        row = snapshot["uw_flow_long"]
        self.assertLess(row["budget_pct"], 12.0)

        plan = book_allocator.plan_entry(
            strategy_tag="uw_flow_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=78.0,
            requested_size_pct=6.0,
            snapshot=snapshot,
        )

        self.assertTrue(plan["allowed"])
        self.assertEqual(plan["status"], "probation")
        self.assertLess(plan["size_pct"], 6.0)
        self.assertIn("status_probation", plan["reason_codes"])
        self.assertIn("control_probation", plan["reason_codes"])

    def test_disable_book_report_blocks_entry(self):
        analytics = {
            "book_report": {
                "books": [
                    {
                        "strategy_tag": "social_momentum_long",
                        "trades": 26,
                        "trade_count": 26,
                        "win_rate_pct": 35.0,
                        "pnl": -140.0,
                        "net_pnl": -140.0,
                        "expectancy": -5.0,
                        "profit_factor": 0.72,
                        "max_drawdown": 160.0,
                        "status": "disable",
                        "recommended_action": "disable",
                        "control_state": "active",
                        "size_multiplier": 1.0,
                    }
                ]
            }
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        self.assertEqual(snapshot["social_momentum_long"]["budget_pct"], 0.0)

        plan = book_allocator.plan_entry(
            strategy_tag="social_momentum_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=84.0,
            requested_size_pct=4.0,
            snapshot=snapshot,
        )

        self.assertFalse(plan["allowed"])
        self.assertEqual(plan["reason"], "book_disabled_by_allocator")
        self.assertEqual(plan["size_pct"], 0.0)
        self.assertIn("status_disable", plan["reason_codes"])

    def test_clean_trade_count_and_anomalies_reduce_unproven_book_size(self):
        analytics = {
            "book_report": {
                "books": [
                    {
                        "strategy_tag": "copy_trader_long",
                        "trades": 30,
                        "trade_count": 30,
                        "clean_trades": 4,
                        "win_rate_pct": 57.0,
                        "clean_win_rate_pct": 50.0,
                        "pnl": 45.0,
                        "clean_pnl": 6.0,
                        "expectancy": 1.5,
                        "profit_factor": 1.08,
                        "max_drawdown": 22.0,
                        "anomaly_count": 12,
                        "status": "hold",
                        "recommended_action": "hold",
                        "control_state": "active",
                        "size_multiplier": 1.0,
                    }
                ]
            }
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        row = snapshot["copy_trader_long"]
        self.assertEqual(row["effective_trade_count"], 4)
        self.assertEqual(row["evidence_state"], "exploratory")
        self.assertLess(row["data_quality_multiplier"], 1.0)

        plan = book_allocator.plan_entry(
            strategy_tag="copy_trader_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=80.0,
            requested_size_pct=4.0,
            snapshot=snapshot,
        )

        self.assertTrue(plan["allowed"])
        self.assertLess(plan["size_pct"], 4.0)
        self.assertIn("evidence_exploratory", plan["reason_codes"])

    def test_negative_play_is_blocked_below_override_confidence(self):
        analytics = {
            "by_strategy_tag": {
                "momentum_long": {
                    "trades": 18,
                    "win_rate_pct": 59.0,
                    "pnl": 140.0,
                    "expectancy": 7.0,
                }
            }
        }
        play_report = {
            "plays": [
                {
                    "play_key": "momentum_long|continuation_long|risk_on|regular",
                    "strategy_tag": "momentum_long",
                    "setup_mode": "continuation_long",
                    "market_regime": "risk_on",
                    "session_type": "regular",
                    "trades": 17,
                    "pnl": -48.0,
                    "expectancy": -2.8,
                    "status": "disable",
                    "recommended_action": "disable",
                }
            ]
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        plan = book_allocator.plan_entry(
            strategy_tag="momentum_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=89.0,
            requested_size_pct=5.0,
            snapshot=snapshot,
            play_report=play_report,
        )

        self.assertFalse(plan["allowed"])
        self.assertEqual(plan["reason"], "play_disabled_by_allocator")
        self.assertEqual(plan["play_status"], "disable")
        self.assertIn("play_disable", plan["reason_codes"])

    def test_positive_play_scales_size_above_book_baseline(self):
        analytics = {
            "by_strategy_tag": {
                "momentum_long": {
                    "trades": 18,
                    "win_rate_pct": 55.0,
                    "pnl": 120.0,
                    "expectancy": 6.0,
                }
            }
        }
        play_report = {
            "plays": [
                {
                    "play_key": "momentum_long|continuation_long|risk_on|regular",
                    "strategy_tag": "momentum_long",
                    "setup_mode": "continuation_long",
                    "market_regime": "risk_on",
                    "session_type": "regular",
                    "trades": 14,
                    "pnl": 95.0,
                    "expectancy": 6.8,
                    "status": "scale",
                    "recommended_action": "scale",
                }
            ]
        }
        snapshot = book_allocator.build_snapshot(
            market_regime="risk_on",
            session_type="regular",
            positions=[],
            analytics=analytics,
            equity=27000.0,
        )

        base_plan = book_allocator.plan_entry(
            strategy_tag="momentum_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=82.0,
            requested_size_pct=5.0,
            snapshot=snapshot,
        )
        play_plan = book_allocator.plan_entry(
            strategy_tag="momentum_long",
            setup_mode="continuation_long",
            market_regime="risk_on",
            session_type="regular",
            confidence=82.0,
            requested_size_pct=5.0,
            snapshot=snapshot,
            play_report=play_report,
        )

        self.assertTrue(play_plan["allowed"])
        self.assertGreater(play_plan["size_pct"], base_plan["size_pct"])
        self.assertEqual(play_plan["play_status"], "scale")
        self.assertIn("play_scale", play_plan["reason_codes"])


if __name__ == "__main__":
    unittest.main()
