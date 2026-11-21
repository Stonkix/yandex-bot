"""Архивы исходников выгрузок (stat/archive/)."""

import asyncio
import datetime
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yandexbot.admin import store as admin_store
from yandexbot.core import config
from yandexbot.modules import archives, epd_prices, prices

CHATS = [
    {"chat_id": "0/0/general", "title": "Ботики", "broadcast": True, "commands": True},
]


def _zip_names(path: Path) -> set[str]:
    with zipfile.ZipFile(path) as archive:
        return {name for name in archive.namelist() if not name.endswith("/")}


class ArchiveTestCase(unittest.TestCase):
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


class ArchiveFilesTests(ArchiveTestCase):
    def test_make_archive_goes_to_stat_archive(self):
        source = self.dir / "src"
        archives.keep_source(source, "sbis/api.json", '{"ok": true}')
        archives.keep_source(source, "kontur/price.pdf", b"%PDF")
        path = archives.make_archive("epd", source)
        self.assertEqual(path.parent, self.dir / "archive")
        self.assertTrue(path.name.startswith("epd_sources_"))
        self.assertEqual(_zip_names(path), {"sbis/api.json", "kontur/price.pdf"})

    def test_nothing_to_pack_gives_no_archive(self):
        (self.dir / "empty").mkdir()
        self.assertIsNone(archives.make_archive("prices", self.dir / "empty"))
        archives.keep_source(None, "sbis/x.json", "ничего не пишется")

    def test_one_archive_per_kind_and_calendar_month(self):
        source = self.dir / "src"
        first = datetime.date(2026, 11, 1)
        later = datetime.date(2026, 11, 30)
        next_month = datetime.date(2026, 12, 1)
        archives.keep_source(source, "sbis/api.json", "первый отчёт")
        archive = archives.make_archive("epd", source, run_date=first)
        self.assertEqual(archive.name, "epd_sources_202611.zip")
        self.assertFalse(archives.needs_archive("epd", later))
        self.assertTrue(archives.needs_archive("prices", later))
        archives.keep_source(source, "sbis/api.json", "второй отчёт")
        self.assertIsNone(archives.make_archive("epd", source, run_date=later))
        with zipfile.ZipFile(archive) as booked:
            self.assertEqual(booked.read("sbis/api.json").decode("utf-8"), "первый отчёт")
        self.assertEqual(archives.make_archive("prices", source, run_date=later).name, "prices_sources_202611.zip")
        self.assertEqual(archives.make_archive("epd", source, run_date=next_month).name, "epd_sources_202612.zip")

    def test_failed_pack_does_not_consume_month(self):
        source = self.dir / "src"
        archives.keep_source(source, "sbis/api.json", "{}")
        day = datetime.date(2026, 11, 8)
        with patch.object(archives.shutil, "make_archive", side_effect=OSError("тест записи")):
            with self.assertRaises(OSError):
                archives.make_archive("epd", source, run_date=day)
        self.assertEqual(archives.list_archives(), [])
        self.assertTrue(archives.needs_archive("epd", day))
        self.assertIsNotNone(archives.make_archive("epd", source, run_date=day))

    def test_old_archives_from_stat_are_moved(self):
        (self.dir / "kontur_sources_20260825_154500.zip").write_bytes(b"zip")
        (self.dir / "sbis_18.09.26.xlsx").write_bytes(b"xlsx")
        self.assertEqual(archives.migrate_old_archives(), 1)
        self.assertTrue((self.dir / "archive" / "kontur_sources_20260825_154500.zip").is_file())
        self.assertTrue((self.dir / "sbis_18.09.26.xlsx").is_file())
        self.assertEqual(archives.kind_of("kontur_sources_20260825_154500.zip"), "prices")
        self.assertEqual(archives.migrate_old_archives(), 0)


class ProductArchiveTests(ArchiveTestCase):
    def test_prices_run_packs_sbis_and_kontur_into_one_archive(self):
        sbis_xlsx, kontur_xlsx = self.dir / "sbis_18.09.26.xlsx", self.dir / "kontur_18.09.26.xlsx"
        sbis_xlsx.write_bytes(b"sbis prices")
        kontur_xlsx.write_bytes(b"kontur prices")

        async def sbis(_delay, sources_dir=None, **_kwargs):
            archives.keep_source(sources_dir / "sbis", "77.json", "{}")
            return {"ok": True, "summary": "СБИС", "file": str(sbis_xlsx)}

        async def kontur(_regions, sources_dir=None, **_kwargs):
            archives.keep_source(sources_dir / "kontur", "77/01_price.docx", b"docx")
            return {"ok": True, "summary": "Контур", "file": str(kontur_xlsx)}

        with patch.object(prices, "run_sbis", new=sbis), patch.object(prices, "run_kontur", new=kontur), \
                patch.object(prices, "ym_send_text", new=AsyncMock()) as sent, \
                patch.object(prices, "ym_send_file", new=AsyncMock()):
            asyncio.run(prices.run("both", force=True))
        [archive] = archives.list_archives("prices")
        self.assertTrue(archive.name.startswith("prices_sources_"))
        # Цены обоих поставщиков — готовыми Excel в корне, рядом исходники.
        self.assertEqual(_zip_names(archive), {
            "sbis_18.09.26.xlsx", "kontur_18.09.26.xlsx", "sbis/77.json", "kontur/77/01_price.docx",
        })
        self.assertIn(f"Архив исходников: {archive.name}", sent.await_args_list[0].args[0])

    def test_test_run_is_not_archived(self):
        async def kontur(_regions, **_kwargs):
            return {"ok": True, "summary": "тест", "file": None}

        with patch.object(prices, "run_kontur", new=kontur), \
                patch.object(prices, "ym_send_text", new=AsyncMock()), patch.object(prices, "ym_send_file", new=AsyncMock()):
            asyncio.run(prices.run("kontur-test", force=True))
        self.assertEqual(archives.list_archives(), [])

    def test_epd_run_packs_excel_and_sources(self):
        rows = [epd_prices._package_row(1000, 5000.0, epd_prices.SBIS_URL, "")]
        collected_dirs = []

        async def sbis(_session, sources_dir=None):
            collected_dirs.append(sources_dir)
            archives.keep_source(sources_dir, "sbis/api_services.json", "{}")
            return rows, []

        async def kontur(_session, sources_dir=None):
            archives.keep_source(sources_dir, "kontur/logistika-price.pdf", b"%PDF")
            return rows, []

        with patch.dict(epd_prices._FETCHERS, {"epd_sbis": sbis, "epd_kontur": kontur}), \
                patch.object(epd_prices, "ym_send_text", new=AsyncMock()) as sent, \
                patch.object(epd_prices, "ym_send_file", new=AsyncMock(return_value=[])):
            result = asyncio.run(epd_prices.run())
            repeated = asyncio.run(epd_prices.run())
        [archive] = archives.list_archives("epd")
        self.assertEqual(archive.parent, self.dir / "archive")
        self.assertEqual(_zip_names(archive), {
            Path(result["file"]).name, "sbis/api_services.json", "kontur/logistika-price.pdf",
        })
        self.assertIn(f"Архив: {archive.name}", sent.await_args_list[0].args[0])
        self.assertEqual(len(sent.await_args_list), 2)
        self.assertTrue(repeated["ok"])
        self.assertTrue(Path(repeated["file"]).is_file())
        self.assertIsNotNone(collected_dirs[0])
        self.assertIsNone(collected_dirs[1])

    def test_partial_runs_leave_month_available(self):
        xlsx = self.dir / "sbis.xlsx"
        xlsx.write_bytes(b"sbis prices")
        sbis_result = {"ok": True, "summary": "СБИС", "file": str(xlsx)}
        kontur_result = {"ok": False, "summary": "Ошибка", "file": None}
        with (
            patch.object(prices, "run_sbis", new=AsyncMock(return_value=sbis_result)),
            patch.object(prices, "run_kontur", new=AsyncMock(return_value=kontur_result)),
            patch.object(prices, "ym_send_text", new_callable=AsyncMock),
            patch.object(prices, "ym_send_file", new_callable=AsyncMock),
        ):
            asyncio.run(prices.run("both", force=True))
            asyncio.run(prices.run("sbis", force=True))
        collected = {
            "results": {"epd_sbis": [epd_prices._package_row(1000, 5000.0, epd_prices.SBIS_URL, "")]},
            "errors": {"Контур": "Ошибка"},
            "notes": [],
        }
        with (
            patch.object(epd_prices, "collect", new=AsyncMock(return_value=collected)),
            patch.object(epd_prices, "ym_send_text", new_callable=AsyncMock),
            patch.object(epd_prices, "ym_send_file", new_callable=AsyncMock),
        ):
            result = asyncio.run(epd_prices.run())
        self.assertTrue(Path(result["file"]).is_file())
        self.assertEqual(archives.list_archives(), [])
