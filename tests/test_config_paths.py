import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yandexbot.core import config


class SettingsPathTests(unittest.TestCase):
    """Файлы настроек переехали в config/, но старая раскладка в корне продолжает работать."""

    def _resolve(self, root: Path, name: str) -> Path:
        with patch.object(config, "PROJECT_DIR", root), patch.object(config, "SETTINGS_DIR", root / "config"):
            return config._settings_file(name)

    def test_new_location_is_preferred(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config").mkdir()
            (root / "config" / "chats.toml").write_text("", encoding="utf-8")
            (root / "chats.toml").write_text("", encoding="utf-8")
            self.assertEqual(self._resolve(root, "chats.toml"), root / "config" / "chats.toml")

    def test_legacy_root_file_is_used_until_moved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "chats.toml").write_text("", encoding="utf-8")
            self.assertEqual(self._resolve(root, "chats.toml"), root / "chats.toml")

    def test_missing_file_points_to_new_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(self._resolve(root, "chats.toml"), root / "config" / "chats.toml")

    def test_runtime_data_stays_in_stat(self):
        self.assertEqual(config.CONFIG_DIR, config.PROJECT_DIR / "stat")
        self.assertEqual(config.DB_PATH, config.PROJECT_DIR / "stat" / "prices.db")


if __name__ == "__main__":
    unittest.main()
