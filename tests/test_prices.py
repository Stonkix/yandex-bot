"""Регрессионные проверки выгрузки цен СБИС и Контур."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from openpyxl import load_workbook

from yandexbot.modules import prices


class _Response:
    ok = True
    status = 200

    async def body(self):
        return b"official price file"


class _Request:
    def __init__(self):
        self.urls = []

    async def get(self, url, **_kwargs):
        self.urls.append(url)
        return _Response()


class _PriceDownloadPage:
    def __init__(self, failures=0):
        self.url = None
        self.request = _Request()
        self.failures = failures
        self.goto_calls = 0

    async def goto(self, url, **_kwargs):
        self.url = url
        self.goto_calls += 1
        if self.goto_calls <= self.failures:
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    # Тест может подменить ссылки страницы загрузки своим списком.
    links = None

    async def evaluate(self, _script):
        if self.links is not None:
            return self.links
        return [
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/81_pricelist.docx?t=1", "text": "Скачать полный прайс-лист, часть 2 (тарифные планы в регионе)"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/93_pricelist.pdf?t=2", "text": "Скачать прайс-лист на тарифные планы «Общий Лайт», «Нулевая отчетность», «Кадровые отчеты»"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/94_pricelist.pdf?t=3", "text": "Условия предоставления услуг"},
        ]


class KonturParserTests(unittest.TestCase):
    def test_downloads_all_official_region_price_files(self):
        page = _PriceDownloadPage()
        with tempfile.TemporaryDirectory() as directory:
            files = asyncio.run(prices.kontur_download_regional_price_files(
                page, 77, Path(directory),
            ))

            self.assertEqual(
                page.url, "https://www.kontur-extern.ru/price-download/77",
            )
            self.assertEqual(len(files), 3)
            self.assertEqual([path.suffix for path in files], [".docx", ".pdf", ".pdf"])
            self.assertTrue(all(path.read_bytes() == b"official price file" for path in files))

    def test_display_prices_are_rounded_up(self):
        self.assertEqual(prices._price_for_display(27115.01), 27116.0)
        self.assertEqual(prices._price_for_display(24210.0), 24210.0)

    def test_manual_downloads_only_main_docx_and_ob_lite_pdf(self):
        page = _PriceDownloadPage()
        with tempfile.TemporaryDirectory() as directory:
            files = asyncio.run(prices.kontur_download_regional_price_files(
                page, 77, Path(directory), main_files_only=True,
            ))

        self.assertEqual(len(files), 2)
        self.assertEqual([path.suffix for path in files], [".docx", ".pdf"])
        self.assertEqual(len(page.request.urls), 2)

    def test_retries_temporary_connection_refusal(self):
        page = _PriceDownloadPage(failures=1)
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(prices.asyncio, "sleep", new=AsyncMock()):
                files = asyncio.run(prices.kontur_download_regional_price_files(
                    page, 41, Path(directory),
                ))

        self.assertEqual(page.goto_calls, 2)
        self.assertEqual(len(files), 3)

    def test_logs_and_reports_http_403_access_restriction(self):
        url = "https://www.kontur-extern.ru/price-download/77"
        page = _PriceDownloadPage()
        page.goto = AsyncMock(return_value=Mock(status=403, url=url + "?private=value"))
        page.title = AsyncMock(return_value="Доступ ограничен")
        with tempfile.TemporaryDirectory() as directory:
            with self.assertLogs(level="ERROR") as logs:
                with self.assertRaisesRegex(prices.KonturAccessDenied, "HTTP 403"):
                    asyncio.run(prices.kontur_download_regional_price_files(page, 77, Path(directory)))

        page.goto.assert_awaited_once()
        self.assertFalse(page.request.urls)
        self.assertIn(url, logs.output[0])
        self.assertIn("Доступ ограничен", logs.output[0])
        self.assertNotIn("private=value", logs.output[0])

    def test_does_not_retry_http_403_for_price_file(self):
        url = "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/81_pricelist.docx"
        page = _PriceDownloadPage()
        page.request.get = AsyncMock(return_value=Mock(status=403, url=url + "?private=value"))
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(prices.asyncio, "sleep", new=AsyncMock()), self.assertLogs(level="ERROR") as logs:
                with self.assertRaisesRegex(prices.KonturAccessDenied, "HTTP 403"):
                    asyncio.run(prices.kontur_download_regional_price_files(page, 77, Path(directory)))

        page.request.get.assert_awaited_once()
        self.assertEqual(page.goto_calls, 1)
        self.assertIn(url, logs.output[0])
        self.assertNotIn("private=value", logs.output[0])


class SbisPriceListTests(unittest.TestCase):
    def test_new_nomenclatures_reach_history_and_excel(self):
        source = {
            "Buhta": 12000, "Ent_ausn": 5100, "org_ausn": 8100,
            "EOpSBISfrmUP100N": 210000, "start_ip": 7500, "EOMVD_staff500": 68000,
            "EOpLiteNIN": 2000, "EOpNI": 900.5, "Filials5": 1500, "Filials100": 10000,
            "Unrelated": 999,
        }
        with patch.object(prices, "sbis_fetch_region", new=AsyncMock(return_value=source)):
            rows, records = asyncio.run(prices.parse_sbis([(77, "Москва")], delay_sec=0))
        saved = {row["tariff_key"]: row["price"] for row in records}
        self.assertEqual(saved["Buhta_УпБ"], 12000)
        self.assertEqual(saved["АУСН_ИП"], 5100)
        self.assertEqual(saved["АУСН_ЮЛ"], 8100)
        self.assertEqual(saved["Корп_100"], 210000)
        self.assertEqual(saved["Старт_ИП"], 7500)
        self.assertEqual(saved["МВД_500"], 68000)
        self.assertEqual(saved["Легкий_доп_направление"], 2000)
        self.assertEqual(saved["Корп_доп_филиалы_100"], 10000)
        self.assertNotIn("Unrelated", saved)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sbis.xlsx"
            prices.save_sbis_excel(rows, {}, str(path))
            wb = load_workbook(path)
            try:
                ws = wb.active
                index = next(cell.column for cell in ws[1] if cell.value == "АУСН")
                self.assertEqual([ws.cell(2, i).value for i in (index, index + 1)], ["ИП", "ЮЛ"])
                self.assertEqual([ws.cell(3, i).value for i in (index, index + 1)], [5100, 8100])
                index = next(cell.column for cell in ws[1] if cell.value == "Дополнительные направления сдачи")
                self.assertEqual([ws.cell(3, i).value for i in (index, index + 1)], [2000, 900.5])
                group = next(merged for merged in ws.merged_cells.ranges if merged.min_col == index)
                self.assertEqual(ws.cell(3, group.min_col).border.left.style, "thin")
                self.assertEqual(ws.cell(3, group.max_col).border.right.style, "thin")
                self.assertEqual(ws.cell(3, group.min_col).border.bottom.style, "thin")
            finally:
                wb.close()


def _make_part2_docx(path: str):
    """Мини-копия «части 2»: таблицы распознаются по заголовкам, не по номеру."""
    from docx import Document

    doc = Document()
    ind = ["Наименование тарифного плана", "Срок действия", "Категория клиента",
           "Система налогообложения", "Состав", "Базовая цена без НДС", "НДС",
           "Итоговая стоимость с НДС"]
    rows = [
        ["Оптимальный плюс без НДС", "1 год", "ИП", "специальная (УСН), общая/смешанная", "Право использования ПО", "4 980,84", "–", "6 500,00"],
        ["Оптимальный плюс без НДС", "1 год", "ИП", "специальная (УСН), общая/смешанная", "Услуги по сопровождению ПО", "1 245,21", "273,95", "6 500,00"],
        ["Оптимальный плюс без НДС", "1 год", "ЮЛ", "специальная (УСН)", "Право использования ПО", "7 279,70", "–", "9 500,00"],
        ["Оптимальный плюс без НДС", "2 года", "ЮЛ", "специальная (УСН)", "Право использования ПО", "14 176,25", "–", "18 500,00"],
        ["Максимальный", "1 год", "ЮЛ", "специальная (УСН)", "Право использования ПО", "12 643,68", "–", "16 500,00"],
        ["Максимальный", "1 год", "ЮЛ", "общая/смешанная", "Право использования ПО", "15 249,04", "–", "19 900,00"],
        ["Учетный для АУСН", "1 год", "ИП", "специальная (АУСН)", "Право использования ПО", "6 130,26", "–", "8 000,00"],
        ["Учетный для АУСН", "1 год", "ЮЛ", "специальная (АУСН)", "Право использования ПО", "8 352,49", "–", "10 900,00"],
    ]
    budget = [["Наименование тарифного плана", "Срок действия", "Состав",
               "Базовая цена без НДС", "НДС", "Итоговая стоимость с НДС"],
              ["Бюджетник плюс", "1 год", "Право использования ПО", "4 521,07", "–", "5 900,00"],
              ["Бюджетник", "1 год", "Право использования ПО", "6 819,92", "–", "8 900,00"]]
    ob = [["Наименование тарифного плана", "Срок действия",
           "Количество организаций на обслуживании", "Состав",
           "Базовая цена без НДС", "НДС", "Итоговая стоимость с НДС"],
          ["Общий", "1 год", "1+4 абонента", "Право использования ПО", "13 793,10", "–", "18 000,00"],
          ["Общий", "1 год", "лицензия +1", "Право использования ПО", "3 065,13", "–", "4 000,00"],
          ["Бюджетная организация", "1 год", "1+9 абонентов", "Право использования ПО", "12 873,56", "–", "16 800,00"],
          ["Бюджетная организация", "1 год", "лицензия +5", "Право использования ПО", "5 977,01", "–", "7 800,00"]]
    branch = [
        ["Наименование тарифного плана", "Срок действия", "Количество обособленных подразделений",
         "Система налогообложения", "Состав", "Базовая цена без НДС", "НДС", "Итоговая стоимость с НДС"],
        ["Филиальный", "1 год", "1+4", "специальная (УСН)", "Право использования ПО", "12 000,00", "–", "15 000,00"],
        ["Филиальный", "1 год", "1+4", "общая/смешанная", "Право использования ПО", "18 000,00", "–", "23 000,00"],
        ["Филиальный", "2 года", "1+4", "общая/смешанная", "Право использования ПО", "29 000,00", "–", "37 000,00"],
    ]
    for data in ([ind, *rows], budget, ob, branch):
        table = doc.add_table(rows=len(data), cols=len(data[0]))
        for r, values in enumerate(data):
            for c, value in enumerate(values):
                table.cell(r, c).text = value
    doc.save(path)


def _make_tax_rep_docx(path: str):
    """Мини-копия прайса «Налоговый представитель»."""
    from docx import Document

    doc = Document()
    head = ["Код региона", "Регион", "Зона регрессивной шкалы", "Состав"]
    periods = ["3 мес."] * 3 + ["12 мес."] * 3
    kinds = ["Базовая цена без НДС", "НДС", "Итоговая стоимость с НДС"] * 2
    regional = [
        head + ["Налоговый представитель Лайт"] * 6,
        head + periods,
        head + kinds,
        ["01", "Республика Адыгея", "1", "Право", "919,54", "–", "1 200,01", "3 065,14", "–", "4 000,00"],
        ["01", "Республика Адыгея", "1", "Услуги", "229,89", "50,58", "1 200,01", "766,28", "168,58", "4 000,00"],
        ["25", "Приморский край", "4", "Право", "1149,42", "–", "1 500,00", "3 678,16", "–", "4 800,00"],
    ]
    premium = [
        ["Состав"] + ["Налоговый представитель Премиум"] * 6,
        ["Состав"] + periods,
        ["Состав"] + kinds,
        ["Право", "9 578,54", "–", "12 500,00", "38 314,18", "–", "50 000,00"],
    ]
    zones = [
        ["Интервалы количества документов, переданных за отчетный период", "Зона 1", "Зона 2"],
        ["До 199", "18", "50"],
        ["От 2000", "10", "15"],
    ]
    zones_4 = [
        ["Интервалы количества документов, переданных за отчетный период", "Зона 4", "Зона 10"],
        ["До 349", "50", "75"],
        ["От 1000", "20", "20"],
    ]
    for data in (regional, premium, zones, zones_4):
        table = doc.add_table(rows=len(data), cols=len(data[0]))
        for r, values in enumerate(data):
            for c, value in enumerate(values):
                table.cell(r, c).text = value
    doc.save(path)


ZERO_PDF_TEXT = """Тарифный план «Нулевая отчетность»
Код
региона Регион Состав
Нулевая отчетность
01 Республика Адыгея Право использования ПО 1 685,82 – 2 200,00 Услуги по сопровождению ПО 421,46 92,72
86 Ханты-Мансийский автономный
округ – Югра Право использования ПО 2 528,74 – 3 300,00 Услуги по сопровождению ПО 632,18 139,08
Тарифный план «Кадровые отчеты»
Стоимость тарифного плана, в руб.
Право использования ПО 2 758,62 – 3 600,00
"""


class KonturPriceListParsingTests(unittest.TestCase):
    def test_only_group_outline_runs_through_all_region_rows(self):
        keys = [("Учетный", "ЮЛ на ОСНО"), ("Максимальный", "ЮЛ на ОСНО"), ("Оптимальный +", "ЮЛ на ОСНО")]
        rows = [("1", "Адыгея", {key: 100 for key in keys}), ("77", "Москва", {key: 200 for key in keys})]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "kontur.xlsx"
            prices.save_kontur_excel(rows, keys, [], [], [], [], {}, str(path))
            book = load_workbook(path)
            try:
                ws = book.active
                self.assertIn("B1:D1", [str(group) for group in ws.merged_cells.ranges])
                self.assertEqual(ws["C1"].border.top.style, "thin")
                self.assertEqual(ws["B3"].border.left.style, "thin")
                self.assertEqual(ws["D3"].border.right.style, "thin")
                self.assertEqual(ws["D4"].border.bottom.style, "thin")
                self.assertFalse(ws["C3"].border.left and ws["C3"].border.left.style)
                self.assertEqual(ws["C3"].value, 100)
            finally:
                book.close()

    def test_business_forms_match_site_selector(self):
        form = prices.kontur_biz_form
        self.assertEqual(form("ЮЛ", "общая/смешанная", "Оптимальный плюс с НДС"), "ЮЛ на ОСНО")
        self.assertEqual(form("ЮЛ", "специальная (УСН)", "Оптимальный плюс без НДС"), "ЮЛ на УСН без НДС")
        self.assertEqual(form("ЮЛ", "специальная (УСН)", "Максимальный"), "ЮЛ на УСН с НДС")
        self.assertEqual(form("ИП", "специальная (УСН), общая/смешанная", "Учетный без НДС"), "ИП без НДС")
        self.assertEqual(form("ИП", "специальная (УСН), общая/смешанная", "Учетный с НДС"), "ИП с НДС")
        self.assertEqual(form("ИП", "специальная (АУСН)", "Учетный для АУСН"), "ИП на АУСН")
        self.assertEqual(form("ЮЛ", "специальная (АУСН)", "Учетный для АУСН"), "ЮЛ на АУСН")
        # Новый спецрежим становится новой формой без правок кода.
        self.assertEqual(form("ИП", "специальная (ПСН)", "Патентный"), "ИП на ПСН")
        self.assertEqual(prices.kontur_tariff_name("Оптимальный плюс без НДС"), "Оптимальный +")

    def test_parses_part2_tables_by_header(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "part2.docx")
            _make_part2_docx(path)
            with patch("builtins.print"):
                rows = prices.parse_docx_pricelist(path, 1)

        individual = {(r["tariff_key"], r["biz_form"]): r["price"]
                      for r in rows if r["table_type"] == "individual"}
        self.assertEqual(individual, {
            ("Оптимальный +", "ИП без НДС"): 6500.0,
            ("Оптимальный +", "ЮЛ на УСН без НДС"): 9500.0,
            ("Максимальный", "ЮЛ на УСН с НДС"): 16500.0,
            ("Максимальный", "ЮЛ на ОСНО"): 19900.0,
            ("Учетный для АУСН", "ИП на АУСН"): 8000.0,
            ("Учетный для АУСН", "ЮЛ на АУСН"): 10900.0,
        })
        other = {(r["table_type"], r["tariff_key"], r["ob_count"]): r["price"]
                 for r in rows if r["table_type"] != "individual"}
        self.assertEqual(other, {
            ("single_budget", "Бюджетник", None): 8900.0,
            ("single_budget", "Бюджетник плюс", None): 5900.0,
            ("ob_docx", "Общий", 4): 18000.0,
            ("ob_docx", "Общий", "+1"): 4000.0,
            ("group", "Бюджетная организация", 9): 16800.0,
            ("group", "Бюджетная организация", "+5"): 7800.0,
            ("other", "1+4 · УСН", None): 15000.0,
            ("other", "1+4 · ОСНО", None): 23000.0,
        })

    def test_tax_representative_prices_and_per_document_scale(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "104.docx")
            _make_tax_rep_docx(path)
            with patch("builtins.print"):
                adygea = prices.parse_docx_pricelist(path, 1)
                primorye = prices.parse_docx_pricelist(path, 25)

        def as_dict(rows):
            return {(r["group"], r["tariff_key"]): r["price"] for r in rows}

        self.assertEqual(as_dict(adygea), {
            ("НП", "Лайт"): 4000.0,
            ("НП", "Премиум"): 50000.0,
            ("НП за документ", "Зона"): 1.0,
            ("НП за документ", "До 199"): 18.0,
            ("НП за документ", "От 2000"): 10.0,
        })
        # У зоны 4 своя шкала интервалов.
        self.assertEqual(as_dict(primorye)[("НП за документ", "До 349")], 50.0)
        self.assertNotIn(("НП за документ", "До 199"), as_dict(primorye))

    def test_zero_reporting_price_for_region(self):
        with patch("builtins.print"):
            self.assertEqual(
                prices.parse_zero_reporting_text(ZERO_PDF_TEXT, 1),
                [{"table_type": "other", "tariff_key": "Нулевая отчетность",
                  "price": 2200.0, "ob_count": None}],
            )
            # Название региона переносится на вторую строку.
            self.assertEqual(prices.parse_zero_reporting_text(ZERO_PDF_TEXT, 86)[0]["price"], 3300.0)

    def test_personnel_reports_and_ip_without_employees_pdf(self):
        with patch("builtins.print"):
            result = prices.parse_kontur_pdf_text(ZERO_PDF_TEXT, 86)
        self.assertEqual({row["tariff_key"]: row["price"] for row in result}, {
            "Нулевая отчетность": 3300, "Кадровые отчеты": 3600,
        })
        ip_text = """Дополнительный прайс-лист
«ИП без сотрудников без НДС»
77 Москва Право использования ПО 5 300,00 – 7 000,00 Услуги 1 400,00 300,00
78 Санкт-Петербург Право использования ПО 4 800,00 – 6 200,00
"""
        [row] = prices.parse_kontur_pdf_text(ip_text, 77)
        self.assertEqual((row["tariff_key"], row["biz_form"], row["price"]),
                         ("ИП без сотрудников", "ИП без НДС", 7000))

    def test_shared_zero_reporting_price_list_fills_missing_region_link(self):
        browser = Mock(close=AsyncMock(), new_context=AsyncMock(return_value=Mock(new_page=AsyncMock())))
        playwright = AsyncMock()
        playwright.__aenter__.return_value.chromium.launch = AsyncMock(return_value=browser)
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "96_pricelist.pdf"
            pdf.touch()
            docx = folder / "part2.docx"
            _make_part2_docx(str(docx))
            with (
                patch("builtins.print"),
                patch.object(prices, "async_playwright", return_value=playwright),
                patch.object(prices, "kontur_download_regional_price_files", new=AsyncMock(
                    side_effect=[[docx, pdf], [docx]],
                )),
                patch.object(prices, "_kontur_pdf_text", return_value=ZERO_PDF_TEXT),
            ):
                _, records, *_, incomplete = asyncio.run(
                    prices.parse_kontur([(1, "Адыгея"), (86, "ХМАО")], folder, region_delay_sec=0)
                )
        restored = {row["tariff_key"]: row["price"] for row in records
                    if row["region_code"] == "86" and row["tariff_key"].startswith("Другие |")}
        self.assertEqual(restored, {"Другие | Нулевая отчетность": 3300, "Другие | Кадровые отчеты": 3600})
        self.assertFalse(incomplete)

    def test_tax_rep_pdf_handles_wrapped_amounts_and_zone_specific_intervals(self):
        text = """Тарифные планы «Налоговый представитель»
Налоговый представитель Лайт
3 мес. 12 мес. 24 мес.
77 Москва 4 Право10 1 200,00 – 1 500,00 6 000,00 – 8 000,00 10 000,00 – 13 000,00
Налоговый представитель Базовый
3 мес. 12 мес. 24 мес.
77
Москва
4
Право 3 000,00 –
4 000,00
16 800,00 –
22 000,00
28 000,00 –
37 000,00
Налоговый представитель Плюс
3 мес. 12 мес. 24 мес.
77 Москва 4 Право 6 000,00 – 7 800,00 20 000,00 – 26 000,00 36 000,00 – 47 000,00
Налоговый представитель Премиум
3 мес. 12 мес.
Право 8 000,00 – 10 000,00 35 000,00 – 45 000,00
Зона 1 Зона 5
До 199 21 71
Зона 4 Зона 10
До 349 51 76
От 350 до 599 36 51
"""
        rows = prices.parse_kontur_pdf_text(text, 77)
        self.assertEqual({(r["group"], r["tariff_key"]): r["price"] for r in rows}, {
            ("НП", "Лайт"): 8000,
            ("НП", "Базовый"): 22000,
            ("НП", "Плюс"): 26000,
            ("НП", "Премиум"): 45000,
            ("НП за документ", "Зона"): 4,
            ("НП за документ", "До 349"): 51,
            ("НП за документ", "От 350 до 599"): 36,
        })

    def test_zonal_ob_uses_totals_for_the_correct_zone_and_package(self):
        text = """Тарифные планы «ОБ Универсальный», «ОБ Все включено»
Код     Регион     Ценовая зона
01      Адыгея     1
77      Москва     5
Ценовая зона № 1
1+2 абонента  Право использования ПО  10 000,00  –  13 000  15 000,00  –  19 500
Ценовая зона № 5
1+2 абонента  Право использования ПО  30 000,00  –  39 000  45 000,00  –  58 500
+5 абонентов  Право использования ПО  7 000  –  7 000  10 000  –  10 000
Ценовая зона № 5
1+499 абонентов  Право использования ПО  450 000,00  –  590 000  750 000,00  –  978 000
"""
        rows = prices.parse_kontur_pdf_text(text, 77)
        self.assertEqual({(r["tariff_key"], r["ob_count"]): r["price"] for r in rows}, {
            ("ОБ Универсальный", 2): 39000,
            ("ОБ Все включено", 2): 58500,
            ("ОБ Универсальный", "+5"): 7000,
            ("ОБ Все включено", "+5"): 10000,
            ("ОБ Универсальный", 499): 590000,
            ("ОБ Все включено", 499): 978000,
        })

    def test_manual_run_downloads_all_files_with_prices(self):
        page = _PriceDownloadPage()
        page.links = [
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/9_pricelist.docx?t=0", "text": "Cкачать полный прайс-лист, часть 1 (термины и определения)"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/104_pricelist.pdf?t=4", "text": "Скачать прайс-лист для налоговых представителей"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/93_pricelist.docx?t=5", "text": "Скачать полный прайс-лист, часть 3 (тарифные модификаторы, доп. сервисы и услуги)"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/84_pricelist.docx?t=1", "text": "Скачать полный прайс-лист, часть 2 (тарифные планы в регионе)"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/96_pricelist.pdf?t=2", "text": "Скачать прайс-лист на тарифные планы «Нулевая отчетность», «Кадровые отчеты»"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/40_pricelist.pdf?t=3", "text": "Скачать прайс-лист на тарифный план «ИП без сотрудников»"},
            {"href": "https://www.kontur-extern.ru/Files/Modules/ExternPriceList/112_pricelist.pdf?t=6", "text": "Скачать прайс-лист на тарифные планы «ОБ Универсальный», «ОБ Всё включено»"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            files = asyncio.run(prices.kontur_download_regional_price_files(
                page, 1, Path(directory), main_files_only=True,
            ))
            names = [path.name for path in files]
        # Файлы с ценами нужны и при ручном запуске, иначе тарифы пропадают
        # из сравнения; части 1 и 3 — без цен тарифов.
        self.assertEqual(names, [
            "01_104_pricelist.pdf", "02_84_pricelist.docx",
            "03_96_pricelist.pdf", "04_40_pricelist.pdf", "05_112_pricelist.pdf",
        ])


if __name__ == "__main__":
    unittest.main()
