"""
Парсинг цен конкурентов (СБИС + Контур.Экстерн) с историей в SQLite
и формированием Excel-отчётов. Отправка результатов — через ym_api.
"""

import asyncio
import datetime
import html as html_lib
import io
import json
import logging
import math
import re
import sqlite3
import shutil
import tempfile
import time
from copy import copy
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Side
from openpyxl.utils import get_column_letter
from playwright.async_api import async_playwright

from yandexbot.core import access, config
from yandexbot.admin import store as admin_store
from yandexbot.modules import archives
from yandexbot.core.utils import (
    BOLD, CENTER, LEFT, RUN_KIND_AUTO, RUN_KIND_MANUAL, apply_diff_fill,
    ensure_run_kind_column, previous_run_filter, progress_text, short_error, staged_report,
)
from yandexbot.messenger.ym_api import PRICE_DELIVERY_RETRY_DELAYS, ym_send_text, ym_send_file
from yandexbot.core import schedule, state
from yandexbot.core.state import prices_lock

# ─── Регионы (встроены в файл) ────────────────────────────────────────────────

REGIONS_SBIS: list[tuple[int, str]] = [
    (1,  "Республика Адыгея"),
    (2,  "Республика Башкортостан"),
    (3,  "Республика Бурятия"),
    (4,  "Республика Алтай"),
    (5,  "Республика Дагестан"),
    (6,  "Республика Ингушетия"),
    (7,  "Республика Кабардино-Балкария"),
    (8,  "Республика Калмыкия"),
    (9,  "Республика Карачаево-Черкесия"),
    (10, "Республика Карелия"),
    (11, "Республика Коми"),
    (12, "Республика Марий Эл"),
    (13, "Республика Мордовия"),
    (14, "Республика Саха (Якутия)"),
    (15, "Республика Северная Осетия - Алания"),
    (16, "Республика Татарстан"),
    (17, "Республика Тыва"),
    (18, "Республика Удмуртия"),
    (19, "Республика Хакасия"),
    (20, "Республика Чечня"),
    (21, "Республика Чувашия"),
    (22, "Алтайский край"),
    (23, "Краснодарский край"),
    (24, "Красноярский край"),
    (25, "Приморский край"),
    (26, "Ставропольский край"),
    (27, "Хабаровский край"),
    (28, "Амурская область"),
    (29, "Архангельская область"),
    (30, "Астраханская область"),
    (31, "Белгородская область"),
    (32, "Брянская область"),
    (33, "Владимирская область"),
    (34, "Волгоградская область"),
    (35, "Вологодская область"),
    (36, "Воронежская область"),
    (37, "Ивановская область"),
    (38, "Иркутская область"),
    (39, "Калининградская область"),
    (40, "Калужская область"),
    (41, "Камчатский край"),
    (42, "Кемеровская область"),
    (43, "Кировская область"),
    (44, "Костромская область"),
    (45, "Курганская область"),
    (46, "Курская область"),
    (47, "Ленинградская область"),
    (48, "Липецкая область"),
    (49, "Магаданская область"),
    (50, "Московская область"),
    (51, "Мурманская область"),
    (52, "Нижегородская область"),
    (53, "Новгородская область"),
    (54, "Новосибирская область"),
    (55, "Омская область"),
    (56, "Оренбургская область"),
    (57, "Орловская область"),
    (58, "Пензенская область"),
    (59, "Пермский край"),
    (60, "Псковская область"),
    (61, "Ростовская область"),
    (62, "Рязанская область"),
    (63, "Самарская область"),
    (64, "Саратовская область"),
    (65, "Сахалинская область"),
    (66, "Свердловская область"),
    (67, "Смоленская область"),
    (68, "Тамбовская область"),
    (69, "Тверская область"),
    (70, "Томская область"),
    (71, "Тульская область"),
    (72, "Тюменская область"),
    (73, "Ульяновская область"),
    (74, "Челябинская область"),
    (75, "Забайкальский край"),
    (76, "Ярославская область"),
    (77, "Москва"),
    (78, "Санкт-Петербург"),
    (79, "Еврейская АО"),
    (83, "Ненецкий АО"),
    (86, "Ханты-Мансийский АО"),
    (87, "Чукотский АО"),
    (89, "Ямало-Ненецкий АО"),
    (90, "Запорожская область"),
    (91, "Республика Крым"),
    (92, "Севастополь"),
    (93, "Донецкая народная республика"),
    (94, "Луганская народная республика"),
    (95, "Херсонская область"),
    (99, "Байконур"),
]

REGIONS_KONTUR: list[tuple[int, str]] = REGIONS_SBIS  # коды совпадают
# ─── SQLite ───────────────────────────────────────────────────────────────────

def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,
            region_code TEXT NOT NULL,
            region_name TEXT NOT NULL,
            tariff_key  TEXT NOT NULL,
            price       REAL,
            fetched_at  TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_prices_lookup
        ON prices(source, region_code, tariff_key, fetched_at)
    """)
    ensure_run_kind_column(conn, "prices")
    conn.commit()
    return conn


def db_save_prices(conn: sqlite3.Connection, source: str,
                   rows: list[dict], fetched_at: str, run_kind: str | None = None):
    conn.executemany(
        "INSERT INTO prices(source,region_code,region_name,tariff_key,price,fetched_at,run_kind)"
        " VALUES(?,?,?,?,?,?,?)",
        [
            (source, r["region_code"], r["region_name"],
             r["tariff_key"], r["price"], fetched_at, run_kind)
            for r in rows
        ]
    )
    conn.commit()


def db_get_previous(conn: sqlite3.Connection, source: str,
                    current_run: str, run_kind: str | None = None) -> dict:
    """Цены прошлой выгрузки; run_kind=RUN_KIND_AUTO — только прошлой плановой."""
    kind_sql, kind_args = previous_run_filter(run_kind)
    cur = conn.execute(f"""
        SELECT DISTINCT fetched_at FROM prices
        WHERE source = ? AND fetched_at < ?{kind_sql}
        ORDER BY fetched_at DESC LIMIT 1
    """, (source, current_run, *kind_args))
    row = cur.fetchone()
    if not row:
        return {}
    prev_run = row[0]
    cur = conn.execute("""
        SELECT region_code, tariff_key, price FROM prices
        WHERE source = ? AND fetched_at = ?
    """ + kind_sql, (source, prev_run, *kind_args))
    previous = {(r[0], r[1]): r[2] for r in cur.fetchall()}
    # Старые версии сохраняли пустые цены СБИС поверх полного снимка.
    if source == "sbis":
        for (region, tariff), price in previous.items():
            if price is not None:
                continue
            row = conn.execute(f"""
                SELECT price FROM prices WHERE source = ? AND region_code = ? AND tariff_key = ?
                AND price IS NOT NULL AND fetched_at < ?{kind_sql}
                ORDER BY fetched_at DESC, id DESC LIMIT 1
            """, (source, region, tariff, prev_run, *kind_args)).fetchone()
            if row is not None:
                previous[(region, tariff)] = row[0]
    return previous

# ─── СБИС API ────────────────────────────────────────────────────────────────

SBIS_API_URL     = "https://saby.ru/service/"
SBIS_API_VERSION = "26.2205-39"
SBIS_CONTRACTOR_ID = 33355772

# В каждой группе: ключ истории цен, номенклатура каталога, подпись колонки.
SBIS_EXCEL_GROUPS = (
    ("Легкий", (
        ("Легкий_ИП", "EOpSBISfrmLIoN", "ИП"),
        ("Легкий_Бюджет", "EOpSBISfrmLB2o", "Бюджет"),
        ("Легкий_УСН", "EOpSBISfrmLUo", "УСН"),
        ("Легкий_ОСНО", "EOpSBISfrmLO2oN", "ОСНО"),
    )),
    ("Базовый", (
        ("Базовый_ИП", "EOpSBISfrmIoN", "ИП"),
        ("Базовый_Бюджет", "EOpSBISfrmB2o", "Бюджет"),
        ("Базовый_УСН", "EOpSBISfrmUo", "УСН"),
        ("Базовый_ОСНО", "EOpSBISfrmO2oN", "ОСНО"),
    )),
    ("", (
        ("Нулевка", "EOpNull", "Нулевка"),
        ("Buhta_УпБ", "Buhta", "ОБ/УпБ"),
    )),
    ("Уп.Бухгалтерия", (
        ("УпБ_лицензия", "EOpSBISfrmUPQ12", "Лицензия"),
        ("УпБ_квартал", "EOussvMin", "Квартал"),
        ("УпБ_1_199", "EOussvl200", "1-199"),
        ("УпБ_200_999", "EOussvl1000", "200-999"),
        ("УпБ_1000plus", "EOussvg1000", ">1000"),
    )),
    ("Корпоративный", (
        ("Корп_5", "EOpSBISfrmUP005N", "5"),
        ("Корп_10", "EOpSBISfrmUP010N", "10"),
        ("Корп_25", "EOpSBISfrmUP025N", "25"),
        ("Корп_50", "EOpSBISfrmUP050N", "50"),
        ("Корп_100", "EOpSBISfrmUP100N", "100"),
    )),
    ("АУСН", (
        ("АУСН_ИП", "Ent_ausn", "ИП"),
        ("АУСН_ЮЛ", "org_ausn", "ЮЛ"),
    )),
    ("Выгодный старт · новым клиентам", (
        ("Старт_ИП", "start_ip", "ИП"),
        ("Старт_УСН", "start_usn", "УСН"),
        ("Старт_ОСНО", "start_osno", "ОСНО"),
    )),
    ("Отчетность в другие ведомства", (
        ("Росприроднадзор", "EO_WI070613", "Росприроднадзор"),
        ("ФСКАТР", "EO_WI070637", "ФСКАТР (ФСРАР)"),
        ("Центробанк", "EO_CB", "Центробанк"),
    )),
    ("МВД по сотрудникам", (
        ("МВД_10", "EOMVD_staff10", "10 отчетов"),
        ("МВД_50", "EOMVD_staff50", "50 отчетов"),
        ("МВД_100", "EOMVD_staff100", "100 отчетов"),
        ("МВД_500", "EOMVD_staff500", "500 отчетов"),
    )),
    ("", (
        ("МВД_отель", "EO_MVD", "МВД для отеля"),
        ("Госпроверки", "SBIS_Ar", "Госпроверки"),
        ("Суперсверка", "Super", "Суперсверка"),
        ("Сверка_ЕНС", "ENS_base", "Сверка ЕНС"),
    )),
    ("Дополнительные направления сдачи", (
        ("Легкий_доп_направление", "EOpLiteNIN", "Легкий"),
        ("Базовый_доп_направление", "EOpNI", "Базовый"),
    )),
    ("Корпоративный · доп. филиалы", (
        ("Корп_доп_филиалы_5", "Filials5", "5"),
        ("Корп_доп_филиалы_10", "Filials10", "10"),
        ("Корп_доп_филиалы_25", "Filials25", "25"),
        ("Корп_доп_филиалы_50", "Filials50", "50"),
        ("Корп_доп_филиалы_100", "Filials100", "100"),
    )),
)
SBIS_NOMENCLATURES = {key: nomenclature for _, items in SBIS_EXCEL_GROUPS for key, nomenclature, _ in items}

SBIS_API_HEADERS = {
    "accept": "application/json, text/javascript, */*; q=0.01",
    "content-type": "application/json; charset=UTF-8",
    "origin": "https://saby.ru",
    "referer": "https://saby.ru/tariffs?tab=ereport",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "x-adaptive": "false",
    "x-requested-with": "XMLHttpRequest",
}


async def sbis_fetch_region(session: aiohttp.ClientSession,
                            region_code: str, raw_dir: Path | None = None) -> dict:
    payload = {
        "jsonrpc": "2.0", "protocol": 7,
        "method": "УслугаДоговора.GetContractServices",
        "params": {
            "ContractId": -1,
            "ClientContractorOfTariffChange": None,
            "FixationThemes": None,
            "AdditionalParameters": {
                "d": [None, None, str(region_code), SBIS_CONTRACTOR_ID, None],
                "s": [
                    {"t": "Число целое", "n": "ParentContract"},
                    {"t": "Число целое", "n": "Duration"},
                    {"t": "Строка",      "n": "RegionCode"},
                    {"t": "Число целое", "n": "ContractorOfInvoice"},
                    {"t": "Число целое", "n": "accountId"},
                ],
                "_type": "record", "f": 0,
            },
        },
        "id": 1,
    }
    url = f"{SBIS_API_URL}?x_version={SBIS_API_VERSION}"
    try:
        async with session.post(
            url, json=payload, headers=SBIS_API_HEADERS,
            timeout=aiohttp.ClientTimeout(total=30)
        ) as resp:
            resp.raise_for_status()
            raw = await resp.json(content_type=None)
        if raw_dir is not None:
            archives.keep_source(raw_dir, f"{region_code}.json", json.dumps(raw, ensure_ascii=False, indent=1))
        result_str = raw.get("result", "{}")
        result = json.loads(result_str) if isinstance(result_str, str) else result_str
        return {
            svc["nomenclature"]: svc["price"]
            for svc in result.get("data", [])
            if svc.get("nomenclature") and svc.get("price") is not None
        }
    except Exception as e:
        logging.error(f"[СБИС] Регион {region_code}: {e}")
        return {}


def sbis_extract_row(region_code: str, region_name: str, prices: dict) -> dict:
    return {
        "Код региона": int(region_code),
        "Название": region_name,
        **{
            key: prices.get(code)
            for key, code in SBIS_NOMENCLATURES.items()
        },
    }


async def parse_sbis(regions: list, progress_cb=None, delay_sec: float = 0.3,
                     raw_dir: Path | None = None) -> tuple[list[dict], list[dict]]:
    rows, db_records = [], []
    async with aiohttp.ClientSession() as session:
        for region_code, region_name in regions:
            code_str = str(region_code).zfill(2)
            prices = await sbis_fetch_region(session, code_str, raw_dir)
            row = sbis_extract_row(code_str, region_name, prices)
            rows.append(row)
            for col in SBIS_NOMENCLATURES:
                db_records.append({
                    "region_code": code_str,
                    "region_name": region_name,
                    "tariff_key":  col,
                    "price":       row.get(col),
                })
            if progress_cb:
                await progress_cb(code_str, region_name)
            await asyncio.sleep(delay_sec)
    return rows, db_records

# ─── Контур API ──────────────────────────────────────────────────────────────

OB_COUNTS = [4, 9, 19, 49, 99, 199, 499]
OB_TARIFF_NAMES = {819: "Общий Лайт", 820: "Общий", 821: "Общий +"}

class KonturAccessDenied(RuntimeError):
    """Контур ограничил доступ к странице или файлу прайса."""


async def kontur_download_regional_price_files(
    page, region_id: int, archive_dir: Path, main_files_only: bool = False,
) -> list[Path]:
    """Скачивает официальные прайсы региона в каталог временных файлов или архива.

    Ручной запуск скачивает прайсы с тарифами в DOCX или PDF.
    Плановый запуск сохраняет полный комплект официальных файлов.
    """
    tag = f"[Контур файлы][{region_id}]"
    download_page = f"https://www.kontur-extern.ru/price-download/{region_id}"
    region_dir = archive_dir / f"{region_id:02d}"
    attempts = 3
    for attempt in range(1, attempts + 1):
        try:
            response = await page.goto(download_page, wait_until="domcontentloaded", timeout=60000)
            if response is not None and response.status == 403:
                page_title = await page.title()
                target_url = urlsplit(response.url)._replace(query="", fragment="").geturl()
                raise KonturAccessDenied(
                    f"{tag} HTTP 403 при открытии {target_url}, title: {page_title!r}"
                )
            await asyncio.sleep(2)
            links = await page.evaluate("""
                () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                    href: a.href,
                    text: a.textContent.trim(),
                }))
                    .filter(link => /\\.(docx|pdf)(?:[?#]|$)/i.test(link.href))
                    .filter(link => link.href.includes('/Files/Modules/ExternPriceList/'))
            """)
            links = list({link["href"]: link for link in links}.values())
            if main_files_only:
                price_links = (
                    "часть 2", "ип без сотрудников", "налоговых представителей",
                    "общий лайт", "нулевая отчетность", "кадровые отчеты",
                    "об универсальный", "об все включено",
                )
                links = [
                    link for link in links
                    if any(name in link["text"].lower().replace("ё", "е") for name in price_links)
                ]
            if not links:
                required = "нужных ссылок на DOCX/PDF прайса" if main_files_only else "ссылок на DOCX/PDF прайса"
                raise RuntimeError(f"на странице нет {required}")

            shutil.rmtree(region_dir, ignore_errors=True)
            region_dir.mkdir(parents=True, exist_ok=True)
            saved: list[Path] = []
            for index, link in enumerate(links, start=1):
                url = link["href"]
                filename = Path(urlsplit(url).path).name
                target = region_dir / f"{index:02d}_{filename}"
                response = await page.request.get(url, timeout=60000)
                if response.status == 403:
                    target_url = urlsplit(response.url)._replace(query="", fragment="").geturl()
                    raise KonturAccessDenied(f"{tag} HTTP 403 при скачивании {target_url}")
                if not response.ok:
                    raise RuntimeError(f"не удалось скачать {filename}: HTTP {response.status}")
                target.write_bytes(await response.body())
                if target.stat().st_size == 0:
                    raise RuntimeError(f"скачан пустой файл {filename}")
                saved.append(target)
            print(f"{tag} сохранено файлов: {len(saved)}", flush=True)
            return saved
        except KonturAccessDenied as e:
            logging.error("%s", e)
            raise
        except Exception as e:
            if attempt == attempts:
                raise RuntimeError(
                    f"{tag} не удалось получить официальные файлы после "
                    f"{attempts} попыток: {short_error(e)}"
                ) from e
            logging.warning(
                f"{tag} попытка {attempt}/{attempts} не удалась: {short_error(e)}; "
                "повтор через 5 с"
            )
            await asyncio.sleep(5)

    raise AssertionError("цикл повторных попыток завершился без результата")


async def kontur_fetch_ob(page, region_id: int) -> list[dict]:
    """ОБ-тарифы извлекаются из официальных региональных DOCX/PDF.

    Контур снял с публикации CalculateBillingPrice: старый домен возвращает
    400, а прежний путь на www.kontur-extern.ru — 404. Не пытаемся подбирать
    новый недокументированный API; parse_kontur() ниже получает те же тарифы
    из прайс-листов через kontur_fetch_docx() и kontur_fetch_ob_lite_pdf().
    """
    return []


# ─── Тарифы из прайс-листа (docx, часть 2) ──────────────────────────────────

DOCX_TARIFF_NAMES = {
    "Бюджетная организация":      "Бюджетная организация",
    "Бюджетная организация плюс": "Бюджетная организация+",
    "Бухгалтерия малого бизнеса": "Бухгалтерия МБ",
}

DOCX_OB_TARIFF_NAMES = {
    "Общий": "Общий",
    "Общий плюс": "Общий +",
}

# Формы бизнеса в порядке переключателя «Форма бизнеса» на kontur.ru/extern/price.
# Форма строится из строки DOCX (категория клиента, система налогообложения,
# «с/без НДС» в названии тарифа), поэтому новые формы и тарифы попадают в
# выгрузку сами: этот список задаёт только порядок колонок, незнакомые — в конце.
KONTUR_BIZ_FORMS = [
    "ЮЛ на ОСНО",
    "ЮЛ на УСН без НДС",
    "ЮЛ на УСН с НДС",
    "ИП без НДС",
    "ИП с НДС",
    "ИП на АУСН",
    "ЮЛ на АУСН",
]

DOCX_SINGLE_BUDGET_TARIFFS = ["Бюджетник", "Бюджетник плюс", "Бюджетник Максимальный"]

# Тарифы из отдельных прайсов региона: группа (префикс ключа в БД) → заголовок
# в Excel. Колонки внутри группы добавляются по мере появления в прайсах.
KONTUR_OTHER_GROUPS = {
    "Другие": "Другие тарифы",
    "Филиальный": "Филиальный, 1 год",
    "НП": "Налоговый представитель, 12 мес.",
    "НП за документ": "Налоговый представитель: цена за документ",
}
ZERO_REPORT_TARIFFS = ("Нулевая отчетность", "Кадровые отчеты")
KONTUR_OTHER_TARIFFS = [("Другие", ZERO_REPORT_TARIFFS[0])]
KONTUR_ZONAL_OB_NAMES = ("ОБ Универсальный", "ОБ Все включено")

# Прайс «Налоговый представитель»: тарифы по регионам берутся за 12 месяцев —
# как «1 год» у остальных тарифов.
TAX_REP_PERIOD = "12 мес."

def _docx_package_count(label: str) -> int | str | None:
    base = re.fullmatch(r"1\+(\d+) абонент(?:а|ов)?", label)
    if base:
        return int(base[1])
    license_count = re.fullmatch(r"лицензия (\+\d+)", label, re.IGNORECASE)
    return license_count[1] if license_count else None


def _price_for_display(value: float) -> float:
    """Контур отображает итоговую стоимость в полных рублях, округляя вверх."""
    return float(math.ceil(value))


def _parse_price_str(s: str) -> float | None:
    s = "".join(s.split()).replace(",", ".")
    if not s or s == "–":
        return None
    try:
        return _price_for_display(float(s))
    except ValueError:
        return None


def kontur_tariff_name(raw: str) -> str:
    """«Оптимальный плюс без НДС» → «Оптимальный +»: НДС уходит в форму бизнеса."""
    name = re.sub(r"\s+(?:без|с)\s+НДС$", "", raw.strip())
    return re.sub(r"\s+плюс\b", " +", name)


def kontur_biz_form(category: str, tax: str, tariff_raw: str) -> str | None:
    """Форма бизнеса так, как её называет сайт: «ЮЛ на УСН без НДС», «ИП на АУСН»…

    Спецрежим кроме УСН (АУСН, а в будущем и другие) даёт форму
    «<категория> на <режим>»; для остальных НДС берётся из названия тарифа
    (тарифы без пометки — «Максимальный» — считаются тарифами с НДС).
    """
    category = category.strip()
    if not category:
        return None
    regimes = re.findall(r"\(([^)]+)\)", tax)
    special = [regime.strip() for regime in regimes if regime.strip() != "УСН"]
    if special:
        return f"{category} на {special[0]}"
    vat = "без НДС" if re.search(r"\bбез\s+НДС\b", tariff_raw) else "с НДС"
    usn = "УСН" in regimes
    osno = "общая" in tax
    if category == "ЮЛ" and osno and not usn:
        return "ЮЛ на ОСНО"
    if category == "ЮЛ" and usn and not osno:
        return f"ЮЛ на УСН {vat}"
    return f"{category} {vat}"


def parse_docx_pricelist(path: str, region_id: int = 0) -> list[dict]:
    """Разбирает региональный прайс-лист, часть 2.

    Таблицы распознаются по заголовку, а не по номеру: Контур добавляет
    тарифы (например, «Учетный для АУСН») и таблицы сдвигаются.
    Берётся итоговая стоимость с НДС на 1 год из строки «Право использования ПО».
    """
    tag = f"[docx][{region_id}][parse]"
    try:
        from docx import Document
    except ImportError:
        print(f"{tag} !! python-docx не установлен!", flush=True)
        logging.error("[docx] python-docx не установлен. pip install python-docx")
        return []
    try:
        doc = Document(path)
    except Exception as e:
        print(f"{tag} !! Не удалось открыть файл: {e}", flush=True)
        logging.error(f"[docx] Не удалось открыть {path}: {e}")
        return []

    print(f"{tag} Открыт файл, таблиц: {len(doc.tables)}", flush=True)

    def _cells(row) -> list[str]:
        return [" ".join(c.text.split()) for c in row.cells]

    if any(table.rows and _cells(table.rows[0])[:3] == TAX_REP_REGION_HEADER for table in doc.tables):
        return parse_tax_rep_tables(doc.tables, region_id, _cells, tag)

    result = []
    for table in doc.tables:
        if not table.rows:
            continue
        header = _cells(table.rows[0])
        if header[:2] == ["Код", "Регион"] and len(table.rows) > 1:
            result.extend(_parse_docx_region_table(table, header, region_id, _cells, tag))
            continue
        if not header or header[0] != "Наименование тарифного плана":
            continue
        col = {name: i for i, name in enumerate(header)}
        price_i = col.get("Итоговая стоимость с НДС")
        consist_i = col.get("Состав")
        period_i = col.get("Срок действия")
        if price_i is None or consist_i is None or period_i is None:
            continue
        category_i = col.get("Категория клиента")
        tax_i = col.get("Система налогообложения")
        count_i = col.get("Количество организаций на обслуживании")
        branches_i = col.get("Количество обособленных подразделений")
        quantity_i = count_i if count_i is not None else branches_i

        cur_tariff = cur_period = cur_count_raw = ""
        for row in table.rows[1:]:
            cells = _cells(row)
            if len(cells) <= price_i:
                continue
            if cells[0]:
                cur_tariff = cells[0]
            if cells[period_i]:
                cur_period = cells[period_i]
            if quantity_i is not None and cells[quantity_i]:
                cur_count_raw = cells[quantity_i]
            if cur_period != "1 год" or cells[consist_i] != "Право использования ПО":
                continue
            price = _parse_price_str(cells[price_i])
            if price is None:
                continue

            if category_i is not None and tax_i is not None:
                biz_form = kontur_biz_form(cells[category_i], cells[tax_i], cur_tariff)
                if biz_form is None:
                    continue
                tariff_key = kontur_tariff_name(cur_tariff)
                print(f"{tag}   IND OK tariff_key={tariff_key!r} biz_form={biz_form!r} price={price}", flush=True)
                result.append({
                    "table_type": "individual",
                    "tariff_key": tariff_key,
                    "biz_form":   biz_form,
                    "price":      price,
                    "ob_count":   None,
                })
            elif count_i is not None:
                count = _docx_package_count(cur_count_raw)
                if count is None:
                    continue
                if cur_tariff in DOCX_OB_TARIFF_NAMES:
                    table_type, tariff_key = "ob_docx", DOCX_OB_TARIFF_NAMES[cur_tariff]
                elif cur_tariff in DOCX_TARIFF_NAMES:
                    table_type, tariff_key = "group", DOCX_TARIFF_NAMES[cur_tariff]
                else:
                    continue
                print(f"{tag}   {table_type} OK tariff={tariff_key!r} count={count} price={price}", flush=True)
                result.append({
                    "table_type": table_type,
                    "tariff_key": tariff_key,
                    "price":      price,
                    "ob_count":   count,
                })
            elif branches_i is not None and tax_i is not None:
                tax = cells[tax_i]
                tax_name = {
                    "специальная (УСН)": "УСН",
                    "общая/смешанная": "ОСНО",
                    "специальная (УСН), общая/смешанная": "УСН/ОСНО",
                }.get(tax, tax)
                result.append({
                    "table_type": "other",
                    "group": cur_tariff,
                    "tariff_key": f"{cur_count_raw} · {tax_name}",
                    "price": price,
                    "ob_count": None,
                })
            elif cur_tariff in DOCX_SINGLE_BUDGET_TARIFFS:
                print(f"{tag}   SINGLE_BUDGET OK tariff={cur_tariff!r} price={price}", flush=True)
                result.append({
                    "table_type": "single_budget",
                    "tariff_key": cur_tariff,
                    "price":      price,
                    "ob_count":   None,
                })

    return result


def _parse_docx_region_table(table, header: list[str], region_id: int, cells_fn, tag: str) -> list[dict]:
    """Таблица «Код | Регион | Состав | …» отдельного прайса тарифа (например,
    «ИП без сотрудников без НДС»): цена берётся из строки своего региона."""
    tariff_raw = header[3] if len(header) > 3 else ""
    category = tariff_raw.split(" ", 1)[0]
    if category not in ("ИП", "ЮЛ"):
        return []
    sub_header = cells_fn(table.rows[1])
    if "Итоговая стоимость с НДС" not in sub_header:
        return []
    price_i = sub_header.index("Итоговая стоимость с НДС")
    for row in table.rows[2:]:
        cells = cells_fn(row)
        if len(cells) <= price_i or cells[0] != f"{region_id:02d}":
            continue
        if cells[2] != "Право использования ПО":
            continue
        price = _parse_price_str(cells[price_i])
        if price is None:
            return []
        tariff_key = kontur_tariff_name(tariff_raw)
        biz_form = kontur_biz_form(category, "", tariff_raw)
        print(f"{tag}   IND OK tariff_key={tariff_key!r} biz_form={biz_form!r} price={price}", flush=True)
        return [{
            "table_type": "individual",
            "tariff_key": tariff_key,
            "biz_form":   biz_form,
            "price":      price,
            "ob_count":   None,
        }]
    return []


TAX_REP_REGION_HEADER = ["Код региона", "Регион", "Зона регрессивной шкалы"]


def parse_tax_rep_tables(tables, region_id: int, cells_fn, tag: str) -> list[dict]:
    """Прайс «Налоговый представитель»: цены тарифов региона и цена за документ.

    Лайт/Базовый/Плюс — таблицы по регионам с трёхстрочной шапкой (тариф,
    срок, вид цены), Премиум — одна цена для всех регионов. Цена за документ
    зависит от «зоны регрессивной шкалы» региона и числа документов за период;
    у зон 4 и 10 свои интервалы, поэтому колонки строятся по самому прайсу.
    """
    code = f"{region_id:02d}"
    prices_by_label: dict[tuple[str, str], float] = {}
    zone = None
    zone_tables: list[list[list[str]]] = []

    def _take_prices(names: list[str], periods: list[str], kinds: list[str], row: list[str]):
        for i in range(1, len(row)):
            if i >= len(kinds) or periods[i] != TAX_REP_PERIOD or kinds[i] != "Итоговая стоимость с НДС":
                continue
            price = _parse_price_str(row[i])
            if price is not None:
                label = names[i].replace("Налоговый представитель", "").strip() or names[i]
                prices_by_label.setdefault(("НП", label), price)

    for table in tables:
        rows = [cells_fn(row) for row in table.rows]
        if not rows:
            continue
        head = rows[0]
        if head[:3] == TAX_REP_REGION_HEADER and len(rows) > 3:
            for row in rows[3:]:
                if len(row) > 3 and row[0] == code and row[3] == "Право":
                    zone = zone or row[2]
                    _take_prices(rows[0], rows[1], rows[2], row)
        elif head[0] == "Состав" and len(head) > 1 and head[1].startswith("Налоговый представитель") and len(rows) > 3:
            # Тариф без разбивки по регионам (Премиум) — одна цена для всех.
            for row in rows[3:]:
                if row[0] == "Право":
                    _take_prices(rows[0], rows[1], rows[2], row)
        elif head[0].startswith("Интервалы количества документов"):
            zone_tables.append(rows)

    if zone:
        prices_by_label[("НП за документ", "Зона")] = float(zone)
        zone_col = f"Зона {zone}"
        matched = False
        for rows in zone_tables:
            if zone_col not in rows[0]:
                continue
            matched = True
            col = rows[0].index(zone_col)
            for row in rows[1:]:
                price = _parse_price_str(row[col]) if col < len(row) else None
                if price is not None:
                    prices_by_label[("НП за документ", row[0])] = price
        if not matched:
            logging.warning(f"{tag} нет шкалы цен за документ для зоны {zone}")
    else:
        logging.warning(f"{tag} регион не найден в прайсе «Налоговый представитель»")

    return [
        {"table_type": "other", "group": group, "tariff_key": label, "price": price, "ob_count": None}
        for (group, label), price in prices_by_label.items()
    ]


def parse_zero_reporting_pdf(path: str, region_id: int) -> list[dict]:
    """Цена «Нулевой отчетности» региона из официального PDF (итог с НДС за год)."""
    tag = f"[pdf][{region_id}][zero]"
    try:
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as e:
        logging.error(f"{tag} не удалось прочитать PDF: {e}")
        return []
    return parse_zero_reporting_text(text, region_id)


def parse_zero_reporting_text(text: str, region_id: int) -> list[dict]:
    tag = f"[pdf][{region_id}][zero]"
    # В том же PDF после «Нулевой отчетности» идёт таблица «Кадровых отчетов»
    # с такой же разбивкой по регионам — отсекаем её.
    section = text.split("Тарифный план «Кадровые отчеты»", maxsplit=1)[0]
    if "Нулевая отчетность" not in section:
        return []
    row = _kontur_pdf_region_row(section, region_id)
    if not row:
        logging.warning(f"{tag} регион не найден в таблице «Нулевая отчетность»")
        return []
    prices = _kontur_pdf_final_prices(row.partition("Право использования ПО")[2])
    price = prices[0] if prices else None
    if price is None:
        logging.warning(f"{tag} не найдена цена «Нулевая отчетность»")
        return []
    print(f"{tag}   ZERO OK price={price}", flush=True)
    return [{
        "table_type": "other",
        "tariff_key": "Нулевая отчетность",
        "price": price,
        "ob_count": None,
    }]


def _kontur_pdf_region_row(text: str, region_id: int) -> str:
    # Строка может переноситься сразу после кода региона. За кодом должно
    # идти название региона, иначе начальные цифры цены оборвут строку.
    region_name = r"(?:[ \t]+|[ \t]*\n[ \t]*)(?!(?:Право|Услуги)\b)[А-Яа-яЁё]"
    match = re.search(
        rf"(?ms)^[ \t]*{region_id:02d}(?={region_name}).*?(?=^[ \t]*\d{{2}}{region_name}|\Z)",
        text,
    )
    return match.group(0) if match else ""


def _kontur_pdf_final_prices(row: str) -> list[float]:
    # После прочерка в столбце «НДС» стоит итоговая цена лицензии с услугами.
    totals = re.findall(r"–\s*((?:\d{1,3}(?:[^\S\r\n]\d{3})+|\d+),\d{2})", row.partition("Услуги")[0])
    return [_parse_price_str(total) for total in totals]


def parse_tax_rep_pdf_text(text: str, region_id: int) -> list[dict]:
    parts = re.split(r"(?m)^[ \t]*Налоговый представитель (Лайт|Базовый|Плюс|Премиум)[ \t]*$", text)
    result = {}
    zone = None
    for tariff, section in zip(parts[1::2], parts[2::2]):
        if tariff == "Премиум":
            row = section.partition("Право")[2]
        else:
            regional = _kontur_pdf_region_row(section, region_id)
            match = re.search(r"(?s)\b(\d+)\s+Право\d*\b(.*)", regional)
            if not match:
                continue
            zone = zone or int(match[1])
            row = match[2]
        header = re.search(r"(?m)^[ \t]*(?:\d+\s+мес\.[ \t]*)+$", section)
        periods = re.findall(r"\d+ мес\.", header[0]) if header else []
        prices = _kontur_pdf_final_prices(row)
        if TAX_REP_PERIOD in periods and len(prices) == len(periods):
            result.setdefault(("НП", tariff), prices[periods.index(TAX_REP_PERIOD)])

    if zone is not None:
        result[("НП за документ", "Зона")] = float(zone)
        headers = re.finditer(r"(?m)^[ \t]*Зона \d+(?:[ \t]+Зона \d+)*[ \t]*$", text)
        for header in headers:
            zones = [int(value) for value in re.findall(r"\d+", header[0])]
            if zone not in zones:
                continue
            for line in text[header.end():].lstrip().splitlines():
                match = re.fullmatch(r"(До \d+|От \d+(?: до \d+)?)\s+(.+)", line.strip())
                if not match:
                    break
                values = match[2].split()
                if len(values) == len(zones):
                    price = _parse_price_str(values[zones.index(zone)])
                    if price is not None:
                        result[("НП за документ", match[1])] = price
            break

    if not all(("НП", tariff) in result for tariff in ("Лайт", "Базовый", "Плюс", "Премиум")):
        logging.warning("[pdf][%s] неполные цены в прайсе «Налоговый представитель»", region_id)
    if zone is not None and not any(
        group == "НП за документ" and label != "Зона" for group, label in result
    ):
        logging.warning("[pdf][%s] нет шкалы цен за документ для зоны %s", region_id, zone)
    return [
        {"table_type": "other", "group": group, "tariff_key": label, "price": price, "ob_count": None}
        for (group, label), price in result.items()
    ]


def parse_zonal_ob_pdf_text(text: str, region_id: int) -> list[dict]:
    """Разбирает таблицы двух тарифов ОБ по ценовой зоне региона."""
    parts = re.split(r"(?m)^[ \t]*Ценовая зона №[ \t]*(\d+)[ \t]*$", text)
    match = re.search(rf"(?m)^[ \t]*{region_id:02d}[ \t]+.+?[ \t]{{2,}}(\d+)[ \t]*$", parts[0])
    if not match:
        logging.warning("[pdf][%s] регион не найден в таблице ценовых зон ОБ", region_id)
        return []
    zone = match[1]
    result = []
    for section_zone, section in zip(parts[1::2], parts[2::2]):
        if section_zone != zone:
            continue
        for line in section.splitlines():
            cells = re.split(r" {2,}", line.strip())
            if len(cells) != 8 or cells[1] != "Право использования ПО":
                continue
            count = re.match(r"(1\+\d+|\+\d+)\s+абонент", cells[0])
            if not count:
                continue
            package = count[1]
            for tariff, total in zip(KONTUR_ZONAL_OB_NAMES, (cells[4], cells[7])):
                price = _parse_price_str(total)
                if price is not None:
                    result.append({
                        "table_type": "ob_docx", "tariff_key": tariff, "price": price,
                        "ob_count": int(package[2:]) if package.startswith("1+") else package,
                    })
    if not result:
        logging.warning("[pdf][%s] не найдены цены тарифов ОБ для зоны %s", region_id, zone)
    return result


def _kontur_pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    pages = PdfReader(io.BytesIO(content)).pages
    if not pages:
        return ""
    first = pages[0].extract_text() or ""
    heading = " ".join(first.replace("ё", "е").split())
    if all(tariff in heading for tariff in KONTUR_ZONAL_OB_NAMES):
        # В таблице два тарифа. Пробелы макета отделяют столбцы цен.
        return "\n".join(page.extract_text(extraction_mode="layout") or "" for page in pages)
    return "\n".join([first, *(page.extract_text() or "" for page in pages[1:])])


def parse_kontur_pdf_text(text: str, region_id: int) -> list[dict]:
    heading = " ".join(text[:1000].replace("ё", "е").split())
    if all(tariff in heading for tariff in KONTUR_ZONAL_OB_NAMES):
        return parse_zonal_ob_pdf_text(text, region_id)
    if "Тарифные планы «Налоговый представитель»" in heading:
        return parse_tax_rep_pdf_text(text, region_id)
    if "«ИП без сотрудников без НДС»" in heading:
        row = _kontur_pdf_region_row(text, region_id)
        prices = _kontur_pdf_final_prices(row.partition("Право использования ПО")[2])
        if prices:
            return [{
                "table_type": "individual", "tariff_key": "ИП без сотрудников",
                "biz_form": "ИП без НДС", "price": prices[0], "ob_count": None,
            }]
        logging.warning("[pdf][%s] не найдена цена «ИП без сотрудников»", region_id)
        return []

    result = []
    if "Общий Лайт" in text:
        result.extend(parse_ob_lite_text(text, region_id))
    if "Нулевая отчетность" in text:
        result.extend(parse_zero_reporting_text(text, region_id))
    personnel = text.partition("Тарифный план «Кадровые отчеты»")[2]
    if personnel:
        table = personnel.partition("Стоимость тарифного плана")[2]
        prices = _kontur_pdf_final_prices(table.partition("Право использования ПО")[2])
        if prices:
            result.append({
                "table_type": "other", "tariff_key": "Кадровые отчеты",
                "price": prices[0], "ob_count": None,
            })
        else:
            logging.warning("[pdf][%s] не найдена цена «Кадровые отчеты»", region_id)
    return result


def parse_ob_lite_pdf(path: str, region_id: int) -> list[dict]:
    """Извлекает цены «Общий Лайт» для одного региона из официального PDF."""
    tag = f"[pdf][{region_id}][parse]"
    try:
        from pypdf import PdfReader
        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    except Exception as e:
        logging.error(f"{tag} не удалось прочитать PDF: {e}")
        return []
    return parse_ob_lite_text(text, region_id)


def parse_ob_lite_text(text: str, region_id: int) -> list[dict]:
    tag = f"[pdf][{region_id}][parse]"
    # В PDF после раздела «Общий Лайт» идут «Нулевая отчетность» и «Кадровые
    # отчеты». Ограничиваем поиск первым разделом, чтобы не принять их цены.
    lite_section = text.split("Тарифный план «Нулевая отчетность»", maxsplit=1)[0]
    region_match = re.search(
        rf"(?ms)^{region_id:02d}\s+.*?(?=^\d{{2}}\s+|\Z)", lite_section
    )
    if not region_match:
        logging.warning(f"{tag} регион не найден в разделе «Общий Лайт»")
        return []

    region_text = region_match.group(0)
    result = []
    for count in OB_COUNTS:
        count_pattern = rf"1\+{count}\s+абонент(?:а|ов).*?–\s*([\d\s]+,\d{{2}})"
        price_match = re.search(count_pattern, region_text, flags=re.S)
        if not price_match:
            logging.warning(f"{tag} не найдена цена для 1+{count}")
            continue
        price = _parse_price_str(price_match.group(1))
        if price is None:
            continue
        print(f"{tag}   OB_LITE OK count={count} price={price}", flush=True)
        result.append({
            "table_type": "ob_docx",
            "tariff_key": "Общий Лайт",
            "price": price,
            "ob_count": count,
        })
    return result


async def kontur_fetch_ob_lite_pdf(page, region_id: int, tmp_dir: str) -> list[dict]:
    """Скачивает региональный PDF с прайсом «Общий Лайт» и разбирает его."""
    import os

    download_page = f"https://www.kontur-extern.ru/price-download/{region_id}"
    try:
        await page.goto(download_page, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)
        pdf_url = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const match = links.find(a =>
                    a.textContent.includes('Общий Лайт') &&
                    a.href.toLowerCase().includes('.pdf')
                );
                return match ? match.href : null;
            }
        """)
        if not pdf_url:
            logging.warning(f"[pdf] Регион {region_id}: ссылка «Общий Лайт» не найдена")
            return []
        tmp_path = os.path.join(tmp_dir, f"{region_id}_ob_lite.pdf")
        async with page.expect_download() as dl_info:
            await page.evaluate("url => { window.location.href = url; }", pdf_url)
        await (await dl_info.value).save_as(tmp_path)
        return parse_ob_lite_pdf(tmp_path, region_id)
    except Exception as e:
        logging.error(f"[pdf] Регион {region_id}: {e}", exc_info=True)
        return []
    finally:
        try:
            if 'tmp_path' in locals() and os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


async def kontur_fetch_docx(page, region_id: int, tmp_dir: str) -> list[dict]:
    import os
    download_page = f"https://www.kontur-extern.ru/price-download/{region_id}"
    print(f"\n[docx][{region_id}] -> переходим на {download_page}", flush=True)
    try:
        await page.goto(download_page, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        all_links = await page.evaluate("""
            () => Array.from(document.querySelectorAll('a[href]')).map(a => ({
                href: a.href,
                text: a.textContent.trim().slice(0, 80)
            }))
        """)
        print(f"[docx][{region_id}] Ссылки на странице ({len(all_links)} шт.):", flush=True)
        for lnk in all_links:
            print(f"  TEXT={repr(lnk['text'])}  HREF={lnk['href']}", flush=True)

        docx_url = await page.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const match = links.find(a =>
                    a.href.toLowerCase().includes('pricelist') &&
                    a.href.toLowerCase().includes('.docx')
                );
                return match ? match.href : null;
            }
        """)
        print(f"[docx][{region_id}] Найденная ссылка: {docx_url!r}", flush=True)
        if not docx_url:
            print(f"[docx][{region_id}] !! ссылка не найдена — пропускаем", flush=True)
            logging.warning(f"[docx] Регион {region_id}: ссылка не найдена")
            return []

        tmp_path = os.path.join(tmp_dir, f"{region_id}_pricelist.docx")
        print(f"[docx][{region_id}] Скачиваем -> {tmp_path}", flush=True)
        async with page.expect_download() as dl_info:
            await page.evaluate(f"() => {{ window.location.href = '{docx_url}'; }}")
        download = await dl_info.value
        await download.save_as(tmp_path)
        size = os.path.getsize(tmp_path)
        print(f"[docx][{region_id}] Скачано {size} байт", flush=True)
    except Exception as e:
        print(f"[docx][{region_id}] !! Ошибка при скачивании: {e}", flush=True)
        logging.error(f"[docx] Регион {region_id}: {e}")
        return []

    parsed = parse_docx_pricelist(tmp_path, region_id)
    print(f"[docx][{region_id}] Распарсено строк: {len(parsed)}", flush=True)
    for r in parsed:
        print(f"  {r}", flush=True)
    try:
        os.unlink(tmp_path)
    except Exception:
        pass

    return parsed


def _parse_widget_html(html_str: str) -> list[dict]:
    if not html_str:
        return []
    blocks = re.findall(
        r'data-price-table-tariffs="([^"]+)"[^>]*data-table-group-title="([^"]+)"',
        html_str
    )
    result = []
    for raw_tariffs, raw_title in blocks:
        title = html_lib.unescape(raw_title)
        try:
            tariffs_data = json.loads(html_lib.unescape(raw_tariffs))
        except Exception:
            continue
        for t in tariffs_data:
            name = t.get("name", "")
            bp   = t.get("billingPriceInfo") or t.get("priceInfo") or {}
            price = bp.get("price")
            if name and price is not None:
                result.append({
                    "tariff_group":  title,
                    "tariff_name":   name,
                    "price":         _price_for_display(float(price)),
                    "offline_price": bp.get("offlinePrice"),
                })
    return result


async def parse_kontur(
    regions: list, source_dir: Path, progress_cb=None, region_delay_sec: float = 1.0,
    region_delay_max_sec: float | None = None,
    main_files_only: bool = False,
) -> tuple:
    rows_by_region: dict[str, dict] = {}
    db_records: list[dict] = []
    incomplete_regions: set[str] = set()

    msb_col_order: list[tuple] = []
    msb_col_set:   set         = set()

    ob_columns: dict[str, set[int | str]] = {
        name: set() for name in (*OB_TARIFF_NAMES.values(), *KONTUR_ZONAL_OB_NAMES)
    }

    docx_columns: dict[str, set[int | str]] = {
        name: set(OB_COUNTS) for name in DOCX_TARIFF_NAMES.values()
    }

    sb_col_order: list[str] = list(DOCX_SINGLE_BUDGET_TARIFFS)
    other_col_order: list[tuple[str, str]] = list(KONTUR_OTHER_TARIFFS)
    zero_reporting_pdfs: set[str] = set()

    def store_other(code: str, region_name: str, region_row: dict, tariff: dict) -> None:
        group, name = tariff.get("group", "Другие"), tariff["tariff_key"]
        if (group, name) not in other_col_order:
            other_col_order.append((group, name))
        region_row[("other", group, name)] = tariff["price"]
        db_records.append({
            "region_code": code,
            "region_name": region_name,
            "tariff_key": f"{group} | {name}",
            "price": tariff["price"],
        })

    total = len(regions)
    # Общие для нескольких регионов PDF скачиваются для архивов, а текст извлекается один раз.
    pdf_text = lru_cache(maxsize=8)(_kontur_pdf_text)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36 Edg/147.0.0.0"
            ),
            locale="ru-RU",
            extra_http_headers={"Accept-Language": "ru,en;q=0.9", "DNT": "1"},
        )
        page = await context.new_page()
        for done, (region_id, region_name) in enumerate(regions, start=1):
            code       = str(region_id)
            region_row = rows_by_region.setdefault(code, {"region_name": region_name})

            source_files = await kontur_download_regional_price_files(
                page, region_id, source_dir, main_files_only=main_files_only,
            )
            docx_parsed: list[dict] = []
            pdf_parsed: list[dict] = []
            for source_file in source_files:
                if source_file.suffix.lower() == ".docx":
                    docx_parsed.extend(parse_docx_pricelist(str(source_file), region_id))
                elif source_file.suffix.lower() == ".pdf":
                    try:
                        text = pdf_text(source_file.read_bytes())
                    except Exception as e:
                        logging.warning(
                            f"[pdf][{region_id}] не удалось проверить {source_file.name}: {e}"
                        )
                        continue
                    if "«Нулевая отчетность»" in " ".join(text[:1000].split()):
                        zero_reporting_pdfs.add(text)
                    pdf_parsed.extend(parse_kontur_pdf_text(text, region_id))

            if not any(row["table_type"] == "individual" for row in docx_parsed):
                incomplete_regions.add(code)

            for t in [*docx_parsed, *pdf_parsed]:
                tt = t["table_type"]
                if tt == "individual":
                    col_key = (t["tariff_key"], t["biz_form"])
                    if col_key not in msb_col_set:
                        msb_col_set.add(col_key)
                        msb_col_order.append(col_key)
                    region_row[col_key] = t["price"]
                    db_records.append({
                        "region_code": code,
                        "region_name": region_name,
                        "tariff_key": f"DOCX | {t['tariff_key']} | {t['biz_form']}",
                        "price": t["price"],
                    })
                elif tt == "single_budget":
                    sb_key = ("docx_sb", t["tariff_key"])
                    region_row[sb_key] = t["price"]
                    db_records.append({
                        "region_code": code,
                        "region_name": region_name,
                        "tariff_key":  f"Docx SB | {t['tariff_key']}",
                        "price":       t["price"],
                    })
                elif tt == "group":
                    name, count = t["tariff_key"], t["ob_count"]
                    docx_columns[name].add(count)
                    docx_key = ("docx_group", name, count)
                    region_row[docx_key] = t["price"]
                    db_records.append({
                        "region_code": code,
                        "region_name": region_name,
                        "tariff_key":  f"Docx | {name} | {_ob_package_label(count)}",
                        "price":       t["price"],
                    })
                elif tt == "ob_docx":
                    name, count = t["tariff_key"], t["ob_count"]
                    ob_columns.setdefault(name, set()).add(count)
                    ob_key = ("ob", name, count)
                    region_row[ob_key] = t["price"]
                    db_records.append({
                        "region_code": code,
                        "region_name": region_name,
                        "tariff_key": f"ОБ | {name} | {_ob_package_label(count)}",
                        "price": t["price"],
                    })
                elif tt == "other":
                    store_other(code, region_name, region_row, t)

            if progress_cb:
                await progress_cb(done, total, region_name)
            if region_delay_sec > 0:
                max_sec = max(
                    region_delay_sec,
                    region_delay_max_sec if region_delay_max_sec is not None else (region_delay_sec * 1.5),
                )
                await asyncio.sleep(random.uniform(region_delay_sec, max_sec))
            else:
                await asyncio.sleep(0)

        await browser.close()

    if len(zero_reporting_pdfs) == 1:
        # Общий PDF содержит региональные цены. Ссылка на него бывает не на
        # каждой странице региона, хотя цена в самом прайсе опубликована.
        [shared_price_list] = zero_reporting_pdfs
        for region_id, region_name in regions:
            code = str(region_id)
            region_row = rows_by_region[code]
            missing = {name for name in ZERO_REPORT_TARIFFS if ("other", "Другие", name) not in region_row}
            if missing:
                for tariff in parse_kontur_pdf_text(shared_price_list, region_id):
                    if tariff["tariff_key"] in missing:
                        store_other(code, region_name, region_row, tariff)

    rows = [
        (str(rid), rname, rows_by_region.get(str(rid), {"region_name": rname}))
        for rid, rname in regions
    ]
    return (rows, db_records, msb_col_order, _ob_column_order(ob_columns), _ob_column_order(docx_columns),
            sb_col_order, other_col_order, incomplete_regions)


def _ob_column_order(columns: dict[str, set[int | str]]) -> list[tuple[str, int | str]]:
    return [
        (name, count)
        for name, counts in columns.items()
        for count in sorted(counts, key=lambda value: (isinstance(value, str), int(value)))
    ]


def _ob_package_label(count: int | str) -> str:
    return f"1+{count}" if isinstance(count, int) else count


def _outline_tariff_groups(sheet):
    """Обводит объединённые заголовки вместе со всеми строками их группы."""
    edge = Side(style="thin", color="000000")
    bottom = sheet.max_row
    for merged in sheet.merged_cells.ranges:
        if merged.min_row != 1 or merged.max_row != 1:
            continue
        for row in (1, bottom):
            for column in range(merged.min_col, merged.max_col + 1):
                cell = sheet.cell(row, column)
                border = copy(cell.border)
                setattr(border, "top" if row == 1 else "bottom", edge)
                cell.border = border
        for row in range(1, bottom + 1):
            for column, side in ((merged.min_col, "left"), (merged.max_col, "right")):
                cell = sheet.cell(row, column)
                border = copy(cell.border)
                setattr(border, side, edge)
                cell.border = border


def save_sbis_excel(all_rows: list[dict], prev: dict, filepath: str):
    wb = Workbook()
    ws = wb.active
    ws.title = "СБИС"

    col_keys = ("Код региона", "Название", *SBIS_NOMENCLATURES)
    headers = ["Код", "Регион"]
    for group, items in SBIS_EXCEL_GROUPS:
        if group:
            first = len(headers) + 1
            ws.cell(1, first, group)
            ws.merge_cells(start_row=1, start_column=first, end_row=1, end_column=first + len(items) - 1)
        headers.extend(label for _, _, label in items)
    for column, label in enumerate(headers, start=1):
        ws.cell(2, column, label)

    for row_data in all_rows:
        code_str = str(row_data.get("Код региона","")).zfill(2)
        excel_row = [row_data.get(key) for key in col_keys]
        ws.append(excel_row)
        r = ws.max_row
        for c_idx, key in enumerate(col_keys, start=1):
            cell = ws.cell(row=r, column=c_idx)
            cell.alignment = LEFT if c_idx <= 2 else CENTER
            if c_idx > 2:
                old = prev.get((code_str, key))
                new = row_data.get(key)
                apply_diff_fill(cell, old, new)

    for col in range(1, len(headers) + 1):
        ws.cell(1, col).font = BOLD
        ws.cell(1, col).alignment = CENTER
        ws.cell(2, col).font = BOLD
        ws.cell(2, col).alignment = CENTER

    for col, label in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(col)].width = 24 if col == 2 else max(10, len(label) + 2)

    _outline_tariff_groups(ws)
    wb.save(filepath)


def save_kontur_excel(
    rows: list[tuple],
    msb_col_order: list[tuple],
    ob_col_order:  list[tuple],
    docx_col_order: list[tuple],
    sb_col_order:   list[str],
    other_col_order: list[tuple[str, str]],
    prev: dict,
    filepath: str,
):
    from itertools import groupby as _groupby

    wb = Workbook()
    ws = wb.active
    ws.title = "Контур.Экстерн"

    LIGHT_BLUE  = PatternFill("solid", start_color="BDD7EE", end_color="BDD7EE")
    LIGHT_GRAY  = PatternFill("solid", start_color="D9D9D9", end_color="D9D9D9")
    ORANGE_FILL = PatternFill("solid", start_color="FCE4D6", end_color="FCE4D6")
    PEACH_FILL  = PatternFill("solid", start_color="FFF2CC", end_color="FFF2CC")
    GREEN_FILL  = PatternFill("solid", start_color="E2EFDA", end_color="E2EFDA")
    GREEN_DARK  = PatternFill("solid", start_color="C6EFCE", end_color="C6EFCE")
    PURPLE_LIGHT= PatternFill("solid", start_color="EAD1DC", end_color="EAD1DC")

    # Верхняя строка — форма бизнеса (в порядке сайта, новые формы в конце),
    # под ней — тарифы этой формы.
    _tariff_order = sorted(
        dict.fromkeys(t for _, t in msb_col_order),
        key=lambda form: (
            KONTUR_BIZ_FORMS.index(form) if form in KONTUR_BIZ_FORMS else len(KONTUR_BIZ_FORMS)
        ),
    )
    _group_order  = list(dict.fromkeys(g for g, _ in msb_col_order))
    _api_set      = set(msb_col_order)
    msb_col_order = [
        (group, tariff)
        for tariff in _tariff_order
        for group  in _group_order
        if (group, tariff) in _api_set
    ]

    cur_col = 2

    msb_col_map: dict[tuple, int] = {}
    for col_key in msb_col_order:
        msb_col_map[col_key] = cur_col
        cur_col += 1

    ob_col_map: dict[tuple, int] = {}
    for tname, cnt in ob_col_order:
        ob_col_map[(tname, cnt)] = cur_col
        cur_col += 1

    docx_col_map: dict[tuple, int] = {}
    for tname, cnt in docx_col_order:
        docx_col_map[(tname, cnt)] = cur_col
        cur_col += 1

    sb_col_map: dict[str, int] = {}
    for tname in sb_col_order:
        sb_col_map[tname] = cur_col
        cur_col += 1

    # Группы «Другие тарифы», «Налоговый представитель»… — подряд, в порядке
    # KONTUR_OTHER_GROUPS (незнакомые группы — в конце).
    _other_groups = list(KONTUR_OTHER_GROUPS)
    other_col_order = sorted(
        other_col_order,
        key=lambda key: _other_groups.index(key[0]) if key[0] in _other_groups else len(_other_groups),
    )
    other_col_map: dict[tuple[str, str], int] = {}
    for other_key in other_col_order:
        other_col_map[other_key] = cur_col
        cur_col += 1

    total_cols = cur_col - 1

    ws.cell(1, 1).value = "Регион"
    ws.cell(1, 1).font = BOLD; ws.cell(1, 1).alignment = CENTER; ws.cell(1, 1).fill = LIGHT_BLUE
    ws.cell(2, 1).value = "Регион"
    ws.cell(2, 1).font = BOLD; ws.cell(2, 1).alignment = CENTER; ws.cell(2, 1).fill = LIGHT_GRAY

    for col_key, col_i in msb_col_map.items():
        c1 = ws.cell(1, col_i)
        c1.value = col_key[1]; c1.font = BOLD; c1.alignment = CENTER; c1.fill = LIGHT_BLUE
        c2 = ws.cell(2, col_i)
        c2.value = col_key[0]; c2.font = BOLD; c2.alignment = CENTER; c2.fill = LIGHT_GRAY

    for (tname, cnt), col_i in ob_col_map.items():
        ws.cell(1, col_i).value = tname;      ws.cell(1, col_i).font = BOLD; ws.cell(1, col_i).alignment = CENTER; ws.cell(1, col_i).fill = ORANGE_FILL
        ws.cell(2, col_i).value = _ob_package_label(cnt); ws.cell(2, col_i).font = BOLD; ws.cell(2, col_i).alignment = CENTER; ws.cell(2, col_i).fill = PEACH_FILL

    for (tname, cnt), col_i in docx_col_map.items():
        ws.cell(1, col_i).value = tname;      ws.cell(1, col_i).font = BOLD; ws.cell(1, col_i).alignment = CENTER; ws.cell(1, col_i).fill = GREEN_FILL
        ws.cell(2, col_i).value = _ob_package_label(cnt); ws.cell(2, col_i).font = BOLD; ws.cell(2, col_i).alignment = CENTER; ws.cell(2, col_i).fill = GREEN_DARK

    for tname, col_i in sb_col_map.items():
        ws.cell(1, col_i).value = "Одиночные бюджетники"; ws.cell(1, col_i).font = BOLD; ws.cell(1, col_i).alignment = CENTER; ws.cell(1, col_i).fill = PURPLE_LIGHT
        ws.cell(2, col_i).value = tname;                  ws.cell(2, col_i).font = BOLD; ws.cell(2, col_i).alignment = CENTER; ws.cell(2, col_i).fill = PURPLE_LIGHT

    for (group, tname), col_i in other_col_map.items():
        ws.cell(1, col_i).value = KONTUR_OTHER_GROUPS.get(group, group); ws.cell(1, col_i).font = BOLD; ws.cell(1, col_i).alignment = CENTER; ws.cell(1, col_i).fill = LIGHT_BLUE
        ws.cell(2, col_i).value = tname;                                 ws.cell(2, col_i).font = BOLD; ws.cell(2, col_i).alignment = CENTER; ws.cell(2, col_i).fill = LIGHT_GRAY

    for tname, items in _groupby(msb_col_order, key=lambda x: x[1]):
        items = list(items)
        fc = msb_col_map[items[0]]; lc = msb_col_map[items[-1]]
        if lc > fc:
            ws.merge_cells(start_row=1, start_column=fc, end_row=1, end_column=lc)

    for tname, items in _groupby(ob_col_order, key=lambda x: x[0]):
        items = list(items)
        fc = ob_col_map[items[0]]; lc = ob_col_map[items[-1]]
        if lc > fc:
            ws.merge_cells(start_row=1, start_column=fc, end_row=1, end_column=lc)

    for tname, items in _groupby(docx_col_order, key=lambda x: x[0]):
        items = list(items)
        fc = docx_col_map[items[0]]; lc = docx_col_map[items[-1]]
        if lc > fc:
            ws.merge_cells(start_row=1, start_column=fc, end_row=1, end_column=lc)

    if sb_col_order:
        fc = sb_col_map[sb_col_order[0]]; lc = sb_col_map[sb_col_order[-1]]
        if lc > fc:
            ws.merge_cells(start_row=1, start_column=fc, end_row=1, end_column=lc)

    for group, items in _groupby(other_col_order, key=lambda x: x[0]):
        items = list(items)
        fc = other_col_map[items[0]]; lc = other_col_map[items[-1]]
        if lc > fc:
            ws.merge_cells(start_row=1, start_column=fc, end_row=1, end_column=lc)

    for code, region_name, price_dict in rows:
        data_row = [""] * total_cols
        data_row[0] = region_name

        for col_key, col_i in msb_col_map.items():
            data_row[col_i - 1] = price_dict.get(col_key)

        for (tname, cnt), col_i in ob_col_map.items():
            data_row[col_i - 1] = price_dict.get(("ob", tname, cnt))

        for (tname, cnt), col_i in docx_col_map.items():
            data_row[col_i - 1] = price_dict.get(("docx_group", tname, cnt))

        for tname, col_i in sb_col_map.items():
            data_row[col_i - 1] = price_dict.get(("docx_sb", tname))

        for other_key, col_i in other_col_map.items():
            data_row[col_i - 1] = price_dict.get(("other", *other_key))

        ws.append(data_row)
        r = ws.max_row
        ws.cell(r, 1).alignment = LEFT

        for col_key, col_i in msb_col_map.items():
            price = price_dict.get(col_key)
            pc = ws.cell(r, col_i); pc.alignment = CENTER
            apply_diff_fill(pc, prev.get((code, f"DOCX | {col_key[0]} | {col_key[1]}")), price)

        for (tname, cnt), col_i in ob_col_map.items():
            price = price_dict.get(("ob", tname, cnt))
            pc = ws.cell(r, col_i); pc.alignment = CENTER
            apply_diff_fill(pc, prev.get((code, f"ОБ | {tname} | {_ob_package_label(cnt)}")), price)

        for (tname, cnt), col_i in docx_col_map.items():
            price = price_dict.get(("docx_group", tname, cnt))
            pc = ws.cell(r, col_i); pc.alignment = CENTER
            apply_diff_fill(pc, prev.get((code, f"Docx | {tname} | {_ob_package_label(cnt)}")), price)

        for tname, col_i in sb_col_map.items():
            price = price_dict.get(("docx_sb", tname))
            pc = ws.cell(r, col_i); pc.alignment = CENTER
            apply_diff_fill(pc, prev.get((code, f"Docx SB | {tname}")), price)

        for (group, tname), col_i in other_col_map.items():
            price = price_dict.get(("other", group, tname))
            pc = ws.cell(r, col_i); pc.alignment = CENTER
            apply_diff_fill(pc, prev.get((code, f"{group} | {tname}")), price)

    ws.freeze_panes = "B3"
    ws.column_dimensions["A"].width = 26
    for col_i in msb_col_map.values():
        ws.column_dimensions[get_column_letter(col_i)].width = 13
    for col_i in ob_col_map.values():
        ws.column_dimensions[get_column_letter(col_i)].width = 12
    for col_i in docx_col_map.values():
        ws.column_dimensions[get_column_letter(col_i)].width = 12
    for col_i in sb_col_map.values():
        ws.column_dimensions[get_column_letter(col_i)].width = 16
    for col_i in other_col_map.values():
        ws.column_dimensions[get_column_letter(col_i)].width = 16

    _outline_tariff_groups(ws)
    wb.save(filepath)

# ─── Формирование сообщения об изменениях ────────────────────────────────────

def compare_prices(all_rows, prev: dict, key_fn, price_fn,
                   confirmed: set[tuple[str, str]] | frozenset[tuple[str, str]] = frozenset()) -> dict | None:
    """Сравнение с прошлой выгрузкой; None — сравнивать не с чем.

    changed — сколько цен изменилось, missing — сколько прошлых позиций
    нет в текущей выгрузке без подтверждения снятия, withdrawn — подтверждённые снятия,
    new_tariffs — тарифы (вторая часть ключа), которых не было ни в одном
    регионе прошлой выгрузки: новые тарифы и формы бизнеса.
    """
    if not prev:
        return None
    previous = {key: value for key, value in prev.items() if value is not None}
    current = {}
    for row in all_rows:
        new = price_fn(row)
        if new is not None:
            current[key_fn(row)] = new
    changed = sum(previous[key] != current[key] for key in previous.keys() & current.keys())
    missing_keys = previous.keys() - current.keys()
    withdrawn = missing_keys & confirmed
    prev_tariffs = {key[1] for key in previous}
    new_tariffs = sorted({key[1] for key in current} - prev_tariffs)
    result = {"changed": changed, "missing": len(missing_keys - withdrawn), "new_tariffs": new_tariffs}
    if withdrawn:
        result["withdrawn"] = sorted({key[1] for key in withdrawn})
    return result


def build_diff_message(source: str, comparison: dict | None) -> str:
    if comparison is None:
        return f"📊 {source}: первая выгрузка, история сравнения отсутствует."
    changed, missing = comparison["changed"], comparison["missing"]
    lines = [f"⚠️ {source}: изменилось {changed} цен." if changed
             else f"✅ {source}: изменений цен не обнаружено." if not missing
             else f"⚠️ {source}: часть цен не получена."]
    if missing:
        lines.append(f"⚠️ {source}: нет данных по {missing} позициям из прошлой выгрузки.")
        lines.append("Подтвердить снятие по официальному прайсу можно в панели → Цены 1СО.")
    withdrawn = comparison.get("withdrawn", [])
    if withdrawn:
        lines.append(f"➖ {source}: подтверждено снятие тарифов ({len(withdrawn)}):")
        lines.extend(f"  • {name}" for name in withdrawn[:20])
        if len(withdrawn) > 20:
            lines.append(f"  … и ещё {len(withdrawn) - 20}")
    new_tariffs = comparison.get("new_tariffs") or []
    if new_tariffs:
        lines.append(f"🆕 {source}: новые тарифы/формы бизнеса ({len(new_tariffs)}):")
        lines.extend(f"  • {name}" for name in new_tariffs[:20])
        if len(new_tariffs) > 20:
            lines.append(f"  … и ещё {len(new_tariffs) - 20}")
    return "\n".join(lines)


def has_news(result: dict) -> bool:
    """Стоит ли сообщать о результате источника при режиме «только изменения»."""
    comparison = result.get("comparison")
    return not result["ok"] or comparison is None or any(comparison.values())


# ─── Запуск парсинга источников ───────────────────────────────────────────────

async def run_sbis(delay_sec: float = 0.3, sources_dir: Path | None = None,
                   run_kind: str | None = None) -> dict:
    """sources_dir — куда сложить сырые ответы API для архива (папка sbis/).

    run_kind — RUN_KIND_AUTO/RUN_KIND_MANUAL: с какой прошлой выгрузкой сравнивать.
    """
    now_str  = datetime.datetime.now().isoformat(timespec="microseconds")
    date_str = datetime.datetime.now().strftime("%d.%m.%y")
    conn     = db_connect()

    _done = [0]
    total = len(REGIONS_SBIS)

    async def progress_hook(region_code: str, region_name: str):
        _done[0] += 1
        print(progress_text("Парсинг СБИС…", _done[0], total, region_name), flush=True)

    try:
        rows_s, db_s = await parse_sbis(
            REGIONS_SBIS, progress_cb=progress_hook, delay_sec=delay_sec,
            raw_dir=sources_dir / "sbis" if sources_dir else None,
        )
        if not any(row["price"] is not None for row in db_s):
            raise RuntimeError("в каталоге не найдено ни одной цены отчётности")
        prev_s = db_get_previous(conn, "sbis", now_str, run_kind)
        expected = {str(code).zfill(2) for code, _ in REGIONS_SBIS}
        priced_rows = [r for r in db_s if r["price"] is not None]
        received = {r["region_code"] for r in priced_rows}
        active = {(r["region_code"], r["tariff_key"]) for r in priced_rows}
        missing = ({(region, key) for region in expected for key in SBIS_NOMENCLATURES}
                   | {key for key, value in prev_s.items() if value is not None}) - active
        confirmed = admin_store.reconcile_price_gaps(config.ADMIN_DB_PATH, "sbis", missing, active, expected <= received)
        comparison = compare_prices(
            db_s, prev_s,
            key_fn   = lambda r: (r["region_code"].zfill(2), r["tariff_key"]),
            price_fn = lambda r: r["price"],
            confirmed=confirmed,
        )
        full = expected <= received and missing <= confirmed
        suffix = "" if full else "_partial"
        output = config.CONFIG_DIR / f"sbis_{date_str}{suffix}.xlsx"
        with staged_report(output) as staged:
            save_sbis_excel(rows_s, prev_s, staged)
        summary = build_diff_message("СБИС", comparison)
        if full:
            db_save_prices(conn, "sbis", priced_rows, now_str, run_kind)
        else:
            summary += (
                f"\n⚠️ СБИС: цены получены по {len(expected & received)} из {len(expected)} регионов, "
                f"неподтверждённых пропусков: {len(missing - confirmed)}. Снимок неполный."
            )
        return {"ok": full, "summary": summary, "file": str(output), "comparison": comparison}
    except Exception as e:
        logging.error(f"Ошибка парсинга СБИС: {e}", exc_info=True)
        return {"ok": False, "summary": f"❌ СБИС: {short_error(e)}", "file": None}
    finally:
        conn.close()


async def run_kontur(
    regions: list, test: bool = False, region_delay_sec: float = 1.0,
    region_delay_max_sec: float | None = None,
    main_files_only: bool = False, sources_dir: Path | None = None,
    run_kind: str | None = None,
) -> dict:
    """sources_dir — куда скопировать скачанные прайсы для архива (папка kontur/).

    run_kind — RUN_KIND_AUTO/RUN_KIND_MANUAL: с какой прошлой выгрузкой сравнивать.
    """
    label = "🧪 Контур ТЕСТ" if test else "Контур.Экстерн"

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
    except Exception as e:
        logging.error(f"Playwright/Chromium не готов: {e}", exc_info=True)
        return {
            "ok": False,
            "summary": f"❌ {label}: браузер Playwright не установлен (playwright install chromium)",
            "file": None,
        }

    now_str  = datetime.datetime.now().isoformat(timespec="microseconds")
    date_str = datetime.datetime.now().strftime("%d.%m.%y")
    conn     = db_connect()

    total = len(regions)

    async def progress_hook(done: int, tot: int, region_name: str):
        print(progress_text(f"Парсинг {label}…", done, tot, region_name), flush=True)

    source_dir: Path | None = None
    try:
        source_dir = Path(tempfile.mkdtemp(prefix="kontur_sources_"))
        rows_k, db_k, msb_o, ob_o, docx_o, sb_o, other_o, incomplete_regions = await parse_kontur(
            regions, source_dir, progress_hook, region_delay_sec=region_delay_sec,
            region_delay_max_sec=region_delay_max_sec,
            main_files_only=main_files_only,
        )
        if not db_k:
            raise RuntimeError("в региональных прайсах не найдено ни одной цены отчётности")

        prev_k = {} if test else db_get_previous(conn, "kontur", now_str, run_kind)
        comparison = None
        expected = {str(code) for code, _ in regions}
        received = {r["region_code"] for r in db_k} - incomplete_regions
        full = expected <= received
        if test:
            summary = f"🧪 {label}: {total} регионов обработано (тест, без записи в БД)."
        else:
            active = {(r["region_code"], r["tariff_key"]) for r in db_k}
            missing = {key for key, value in prev_k.items() if value is not None} - active
            confirmed = admin_store.reconcile_price_gaps(config.ADMIN_DB_PATH, "kontur", missing, active, full)
            comparison = compare_prices(
                db_k, prev_k,
                key_fn   = lambda r: (r["region_code"], r["tariff_key"]),
                price_fn = lambda r: r["price"],
                confirmed=confirmed,
            )
            summary = build_diff_message(label, comparison)
            if comparison and comparison["missing"]:
                full = False
        prefix = "kontur_test" if test else "kontur"
        suffix = "" if full else "_partial"
        output = config.CONFIG_DIR / f"{prefix}_{date_str}{suffix}.xlsx"
        with staged_report(output) as staged:
            save_kontur_excel(rows_k, msb_o, ob_o, docx_o, sb_o, other_o, prev_k, staged)
        if not test and full:
            db_save_prices(conn, "kontur", db_k, now_str, run_kind)
        if sources_dir is not None:
            shutil.copytree(source_dir, sources_dir / "kontur", dirs_exist_ok=True)
        if not full:
            summary += f"\n⚠️ Контур: цены получены по {len(expected & received)} из {len(expected)} регионов. Снимок неполный."
        return {"ok": full, "summary": summary, "file": str(output), "comparison": comparison}
    except Exception as e:
        logging.error(f"Ошибка парсинга Контур: {e}", exc_info=True)
        return {"ok": False, "summary": f"❌ {label}: {short_error(e)}", "file": None}
    finally:
        if source_dir is not None:
            shutil.rmtree(source_dir, ignore_errors=True)
        conn.close()




def _load_last_auto_run() -> datetime.datetime | None:
    return schedule.load_last_run(config.STATE_FILE)


def _save_last_auto_run(ts: datetime.datetime):
    schedule.save_last_run(config.STATE_FILE, ts)


TEST_REGIONS = [
    (77, "Москва"),
    (78, "Санкт-Петербург"),
    (66, "Свердловская область"),
    (23, "Краснодарский край"),
    (54, "Новосибирская область"),
]

_SOURCE_LABELS = {
    "sbis":        "СБИС",
    "kontur":      "Контур",
    "both":        "СБИС + Контур",
    "kontur-test": "Контур (тест, 5 регионов)",
}


async def run(source: str, announce: bool = False, announce_text: str | None = None,
              force: bool = False, target: dict | list[dict] | None = None,
              profile: str | None = None):
    """Запускает выгрузку; target задан только для вызова командой из конкретного чата.

    target=None — рассылка в чаты модуля «Цены 1СО», выбранные в панели.
    Получателей определяем в момент каждой отправки: выгрузка идёт до часа,
    и правка списка чатов в панели за это время должна учитываться.
    """
    def recipients() -> dict | list[dict]:
        chosen = target if target is not None else access.module_targets("prices")
        logging.info("[1СО] отправка итога: %s", chosen)
        return chosen

    manual = announce or announce_text is not None
    profile = profile or ("manual" if manual else "auto")
    run_settings = config.prices_run_settings(profile)
    started = datetime.datetime.now()

    if not manual and not force:
        auto_schedule = config.prices_auto_schedule()
        if not auto_schedule["module_enabled"] or not auto_schedule["enabled"]:
            logging.info("Плановый запуск цен отключён в настройках модуля")
            return
        if not schedule.should_run(config.STATE_FILE, auto_schedule["interval_days"], auto_schedule["at"], started):
            return

    if not manual:
        schedule.save_attempt(config.STATE_FILE, started)
    date_str = started.strftime("%d.%m.%y")

    if manual:
        await ym_send_text(
            announce_text or f"🔷 Запускаю выгрузку {_SOURCE_LABELS[source]}…",
            target=recipients(),
        )

    results: list[dict] = []
    run_kind = RUN_KIND_MANUAL if manual else RUN_KIND_AUTO
    manual_files_only = manual and config.prices_module_settings()["manual"]["main_files_only"]
    sources_dir = None
    if source == "both" and archives.needs_archive("prices", started):
        sources_dir = Path(tempfile.mkdtemp(prefix="prices_sources_"))
    archive = None
    try:
        if source in ("sbis", "both"):
            results.append(await run_sbis(
                run_settings["sbis_delay_sec"], sources_dir=sources_dir, run_kind=run_kind,
            ))
        if source in ("kontur", "both"):
            results.append(await run_kontur(
                REGIONS_KONTUR,
                region_delay_sec=run_settings["kontur_region_delay_sec"],
                region_delay_max_sec=run_settings.get("kontur_region_delay_max_sec"),
                main_files_only=manual_files_only,
                sources_dir=sources_dir,
                run_kind=run_kind,
            ))
        if source == "kontur-test":
            results.append(await run_kontur(
                TEST_REGIONS, test=True, main_files_only=manual_files_only,
                region_delay_sec=run_settings["kontur_region_delay_sec"],
                region_delay_max_sec=run_settings.get("kontur_region_delay_max_sec"),
            ))
        if sources_dir is not None and all(r["ok"] and r.get("file") for r in results):
            try:
                # В корень архива — Excel с ценами СБИС и Контура, рядом исходники.
                archive = archives.make_archive(
                    "prices", sources_dir, extra_files=[r["file"] for r in results], run_date=started,
                )
            except OSError as error:
                logging.error("Не удалось собрать архив исходников 1СО: %s", error, exc_info=True)
    finally:
        if sources_dir is not None:
            shutil.rmtree(sources_dir, ignore_errors=True)

    auto_schedule = config.prices_auto_schedule()
    ok = bool(results) and all(result["ok"] for result in results)
    files = [result["file"] for result in results if result.get("file")]
    if not manual and ok:
        _save_last_auto_run(datetime.datetime.now())
    if not manual and auto_schedule["only_changes"] and results and not any(map(has_news, results)):
        # Плановая выгрузка без изменений: Excel и история уже сохранены,
        # в чаты не пишем, чтобы не повторять одни и те же цены.
        logging.info("[1СО] плановая выгрузка: цены без изменений — итог в чаты не отправляется")
        return {"ok": True, "summary": "\n".join(r["summary"] for r in results),
                "files": files, "sent": False, "delivery_errors": []}

    summary_lines = [f"📦 Выгрузка — {date_str}", ""]
    summary_lines += [r["summary"] for r in results]
    if archive is not None:
        summary_lines += ["", f"🗄 Архив исходников: {archive.name}"]
    if not manual:
        if ok:
            next_run = schedule.next_run(auto_schedule["interval_days"], auto_schedule["at"], datetime.datetime.now())
            summary_lines += ["", f"🗓 Следующая автоматическая выгрузка: {next_run:%d.%m.%y} в {auto_schedule['at']}"]
        else:
            summary_lines += ["", "🗓 При неполной выгрузке повтор — не раньше чем через час."]
    targets = recipients()
    delivery_errors = []
    for file in files:
        delivery_errors.extend(await ym_send_file(file, target=targets, retry_delays=PRICE_DELIVERY_RETRY_DELAYS) or [])
    if delivery_errors:
        summary_lines += ["", "⚠ Вложения доставлены не всем получателям. Файлы доступны в панели → Отчёты."]
    summary = "\n".join(summary_lines)
    delivery_errors.extend(await ym_send_text(summary, target=targets, retry_delays=PRICE_DELIVERY_RETRY_DELAYS) or [])
    return {"ok": ok, "summary": summary, "files": files, "sent": not delivery_errors,
            "delivery_errors": delivery_errors}


# ─── Плановая выгрузка раз в 7 дней (для --daemon) ────────────────────────────

# config.SCHEDULER_CHECK_INTERVAL_SEC определён в config.py


async def _scheduler_tick():
    """Одна проверка: не пора ли делать плановую (раз в 7 дней) выгрузку."""
    auto_schedule = config.prices_auto_schedule()
    if not auto_schedule["module_enabled"] or not auto_schedule["enabled"]:
        return
    if prices_lock.locked():
        # что-то уже выполняется (например, команда из чата) — попробуем на следующем тике
        return
    async with prices_lock:
        # run() сам решит, пора ли (проверка config.AUTO_RUN_INTERVAL_DAYS внутри),
        # здесь просто гарантируем, что не пересечёмся с ручным запуском
        await run("both", force=False)


async def scheduler_loop():
    """
    Фоновая задача для --daemon: раз в config.SCHEDULER_CHECK_INTERVAL_SEC проверяет,
    не прошло ли config.AUTO_RUN_INTERVAL_DAYS дней с последней плановой выгрузки,
    и если да — запускает её. Сама выгрузка (внутри run()) выполнится не
    чаще раза в 7 дней независимо от того, как часто вызывается этот тик.
    """
    auto_schedule = config.prices_auto_schedule()
    logging.info(f"Планировщик запущен: проверка каждые {config.SCHEDULER_CHECK_INTERVAL_SEC} с, "
                 f"интервал автовыгрузки {auto_schedule['interval_days']} дн. "
                 f"в {auto_schedule['at']}")
    while True:
        state.heartbeat["scheduler"] = time.time()
        try:
            await _scheduler_tick()
        except Exception as e:
            logging.error(f"scheduler_loop: ошибка тика: {e}", exc_info=True)
        await asyncio.sleep(config.SCHEDULER_CHECK_INTERVAL_SEC)
