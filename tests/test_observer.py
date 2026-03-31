import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai import observer as observer_module
from src.ai.observer import Observer, _parse_json


class ObserverPersistenceTests(unittest.TestCase):
    def test_save_ignores_dict_shaped_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "observations.json").write_text('{"oops": "dict"}')

            with patch.object(observer_module, "DATA_DIR", data_dir), \
                 patch.object(observer_module.settings, "ANTHROPIC_API_KEY", None):
                observer = Observer()
                observer._save({"market_assessment": "stable"})

            saved = observer_module.json.loads((data_dir / "observations.json").read_text())
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["market_assessment"], "stable")
            self.assertIn("timestamp", saved[0])

    def test_save_ignores_malformed_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_dir = Path(tmp)
            (data_dir / "observations.json").write_text("{not json")

            with patch.object(observer_module, "DATA_DIR", data_dir), \
                 patch.object(observer_module.settings, "ANTHROPIC_API_KEY", None):
                observer = Observer()
                observer._save({"market_assessment": "risk off"})

            saved = observer_module.json.loads((data_dir / "observations.json").read_text())
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0]["market_assessment"], "risk off")

    def test_runtime_ready_waits_for_first_completed_scan(self):
        bot = type(
            "Bot",
            (),
            {
                "start_time": 100.0,
                "scanner": type(
                    "Scanner",
                    (),
                    {"_last_scan_stats": {"status": "running", "last_completed_at": None}},
                )(),
            },
        )()

        self.assertFalse(Observer._runtime_ready(bot, 160.0))
        self.assertTrue(Observer._runtime_ready(bot, 281.0))

    def test_parse_json_unwraps_list_payload_into_last_dict(self):
        parsed = _parse_json(
            '[{"market_assessment":"stale"},{"market_assessment":"fresh","risk_flags":["retest"]}]'
        )

        self.assertIsInstance(parsed, dict)
        self.assertEqual(parsed["market_assessment"], "fresh")
        self.assertEqual(parsed["risk_flags"], ["retest"])


if __name__ == "__main__":
    unittest.main()
