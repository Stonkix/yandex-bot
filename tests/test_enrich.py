import asyncio
import random
import string
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from openpyxl import Workbook, load_workbook

from yandexbot import bot
from yandexbot.admin import status
from yandexbot.admin import store
from yandexbot.modules import enrich


class EnrichStatsTest(unittest.TestCase):
    def test_xlsx_stats_count_duplicates_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "in.xlsx")
            wb = Workbook()
            ws = wb.active
            ws.append(["ИНН"])
            for value in ["7707083893", "7707083893", 770708389, "0770708389", "abc", "5003052454"]:
                ws.append([value])
            wb.save(path)

            inns, total, invalid = enrich.extract_inns_from_xlsx_with_stats(path)

        self.assertEqual(inns, ["7707083893", "0770708389", "5003052454"])
        self.assertEqual((total, invalid), (6, 1))
        self.assertEqual(total - invalid - len(inns), 2)

    def test_txt_stats(self):
        raw = "ИНН\n7707083893\n7707083893\nпусто\n5003052454, 7707083893\n".encode("utf-8")
        inns, total, invalid = enrich.extract_inns_from_txt_with_stats(raw)
        self.assertEqual(inns, ["7707083893", "5003052454"])
        self.assertEqual((total, invalid), (5, 1))
        self.assertEqual(enrich.extract_inns_from_txt(raw), inns)

    def test_duplicates_filled_from_one_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, out = str(Path(tmp) / "in.xlsx"), str(Path(tmp) / "out.xlsx")
            wb = Workbook()
            ws = wb.active
            ws.append(["ИНН"])
            ws.append(["7707083893"])
            ws.append(["7707083893"])
            wb.save(src)

            enrich.save_enriched_source_excel(
                src, out, [{"inn": "7707083893", "found": True, "name": "ПАО Сбербанк"}]
            )
            ws = load_workbook(out).active
            self.assertEqual(ws.cell(2, 2).value, "ПАО Сбербанк")
            self.assertEqual(ws.cell(3, 2).value, "ПАО Сбербанк")

    def test_smb_fields(self):
        # 1420070400000 мс — 01.01.2015 UTC.
        data = {"documents": {"smb": {"type": "SMB", "category": "SMALL", "issue_date": 1420070400000}}}
        self.assertEqual(
            enrich._smb_fields(data),
            {"smb_category": "Малое предприятие", "smb_date": "01.01.2015"},
        )
        self.assertEqual(
            enrich._smb_fields({"documents": {"smb": None}}),
            {"smb_category": "Не в реестре", "smb_date": None},
        )
        self.assertEqual(enrich._smb_fields({})["smb_category"], "Не в реестре")
        self.assertEqual(
            enrich._smb_fields({"documents": {"smb": {"category": "NEW"}}})["smb_category"], "NEW"
        )

    def test_smb_columns_after_employee_count(self):
        result = {
            "inn": "7707083893", "found": True, "employee_count": 12,
            "smb_category": "Микропредприятие", "smb_date": "10.08.2016", "tax_system": "USN",
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.xlsx")
            enrich.save_dadata_excel([result], out)
            ws = load_workbook(out).active
            headers = [cell.value for cell in ws[1]]
            col = headers.index("МСП категория") + 1
            self.assertEqual(headers[col - 2], "Числ. сотрудников")
            self.assertEqual(headers[col], "МСП дата включения")
            self.assertEqual(
                [ws.cell(2, c).value for c in (col - 1, col, col + 1, col + 2)],
                [12, "Микропредприятие", "10.08.2016", "УСН"],
            )

    def test_duration(self):
        self.assertEqual(enrich.format_duration(enrich.estimate_seconds(16000, 25)), "11 мин")
        self.assertEqual(enrich.format_duration(30), "меньше минуты")
        self.assertEqual(enrich.format_duration(2 * 3600 + 300), "2 ч 5 мин")

    def test_finance_headers_and_values(self):
        result = {
            "inn": "7707083893",
            "found": True,
            "finance_by_year": {
                2024: {"profit": 100, "revenue": 500, "income": 550, "expense": 400},
                2023: {"profit": 80, "revenue": 450, "income": 480, "expense": 370},
            },
            "debt": 0,
            "penalty": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            out = str(Path(tmp) / "out.xlsx")
            enrich.save_dadata_excel([result], out)
            ws = load_workbook(out).active
            headers = [cell.value for cell in ws[1]]
            expected_finance = [
                "Прибыль 2024", "Прибыль 2023",
                "Выручка 2024", "Выручка 2023",
                "Доходы 2024", "Доходы 2023",
                "Расходы 2024", "Расходы 2023",
            ]
            for h in expected_finance:
                self.assertIn(h, headers)

            row2 = [cell.value for cell in ws[2]]
            vals = [row2[headers.index(h)] for h in expected_finance]
            self.assertEqual(vals, [100, 80, 500, 450, 550, 480, 400, 370])


class EnrichDeliveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_large_workbook_is_sent_as_complete_numbered_excel_parts(self):
        rows = [("7707083893", "".join(random.Random(i).choices(string.ascii_letters, k=1000)))
                for i in range(1, 36)]
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "enrich_example.xlsx"
            original = Workbook(write_only=True)
            sheet = original.create_sheet("Результат")
            sheet.append(["ИНН", "Результат"])
            for row in rows:
                sheet.append(row)
            original.create_sheet("Приложение")
            original.save(output)
            with (
                patch.object(bot, "YM_MAX_FILE_BYTES", 8800),
                patch.object(bot, "ym_send_file", new=AsyncMock(return_value=[])) as send,
            ):
                note = await bot._send_enrichment_report(output, "Итог", {"chat_id": "chat-A"})
            self.assertIn("отправлен частями", note)
            self.assertGreater(send.await_count, 1)
            reconstructed = []
            for i, call in enumerate(send.await_args_list, 1):
                attachment = Path(call.args[0])
                self.assertLessEqual(attachment.stat().st_size, 8800)
                self.assertIn(f"Часть {i} из {send.await_count}", call.kwargs["caption"])
                self.assertEqual(call.kwargs["target"], {"chat_id": "chat-A"})
                report = load_workbook(attachment, read_only=True)
                try:
                    header, *values = list(report.active.values)
                    self.assertEqual(header, ("ИНН", "Результат"))
                    reconstructed.extend(values)
                finally:
                    report.close()
            self.assertEqual(reconstructed, rows)
            self.assertTrue(output.exists())

    async def test_poller_saves_job_before_advancing_offset(self):
        updates = [
            {"update_id": 10, "chat": {"id": "chat-A", "type": "group"}, "from": {"login": "a"}, "text": "/обогатить"},
            {"update_id": 11, "chat": {"id": "chat-A", "type": "group"}, "from": {"login": "a"},
             "file": {"id": "file-id", "name": "inn.xlsx"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "admin.db"
            store.init_db(db)

            def save_offset(offset):
                if offset == 12:
                    update_id, payload = store.claim_chat_job(db, "enrich")
                    self.assertEqual(update_id, 11)
                    self.assertEqual(payload["file"]["id"], "file-id")

            with (
                patch.object(bot.config, "ADMIN_DB_PATH", db),
                patch.object(bot.config, "enrich_module_settings",
                             return_value={"enabled": True, "commands": ["/обогатить"]}),
                patch.object(bot, "_enrich_allowed_here", return_value=True),
                patch.object(bot, "_load_offset", return_value=None),
                patch.object(bot, "_save_offset", side_effect=save_offset),
                patch.object(bot, "ym_send_text", new=AsyncMock()),
                patch.object(bot, "ym_get_updates", new=AsyncMock(side_effect=[
                    {"ok": True, "updates": updates}, asyncio.CancelledError(),
                ])),
                patch("builtins.print"),
            ):
                with self.assertRaises(asyncio.CancelledError):
                    await bot.listen_commands()
            self.assertEqual(store.recover_chat_jobs(db), 1)
            self.assertEqual(store.claim_chat_job(db, "enrich")[0], 11)
            store.expect_enrich_file(db, ("chat-A", "a"), time.time() + 10)
            self.assertFalse(store.enqueue_chat_job(db, 11, "enrich", {}, ("chat-A", "a")))
            self.assertTrue(store.enqueue_chat_job(db, 12, "enrich", {"new": True}, ("chat-A", "a")))

    async def test_dadata_retry_uses_quota_and_keeps_http_error(self):
        response = AsyncMock()
        response.__aenter__.return_value = response
        response.status = 429
        response.headers = {"Retry-After": "1"}
        session = Mock()
        session.post.return_value = response
        budget = enrich.DaDataBudget(2)
        limiter = Mock(wait_turn=AsyncMock())
        with patch.object(enrich.asyncio, "sleep", new=AsyncMock()):
            result = await enrich.dadata_lookup(session, "7707083893", asyncio.Semaphore(1), limiter, budget)
        self.assertEqual((result["error"], result["found"], budget.used), ("HTTP 429", False, 2))
        self.assertEqual(session.post.call_count, 2)

    def test_pending_request_key_is_scoped_to_chat_and_sender(self):
        alice = {"login": "alice"}
        bob = {"login": "bob"}
        group = {"id": "first", "type": "group"}
        other_group = {**group, "id": "second"}
        self.assertNotEqual(bot._enrich_request_key(group, alice), bot._enrich_request_key(other_group, alice))
        self.assertNotEqual(bot._enrich_request_key(group, alice), bot._enrich_request_key(group, bob))

    async def test_remainder_contains_only_unprocessed_and_failed_inns(self):
        target = {"login": "test-user"}
        settings = {"enabled": True, "daily_limit": 2, "requests_per_second": 25}
        inns = ["7707083893", "7702070139", "7702319337"]

        async def download(_session, _file_id, path):
            Path(path).write_text("\n".join(inns), encoding="utf-8", newline="\n")

        async def lookup(_session, inn, _sem, _limiter, budget):
            self.assertTrue(budget.take())
            return {"inn": inn, "found": False, **({"error": "HTTP 503"} if inn == inns[1] else {})}

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(bot.config, "CONFIG_DIR", Path(tmp)),
            patch.object(bot.config, "DADATA_TOKEN", "test"),
            patch.object(bot.config, "enrich_module_settings", return_value=settings),
            patch.object(bot, "ym_download_file", side_effect=download),
            patch.object(bot, "ym_send_file", new=AsyncMock(return_value=[])),
            patch.object(bot, "ym_send_text", new=AsyncMock()) as send_text,
            patch.object(enrich, "_load_dadata_usage", return_value={"count": 0}),
            patch.object(enrich, "_save_dadata_usage") as save_usage,
            patch.object(enrich, "record_dadata_usage"),
            patch.object(enrich, "dadata_lookup", side_effect=lookup),
        ):
            await bot._handle_enrich(target, "file-id", "source.txt", target)
            remainder, = Path(tmp).glob("*_остаток.xlsx")
            self.assertEqual(enrich.extract_inns_from_xlsx(str(remainder)), inns[1:])
        save_usage.assert_called_once_with({"count": 2})
        self.assertIn("не найдено 1", send_text.await_args.args[0])
        self.assertIn("Ошибок запросов: 1", send_text.await_args.args[0])

    async def test_saved_report_is_available_when_attachment_cannot_be_delivered(self):
        target = {"login": "test-user"}
        settings = {"enabled": True, "daily_limit": 10, "requests_per_second": 25}

        async def download(_session, _file_id, path):
            Path(path).write_text("7707083893\n", encoding="utf-8", newline="\n")

        for size_limit, errors, expected_warning in (
            (1, [], "Лимит вложения бота"),
            (bot.YM_MAX_FILE_BYTES, ["HTTP 413"], "не подтвердил отправку"),
            (bot.YM_MAX_FILE_BYTES, [], None),
        ):
            with self.subTest(size_limit=size_limit, errors=errors), tempfile.TemporaryDirectory() as tmp:
                with (
                    patch.object(bot.config, "CONFIG_DIR", Path(tmp)),
                    patch.object(bot.config, "DADATA_TOKEN", "test"),
                    patch.object(bot.config, "enrich_module_settings", return_value=settings),
                    patch.object(bot, "YM_MAX_FILE_BYTES", size_limit),
                    patch.object(bot, "ym_download_file", side_effect=download),
                    patch.object(bot, "ym_send_file", new_callable=AsyncMock, return_value=errors) as send_file,
                    patch.object(bot, "ym_send_text", new_callable=AsyncMock) as send_text,
                    patch.object(enrich, "_load_dadata_usage", return_value={"count": 0}),
                    patch.object(enrich, "_save_dadata_usage"),
                    patch.object(enrich, "record_dadata_usage"),
                    patch.object(enrich, "dadata_lookup", return_value={"inn": "7707083893", "found": True}),
                ):
                    await bot._handle_enrich(target, "file-id", "input.txt", target)
                    report, = status.list_reports()
                    self.assertEqual(report["kind"], "Обогащение ИНН")
                    path = status.report_path(report["name"])
                    with path.open("rb") as file:
                        workbook = load_workbook(file, read_only=True)
                        try:
                            self.assertEqual(workbook.active["A2"].value, "7707083893")
                        finally:
                            workbook.close()

                if report["size"] > size_limit:
                    send_file.assert_not_called()
                else:
                    send_file.assert_awaited_once()
                    self.assertEqual(send_file.await_args.args[0], str(path))
                    self.assertEqual(send_file.await_args.kwargs["target"], target)
                message = send_text.await_args.args[0]
                self.assertIn("Обработано: 1", message)
                if expected_warning:
                    self.assertIn(expected_warning, message)
                    self.assertIn(report["name"], message)
                    self.assertIn("Отчёты → Обогащение ИНН", message)
                else:
                    self.assertNotIn("Отчёты", message)


if __name__ == "__main__":
    unittest.main()
