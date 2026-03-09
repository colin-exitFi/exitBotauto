import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.ai import observer as observer_module
from src.ai.observer import Observer


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


if __name__ == "__main__":
    unittest.main()
