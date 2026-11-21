import asyncio
import datetime
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yandexbot.core import config, schedule
from yandexbot.modules import epd_prices

D = datetime.datetime


class ScheduleTests(unittest.TestCase):
    def test_first_run_waits_for_time_of_day(self):
        self.assertFalse(schedule.is_due(7, "09:00", None, D(2026, 9, 18, 8, 59)))
        self.assertTrue(schedule.is_due(7, "09:00", None, D(2026, 9, 18, 9, 0)))

    def test_interval_is_counted_in_calendar_days(self):
        last = D(2026, 9, 11, 9, 4)  # проверка раз в 5 минут — запуск случился в 09:04
        self.assertFalse(schedule.is_due(7, "09:00", last, D(2026, 9, 18, 8, 59)))
        # Не сдвигаемся на 09:04: следующий запуск снова в 09:00.
        self.assertTrue(schedule.is_due(7, "09:00", last, D(2026, 9, 18, 9, 0)))
        self.assertEqual(schedule.next_run(7, "09:00", last), D(2026, 9, 18, 9, 0))

    def test_missed_run_happens_at_next_check(self):
        self.assertTrue(schedule.is_due(1, "02:30", D(2026, 9, 10, 2, 31), D(2026, 9, 18, 14, 0)))

    def test_validation(self):
        self.assertEqual(schedule.validate("3", "07:05"), (3, "07:05"))
        for interval, at in ((0, "07:00"), (1, "7:00"), (1, "24:00")):
            with self.subTest(interval=interval, at=at), self.assertRaises(ValueError):
                schedule.validate(interval, at)

    def test_last_run_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "last.json"
            self.assertIsNone(schedule.load_last_run(path))
            schedule.save_last_run(path, D(2026, 9, 18, 9, 1))
            self.assertEqual(schedule.load_last_run(path), D(2026, 9, 18, 9, 1))


class EpdSchedulerTests(unittest.TestCase):
    def setUp(self):
        self._stack = ExitStack()
        directory = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self._stack.enter_context(patch.object(config, "ADMIN_DB_PATH", directory / "admin.db"))
        self._stack.enter_context(patch.object(config, "EPD_STATE_FILE", directory / "epd_last_run.json"))
        self.run_mock = self._stack.enter_context(
            patch.object(epd_prices, "run", new=AsyncMock(return_value={"ok": True, "summary": "", "file": "x"}))
        )

    def tearDown(self):
        self._stack.close()

    def _save(self, enabled: bool, auto_enabled: bool, interval: int = 7, at: str = "09:00"):
        config.save_epd_prices_module_settings({
            **config.epd_prices_module_settings(), "enabled": enabled,
            "auto": {"enabled": auto_enabled, "interval_days": interval, "at": at},
        }, "test")

    def test_auto_run_is_off_by_default(self):
        self.assertFalse(config.epd_prices_module_settings()["auto"]["enabled"])
        self.assertFalse(asyncio.run(epd_prices.scheduler_tick(D(2026, 9, 18, 12, 0))))
        self.run_mock.assert_not_called()

    def test_due_run_broadcasts_and_remembers_time(self):
        self._save(True, True, interval=3, at="09:00")
        self.assertTrue(asyncio.run(epd_prices.scheduler_tick(D(2026, 9, 18, 12, 0))))
        self.run_mock.assert_awaited_once()
        self.assertIsNone(self.run_mock.await_args.kwargs.get("target"))
        self.assertIn("Следующая автоматическая выгрузка ЭПД", self.run_mock.await_args.kwargs["footer"])
        self.assertTrue(self.run_mock.await_args.kwargs["only_changes"])  # по умолчанию включено
        self.assertIsNotNone(epd_prices.load_last_auto_run())
        # Сразу после запуска следующий ещё не положен.
        self.assertFalse(asyncio.run(epd_prices.scheduler_tick()))
        self.assertEqual(self.run_mock.await_count, 1)

    def test_disabled_module_does_not_run(self):
        self._save(False, True)
        self.assertFalse(asyncio.run(epd_prices.scheduler_tick(D(2026, 9, 18, 12, 0))))
        self.run_mock.assert_not_called()

    def test_failed_run_is_not_retried_every_check(self):
        self._save(True, True)
        self.run_mock.side_effect = RuntimeError("сеть")
        with self.assertRaises(RuntimeError):
            asyncio.run(epd_prices.scheduler_tick(D(2026, 9, 18, 12, 0)))
        self.assertIsNotNone(epd_prices.load_last_auto_run())

    def test_saving_commands_keeps_schedule(self):
        self._save(True, True, interval=2, at="06:15")
        config.save_epd_prices_module_settings({
            **config.epd_prices_module_settings(), "enabled": True, "commands": "/эпд",
        }, "test")
        self.assertEqual(config.epd_prices_module_settings()["auto"],
                         {"enabled": True, "interval_days": 2, "at": "06:15", "only_changes": True})


if __name__ == "__main__":
    unittest.main()
