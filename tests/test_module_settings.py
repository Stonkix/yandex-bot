"""Проверки изменяемых настроек модуля prices."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yandexbot.core import config


class PricesModuleSettingsTests(unittest.TestCase):
    def test_saved_override_changes_auto_and_manual_profiles(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "admin.db"
            with patch.object(config, "ADMIN_DB_PATH", db_path):
                config.save_prices_module_settings({
                    "enabled": True,
                    "auto": {
                        "enabled": True,
                        "interval_days": "3",
                        "at": "01:45",
                        "sbis_delay_sec": "2.5",
                        "kontur_region_delay_sec": "17",
                        "kontur_region_delay_max_sec": "25",
                    },
                    "manual": {
                        "sbis_delay_sec": "0.1",
                        "kontur_region_delay_sec": "0.2",
                        "kontur_region_delay_max_sec": "0.6",
                        "main_files_only": True,
                    },
                })
                self.assertEqual(config.prices_run_settings("auto"), {
                    "sbis_delay_sec": 2.5, "kontur_region_delay_sec": 17.0,
                    "kontur_region_delay_max_sec": 25.0,
                })
                self.assertEqual(config.prices_run_settings("manual"), {
                    "sbis_delay_sec": 0.1, "kontur_region_delay_sec": 0.2,
                    "kontur_region_delay_max_sec": 0.6,
                })
                self.assertTrue(config.prices_module_settings()["manual"]["main_files_only"])
                self.assertEqual(config.prices_auto_schedule(), {
                    "module_enabled": True, "enabled": True,
                    "interval_days": 3, "at": "01:45", "only_changes": True,
                })

    def test_manual_file_selection_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "admin.db"
            with patch.object(config, "ADMIN_DB_PATH", db_path):
                config.save_prices_module_settings({
                    "enabled": True,
                    "auto": {
                        "enabled": True, "interval_days": 7, "at": "02:30",
                        "sbis_delay_sec": 1, "kontur_region_delay_sec": 10,
                    },
                    "manual": {
                        "sbis_delay_sec": 0.3, "kontur_region_delay_sec": 1,
                        "main_files_only": False,
                    },
                })
                self.assertFalse(config.prices_module_settings()["manual"]["main_files_only"])

    def test_rejects_invalid_clock_value(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "admin.db"
            with patch.object(config, "ADMIN_DB_PATH", db_path):
                with self.assertRaisesRegex(ValueError, "ЧЧ:ММ"):
                    config.save_prices_module_settings({
                        "enabled": True,
                        "auto": {
                            "enabled": True, "interval_days": 1, "at": "25:00",
                            "sbis_delay_sec": 1, "kontur_region_delay_sec": 1,
                        },
                        "manual": {"sbis_delay_sec": 0, "kontur_region_delay_sec": 0, "main_files_only": True},
                    })

    def test_enrich_settings_save_commands_and_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "admin.db"
            with patch.object(config, "ADMIN_DB_PATH", db_path):
                config.save_enrich_module_settings({
                    "enabled": True, "daily_limit": 5000,
                    "requests_per_second": 12,
                    "commands": "/обогатить\n/инн",
                }, "tester")
                settings = config.enrich_module_settings()
                self.assertEqual(settings["commands"], ["/инн", "/обогатить"])
                self.assertEqual(settings["requests_per_second"], 12)

    def test_rejects_unsafe_or_excessive_enrich_speed(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "admin.db"
            with patch.object(config, "ADMIN_DB_PATH", db_path):
                with self.assertRaisesRegex(ValueError, "от 1 до 25"):
                    config.save_enrich_module_settings({
                        "enabled": True, "daily_limit": 5000,
                        "requests_per_second": 26, "commands": "/обогатить",
                    }, "tester")


if __name__ == "__main__":
    unittest.main()
