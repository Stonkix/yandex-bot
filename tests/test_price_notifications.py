"""Режим «Отправлять только при изменении цен» для плановых выгрузок 1СО и ЭПД."""

import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yandexbot import bot
from yandexbot.admin import store as admin_store
from yandexbot.core import config
from yandexbot.core.utils import RUN_KIND_AUTO, RUN_KIND_MANUAL
from yandexbot.modules import epd_prices, prices

CHATS = [{"chat_id": "0/0/general", "title": "Ботики", "broadcast": True, "commands": True}]
KEY = lambda row: (row["region_code"], row["tariff_key"])  # noqa: E731
PRICE = lambda row: row["price"]  # noqa: E731


class ComparePricesTests(unittest.TestCase):
    def test_counts_changed_and_missing_positions(self):
        prev = {("77", "a"): 100, ("77", "b"): 200, ("78", "a"): 300}
        rows = [{"region_code": "77", "tariff_key": "a", "price": 100},
                {"region_code": "77", "tariff_key": "b", "price": 250}]
        comparison = prices.compare_prices(rows, prev, KEY, PRICE)
        self.assertEqual(comparison, {"changed": 1, "missing": 1, "new_tariffs": []})
        message = prices.build_diff_message("СБИС", comparison)
        self.assertIn("изменилось 1 цен", message)
        self.assertIn("нет данных по 1 позициям", message)

    def test_reports_new_tariffs_and_business_forms(self):
        prev = {("77", "DOCX | Оптимальный + | ЮЛ на ОСНО"): 100}
        rows = [
            {"region_code": "77", "tariff_key": "DOCX | Оптимальный + | ЮЛ на ОСНО", "price": 100},
            {"region_code": "77", "tariff_key": "DOCX | Учетный для АУСН | ИП на АУСН", "price": 8000},
            {"region_code": "78", "tariff_key": "DOCX | Учетный для АУСН | ИП на АУСН", "price": 8000},
        ]
        comparison = prices.compare_prices(rows, prev, KEY, PRICE)
        self.assertEqual(comparison["new_tariffs"], ["DOCX | Учетный для АУСН | ИП на АУСН"])
        self.assertTrue(prices.has_news({"ok": True, "comparison": comparison}))
        self.assertIn("Учетный для АУСН | ИП на АУСН", prices.build_diff_message("Контур", comparison))

    def test_no_history(self):
        self.assertIsNone(prices.compare_prices([], {}, KEY, PRICE))
        self.assertIn("первая выгрузка", prices.build_diff_message("СБИС", None))

    def test_missing_amount_is_not_a_price_change(self):
        row = {"region_code": "77", "tariff_key": "a", "price": None}
        comparison = prices.compare_prices([row], {("77", "a"): 100}, KEY, PRICE)
        self.assertEqual(comparison, {"changed": 0, "missing": 1, "new_tariffs": []})

    def test_has_news(self):
        self.assertFalse(prices.has_news({"ok": True, "comparison": {"changed": 0, "missing": 0}}))
        self.assertTrue(prices.has_news({"ok": True, "comparison": {"changed": 2, "missing": 0}}))
        self.assertTrue(prices.has_news({"ok": True, "comparison": {"changed": 0, "missing": 3}}))
        self.assertTrue(prices.has_news({"ok": True, "comparison": None}))
        self.assertTrue(prices.has_news({"ok": False, "summary": "❌ СБИС: 403", "file": None}))


class NotificationTestCase(unittest.TestCase):
    def setUp(self):
        self._stack = ExitStack()
        self.dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in {
            "ADMIN_DB_PATH": self.dir / "admin.db", "CONFIG_DIR": self.dir, "DB_PATH": self.dir / "prices.db",
            "STATE_FILE": self.dir / "last_run.json", "EPD_STATE_FILE": self.dir / "epd_last_run.json",
        }.items():
            self._stack.enter_context(patch.object(config, name, value))
        admin_store.replace_chats(config.ADMIN_DB_PATH, CHATS, "test")

    def tearDown(self):
        self._stack.close()


class PricesOnlyChangesTests(NotificationTestCase):
    def _run(self, comparisons: list, manual: bool = False, only_changes: bool = True):
        config.save_prices_module_settings({
            **config.prices_module_settings(),
            "auto": {**config.prices_module_settings()["auto"], "only_changes": only_changes},
        }, "test")
        results = [{"ok": True, "summary": f"итог {i}", "file": None, "comparison": comparison}
                   for i, comparison in enumerate(comparisons)]

        async def sbis(*_args, **_kwargs):
            return results[0]

        async def kontur(*_args, **_kwargs):
            return results[1]

        with patch.object(prices, "run_sbis", new=sbis), patch.object(prices, "run_kontur", new=kontur), \
                patch.object(prices, "ym_send_text", new=AsyncMock()) as sent, \
                patch.object(prices, "ym_send_file", new=AsyncMock()):
            asyncio.run(prices.run("both", force=True, announce=manual))
        return sent

    def test_scheduled_run_without_changes_is_silent(self):
        same = {"changed": 0, "missing": 0}
        sent = self._run([same, same])
        sent.assert_not_awaited()
        self.assertIsNotNone(prices._load_last_auto_run())

    def test_scheduled_run_with_changes_is_sent(self):
        sent = self._run([{"changed": 0, "missing": 0}, {"changed": 3, "missing": 0}])
        self.assertIn("итог 1", sent.await_args.args[0])

    def test_missing_positions_are_sent(self):
        sent = self._run([{"changed": 0, "missing": 0}, {"changed": 0, "missing": 90}])
        sent.assert_awaited()

    def test_switch_off_sends_always(self):
        same = {"changed": 0, "missing": 0}
        self._run([same, same], only_changes=False).assert_awaited()

    def test_manual_run_sends_always(self):
        same = {"changed": 0, "missing": 0}
        self._run([same, same], manual=True).assert_awaited()

    def test_admin_run_reports_partial_collection_and_delivery_failure(self):
        job = {"module_code": "prices", "kind": "full", "delivery": "chat", "target_chat_id": "0/0/general"}
        for ok, sent, expected_status, expected_message in (
            (False, True, "failed", "Сбор цен — частично. Доставка — подтверждена."),
            (True, False, "failed", "Сбор цен — успешно. Доставка — с ошибками"),
            (True, True, "success", "Сбор цен — успешно. Доставка — подтверждена."),
        ):
            result = {"ok": ok, "sent": sent, "files": ["prices.xlsx"], "summary": "итог",
                      "delivery_errors": [] if sent else ["HTTP 413"]}
            with self.subTest(result=result), patch.object(prices, "run", new=AsyncMock(return_value=result)):
                status, summary = asyncio.run(bot._run_admin_job(job))
                self.assertEqual(status, expected_status)
                self.assertIn(expected_message, summary)


class EpdOnlyChangesTests(NotificationTestCase):
    ROWS = [epd_prices._package_row(1000, 5000.0, epd_prices.SBIS_URL, "")]

    def _run(self, rows, only_changes: bool = True, fail_kontur: bool = False):
        async def sbis(_session, sources_dir=None):
            return rows, []

        async def kontur(_session, sources_dir=None):
            if fail_kontur:
                raise RuntimeError("HTTP 403")
            return rows, []

        with patch.dict(epd_prices._FETCHERS, {"epd_sbis": sbis, "epd_kontur": kontur}), \
                patch.object(epd_prices, "ym_send_text", new=AsyncMock()) as sent, \
                patch.object(epd_prices, "ym_send_file", new=AsyncMock(return_value=[])) as sent_file:
            result = asyncio.run(epd_prices.run(only_changes=only_changes))
        return result, sent, sent_file

    def _seed(self, rows):
        """Прошлая выгрузка — с более ранним временем, чем текущая."""
        conn = epd_prices.db_connect()
        try:
            for source in ("epd_sbis", "epd_kontur"):
                epd_prices.db_save(conn, source, rows, "2026-09-01T09:00:00")
        finally:
            conn.close()

    def test_first_run_is_sent(self):
        result, sent, _ = self._run(self.ROWS)
        self.assertTrue(result["sent"])
        sent.assert_awaited_once()

    def test_same_prices_are_silent(self):
        self._seed(self.ROWS)
        result, sent, sent_file = self._run(self.ROWS)
        self.assertFalse(result["sent"])
        sent.assert_not_awaited()
        sent_file.assert_not_awaited()
        self.assertTrue(Path(result["file"]).is_file())  # Excel всё равно сохранён

    def test_changed_prices_are_sent(self):
        self._seed(self.ROWS)
        changed = [epd_prices._package_row(1000, 5500.0, epd_prices.SBIS_URL, "")]
        result, sent, _ = self._run(changed)
        self.assertTrue(result["sent"])
        self.assertIn("ЕСТЬ ИЗМЕНЕНИЯ ЦЕН", sent.await_args.args[0])

    def test_source_error_is_sent(self):
        self._seed(self.ROWS)
        result, sent, _ = self._run(self.ROWS, fail_kontur=True)
        self.assertTrue(result["sent"])
        self.assertIn("HTTP 403", sent.await_args.args[0])

    def test_excel_failure_leaves_price_history_intact(self):
        self._seed(self.ROWS)
        changed = [epd_prices._package_row(1000, 5500.0, epd_prices.SBIS_URL, "")]
        with patch.object(epd_prices, "save_excel", side_effect=OSError("файл недоступен")):
            with self.assertRaises(OSError):
                self._run(changed)
        with closing(epd_prices.db_connect()) as conn:
            saved = conn.execute("SELECT DISTINCT price_tariff FROM epd_prices").fetchall()
            self.assertEqual(saved, [(5000.0,)])

    def test_switch_off_sends_same_prices(self):
        self._seed(self.ROWS)
        result, sent, _ = self._run(self.ROWS, only_changes=False)
        self.assertTrue(result["sent"])
        sent.assert_awaited_once()


class ScheduledBaselineTests(unittest.TestCase):
    """Плановая выгрузка сравнивается с прошлой плановой, а не с ручной между ними."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.object(config, "DB_PATH", Path(self._tmp.name) / "prices.db")
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def _rows(price):
        return [{"region_code": "77", "region_name": "Москва", "tariff_key": "a", "price": price}]

    def test_1co_manual_run_does_not_hide_change_from_scheduled(self):
        conn = prices.db_connect()
        try:
            prices.db_save_prices(conn, "sbis", self._rows(100), "2026-10-01T02:30:00", RUN_KIND_AUTO)
            prices.db_save_prices(conn, "sbis", self._rows(120), "2026-10-03T12:00:00", RUN_KIND_MANUAL)
            now = "2026-10-08T02:30:00"
            self.assertEqual(prices.db_get_previous(conn, "sbis", now, RUN_KIND_AUTO), {("77", "a"): 100})
            self.assertEqual(prices.db_get_previous(conn, "sbis", now, RUN_KIND_MANUAL), {("77", "a"): 120})
        finally:
            conn.close()

    def test_1co_old_history_without_mark_is_a_baseline(self):
        conn = sqlite3.connect(config.DB_PATH)
        conn.execute("""CREATE TABLE prices (id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL, region_code TEXT NOT NULL, region_name TEXT NOT NULL,
            tariff_key TEXT NOT NULL, price REAL, fetched_at TEXT NOT NULL)""")
        conn.execute("INSERT INTO prices(source,region_code,region_name,tariff_key,price,fetched_at)"
                     " VALUES('sbis','77','Москва','a',90,'2026-09-01T02:30:00')")
        conn.commit()
        conn.close()
        conn = prices.db_connect()  # добавляет колонку run_kind к старой таблице
        try:
            self.assertEqual(
                prices.db_get_previous(conn, "sbis", "2026-10-01T02:30:00", RUN_KIND_AUTO),
                {("77", "a"): 90},
            )
        finally:
            conn.close()

    def test_blank_prices_in_legacy_sbis_run_do_not_erase_previous_amount(self):
        with closing(prices.db_connect()) as conn:
            prices.db_save_prices(conn, "sbis", self._rows(90), "2026-10-01T02:30:00", RUN_KIND_AUTO)
            prices.db_save_prices(conn, "sbis", self._rows(None), "2026-10-03T02:30:00", RUN_KIND_AUTO)
            previous = prices.db_get_previous(conn, "sbis", "2026-10-05T02:30:00", RUN_KIND_AUTO)
        self.assertEqual(previous, {("77", "a"): 90})

    def test_epd_manual_run_does_not_hide_change_from_scheduled(self):
        row = {"entry_type": "tariff", "tariff_name": "100 документов", "variant": "",
               "price_document": None, "source_url": "https://example.test"}
        results = lambda price: {"sbis": [{**row, "price_tariff": price}]}  # noqa: E731
        epd_prices.previous_prices(results(1000), "2026-10-01T03:00:00", save=True, run_kind=RUN_KIND_AUTO)
        epd_prices.previous_prices(results(1200), "2026-10-03T12:00:00", save=True)
        previous = epd_prices.previous_prices(
            results(1200), "2026-10-08T03:00:00", save=True, run_kind=RUN_KIND_AUTO,
        )
        self.assertEqual(previous["sbis"], {("tariff", "100 документов", ""): (1000, None)})
        changes = epd_prices.price_changes(results(1200)["sbis"], previous["sbis"])
        self.assertEqual(len(changes["changed"]), 1)


if __name__ == "__main__":
    unittest.main()
