import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook

from yandexbot.modules import epd_prices

# Фрагменты текстового слоя официального PDF Контура в том виде, как их
# отдаёт pypdf: разряды чисел и колонки таблицы разделены одинаковыми пробелами.
KONTUR_PDF_PACKAGES = """Действительно с 15 сентября 2026 г.
Таблица 1
№
п/п Наименование Базовая цена Количество
документов в пакете
1 Тарифный план «250 документов» 1 900 250
3 Тарифный план «1000 документов» 6 500 1000
18 Тарифный план «100000 документов» 400 000 100000
19 Тарифный план «150 000 документов»  600 000   150 000
"""

KONTUR_PDF_ADDONS = """3. Услуги по сопровождению
3.1. Услуги по сопровождению сервиса «Логистика» программы для ЭВМ «Контур.Диадок»
(консультирование по работе в веб-версии)
Таблица 2
Максимальное
количество
пользователей
Базовая цена,
без НДС НДС
Итоговая
стоимость с
НДС
3 1,5 5 500 1 210 6 710
100  2,5 143 500 31 570 175 070
5 Отсчет начинается с момента приобретения тарифного плана.
4. АPI-лицензия – передаваемые Клиентам права
Таблица 5
№
п/п Наименование
Базовая
цена,
без НДС
НДС
Итоговая
стоимость
с НДС
1
Право использования программы для ЭВМ
«Контур.Диадок», АPI-лицензия для сервиса
«Логистика» сроком на 1 год
23 400 – 23 400
2
Услуги по сопровождению сервиса «Логистика»
(консультации по интеграции при помощи API), 1 час7
4 500 990 5 490
5.1. Лицензия на модуль
Таблица 6
№
п/п Наименование  Базовая цена
1 Право использования программы для ЭВМ «Контур.Диадок», типовой
модуль «Интеграция.Логистика» на 1 год 19 700
7 Минимальное количество часов, которое можно приобрести – 15.
5.3. Дополнительные работы и услуги
Таблица 7
№
п/п Наименование
Базовая
цена,
без НДС
НДС
Итоговая
стоимость
с НДС
1
Работы по адаптации (интеграции) модуля
«Интеграция.Логистика», за 1 час
4 500 990 5 490
2 Доработка модуля «Интеграция.Логистика» 8 Рассчитывается индивидуально
8 Итоговая стоимость доработки модуля зависит от сложности.
11. Организация рабочего места
Таблица 14
№
п.п. Наименование
Базовая
цена,
без НДС
НДС
Итоговая
стоимость с
НДС
1 Организация выдачи сертификата  3 278,69 721,31 4 000,00
2
Рабочее место с сертификатом, в том числе:
5 800, 00
Лицензия на право использования программы
1 800 –
Организация выдачи сертификата  3 278,69 721,31
Примечание
1. Все цены указаны в рублях Российской Федерации.
2 Строка после примечаний в отчёт не попадает 100 000
"""


def _names_and_prices(rows):
    return [(row["tariff_name"], row["price_tariff"]) for row in rows]


class KonturPdfTests(unittest.TestCase):
    def test_packages_split_price_and_quantity_without_gluing(self):
        rows = epd_prices.parse_kontur_pdf_packages(KONTUR_PDF_PACKAGES)

        self.assertEqual([(row["variant"], row["price_tariff"], row["price_document"]) for row in rows], [
            ("250", 1900.0, 7.6),
            ("1000", 6500.0, 6.5),
            ("100000", 400000.0, 4.0),
            ("150000", 600000.0, 4.0),
        ])
        self.assertEqual(rows[0]["tariff_name"], "Пакет 250 документов")
        self.assertIn("15 сентября 2026", rows[0]["conditions"])

    def test_package_with_absurd_price_per_document_is_dropped(self):
        rows = epd_prices.parse_kontur_pdf_packages("1 Тарифный план «250 документов» 1 900 000 250")
        self.assertEqual(rows, [])

    def test_quotes_for_large_package_and_unlimited_are_exported(self):
        text = KONTUR_PDF_PACKAGES + """25 Тарифный план на более 400 000
документов
Рассчитывается
индивидуально
26 Тарифный план «Безлимит» сроком на
12 мес.
Рассчитывается
индивидуально Не ограничено
Таблица 2
"""
        quotes = epd_prices.parse_kontur_pdf_packages(text)[-2:]
        self.assertEqual([row["variant"] for row in quotes], [">400000", "unlimited"])
        self.assertTrue(all(row["price_tariff"] is None and row["price_document"] is None for row in quotes))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "quotes.xlsx"
            epd_prices.save_excel({"epd_kontur": quotes}, {}, str(path))
            book = load_workbook(path)
            try:
                self.assertEqual([book.active[f"B{row}"].value for row in (2, 3)], ["По запросу", "По запросу"])
            finally:
                book.close()

    def test_addons_from_tables_with_multiline_rows(self):
        rows = epd_prices.parse_kontur_pdf_addons(KONTUR_PDF_ADDONS)

        self.assertEqual(_names_and_prices(rows), [
            ("Услуги по сопровождению сервиса «Логистика» (консультирование по работе в веб-версии): "
             "до 3 пользователей, до 1,5 ч", 6710.0),
            ("Услуги по сопровождению сервиса «Логистика» (консультирование по работе в веб-версии): "
             "до 100 пользователей, до 2,5 ч", 175070.0),
            ("Лицензия: АPI-лицензия для сервиса «Логистика» сроком на 1 год", 23400.0),
            ("Услуги по сопровождению сервиса «Логистика» (консультации по интеграции при помощи API), 1 час",
             5490.0),
            ("Лицензия: типовой модуль «Интеграция.Логистика» на 1 год", 19700.0),
            ("Работы по адаптации (интеграции) модуля «Интеграция.Логистика», за 1 час", 5490.0),
            ("Организация выдачи сертификата", 4000.0),
            ("Рабочее место с сертификатом", 5800.0),
        ])
        self.assertTrue(all(row["entry_type"] == epd_prices.ADDON for row in rows))
        self.assertEqual(rows[0]["conditions"], "Без НДС 5 500 ₽, НДС 1 210 ₽")
        self.assertEqual(rows[2]["conditions"], "НДС не облагается")

    def test_vat_triple_picks_digit_grouping_by_sum(self):
        self.assertEqual(epd_prices._vat_triple("за 1 час 4 500 990 5 490"), ("за 1 час", 4500.0, 990.0, 5490.0))
        self.assertEqual(epd_prices._vat_triple("850 187 1 037"), ("", 850.0, 187.0, 1037.0))
        self.assertIsNone(epd_prices._vat_triple("сроком на 1 год"))

    def test_page_price_mismatch_is_reported_and_pdf_wins(self):
        pdf = [epd_prices._package_row(500, 3500.0, epd_prices.KONTUR_PDF_URL, "")]
        page = [epd_prices._package_row(500, 3900.0, epd_prices.KONTUR_URL, "")]

        rows, notes = epd_prices.merge_kontur_packages(pdf, page)

        self.assertEqual(rows, pdf)
        self.assertEqual(len(notes), 1)
        self.assertIn("3 900", notes[0])

    def test_page_is_fallback_when_pdf_has_no_packages(self):
        page = [epd_prices._package_row(500, 3500.0, epd_prices.KONTUR_URL, "")]
        self.assertEqual(epd_prices.merge_kontur_packages([], page), (page, []))

    def test_parse_kontur_calculator_links_package_and_price(self):
        page = """<div data-price-calculator="{&quot;billingIntegrationData&quot;:{&quot;initialData&quot;:{&quot;tariffs&quot;:[{&quot;tariffId&quot;:830,&quot;priceInfo&quot;:{&quot;price&quot;:3500}}]}}}" data-calculator-tariffs="[{&quot;id&quot;:830,&quot;name&quot;:&quot;500&quot;}]"></div>"""
        rows = epd_prices.parse_kontur_calculator(page)
        self.assertEqual(rows[0]["tariff_name"], "Пакет 500 документов")
        self.assertEqual(rows[0]["price_document"], 7.0)


class SbisTests(unittest.TestCase):
    def test_api_services_give_packages_and_postpaid(self):
        services = [
            {"nomenclature": "ETRN_1000", "price": 5000.0, "name": "ЭПД 1000"},
            {"nomenclature": "ETRN_200", "price": 1500.0, "name": "ЭПД 200"},
            {"nomenclature": "ETRNPOSTO_10000", "price": 5.0},
            {"nomenclature": "ETRNPOST_1000", "price": 8.0},
            {"nomenclature": "Waybills_1", "price": 6000.0, "name": "Путевые листы"},
            {"nomenclature": "Waybills", "price": 18000.0, "name": "TMS для перевозчиков"},
            {"nomenclature": "Sapper_etrn", "price": 490000.0, "name": "TMS SAPPER Activation"},
        ]

        rows = epd_prices.parse_sbis_services(services)

        self.assertEqual([(row["entry_type"], row["tariff_name"], row["price_tariff"], row["price_document"])
                          for row in rows], [
            (epd_prices.TARIFF, "Пакет 200 документов", 1500.0, 7.5),
            (epd_prices.TARIFF, "Пакет 1 000 документов", 5000.0, 5.0),
            (epd_prices.TARIFF, "Постоплата: до 1 000 документов", None, 8.0),
            (epd_prices.TARIFF, "Постоплата: свыше 10 000 документов", None, 5.0),
            # Дополнения идут в порядке сайта, а не ответа API; SAPPER в отчёт не берём.
            (epd_prices.ADDON, "Управление транспортом (TMS): Перевозки", 18000.0, None),
            (epd_prices.ADDON, "Путевые листы: создание, печать и учет ГСМ", 6000.0, None),
        ])
        notes = epd_prices._missing_sbis_addons(rows)
        self.assertEqual(len(notes), 1)
        self.assertIn("Выездные сотрудники: Заявки и наряды", notes[0])

    def test_api_response_with_string_result(self):
        raw = {"result": '{"data": [{"nomenclature": "ETRN_1000", "price": 5000}]}'}
        self.assertEqual(epd_prices.sbis_services_from_response(raw),
                         [{"nomenclature": "ETRN_1000", "price": 5000}])
        with self.assertRaises(RuntimeError):
            epd_prices.sbis_services_from_response({"error": {"message": "boom"}})

    def test_page_fallback_reads_nomenclature_cells(self):
        page = """
        <span nomenclaturecode="ETRN_1000" data-qa="ETRN_1000" class="billing-PriceList__priceButton">5 000</span>
        <span nomenclaturecode="ETRN_3000" data-qa="ETRN_3000" class="billing-PriceList__priceButton">13 500</span>
        <span nomenclaturecode="ETRN_1000" class="duplicate-layout">5 000</span>
        <span nomenclaturecode="Applic_Stock" class="billing-PriceList__priceButton">17 000</span>
        """
        rows = epd_prices.parse_sbis_page(page)
        self.assertEqual([(row["tariff_name"], row["price_tariff"], row["price_document"]) for row in rows], [
            ("Пакет 1 000 документов", 5000.0, 5.0),
            ("Пакет 3 000 документов", 13500.0, 4.5),
            ("Выездные сотрудники: Заявки, наряды, касса и склад", 17000.0, None),
        ])


class EpdPricesTests(unittest.TestCase):
    def test_range_is_not_turned_into_a_false_exact_price(self):
        self.assertIsNone(epd_prices.price_value("от 1 200 ₽"))
        self.assertIsNone(epd_prices.price_value("1 200–1 500 ₽"))
        self.assertEqual(epd_prices.price_value("1 200,50 ₽"), 1200.5)

    def test_summary_marks_partial_run_and_notes(self):
        summary = epd_prices.build_summary({
            "results": {"epd_sbis": [epd_prices._package_row(1000, 5000.0, epd_prices.SBIS_URL, "")]},
            "errors": {"Контур": "HTTP 403"},
            "notes": ["СБИС: API недоступно"],
        })
        self.assertIn("частичная", summary)
        self.assertIn("СБИС (тарифов 1, дополнений 0)", summary)
        self.assertIn("Контур: HTTP 403", summary)
        self.assertIn("API недоступно", summary)


def _package(quantity: int, price: float) -> dict:
    return epd_prices._package_row(quantity, price, epd_prices.SBIS_URL, "")


def _history(rows: list[dict]) -> dict:
    return {epd_prices._row_key(row): (row["price_tariff"], row["price_document"]) for row in rows}


class PriceChangesMessageTests(unittest.TestCase):
    def _summary(self, rows: list[dict], previous_rows: list[dict] | None) -> str:
        previous = {"epd_sbis": _history(previous_rows)} if previous_rows is not None else {}
        return epd_prices.build_summary({"results": {"epd_sbis": rows}, "errors": {}, "notes": []}, previous)

    def test_changes_are_highlighted_in_title_and_listed(self):
        postpaid = epd_prices._row(epd_prices.TARIFF, "Постоплата: до 1 000 документов", epd_prices.SBIS_URL,
                                   price_document=8.0, variant="<=1000")
        cheaper = {**postpaid, "price_document": 7.5}
        summary = self._summary(
            [_package(1000, 5500.0), _package(3000, 13500.0), cheaper, _package(100000, 250000.0)],
            [_package(1000, 5000.0), _package(3000, 13500.0), postpaid, _package(200, 1500.0)],
        )
        self.assertTrue(summary.startswith("📦 ЭПД-цены — ⚠️ ЕСТЬ ИЗМЕНЕНИЯ ЦЕН"))
        self.assertIn("изменились цены — 2, новые позиции — 1, пропали — 1", summary)
        self.assertIn("📈 Пакет 1 000 документов: 5 000 ₽ → 5 500 ₽ (+10%)", summary)
        self.assertIn("📉 Постоплата: до 1 000 документов: 8 ₽ за документ → 7,50 ₽ за документ (−6,2%)", summary)
        self.assertIn("🆕 Пакет 100 000 документов: 250 000 ₽", summary)
        self.assertIn("➖ Пакет 200 документов (было 1 500 ₽)", summary)
        self.assertNotIn("Пакет 3 000", summary)

    def test_no_changes(self):
        rows = [_package(1000, 5000.0)]
        summary = self._summary(rows, rows)
        self.assertTrue(summary.startswith("📦 ЭПД-цены — ✅ цены без изменений"))
        self.assertIn("✅ цены не изменились", summary)

    def test_first_run_has_nothing_to_compare(self):
        summary = self._summary([_package(1000, 5000.0)], None)
        self.assertTrue(summary.startswith("📦 ЭПД-цены — первая выгрузка"))
        self.assertIn("сравнивать не с чем", summary)

    def test_mass_disappearance_is_reported_as_incomplete_source(self):
        before = [_package(quantity, quantity * 5.0) for quantity in range(1000, 11000, 1000)]
        summary = self._summary(before[:2], before)
        self.assertIn("пропало 8 из 10 позиций", summary)
        self.assertNotIn("➖", summary)

    def test_history_in_old_format_is_not_reported_as_changes(self):
        old_format = [{**_package(1000, 5000.0), "tariff_name": "Электронные перевозочные документы — 1 000 документов",
                       "variant": "1 000 документов"}]
        summary = self._summary([_package(1000, 5000.0)], old_format)
        self.assertTrue(summary.startswith("📦 ЭПД-цены — первая выгрузка"))
        self.assertIn("прошлая выгрузка в другом формате", summary)
        self.assertNotIn("🆕 Пакет", summary)

    def test_excel_marks_changed_and_new_rows(self):
        rows = {"epd_sbis": [_package(1000, 5500.0), _package(3000, 13500.0), _package(5000, 20000.0)]}
        previous = {"epd_sbis": _history([_package(1000, 5000.0), _package(3000, 13500.0)])}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.xlsx"
            epd_prices.save_excel(rows, previous, str(output))
            sheet = load_workbook(output)["СБИС ЭПД"]
            fills = [sheet.cell(row, 1).fill.fgColor.rgb[-6:] for row in (2, 3, 4)]
            self.assertEqual(fills, ["FFF2CC", "000000", "E2EFDA"])
            self.assertEqual(sheet.cell(2, 2).comment.text, "Было: 5 000 ₽")
            self.assertEqual(sheet.cell(2, 3).comment.text, "Было: 5 ₽")

    def test_long_list_is_truncated(self):
        before = [_package(quantity, quantity * 5.0) for quantity in range(1000, 31000, 1000)]
        after = [_package(quantity, quantity * 6.0) for quantity in range(1000, 31000, 1000)]
        summary = self._summary(after, before)
        self.assertEqual(summary.count("📈"), epd_prices.MAX_CHANGE_LINES)
        self.assertIn("… и ещё 15, все изменения выделены в Excel", summary)

    def test_history_uses_separate_epd_table(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "prices.db"
            with patch.object(epd_prices.config, "DB_PATH", db_path):
                conn = epd_prices.db_connect()
                try:
                    epd_prices.db_save(conn, "epd_sbis", [{
                        "entry_type": epd_prices.TARIFF,
                        "tariff_name": "Старт",
                        "variant": "",
                        "price_tariff": 100.0,
                        "price_document": 2.0,
                        "conditions": "",
                        "source_url": epd_prices.SBIS_URL,
                    }], "2026-09-15T10:00:00")
                    tables = {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )}
                finally:
                    conn.close()
            self.assertIn("epd_prices", tables)
            self.assertNotIn("prices", tables)

    def test_previous_prices_without_save_leaves_db_untouched(self):
        rows = {"epd_sbis": [epd_prices._package_row(1000, 5000.0, epd_prices.SBIS_URL, "")]}
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "prices.db"
            with patch.object(epd_prices.config, "DB_PATH", db_path):
                self.assertEqual(epd_prices.previous_prices(rows, "2026-09-18T10:00:00", save=False),
                                 {"epd_sbis": {}})
                self.assertFalse(db_path.exists())
                conn = sqlite3.connect(db_path)
                try:
                    conn.execute("CREATE TABLE prices (price REAL)")
                    conn.commit()
                    original = db_path.read_bytes()
                    epd_prices.previous_prices(rows, "2026-09-18T10:00:00", save=False)
                    self.assertEqual(db_path.read_bytes(), original)
                finally:
                    conn.close()

    def test_excel_has_tariffs_above_addons(self):
        rows = {
            "epd_sbis": [
                {"entry_type": epd_prices.TARIFF, "tariff_name": "Старт", "variant": "",
                 "price_tariff": 100.0, "price_document": 1.0, "conditions": "",
                 "source_url": epd_prices.SBIS_URL},
                {"entry_type": epd_prices.ADDON, "tariff_name": "Пользователь", "variant": "",
                 "price_tariff": 50.0, "price_document": None, "conditions": "",
                 "source_url": epd_prices.SBIS_URL},
            ],
            "epd_kontur": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.xlsx"
            epd_prices.save_excel(rows, {"epd_sbis": {}, "epd_kontur": {}}, str(output))
            sheet = load_workbook(output, data_only=True)["СБИС ЭПД"]
            self.assertEqual([sheet.cell(1, col).value for col in range(1, 4)], [
                "Название тарифа", "Цена тарифа", "Цена за документ",
            ])
            self.assertEqual(sheet.cell(2, 1).value, "Старт")
            self.assertEqual(sheet.cell(4, 1).value, "Дополнения")
            self.assertEqual([sheet.cell(4, col).value for col in (2, 3)], ["Цена с НДС", None])
            self.assertEqual(sheet.cell(5, 1).value, "Пользователь")
            self.assertIsNone(sheet.cell(5, 3).value)
            self.assertEqual(sheet.cell(2, 2).number_format, '#,##0.00 "₽"')


if __name__ == "__main__":
    unittest.main()
