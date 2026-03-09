import tempfile
import unittest
import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.dashboard import dashboard as dashboard_module
from src.scanner.scanner import Scanner
from src.signals.human_intel import HumanIntelStore


class HumanIntelStoreTests(unittest.TestCase):
    def test_store_summarizes_bearish_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HumanIntelStore(path=Path(tmp) / "human_intel.json")
            store.add_entry("BATL", title="Day-2 fade", bias="bearish", confidence=0.9, notes="Runner exhausted")
            store.add_entry("BATL", title="Discord squeeze rumor fading", bias="bearish", confidence=0.7)

            summary = store.summarize_for_symbol("BATL")

        self.assertEqual(summary["bias"], "bearish")
        self.assertEqual(summary["count"], 2)
        self.assertLess(summary["score_adjustment"], 0)
        self.assertIn("Day-2 fade", summary["summary"])

    def test_scanner_human_intel_candidates_get_lenient_filter(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = HumanIntelStore(path=Path(tmp) / "human_intel.json")
            store.add_entry("BATL", title="Cup-and-handle", bias="bullish", confidence=0.8)
            scanner = Scanner(human_intel_store=store)
            candidate = {
                "symbol": "BATL",
                "price": 22.0,
                "change_pct": 0.0,
                "volume": 150_000,
                "avg_volume": 150_000,
                "source": "human_intel",
                "human_intel": "operator context",
            }

            self.assertTrue(scanner._passes_filter(candidate))

    def test_watchlist_candidates_also_get_lenient_filter(self):
        class _Watchlist:
            def get_all(self):
                return [
                    {
                        "ticker": "HOOD",
                        "side": "long",
                        "reason": "ARK buy across funds",
                        "sources": "ark_buy",
                        "conviction": 0.6,
                    }
                ]

        scanner = Scanner(watchlist_provider=_Watchlist())
        candidate = {
            "symbol": "HOOD",
            "price": 44.0,
            "change_pct": 0.0,
            "volume": 150_000,
            "avg_volume": 150_000,
            "source": "ark_buy",
            "watchlist_reason": "ARK buy across funds",
            "watchlist_conviction": 0.6,
        }

        self.assertTrue(scanner._passes_filter(candidate))

    def test_extract_query_symbols_matches_lowercase_known_ticker(self):
        class _Bot:
            scanner = type(
                "ScannerStub",
                (),
                {
                    "get_cached_candidates": lambda self: [{"symbol": "SOTY"}],
                },
            )()

        dashboard_module.set_bot(_Bot())
        try:
            symbols = dashboard_module._extract_query_symbols("tell me more about soty pharma")
            self.assertIn("SOTY", symbols)
        finally:
            dashboard_module.set_bot(None)


class DashboardHumanIntelTests(unittest.TestCase):
    def test_dashboard_human_intel_endpoint_persists_and_promotes_watchlist(self):
        class _Watchlist:
            def __init__(self):
                self.calls = []

            def add(self, *args, **kwargs):
                self.calls.append((args, kwargs))
                return True

        class _Bot:
            def __init__(self, store):
                self.human_intel_store = store
                self.watchlist = _Watchlist()
                self.orchestrator = type(
                    "OrchestratorStub",
                    (),
                    {"_cache": {"BATL:BUY": object()}, "_skip_cache": {"BATL:SHORT": 1.0}},
                )()

        with tempfile.TemporaryDirectory() as tmp:
            store = HumanIntelStore(path=Path(tmp) / "human_intel.json")
            bot = _Bot(store)
            dashboard_module.set_bot(bot)
            try:
                with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"):
                    client = TestClient(dashboard_module.app)
                    resp = client.post(
                        "/api/human-intel?token=secret-token",
                        json={
                            "ticker": "BATL",
                            "title": "Discord rumor fading",
                            "notes": "Short squeeze chatter rolled over",
                            "bias": "bearish",
                            "confidence": 0.8,
                            "kind": "rumor",
                        },
                    )
                    self.assertEqual(resp.status_code, 200)
                    self.assertTrue(resp.json()["ok"])

                    listing = client.get("/api/human-intel?token=secret-token")
                    self.assertEqual(listing.status_code, 200)
                    self.assertEqual(len(listing.json()), 1)
                    self.assertTrue(bot.watchlist.calls)
                    self.assertEqual(bot.orchestrator._cache, {})
                    self.assertEqual(bot.orchestrator._skip_cache, {})

                    entry_id = listing.json()[0]["id"]
                    deleted = client.delete(f"/api/human-intel/{entry_id}?token=secret-token")
                    self.assertEqual(deleted.status_code, 200)
                    self.assertTrue(deleted.json()["ok"])
            finally:
                dashboard_module.set_bot(None)

    def test_dashboard_copilot_chat_uses_persisted_runners_for_recall_queries(self):
        called = {"perplexity": False}

        async def _fake_perplexity(prompt, max_tokens=900):
            called["perplexity"] = True
            return "Should not be used"

        with tempfile.TemporaryDirectory() as tmp:
            runners_path = Path(tmp) / "yesterdays_runners.json"
            watchlist_path = Path(tmp) / "watchlist.json"
            runners_path.write_text(
                json.dumps(
                    {
                        "runners": [
                            {
                                "symbol": "RVI",
                                "date": "2026-03-06",
                                "change_pct": 599.83,
                                "close_price": 21.0,
                                "volume_spike": 0.87,
                            }
                        ]
                    }
                )
            )
            watchlist_path.write_text(json.dumps({"items": []}))

            dashboard_module.set_bot(None)
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
                 patch.object(dashboard_module, "_RUNNERS_FILE", runners_path), \
                 patch.object(dashboard_module, "_WATCHLIST_FILE", watchlist_path), \
                 patch.object(dashboard_module, "call_perplexity_text", new=_fake_perplexity):
                client = TestClient(dashboard_module.app)
                resp = client.post(
                    "/api/copilot/chat?token=secret-token",
                    json={
                        "message": "There was a stock that closed up over 500% on Friday, 3/6 and I can't remember what it was.",
                        "history": [],
                    },
                )
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertTrue(payload["ok"])
                self.assertEqual(payload["provider"], "local")
                self.assertIn("RVI", payload["answer"])
                self.assertFalse(called["perplexity"])

    def test_dashboard_copilot_chat_endpoint_uses_engine_context(self):
        captured = {}

        async def _fake_perplexity(prompt, max_tokens=900):
            captured["prompt"] = prompt
            return "Most likely SOTY based on the current scanner context."

        class _Scanner:
            def get_cached_candidates(self):
                return [
                    {
                        "symbol": "SOTY",
                        "score": 1.42,
                        "price": 7.25,
                        "change_pct": 552.0,
                        "strategy_tag": "pharma_catalyst",
                    }
                ]

            def get_last_market_regime(self):
                return "risk_on"

        class _Watchlist:
            def get_all(self):
                return [{"ticker": "SOTY", "side": "long", "reason": "FDA approval", "conviction": 0.82, "sources": "pharma"}]

        class _EntryManager:
            def get_positions(self):
                return []

        class _Bot:
            scanner = _Scanner()
            watchlist = _Watchlist()
            entry_manager = _EntryManager()
            human_intel_store = type("IntelStub", (), {"list_entries": lambda self, limit=20: []})()
            copy_trader_monitor = None

        dashboard_module.set_bot(_Bot())
        try:
            with patch.object(dashboard_module.settings, "DASHBOARD_TOKEN", "secret-token"), \
                 patch.object(dashboard_module, "call_perplexity_text", new=_fake_perplexity):
                client = TestClient(dashboard_module.app)
                resp = client.post(
                    "/api/copilot/chat?token=secret-token",
                    json={"message": "I saw soty up huge after hours Friday. What was it?", "history": []},
                )
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertTrue(payload["ok"])
                self.assertIn("SOTY", payload["answer"])
                self.assertIn('"symbol": "SOTY"', captured["prompt"])
        finally:
            dashboard_module.set_bot(None)


if __name__ == "__main__":
    unittest.main()
