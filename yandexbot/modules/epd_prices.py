"""Сбор публичных цен ЭПД СБИС и Контур.Логистики.

Модуль намеренно не использует ``prices.py``: номенклатура ЭПД не связана с
региональными тарифами 1СО. История хранится в отдельной SQLite-таблице.

Источники:
- СБИС — JSON-API прайса Saby (номенклатуры ETRN_*: пакеты и постоплата);
  если API недоступно, пакеты берутся из HTML страницы тарифов.
- Контур — официальный PDF прайс-лист: полный список пакетов (таблица 1) и
  дополнения (таблицы 2–14). Страница цен используется для сверки пакетов и
  как резерв, если PDF не скачался или не разобрался.

Тестовый прогон из терминала (из корня проекта), без бота и без отправки в мессенджер:
    python scripts/epd_prices_live_test.py                  # оба источника, Excel в stat/, БД не меняется
    python scripts/epd_prices_live_test.py --source kontur  # только один источник
    python scripts/epd_prices_live_test.py --save-db        # дополнительно записать историю, как боевой запуск
То же самое: python -m yandexbot.modules.epd_prices [аргументы].
"""

import argparse
import asyncio
import datetime
import html
import json
import logging
import re
import shutil
import sqlite3
import tempfile
import time
from io import BytesIO
from pathlib import Path

import aiohttp
from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

from yandexbot.core import access, config, schedule, state
from yandexbot.core.state import epd_prices_lock
from yandexbot.core.utils import (
    RUN_KIND_AUTO, RUN_KIND_MANUAL, ensure_run_kind_column, previous_run_filter, short_error,
)
from yandexbot.modules import archives
from yandexbot.messenger.ym_api import ym_send_file, ym_send_text

SBIS_URL = "https://saby.ru/tariffs?tab=mobile_workers"
SBIS_API_URL = "https://saby.ru/service/?x_version=26.2205-39"
SBIS_CONTRACTOR_ID = 33355772
# Цены ЭПД у Saby одинаковы во всех регионах (проверено 18.09.2026), берём Москву.
SBIS_REGION = "77"
KONTUR_URL = "https://kontur.ru/logistika/price"
KONTUR_PDF_URL = (
    "https://kontur.ru/Files/userfiles/file/products/diadoc/logistika/"
    "logistika-price.pdf"
)
TABLE_NAME = "epd_prices"
TARIFF = "tariff"
ADDON = "addon"
QUOTE_PRICE = "По запросу"

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru,en;q=0.9",
}
SBIS_API_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=UTF-8",
    "origin": "https://saby.ru",
    "referer": SBIS_URL,
    "x-requested-with": "XMLHttpRequest",
}

# Защита от «склеенных» чисел: цена одного документа ЭПД у обоих
# поставщиков — единицы рублей. Всё, что вне диапазона, — ошибка разбора.
PRICE_PER_DOCUMENT_RANGE = (0.5, 100.0)
# Минимум пакетов в таблице 1 PDF Контура; меньше — структура изменилась.
KONTUR_MIN_PDF_PACKAGES = 5

SOURCES = (("epd_sbis", "СБИС"), ("epd_kontur", "Контур"))

# Дополнения СБИС со страницы «Транспорт и логистика» в порядке сайта:
# номенклатура прайса → название, как на странице тарифов.
SBIS_ADDONS = {
    "Waybills": "Управление транспортом (TMS): Перевозки",
    "Logistic": "Управление транспортом (TMS): Перевозки и склад",
    "Waybills_1": "Путевые листы: создание, печать и учет ГСМ",
    "Applications": "Выездные сотрудники: Заявки и наряды",
    "Applic_Stock": "Выездные сотрудники: Заявки, наряды, касса и склад",
}


# ─── SQLite ───────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    """Открывает БД и создаёт только таблицу истории ЭПД."""
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(f"""
        CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('tariff', 'addon')),
            tariff_name TEXT NOT NULL,
            variant TEXT NOT NULL DEFAULT '',
            price_tariff REAL,
            price_document REAL,
            conditions TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL
        )
    """)
    conn.execute(f"""
        CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_lookup
        ON {TABLE_NAME}(source, entry_type, tariff_name, variant, fetched_at)
    """)
    ensure_run_kind_column(conn, TABLE_NAME)
    conn.commit()
    return conn


def _row_key(row: dict) -> tuple[str, str, str]:
    return row["entry_type"], row["tariff_name"], row.get("variant", "")


def db_previous(conn: sqlite3.Connection, source: str, current_run: str,
                run_kind: str | None = None) -> dict:
    """Цены прошлой выгрузки; run_kind=RUN_KIND_AUTO — только прошлой плановой."""
    kind_sql, kind_args = previous_run_filter(run_kind)
    previous = conn.execute(
        f"SELECT DISTINCT fetched_at FROM {TABLE_NAME} WHERE source = ? "
        f"AND fetched_at < ?{kind_sql} ORDER BY fetched_at DESC LIMIT 1",
        (source, current_run, *kind_args),
    ).fetchone()
    if previous is None:
        return {}
    return {
        (entry_type, tariff_name, variant): (price_tariff, price_document)
        for entry_type, tariff_name, variant, price_tariff, price_document in conn.execute(
            f"SELECT entry_type, tariff_name, variant, price_tariff, price_document "
            f"FROM {TABLE_NAME} WHERE source = ? AND fetched_at = ?",
            (source, previous[0]),
        )
    }


def db_save(conn: sqlite3.Connection, source: str, rows: list[dict], fetched_at: str,
            run_kind: str | None = None) -> None:
    conn.executemany(
        f"INSERT INTO {TABLE_NAME}(source, entry_type, tariff_name, variant, "
        "price_tariff, price_document, conditions, source_url, fetched_at, run_kind) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        [
            (
                source, row["entry_type"], row["tariff_name"], row.get("variant", ""),
                row.get("price_tariff"), row.get("price_document"),
                row.get("conditions", ""), row["source_url"], fetched_at, run_kind,
            )
            for row in rows
        ],
    )
    conn.commit()


# ─── Общие помощники разбора ──────────────────────────────────────────────────

def _clean(value: object) -> str:
    return re.sub(r"\s+", " ", html.unescape(str(value or ""))).strip()


def price_value(value: object) -> float | None:
    """Превращает цену с пробелами и ₽ в число, не угадывая диапазоны."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    text = _clean(value)
    if not text or re.search(r"\bот\b|[-–]", text, re.IGNORECASE):
        return None
    match = re.search(r"\d[\d\s]*([,.]\d+)?", text)
    if match is None:
        return None
    return float(re.sub(r"\s", "", match.group(0)).replace(",", "."))


def _count(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _row(entry_type: str, name: str, source_url: str, *, price_tariff: float | None = None,
         price_document: float | None = None, variant: str = "", conditions: str = "") -> dict:
    return {
        "entry_type": entry_type,
        "tariff_name": name,
        "variant": variant,
        "price_tariff": price_tariff,
        "price_document": price_document,
        "conditions": conditions,
        "source_url": source_url,
    }


def _package_row(quantity: int, price: float, source_url: str, conditions: str) -> dict | None:
    """Пакет «N документов»; отбрасывает цену, похожую на ошибку разбора."""
    if quantity <= 0:
        return None
    per_document = round(price / quantity, 4)
    low, high = PRICE_PER_DOCUMENT_RANGE
    if not low <= per_document <= high:
        logging.warning(
            "[ЭПД] пакет %s документов: цена %s ₽ даёт %s ₽ за документ — отброшено",
            quantity, price, per_document,
        )
        return None
    return _row(
        TARIFF, f"Пакет {_count(quantity)} документов", source_url,
        price_tariff=price, price_document=per_document,
        variant=str(quantity), conditions=conditions,
    )


def _sorted_packages(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: int(row["variant"]))


# ─── СБИС ─────────────────────────────────────────────────────────────────────

def parse_sbis_services(services: list[dict]) -> list[dict]:
    """Пакеты ETRN_<N>, постоплата ETRNPOST[O]_<N> и дополнения SBIS_ADDONS."""
    packages: list[dict] = []
    postpaid: list[tuple[int, bool, dict]] = []
    addons: dict[str, dict] = {}
    for item in services:
        code = str(item.get("nomenclature") or "")
        price = price_value(item.get("price"))
        if price is None:
            continue
        if code in SBIS_ADDONS:
            addons[code] = _row(ADDON, SBIS_ADDONS[code], SBIS_URL, price_tariff=price,
                                conditions="Цена на сайте за 1 год")
        elif match := re.fullmatch(r"ETRN_(\d+)", code):
            row = _package_row(int(match[1]), price, SBIS_URL, "Предоплатный пакет")
            if row:
                packages.append(row)
        elif match := re.fullmatch(r"ETRNPOST(O?)_(\d+)", code):
            limit, over = int(match[2]), bool(match[1])
            name = f"Постоплата: {'свыше' if over else 'до'} {_count(limit)} документов"
            postpaid.append((limit, over, _row(
                TARIFF, name, SBIS_URL, price_document=price,
                variant=f"{'>' if over else '<='}{limit}",
                conditions="Оплата за каждый документ по факту",
            )))
    postpaid.sort(key=lambda item: (item[0], item[1]))
    return (
        _sorted_packages(packages) + [row for *_, row in postpaid]
        + [addons[code] for code in SBIS_ADDONS if code in addons]
    )


def parse_sbis_page(html_text: str) -> list[dict]:
    """Резерв: пакеты и дополнения из HTML страницы тарифов по атрибуту nomenclaturecode."""
    services = [
        {"nomenclature": code, "price": price}
        for code, price in re.findall(
            r'nomenclaturecode="(\w+)"[^>]*>\s*([^<]+?)\s*<', html_text, re.I,
        )
    ]
    return parse_sbis_services(_unique_by_nomenclature(services))


def _unique_by_nomenclature(services: list[dict]) -> list[dict]:
    # В HTML одна и та же ячейка встречается в нескольких вариантах вёрстки.
    return list({item["nomenclature"]: item for item in services}.values())


def _sbis_api_payload(region: str) -> dict:
    return {
        "jsonrpc": "2.0", "protocol": 7,
        "method": "УслугаДоговора.GetContractServices",
        "params": {
            "ContractId": -1,
            "ClientContractorOfTariffChange": None,
            "FixationThemes": None,
            "AdditionalParameters": {
                "d": [None, None, region, SBIS_CONTRACTOR_ID, None],
                "s": [
                    {"t": "Число целое", "n": "ParentContract"},
                    {"t": "Число целое", "n": "Duration"},
                    {"t": "Строка", "n": "RegionCode"},
                    {"t": "Число целое", "n": "ContractorOfInvoice"},
                    {"t": "Число целое", "n": "accountId"},
                ],
                "_type": "record", "f": 0,
            },
        },
        "id": 1,
    }


def sbis_services_from_response(raw: dict) -> list[dict]:
    if raw.get("error"):
        error = raw["error"]
        message = error.get("message") if isinstance(error, dict) else error
        raise RuntimeError(f"API Saby вернуло ошибку: {message}")
    result = raw.get("result") or {}
    if isinstance(result, str):
        result = json.loads(result)
    return result.get("data") or []


async def fetch_sbis(session: aiohttp.ClientSession,
                     sources_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """sources_dir — куда сложить скачанное для архива исходников (папка sbis/)."""
    notes: list[str] = []
    try:
        async with session.post(
            SBIS_API_URL, json=_sbis_api_payload(SBIS_REGION), headers=SBIS_API_HEADERS,
        ) as response:
            if response.status != 200:
                raise RuntimeError(f"HTTP {response.status}")
            raw = await response.text()
        archives.keep_source(sources_dir, "sbis/api_services.json", raw)
        rows = parse_sbis_services(sbis_services_from_response(json.loads(raw)))
        if not any(row["entry_type"] == TARIFF for row in rows):
            raise RuntimeError("в ответе нет номенклатур ETRN_*")
        return rows, notes + _missing_sbis_addons(rows)
    except Exception as error:
        logging.warning("[ЭПД] СБИС: API недоступно, беру страницу тарифов: %s", error)
        notes.append(f"API недоступно ({short_error(error, 120)}), цены взяты со страницы тарифов")
    page = await _fetch_text(session, SBIS_URL)
    archives.keep_source(sources_dir, "sbis/tariffs.html", page)
    rows = parse_sbis_page(page)
    if not any(row["entry_type"] == TARIFF for row in rows):
        raise RuntimeError("пакеты ЭПД не найдены ни в API, ни на странице тарифов")
    if not any(row["entry_type"] == TARIFF and row["price_document"] is not None
               and row["price_tariff"] is None for row in rows):
        notes.append("на резервной странице нет цен постоплаты")
    return rows, notes + _missing_sbis_addons(rows)


def _missing_sbis_addons(rows: list[dict]) -> list[str]:
    found = {row["tariff_name"] for row in rows if row["entry_type"] == ADDON}
    missing = [name for name in SBIS_ADDONS.values() if name not in found]
    return [f"не найдены дополнения: {'; '.join(missing)}"] if missing else []


# ─── Контур: страница цен ─────────────────────────────────────────────────────

def parse_kontur_calculator(html_text: str) -> list[dict]:
    """Связывает объём пакета с ценой из публичного калькулятора Контур."""
    attribute = re.search(r'data-price-calculator="([^"]+)"', html_text, re.I | re.S)
    tariffs_attribute = re.search(r'data-calculator-tariffs="([^"]+)"', html_text, re.I | re.S)
    if attribute is None or tariffs_attribute is None:
        return []
    try:
        price_data = json.loads(html.unescape(attribute.group(1)))
        tariff_data = json.loads(html.unescape(tariffs_attribute.group(1)))
    except json.JSONDecodeError:
        return []
    prices = {
        item.get("tariffId"): (item.get("priceInfo") or {}).get("price")
        for item in (((price_data.get("billingIntegrationData") or {}).get("initialData") or {}).get("tariffs") or [])
    }
    rows = []
    for item in tariff_data:
        quantity = price_value(item.get("name"))
        price = price_value(prices.get(item.get("id")))
        if quantity is None or price is None:
            continue
        row = _package_row(int(quantity), price, KONTUR_URL, "Пакет действует в течение года")
        if row:
            rows.append(row)
    return _sorted_packages(rows)


# ─── Контур: официальный PDF ──────────────────────────────────────────────────

def pdf_text(pdf_data: bytes) -> str:
    from pypdf import PdfReader

    return "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(pdf_data)).pages)


def kontur_pdf_valid_from(text: str) -> str:
    match = re.search(r"Действительно с\s+(\d{1,2}\s+\S+\s+\d{4})", text)
    return match.group(1) if match else ""


def parse_kontur_pdf_packages(text: str) -> list[dict]:
    """Таблица 1: «N Тарифный план «Q документов» ЦЕНА Q».

    Цена и количество в текстовом слое PDF разделены только пробелом, как и
    разряды чисел («1 900 250»). Количество известно из названия пакета,
    поэтому цену получаем, отрезая его с конца строки.
    """
    valid_from = kontur_pdf_valid_from(text)
    conditions = "Официальный PDF прайс-лист" + (f", действует с {valid_from}" if valid_from else "")
    rows = []
    for raw_line in text.splitlines():
        line = _clean(raw_line)
        match = re.search(r"Тарифный план\s*«\s*([\d\s]+?)\s*документов\s*»\s*(.*)$", line)
        if match is None:
            continue
        quantity_digits = re.sub(r"\s", "", match.group(1))
        quantity_pattern = r"\s*".join(quantity_digits)
        split = re.fullmatch(rf"(\d[\d\s]*?)\s+{quantity_pattern}", match.group(2).strip())
        if split is None:
            logging.warning("[ЭПД] Контур PDF: не разобрана строка пакета: %s", line)
            continue
        row = _package_row(int(quantity_digits), price_value(split.group(1)), KONTUR_PDF_URL, conditions)
        if row:
            rows.append(row)
    rows = _sorted_packages(rows)
    # Отдельные строки таблицы 1 без фиксированной цены.
    table = re.search(r"\bТаблица\s+1\b(.*?)(?=\bТаблица\s+2\b|\Z)", text, re.S)
    if table is not None:
        body = _clean(table[1])
        quote = re.search(r"Тарифный план на более ([\d ]+) документов Рассчитывается индивидуально", body)
        if quote:
            quantity = int(quote[1].replace(" ", ""))
            rows.append(_row(
                TARIFF, f"Пакет свыше {_count(quantity)} документов", KONTUR_PDF_URL,
                variant=f">{quantity}", conditions=f"{conditions}. {QUOTE_PRICE}",
            ))
        if re.search(r"Тарифный план «Безлимит».+?Рассчитывается индивидуально", body):
            rows.append(_row(
                TARIFF, "Безлимит, 12 месяцев", KONTUR_PDF_URL,
                variant="unlimited", conditions=f"{conditions}. {QUOTE_PRICE}",
            ))
    return rows


_END_OF_TABLES_RE = re.compile(r"^1\.\s+Все цены указаны")
_TABLE_RE = re.compile(r"^Таблица\s+(\d+)")
_HEADING_RE = re.compile(r"^\d{1,2}(?:\.\d{1,2})*\.\s+\S")
_ROW_START_RE = re.compile(r"^(\d{1,2})(?:\s+(\D.*))?$")
_CONSULTATION_ROW_RE = re.compile(r"^(\d+)\s+(\d+(?:,\d+)?)\s+(.+)$")
_MAX_ROW_LINES = 8


def _numeric_tail(text: str) -> tuple[list[str], list[str]]:
    """Делит строку на слова-название и хвост из чисел/прочерков."""
    text = re.sub(r",\s+(\d{2})\b", r",\1", text)  # «5 800, 00» → «5 800,00»
    words = text.split()
    tail: list[str] = []
    while words and re.fullmatch(r"\d+(?:,\d+)?|[–-]", words[-1]):
        tail.insert(0, words.pop())
    return words, tail


def _number_at(tokens: list[str], start: int, end: int) -> float | str | None:
    """Число из токенов tokens[start:end+1] с разбивкой на разряды или «–»."""
    segment = tokens[start:end + 1]
    if segment == ["–"] or segment == ["-"]:
        return "–"
    if len(segment) == 1:
        return float(segment[0].replace(",", ".")) if re.fullmatch(r"\d+(?:,\d+)?", segment[0]) else None
    if not re.fullmatch(r"\d{1,3}", segment[0]):
        return None
    if not all(re.fullmatch(r"\d{3}", token) for token in segment[1:-1]):
        return None
    if not re.fullmatch(r"\d{3}(?:,\d+)?", segment[-1]):
        return None
    return float("".join(segment).replace(",", "."))


def _numbers_ending_at(tokens: list[str], end: int) -> list[tuple[int, float | str]]:
    found = []
    for start in range(end, max(-1, end - 4), -1):
        value = _number_at(tokens, start, end)
        if value is not None:
            found.append((start, value))
    return found


def _vat_triple(text: str) -> tuple[str, float, float | None, float] | None:
    """Хвост «БАЗА НДС ИТОГО»: разбивку на разряды выбираем по БАЗА + НДС = ИТОГО."""
    words, tokens = _numeric_tail(text)
    for total_start, total in _numbers_ending_at(tokens, len(tokens) - 1):
        if total == "–":
            continue
        for vat_start, vat in _numbers_ending_at(tokens, total_start - 1):
            for base_start, base in _numbers_ending_at(tokens, vat_start - 1):
                if base == "–":
                    continue
                if vat == "–":
                    matches = abs(base - total) < 0.01
                else:
                    matches = abs(base + vat - total) <= 0.02 and 0.05 <= vat / base <= 0.3
                if matches:
                    name = " ".join(words + tokens[:base_start])
                    return name, base, None if vat == "–" else vat, total
    return None


def _single_price(text: str) -> tuple[str, float] | None:
    """Хвост из одной цены (таблицы без колонок НДС)."""
    words, tokens = _numeric_tail(text)
    candidates = [(start, value) for start, value in _numbers_ending_at(tokens, len(tokens) - 1)
                  if value != "–" and value >= 100]
    if not candidates:
        return None
    start, value = min(candidates)
    return " ".join(words + tokens[:start]), value


def _clean_addon_name(name: str) -> str:
    name = re.sub(r"^\d+(?:\.\d+)*\.\s*", "", _clean(name))
    name = re.sub(r"«\s+", "«", name)
    name = re.sub(r"\s+»", "»", name)
    name = re.sub(r"Право использования программы для ЭВМ\s*«Контур\.Диадок»,?\s*", "Лицензия: ", name)
    name = re.sub(r",?\s*программы для ЭВМ\s*«Контур\.Диадок»", "", name)
    # Номера сносок приклеены к концу слова: «1 час7», «»Логистика.Базовый»6».
    name = re.sub(r"(?<=[A-Za-zА-Яа-яЁё»)])\d{1,2}(?=[\s),:;]|$)", "", name)
    name = re.sub(r"\s+,", ",", name)
    return name.strip(" ,;:")


def _vat_conditions(base: float, vat: float | None) -> str:
    if vat is None:
        return "НДС не облагается"
    return f"Без НДС {_money(base)}, НДС {_money(vat)}"


def parse_kontur_pdf_addons(text: str) -> list[dict]:
    """Дополнения из таблиц 2–14 PDF: лицензии модулей, услуги, сопровождение.

    Строки таблиц в текстовом слое разорваны на несколько строк: номер,
    название в несколько строк, цены. Собираем строку, пока в её конце не
    появится цена. Сноски («5 Отсчет начинается…») отсекаются тем, что номер
    строки таблицы должен идти по порядку. В цену дополнения пишем итог с НДС.
    """
    rows: list[dict] = []
    started = False
    heading = ""
    heading_lines = 0
    header: list[str] = []
    expected = 1
    current: list[str] | None = None

    def table_has_vat() -> bool:
        return "НДС" in " ".join(header)

    def is_consultation() -> bool:
        return "пользовател" in " ".join(header).lower()

    def try_complete() -> bool:
        text = " ".join(current)
        if re.search(r"индивидуально", text, re.I):
            logging.info("[ЭПД] Контур PDF: цена рассчитывается индивидуально — %s", _clean(text)[:120])
            return True
        parsed = _vat_triple(text)
        if parsed:
            name, base, vat, total = parsed
            conditions = _vat_conditions(base, vat)
        elif not table_has_vat() and (single := _single_price(text)):
            name, total = single
            conditions = "Базовая цена"
        elif (bundle := re.fullmatch(r"(.*в том числе:)\s*(.+)", text)) and (
            single := _single_price(bundle.group(2))
        ) and not single[0]:
            # Комплект («Рабочее место…, в том числе:») — дан только итог с НДС,
            # ниже идут его составляющие, их в отчёт не берём.
            name, total = bundle.group(1).removesuffix(", в том числе:").removesuffix(" в том числе:"), single[1]
            conditions = "Комплект, итоговая стоимость с НДС"
        else:
            return False
        name = _clean_addon_name(name)
        if name:
            rows.append(_row(ADDON, name, KONTUR_PDF_URL, price_tariff=total, conditions=conditions))
        return True

    for raw_line in text.splitlines():
        line = _clean(raw_line)
        if not line:
            continue
        if _END_OF_TABLES_RE.match(line):
            break
        if table := _TABLE_RE.match(line):
            started = started or int(table.group(1)) >= 2
            current, header, expected, heading_lines = None, [], 1, 0
            continue
        if _HEADING_RE.match(line):
            current, header, expected = None, [], 1
            heading, heading_lines = line, 1
            continue
        if (0 < heading_lines < 3 and current is None and expected == 1 and not header
                and not _ROW_START_RE.match(line)):
            # Заголовок раздела часто перенесён на следующую строку.
            heading, heading_lines = f"{heading} {line}", heading_lines + 1
            continue
        if not started:
            continue
        if is_consultation() and (consult := _CONSULTATION_ROW_RE.match(line)):
            parsed = _vat_triple(consult.group(3))
            if parsed and not parsed[0]:
                _, base, vat, total = parsed
                name = (f"{_clean_addon_name(heading)}: до {consult.group(1)} пользователей, "
                        f"до {consult.group(2)} ч")
                rows.append(_row(ADDON, name, KONTUR_PDF_URL, price_tariff=total,
                                 conditions=_vat_conditions(base, vat)))
            continue
        row_start = _ROW_START_RE.match(line)
        if row_start and int(row_start.group(1)) == expected:
            current = [row_start.group(2)] if row_start.group(2) else []
            expected += 1
            if current and try_complete():
                current = None
            continue
        if current is not None:
            current.append(line)
            if try_complete() or len(current) > _MAX_ROW_LINES:
                current = None
            continue
        header.append(line)
    return rows


def merge_kontur_packages(pdf_rows: list[dict], page_rows: list[dict]) -> tuple[list[dict], list[str]]:
    """PDF — основной источник пакетов; страница — сверка и резерв."""
    notes = []
    if not pdf_rows:
        return page_rows, notes
    pdf_by_quantity = {row["variant"]: row for row in pdf_rows}
    for page_row in page_rows:
        pdf_row = pdf_by_quantity.get(page_row["variant"])
        if pdf_row and pdf_row["price_tariff"] != page_row["price_tariff"]:
            notes.append(
                f"{page_row['tariff_name']}: на сайте {page_row['price_tariff']:,.0f} ₽, "
                f"в PDF {pdf_row['price_tariff']:,.0f} ₽ (в отчёте цена из PDF)".replace(",", " ")
            )
    return pdf_rows, notes


async def fetch_kontur(session: aiohttp.ClientSession,
                       sources_dir: Path | None = None) -> tuple[list[dict], list[str]]:
    """sources_dir — куда сложить скачанное для архива исходников (папка kontur/)."""
    notes: list[str] = []
    page_rows: list[dict] = []
    pdf_packages: list[dict] = []
    addons: list[dict] = []
    try:
        page = await _fetch_text(session, KONTUR_URL)
        page_rows = parse_kontur_calculator(page)
        if page_rows:
            archives.keep_source(sources_dir, "kontur/price.html", page)
        else:
            notes.append("веб-страница не дала цен для сверки")
    except Exception as error:
        logging.warning("[ЭПД] Контур: страница цен недоступна: %s", error)
        notes.append(f"страница цен недоступна ({short_error(error, 120)})")
    try:
        pdf = await _fetch_bytes(session, KONTUR_PDF_URL)
        archives.keep_source(sources_dir, "kontur/logistika-price.pdf", pdf)
        text = pdf_text(pdf)
        pdf_packages = parse_kontur_pdf_packages(text)
        addons = parse_kontur_pdf_addons(text)
        priced_packages = sum(row["price_tariff"] is not None for row in pdf_packages)
        if priced_packages < KONTUR_MIN_PDF_PACKAGES:
            raise RuntimeError(
                f"в таблице пакетов найдено {priced_packages} фиксированных цен — вероятно, изменился формат PDF"
            )
    except Exception as error:
        logging.warning("[ЭПД] Контур: PDF прайс-лист не обработан: %s", error, exc_info=True)
        notes.append(f"PDF прайс-лист не обработан ({short_error(error, 120)}), пакеты взяты со страницы")
        pdf_packages, addons = [], []
    packages, merge_notes = merge_kontur_packages(pdf_packages, page_rows)
    notes.extend(merge_notes)
    if not packages:
        raise RuntimeError("пакеты ЭПД не найдены ни в PDF, ни на странице цен")
    return packages + addons, notes


# ─── Сетевые запросы ──────────────────────────────────────────────────────────

async def _fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, allow_redirects=True) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} ({response.url})")
        return await response.text()


async def _fetch_bytes(session: aiohttp.ClientSession, url: str) -> bytes:
    async with session.get(url, allow_redirects=True) as response:
        if response.status != 200:
            raise RuntimeError(f"HTTP {response.status} ({response.url})")
        return await response.read()


_FETCHERS = {"epd_sbis": fetch_sbis, "epd_kontur": fetch_kontur}


async def collect(sources: tuple[str, ...] = ("epd_sbis", "epd_kontur"),
                  sources_dir: Path | None = None) -> dict:
    """Собирает выбранные источники. Ошибка одного не отменяет другой.

    sources_dir — папка, куда складываются скачанные исходники для архива.
    """
    results: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    notes: list[str] = []
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout, headers=HTTP_HEADERS) as session:
        for source, label in SOURCES:
            if source not in sources:
                continue
            try:
                rows, source_notes = await _FETCHERS[source](session, sources_dir)
                results[source] = rows
                notes.extend(f"{label}: {note}" for note in source_notes)
            except Exception as error:
                logging.error("Ошибка выгрузки ЭПД %s: %s", label, error, exc_info=True)
                errors[label] = short_error(error)
    return {"results": results, "errors": errors, "notes": notes}


def previous_prices(results: dict[str, list[dict]], fetched_at: str, save: bool,
                    run_kind: str = RUN_KIND_MANUAL) -> dict[str, dict]:
    """Цены прошлого запуска для подсветки; при save=True сохраняет текущий.

    Плановый запуск (RUN_KIND_AUTO) сравнивается только с прошлым плановым.
    """
    if save:
        conn = db_connect()
    elif config.DB_PATH.is_file():
        conn = sqlite3.connect(config.DB_PATH.resolve().as_uri() + "?mode=ro", uri=True)
    else:
        return {source: {} for source in results}
    try:
        if not save:
            columns = {row[1] for row in conn.execute(f"PRAGMA table_info({TABLE_NAME})")}
            if not columns:
                return {source: {} for source in results}
            if "run_kind" not in columns:
                run_kind = RUN_KIND_MANUAL
        previous = {source: db_previous(conn, source, fetched_at, run_kind) for source in results}
        if save:
            for source, rows in results.items():
                db_save(conn, source, rows, fetched_at, run_kind)
    finally:
        conn.close()
    return previous


# Сколько строк изменений показывать по одному поставщику; остальное — в Excel.
MAX_CHANGE_LINES = 15


def price_changes(rows: list[dict], previous: dict) -> dict:
    """Сравнение с прошлой выгрузкой: изменившиеся, новые и пропавшие позиции."""
    current = {_row_key(row): row for row in rows}
    changed, added = [], []
    for key, row in current.items():
        if key not in previous:
            added.append(row)
        elif previous[key] != (row.get("price_tariff"), row.get("price_document")):
            changed.append((row, previous[key]))
    removed = [(key, previous[key]) for key in previous if key not in current]
    return {"changed": changed, "added": added, "removed": removed}


def _price_text(price_tariff: float | None, price_document: float | None) -> str:
    if price_tariff is not None:
        return _money(price_tariff)
    if price_document is not None:
        return f"{_money(price_document)} за документ"
    return QUOTE_PRICE


def _change_line(row: dict, old: tuple) -> str:
    new = (row.get("price_tariff"), row.get("price_document"))
    # Сравниваем ту цену, что есть у позиции: пакет — по цене тарифа,
    # постоплата — по цене документа.
    index = 0 if old[0] is not None or new[0] is not None else 1
    before, after = old[index], new[index]
    trend = ""
    if before and after is not None:
        percent = (after - before) / before * 100
        text = f"{abs(percent):.1f}".rstrip("0").rstrip(".").replace(".", ",")
        trend = f" ({'+' if percent > 0 else '−'}{text}%)"
    arrow = "↔️" if before is None or after is None else ("📈" if after > before else "📉")
    return f"  {arrow} {row['tariff_name']}: {_price_text(*old)} → {_price_text(*new)}{trend}"


def format_changes(changes: dict, previous_count: int) -> list[str]:
    """Строки сообщения об изменениях одного поставщика."""
    changed, added, removed = changes["changed"], changes["added"], changes["removed"]
    if not (changed or added or removed):
        return ["✅ цены не изменились"]
    parts = []
    if changed:
        parts.append(f"изменились цены — {len(changed)}")
    if added:
        parts.append(f"новые позиции — {len(added)}")
    if removed:
        parts.append(f"пропали — {len(removed)}")
    details = [_change_line(row, old) for row, old in changed]
    details += [f"  🆕 {row['tariff_name']}: {_price_text(row.get('price_tariff'), row.get('price_document'))}"
                for row in added]
    if len(removed) > max(3, previous_count // 2):
        # Массовое «исчезновение» — почти всегда неполный ответ источника, а не смена прайса.
        details.append(f"  ❗ пропало {len(removed)} из {previous_count} позиций — похоже, источник "
                       "отдал неполные данные, см. примечания ниже")
    else:
        details += [f"  ➖ {key[1]} (было {_price_text(*old)})" for key, old in removed]
    if len(details) > MAX_CHANGE_LINES:
        rest = len(details) - MAX_CHANGE_LINES
        details = details[:MAX_CHANGE_LINES] + [f"  … и ещё {rest}, все изменения выделены в Excel"]
    return ["⚠️ " + ", ".join(parts), *details]


def has_news(collected: dict, previous: dict[str, dict]) -> bool:
    """Есть что сообщить: ошибка источника, нечего сравнивать или цены изменились.

    False — все поставщики выгружены и совпадают с прошлой выгрузкой
    (для режима «Отправлять только при изменении цен»).
    """
    if collected["errors"] or not collected["results"]:
        return True
    for source, rows in collected["results"].items():
        source_previous = previous.get(source) or {}
        if not {_row_key(row) for row in rows} & source_previous.keys():
            return True  # первая выгрузка или сменился формат названий
        if any(price_changes(rows, source_previous).values()):
            return True
    return False


def build_summary(collected: dict, previous: dict[str, dict] | None = None) -> str:
    """Итоговое сообщение. С previous — с разбором изменений цен по поставщикам."""
    results, errors = collected["results"], collected["errors"]
    if not results:
        return "❌ ЭПД-цены: ни один источник не удалось обработать. " + "; ".join(
            f"{label}: {error}" for label, error in errors.items()
        )
    blocks = []
    has_changes = has_history = False
    for source, label in SOURCES:
        if source not in results:
            continue
        rows = results[source]
        tariffs = sum(row["entry_type"] == TARIFF for row in rows)
        head = f"{label} (тарифов {tariffs}, дополнений {len(rows) - tariffs})"
        source_previous = (previous or {}).get(source) or {}
        if previous is None:
            blocks.append([f"✅ {head}"])
        elif not source_previous:
            blocks.append([head, "🆕 первая выгрузка — сравнивать не с чем"])
        elif not {_row_key(row) for row in rows} & source_previous.keys():
            # Ни одной общей позиции — сменился формат названий (новый парсер),
            # а не прайс. Иначе всё выглядело бы «новым» и «пропавшим».
            blocks.append([head, "🆕 прошлая выгрузка в другом формате — сравнивать не с чем"])
        else:
            has_history = True
            changes = price_changes(rows, source_previous)
            has_changes = has_changes or any(changes.values())
            blocks.append([head, *format_changes(changes, len(source_previous))])

    if previous is None:
        title = "📦 ЭПД-цены: выгрузка завершена."
    elif has_changes:
        title = "📦 ЭПД-цены — ⚠️ ЕСТЬ ИЗМЕНЕНИЯ ЦЕН"
    elif has_history:
        title = "📦 ЭПД-цены — ✅ цены без изменений"
    else:
        title = "📦 ЭПД-цены — первая выгрузка"
    lines = [title]
    if errors:
        lines.append("⚠️ Выгрузка частичная: не все поставщики обработаны.")
    for block in blocks:
        lines += ["", *block]
    if errors or collected["notes"]:
        lines.append("")
    lines.extend(f"❌ {label}: {error}" for label, error in errors.items())
    lines.extend(f"ℹ️ {note}" for note in collected["notes"])
    return "\n".join(lines)


# ─── Excel ────────────────────────────────────────────────────────────────────

def save_excel(results: dict[str, list[dict]], previous: dict[str, dict], path: str) -> None:
    """Сохраняет один лист на поставщика в согласованном формате отчёта."""
    workbook = Workbook()
    workbook.remove(workbook.active)
    fill_header = PatternFill("solid", fgColor="1F4E78")
    fill_section = PatternFill("solid", fgColor="D9EAF7")
    fill_changed = PatternFill("solid", fgColor="FFF2CC")   # цена изменилась
    fill_new = PatternFill("solid", fgColor="E2EFDA")       # позиции не было в прошлой выгрузке
    for source, title in (("epd_sbis", "СБИС ЭПД"), ("epd_kontur", "Контур Логистика")):
        if source not in results:
            continue
        sheet = workbook.create_sheet(title)
        sheet.append(["Название тарифа", "Цена тарифа", "Цена за документ"])
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = fill_header
        rows = results[source]
        tariffs = [row for row in rows if row["entry_type"] == TARIFF]
        addons = [row for row in rows if row["entry_type"] == ADDON]
        source_previous = previous.get(source, {})
        # Новые позиции подсвечиваем, только если прошлая выгрузка сопоставима
        # (есть общие позиции) — как и в сообщении в чат.
        comparable = bool({_row_key(row) for row in rows} & source_previous.keys())

        def append_rows(items: list[dict], with_document_price: bool = True) -> None:
            for row in items:
                total, unit = row.get("price_tariff"), row.get("price_document")
                values = [row["tariff_name"], QUOTE_PRICE if total is None and unit is None else total]
                if with_document_price:
                    values.append(unit)
                sheet.append(values)
                for cell in sheet[sheet.max_row]:
                    if cell.column > 1 and isinstance(cell.value, (int, float)):
                        # «,» в формате — разделитель разрядов; русский Excel
                        # показывает его пробелом во всех разрядах (1 000 000,00 ₽).
                        cell.number_format = '#,##0.00 "₽"'
                key = _row_key(row)
                cells = sheet[sheet.max_row]
                old = source_previous.get(key)
                if old is not None and old != (row.get("price_tariff"), row.get("price_document")):
                    for cell in cells:
                        cell.fill = fill_changed
                    # Прошлая цена — примечанием к ячейке, чтобы видеть «было → стало».
                    for cell, before, after in zip(cells[1:], old, (row.get("price_tariff"), row.get("price_document"))):
                        if before != after:
                            previous_value = QUOTE_PRICE if old == (None, None) else _money(before)
                            cell.comment = Comment(f"Было: {previous_value}", "Бот")
                elif old is None and comparable:
                    for cell in cells:
                        cell.fill = fill_new

        append_rows(tariffs)
        if addons:
            sheet.append([])
            sheet.append(["Дополнения", "Цена с НДС"])
            for cell in sheet[sheet.max_row]:
                cell.font = Font(bold=True)
                cell.fill = fill_section
            append_rows(addons, with_document_price=False)
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:C{max(sheet.max_row, 1)}"
        for column, width in ((1, 70), (2, 20), (3, 22)):
            sheet.column_dimensions[get_column_letter(column)].width = width
    workbook.save(path)


# ─── Запуск из бота ───────────────────────────────────────────────────────────

def pack_archive(sources_dir: Path, excel: Path, run_date: datetime.date) -> Path | None:
    """Архив ЭПД в stat/archive/: Excel с ценами СБИС и Контура + исходники."""
    try:
        return archives.make_archive("epd", sources_dir, extra_files=[excel], run_date=run_date)
    except OSError as error:
        logging.error("Не удалось собрать архив ЭПД: %s", error, exc_info=True)
        return None


async def run(target: dict | list[dict] | None = None, announce_text: str | None = None,
              footer: str = "", only_changes: bool = False, scheduled: bool = False) -> dict:
    """Собирает оба источника, формирует Excel и отправляет итог в чат.

    target=None — рассылка в чаты модуля «Цены ЭПД», выбранные в панели;
    получателей определяем в момент отправки, а не при старте выгрузки.
    only_changes=True — если цены не изменились, в чат ничего не отправляется
    (Excel и история в БД сохраняются, месячный архив создаётся по общим правилам).
    scheduled=True — плановый запуск: сравнивается с прошлым плановым, а не
    с ручным, сделанным между ними.
    """
    def recipients() -> dict | list[dict]:
        chosen = target if target is not None else access.module_targets("epd_prices")
        logging.info("[ЭПД] отправка итога: %s", chosen)
        return chosen

    if announce_text:
        await ym_send_text(announce_text, target=recipients())
    started = datetime.datetime.now()
    sources_dir = None
    if archives.needs_archive("epd", started):
        sources_dir = Path(tempfile.mkdtemp(prefix="epd_sources_"))
    try:
        collected = await collect(sources_dir=sources_dir)
        if not collected["results"]:
            message = build_summary(collected) + (f"\n\n{footer}" if footer else "")
            await ym_send_text(message, target=recipients())
            return {"ok": False, "summary": message, "file": None}

        now = datetime.datetime.now()
        previous = previous_prices(
            collected["results"], now.isoformat(timespec="seconds"), save=True,
            run_kind=RUN_KIND_AUTO if scheduled else RUN_KIND_MANUAL,
        )
        output = config.CONFIG_DIR / f"epd_prices_{now:%d.%m.%y}.xlsx"
        save_excel(collected["results"], previous, str(output))
        archive = None
        if sources_dir is not None and not collected["errors"] and all(
            collected["results"].get(source) for source, _ in SOURCES
        ):
            archive = pack_archive(sources_dir, output, started)
    finally:
        if sources_dir is not None:
            shutil.rmtree(sources_dir, ignore_errors=True)
    message = (build_summary(collected, previous)
               + (f"\n\n🗄 Архив: {archive.name}" if archive else "")
               + (f"\n\n{footer}" if footer else ""))
    if only_changes and not has_news(collected, previous):
        logging.info("[ЭПД] цены без изменений — итог в чаты не отправляется")
        for note in collected["notes"]:
            logging.info("[ЭПД] %s", note)
        return {"ok": True, "summary": message, "file": str(output), "sent": False}
    await ym_send_text(message, target=recipients())
    await ym_send_file(str(output), target=recipients())
    return {"ok": not collected["errors"], "summary": message, "file": str(output), "sent": True}


# ─── Плановый запуск (для main.py / --daemon) ─────────────────────────────────

def load_last_auto_run() -> datetime.datetime | None:
    return schedule.load_last_run(config.EPD_STATE_FILE)


async def scheduler_tick(now: datetime.datetime | None = None) -> bool:
    """Одна проверка расписания ЭПД. True — плановая выгрузка выполнена."""
    settings = config.epd_prices_module_settings()
    auto = settings["auto"]
    if not settings["enabled"] or not auto["enabled"]:
        return False
    if not schedule.is_due(auto["interval_days"], auto["at"], load_last_auto_run(), now):
        return False
    if epd_prices_lock.locked():
        return False  # идёт выгрузка по команде — попробуем на следующей проверке
    async with epd_prices_lock:
        started = datetime.datetime.now()
        upcoming = schedule.next_run(auto["interval_days"], auto["at"], started)
        logging.info("Плановая выгрузка цен ЭПД")
        try:
            await run(footer=f"🗓 Следующая автоматическая выгрузка ЭПД: {upcoming:%d.%m.%y} в {auto['at']}",
                      only_changes=auto["only_changes"], scheduled=True)
        finally:
            # Отмечаем даже при ошибке, иначе при сбое выгрузка повторялась бы
            # на каждой проверке и засыпала чаты сообщениями.
            schedule.save_last_run(config.EPD_STATE_FILE, started)
    return True


async def scheduler_loop():
    while True:
        state.heartbeat["epd_scheduler"] = time.time()
        try:
            await scheduler_tick()
        except Exception as error:
            logging.error("Планировщик ЭПД: ошибка проверки: %s", error, exc_info=True)
        await asyncio.sleep(config.SCHEDULER_CHECK_INTERVAL_SEC)


# ─── Тестовый прогон из терминала ─────────────────────────────────────────────

def _money(value: float | None) -> str:
    if value is None:
        return "—"
    text = f"{value:,.2f}".replace(",", " ")
    return (text[:-3] if text.endswith(".00") else text).replace(".", ",") + " ₽"


def _print_rows(title: str, rows: list[dict]) -> None:
    print(f"\n=== {title} ===", flush=True)
    for entry_type, caption in ((TARIFF, "Тарифы"), (ADDON, "Дополнения")):
        items = [row for row in rows if row["entry_type"] == entry_type]
        if not items:
            continue
        print(f"-- {caption} ({len(items)})")
        for row in items:
            total, unit = row.get("price_tariff"), row.get("price_document")
            tariff_price = QUOTE_PRICE if total is None and unit is None else _money(total)
            print(f"  {row['tariff_name'][:78]:<78} {tariff_price:>14} {_money(unit):>10}")


async def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Тестовая выгрузка цен ЭПД без бота: печатает цены и сохраняет Excel.",
    )
    parser.add_argument("--source", choices=["sbis", "kontur", "both"], default="both")
    parser.add_argument("--save-db", action="store_true",
                        help="записать результат в историю epd_prices (по умолчанию БД не меняется)")
    parser.add_argument("--no-excel", action="store_true", help="только вывести цены в терминал")
    parser.add_argument("--out", help="путь к Excel (по умолчанию stat/epd_prices_test_<время>.xlsx)")
    args = parser.parse_args(argv)

    sources = ("epd_sbis", "epd_kontur") if args.source == "both" else (f"epd_{args.source}",)
    print("ЭПД-цены: тестовая выгрузка, в мессенджер ничего не отправляется…", flush=True)
    started = datetime.datetime.now()
    sources_dir = None
    if args.save_db and args.source == "both" and not args.no_excel and archives.needs_archive("epd", started):
        sources_dir = Path(tempfile.mkdtemp(prefix="epd_sources_"))
    try:
        return await _cli_run(args, sources, sources_dir, started)
    finally:
        if sources_dir is not None:
            shutil.rmtree(sources_dir, ignore_errors=True)


async def _cli_run(args, sources: tuple[str, ...], sources_dir: Path | None,
                   started: datetime.datetime) -> int:
    collected = await collect(sources, sources_dir=sources_dir)
    for source, label in SOURCES:
        if source in collected["results"]:
            _print_rows(label, collected["results"][source])
    if not collected["results"]:
        print("\n" + build_summary(collected), flush=True)
        return 1

    previous = previous_prices(
        collected["results"], started.isoformat(timespec="seconds"), save=args.save_db,
    )
    print("\n" + build_summary(collected, previous), flush=True)
    if args.save_db:
        print(f"[SQLite] история записана в {config.DB_PATH}", flush=True)
    if not args.no_excel:
        output = Path(args.out) if args.out else config.CONFIG_DIR / f"epd_prices_test_{started:%Y%m%d_%H%M%S}.xlsx"
        save_excel(collected["results"], previous, str(output))
        print(f"[Excel] {output.resolve()}", flush=True)
    else:
        output = None
    if sources_dir is not None and output is not None and not collected["errors"] and all(
        collected["results"].get(source) for source, _ in SOURCES
    ):
        if archive := pack_archive(sources_dir, output, started):
            print(f"[Архив] {archive.resolve()}", flush=True)
    return 2 if collected["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_cli()))
