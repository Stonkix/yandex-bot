import csv
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from yandexbot.core import config
from yandexbot.modules import dadata_reports, dadata_store, enrich


class DaDataStoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(config, "CONFIG_DIR", self.directory))
        dadata_store.init_db()

    def expire(self, *paths: Path):
        old = time.time() - dadata_reports.FILE_TTL_SEC - 60
        for path in paths:
            os.utime(path, (old, old))

    def test_request_counts_continue_from_legacy_value(self):
        day = "2026-11-22"
        self.assertEqual(dadata_store.request_count(day, 3), 3)
        self.assertTrue(dadata_store.reserve_request(day, 4, legacy_count=3))
        self.assertFalse(dadata_store.reserve_request(day, 4, legacy_count=3))
        self.assertEqual(dadata_store.request_count(day, 0), 4)
        self.assertEqual(dadata_store.request_count("2026-11-23", 0), 0)

    def test_imports_legacy_report_before_expiring_its_files(self):
        stem = "enrich_22.11.26_130000_000000"
        complete = self.directory / f"{stem}.xlsx"
        parts = self.directory / f"{stem}_часть_01_из_01.xlsx"
        remainder = self.directory / f"{stem}_остаток.xlsx"
        original = dadata_reports.upload_path(".txt", job_id=37)
        other = self.directory / "prices_sources_202611.zip"
        for path in (original, parts, other):
            path.write_bytes(b"test")
        found = {"inn": "0123456789", "found": True, "name": "Организация", "phones": ["+7 000 000-00-00"]}
        enrich.save_dadata_excel([
            found, found, {"inn": "7707083893", "found": False},
            {"inn": "7702070139", "found": False, "error": "HTTP 503"},
        ], str(complete))
        enrich.save_remaining_inns(["7702070139"], remainder)

        dadata_reports.maintain_files()
        dadata_reports.maintain_files()
        totals = dadata_store.summary()
        self.assertEqual((totals["inns"], totals["found"], totals["not_found"], totals["imported_rows"]), (2, 1, 1, 2))
        _, first, second = csv.reader(dadata_store.csv_rows(), delimiter=";")
        self.assertEqual(first[:2], ["0123456789", ""])
        self.assertTrue(first[2].endswith("+00:00"))
        self.assertEqual(json.loads(first[-1])["Номер 1"], "+7 000 000-00-00")
        self.assertEqual(second[5], "Не найдено")
        self.assertTrue(complete.exists())

        self.expire(complete, remainder, parts, original, other)
        dadata_reports.maintain_files()
        self.assertFalse(any(path.exists() for path in (complete, remainder, parts, original)))
        self.assertTrue(other.exists())
        self.assertEqual(dadata_store.summary(), totals)

    def test_imports_parts_and_keeps_unreadable_file(self):
        part = self.directory / "enrich_22.11.26_130001_часть_01_из_02.xlsx"
        broken = self.directory / "enrich_22.11.26_130002.xlsx"
        enrich.save_dadata_excel([{"inn": "7707083893", "found": False}], str(part))
        broken.write_bytes(b"broken")
        self.expire(part, broken)
        with self.assertLogs(level="ERROR"):
            dadata_reports.maintain_files()
        self.assertFalse(part.exists())
        self.assertTrue(broken.exists())
        self.assertEqual(dadata_store.summary()["imported_rows"], 1)

    def test_imports_only_dadata_columns_from_enriched_workbook(self):
        original = self.directory / "original.xlsx"
        report = self.directory / "enrich_22.11.26_130003.xlsx"
        workbook = Workbook()
        workbook.active.append(["ИНН клиента", "Внутренняя пометка"])
        workbook.active.append([123456789, "Свои данные"])
        workbook.save(original)
        enrich.save_enriched_source_excel(str(original), str(report), [
            {"inn": "0123456789", "found": True, "name": "Из DaData", "address": "Москва"},
        ])
        dadata_reports.maintain_files()
        _, row = csv.reader(dadata_store.csv_rows(), delimiter=";")
        self.assertEqual(row[0], "0123456789")
        fields = json.loads(row[-1])
        self.assertEqual(fields["Адрес"], "Москва")
        self.assertNotIn("Внутренняя пометка", fields)
