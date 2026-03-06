import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import src.main as main_module
from src.options.options_engine import OptionsEngine


class FakeVerdict:
    def __init__(self, symbol, decision="BUY", confidence=90, size_pct=2.0, trail_pct=3.0, provider_used="claude"):
        self.symbol = symbol
        self.decision = decision
        self.confidence = confidence
        self.size_pct = size_pct
        self.trail_pct = trail_pct
        self.reasoning = "test verdict"
        self.provider_used = provider_used

    def to_dict(self):
        return {
            "symbol": self.symbol,
            "decision": self.decision,
            "confidence": self.confidence,
            "size_pct": self.size_pct,
            "trail_pct": self.trail_pct,
            "reasoning": self.reasoning,
            "provider_used": self.provider_used,
        }


class FakeOrchestrator:
    def __init__(self, verdict):
        self.verdict = verdict

    async def evaluate(self, symbol: str, price: float, signals_data: dict):
        return self.verdict


class FakeSentimentAnalyzer:
    def get_cached(self, symbol: str):
        return {"score": 0.7}


class FakePositionManager:
    def can_enter(self, symbol: str, positions, risk_manager):
        return True


class FakeRiskManager:
    def __init__(self):
        self.equity = 25000
        self.recorded = []
        self.options_exposure_updates = 0

    def can_trade(self):
        return True

    def get_risk_tier(self):
        return {"name": "TEST", "size_pct": 2.5}

    def can_open_options(self, premium_cost: float):
        return True

    def update_options_exposure(self, options_positions):
        self.options_exposure_updates += 1

    def record_trade(self, trade):
        self.recorded.append(trade)


class FakeEntryManager:
    def __init__(self):
        self.positions = {}
        self.last_sentiment_data = None

    def get_positions(self):
        return list(self.positions.values())

    def remove_position(self, symbol: str):
        self.positions.pop(symbol, None)

    async def can_enter(self, symbol: str, check_sentiment: float, positions):
        return True

    async def enter_position(self, symbol: str, sentiment_data: dict):
        self.last_sentiment_data = dict(sentiment_data)
        pos = {
            "symbol": symbol,
            "entry_price": 100.0,
            "quantity": 10.0,
            "entry_time": time.time() - 60,
            "side": "long",
            "_exit_recorded": False,
            "signal_price": 100.0,
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "signal_sources": sentiment_data.get("signal_sources", ["unknown"]),
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
        }
        self.positions[symbol] = pos
        return pos

    async def enter_short(self, symbol: str, sentiment_data: dict):
        self.last_sentiment_data = dict(sentiment_data)
        pos = {
            "symbol": symbol,
            "entry_price": 100.0,
            "quantity": 10.0,
            "entry_time": time.time() - 60,
            "side": "short",
            "_exit_recorded": False,
            "signal_price": 100.0,
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "signal_sources": sentiment_data.get("signal_sources", ["unknown"]),
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
        }
        self.positions[symbol] = pos
        return pos

    def is_market_open(self):
        return True


class FakeOptionsEngine:
    def __init__(self):
        self.positions = {}
        self.execute_calls = []
        self.close_calls = []
        self.reconcile_calls = 0
        self.exit_actions = {}
        self.reconcile_removed_positions = []

    async def execute_option_trade(self, symbol: str, price: float, direction: str, budget: float, sentiment_data: dict):
        self.execute_calls.append({
            "symbol": symbol,
            "price": price,
            "direction": direction,
            "budget": budget,
            "sentiment_data": dict(sentiment_data),
        })
        contract_symbol = f"{symbol}OPT"
        pos = {
            "contract_symbol": contract_symbol,
            "symbol": contract_symbol,
            "underlying": symbol,
            "option_type": "call" if direction == "BUY" else "put",
            "qty": 2,
            "entry_premium": 1.2,
            "current_premium": 1.2,
            "premium_hwm": 1.2,
            "entry_time": time.time() - 60,
            "expiry": "2030-01-17",
            "status": "open",
            "strategy_tag": sentiment_data.get("strategy_tag", "unknown"),
            "signal_sources": sentiment_data.get("signal_sources", ["unknown"]),
            "decision_confidence": sentiment_data.get("consensus_confidence", 0),
            "provider_used": sentiment_data.get("provider_used", ""),
            "total_cost": 240.0,
        }
        self.positions[contract_symbol] = pos
        return pos

    def get_options_positions(self):
        return list(self.positions.values())

    def close_paired_options(self, underlying_symbol: str, reason: str = "underlying_exit"):
        for sym, pos in list(self.positions.items()):
            if pos.get("underlying") != underlying_symbol:
                continue
            self.positions.pop(sym, None)
            return [{
                "asset_type": "option",
                "symbol": sym,
                "contract_symbol": sym,
                "underlying": underlying_symbol,
                "side": "sell",
                "entry_price": 1.2,
                "exit_price": 1.4,
                "entry_premium": 1.2,
                "exit_premium": 1.4,
                "quantity": 2,
                "pnl": 40.0,
                "pnl_pct": 16.67,
                "reason": reason,
                "hold_seconds": 60,
                "entry_time": time.time() - 60,
                "exit_time": time.time(),
                "strategy_tag": pos.get("strategy_tag", "unknown"),
                "signal_sources": pos.get("signal_sources", ["unknown"]),
                "decision_confidence": pos.get("decision_confidence", 0),
                "provider_used": pos.get("provider_used", ""),
            }]
        return []

    def check_exit_rules(self, contract_symbol: str):
        return self.exit_actions.get(contract_symbol)

    def close_option_position(self, contract_symbol: str, qty: int = None, reason: str = "manual"):
        self.close_calls.append((contract_symbol, qty, reason))
        return {"id": "close-order"}

    def finalize_exit(self, contract_symbol: str, qty: int, exit_premium: float, reason: str):
        pos = self.positions.pop(contract_symbol, None)
        if not pos:
            return None
        qty = int(qty)
        entry = 1.2
        pnl = (exit_premium - entry) * qty * 100
        return {
            "asset_type": "option",
            "symbol": contract_symbol,
            "contract_symbol": contract_symbol,
            "underlying": pos.get("underlying", "AAPL"),
            "side": "sell",
            "entry_price": entry,
            "exit_price": exit_premium,
            "entry_premium": entry,
            "exit_premium": exit_premium,
            "quantity": qty,
            "pnl": pnl,
            "pnl_pct": ((exit_premium - entry) / entry) * 100,
            "reason": reason,
            "hold_seconds": 60,
            "entry_time": pos.get("entry_time", time.time() - 60),
            "exit_time": time.time(),
            "strategy_tag": pos.get("strategy_tag", "unknown"),
            "signal_sources": pos.get("signal_sources", ["unknown"]),
            "decision_confidence": pos.get("decision_confidence", 0),
            "provider_used": pos.get("provider_used", ""),
        }

    def build_external_close_trade(self, pos: dict, reason: str = "options_reconcile_closed"):
        qty = int(pos.get("qty", 0) or 0)
        entry = float(pos.get("entry_premium", 0) or 0)
        exit_premium = float(pos.get("current_premium", entry) or entry)
        return {
            "asset_type": "option",
            "symbol": pos.get("contract_symbol", pos.get("symbol", "OPT")),
            "contract_symbol": pos.get("contract_symbol", pos.get("symbol", "OPT")),
            "underlying": pos.get("underlying", "AAPL"),
            "side": "sell",
            "entry_price": entry,
            "exit_price": exit_premium,
            "entry_premium": entry,
            "exit_premium": exit_premium,
            "quantity": qty,
            "pnl": (exit_premium - entry) * qty * 100,
            "pnl_pct": ((exit_premium - entry) / entry * 100) if entry else 0.0,
            "reason": reason,
            "hold_seconds": 60,
            "entry_time": pos.get("entry_time", time.time() - 60),
            "exit_time": time.time(),
            "strategy_tag": pos.get("strategy_tag", "unknown"),
            "signal_sources": pos.get("signal_sources", ["unknown"]),
            "decision_confidence": pos.get("decision_confidence", 0),
            "provider_used": pos.get("provider_used", ""),
        }

    def reconcile_with_broker(self):
        self.reconcile_calls += 1
        removed = list(self.reconcile_removed_positions)
        self.reconcile_removed_positions = []
        return {"removed": len(removed), "added": 0, "removed_positions": removed}


def _candidate(symbol="AAPL"):
    return {
        "symbol": symbol,
        "price": 100.0,
        "change_pct": 5.1,
        "volume_spike": 3.0,
        "sentiment_score": 0.7,
        "source": "polygon+grok_x",
        "score": 0.9,
    }


class OptionsIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_paired_entry_places_shares_and_options(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.risk_manager = FakeRiskManager()
        bot.entry_manager = FakeEntryManager()
        bot.sentiment_analyzer = FakeSentimentAnalyzer()
        bot.position_manager = FakePositionManager()
        bot.orchestrator = FakeOrchestrator(FakeVerdict("AAPL", decision="BUY", confidence=92))
        bot.ai_layers = {}
        bot.options_engine = FakeOptionsEngine()

        with patch.object(main_module, "log_activity"), \
             patch.object(main_module.persistence, "save_options_positions"):
            await bot._process_candidates([_candidate("AAPL")])

        self.assertEqual(len(bot.options_engine.execute_calls), 1)
        self.assertIn("AAPL", bot.entry_manager.positions)
        self.assertIsNotNone(bot.entry_manager.last_sentiment_data)
        self.assertLess(bot.entry_manager.last_sentiment_data.get("share_notional_multiplier", 1.0), 1.0)

    async def test_paired_exit_underlying_stop_closes_linked_options(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.risk_manager = FakeRiskManager()
        bot.entry_manager = FakeEntryManager()
        bot.options_engine = FakeOptionsEngine()
        bot.pnl_state = {}
        bot.entry_manager.positions = {
            "AAPL": {
                "symbol": "AAPL",
                "entry_price": 100.0,
                "quantity": 10.0,
                "entry_time": time.time() - 120,
                "side": "long",
                "signal_price": 100.0,
                "_exit_recorded": False,
            }
        }
        # seed linked option position
        await bot.options_engine.execute_option_trade("AAPL", 100.0, "BUY", 500.0, {"strategy_tag": "momentum_long"})

        recorded = []
        bot._record_realized_exit = lambda trade: recorded.append(trade)

        with patch.object(main_module, "log_activity"):
            bot._on_trailing_stop_filled("AAPL", 105.0, 10.0)
            await asyncio.sleep(0.05)

        self.assertGreaterEqual(len(recorded), 2)
        symbols = {t.get("symbol") for t in recorded}
        self.assertIn("AAPL", symbols)
        self.assertTrue(any(str(s).endswith("OPT") for s in symbols))

    async def test_options_only_exit_does_not_close_share_position(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.risk_manager = FakeRiskManager()
        bot.entry_manager = FakeEntryManager()
        bot.entry_manager.positions = {
            "AAPL": {
                "symbol": "AAPL",
                "entry_price": 100.0,
                "quantity": 10.0,
                "entry_time": time.time() - 120,
                "side": "long",
                "_exit_recorded": False,
            }
        }
        bot.options_engine = FakeOptionsEngine()
        opt = await bot.options_engine.execute_option_trade("AAPL", 100.0, "BUY", 500.0, {"strategy_tag": "momentum_long"})
        bot.options_engine.exit_actions[opt["contract_symbol"]] = {
            "action": "close",
            "qty": 2,
            "reason": "premium_stop_loss",
            "current_premium": 0.8,
        }

        recorded = []
        bot._record_realized_exit = lambda trade: recorded.append(trade)

        with patch.object(main_module, "log_activity"), \
             patch.object(main_module.persistence, "save_options_positions"):
            await bot._monitor_options_once()

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].get("asset_type"), "option")
        self.assertIn("AAPL", bot.entry_manager.positions)

    async def test_reconcile_removed_option_records_external_close_trade(self):
        bot = main_module.TradingBot.__new__(main_module.TradingBot)
        bot.risk_manager = FakeRiskManager()
        bot.entry_manager = FakeEntryManager()
        bot.options_engine = FakeOptionsEngine()

        pos = await bot.options_engine.execute_option_trade("AAPL", 100.0, "BUY", 500.0, {"strategy_tag": "momentum_long"})
        removed_pos = dict(pos)
        bot.options_engine.positions.pop(pos["contract_symbol"], None)
        bot.options_engine.reconcile_removed_positions = [removed_pos]

        recorded = []
        bot._record_realized_exit = lambda trade: recorded.append(trade)

        with patch.object(main_module, "log_activity"), \
             patch.object(main_module.persistence, "save_options_positions"):
            await bot._monitor_options_once()

        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0].get("reason"), "options_reconcile_closed")
        self.assertEqual(recorded[0].get("asset_type"), "option")

    async def test_options_persistence_roundtrip_restores_positions(self):
        contract_symbol = "AAPLOPT"
        saved = {
            contract_symbol: {
                "asset_type": "option",
                "contract_symbol": contract_symbol,
                "symbol": contract_symbol,
                "underlying": "AAPL",
                "option_type": "call",
                "strike": 100.0,
                "expiry": "2030-01-17",
                "qty": 3,
                "entry_premium": 1.25,
                "current_premium": 1.30,
                "premium_hwm": 1.40,
                "status": "open",
                "entry_time": time.time() - 120,
                "strategy_tag": "momentum_long",
                "signal_sources": ["polygon", "grok_x"],
                "decision_confidence": 88,
                "provider_used": "claude",
            }
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            options_file = Path(tmp_dir) / "options_positions.json"
            with patch.object(main_module.persistence, "OPTIONS_POSITIONS_FILE", options_file):
                main_module.persistence.save_options_positions(saved)
                loaded = main_module.persistence.load_options_positions()

        self.assertIn(contract_symbol, loaded)
        self.assertEqual(int(loaded[contract_symbol].get("qty", 0)), 3)

        engine = OptionsEngine(api_key="k", secret_key="s", base_url="https://paper-api.alpaca.markets")
        engine.load_positions(loaded)
        restored = engine.positions.get(contract_symbol)
        self.assertIsNotNone(restored)
        self.assertEqual(int(restored.get("qty", 0)), 3)
        self.assertEqual(restored.get("underlying"), "AAPL")


if __name__ == "__main__":
    unittest.main()
