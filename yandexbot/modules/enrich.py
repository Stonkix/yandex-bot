"""
Обогащение списка ИНН через DaData (команда /обогатить):
разбор входного .txt/.xlsx, запрос к DaData, сборка Excel-отчёта,
учёт дневного лимита запросов.
"""

import asyncio
import datetime
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path

import aiohttp
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from yandexbot.core import config
from yandexbot.core.utils import BOLD, CENTER, RED
from yandexbot.modules import dadata_store

DADATA_URL       = "https://suggestions.dadata.ru/suggestions/api/4_1/rs/findById/party"
# Ограничение DaData — до 30 запросов в секунду. Оставляем запас и равномерно
# запускаем до 25 запросов в секунду, не создавая кратковременных всплесков.
DADATA_CONCURRENCY = 5
DADATA_REQUESTS_PER_SECOND = 25
DADATA_REQUEST_INTERVAL = 1 / DADATA_REQUESTS_PER_SECOND
# В Excel ведущий ноль часто теряется: тогда ИНН содержит 9 или 11 цифр.
# Границы не дают извлекать фрагменты более длинных чисел из текста.
INN_RE = re.compile(r"(?<!\d)(?:\d{9}|\d{10}|\d{11}|\d{12})(?!\d)")
INN_COLUMN_NAMES = {"ИНН", "ИНН КЛИЕНТА", "INN"}

DADATA_STATUS_RU = {
    "ACTIVE":       "Действующая",
    "LIQUIDATING":  "Ликвидируется",
    "LIQUIDATED":   "Ликвидирована",
    "BANKRUPT":     "Банкротство",
    "REORGANIZING": "Реорганизация",
}
DADATA_TYPE_RU = {"LEGAL": "Юридическое лицо", "INDIVIDUAL": "ИП"}
DADATA_BRANCH_TYPE_RU = {"MAIN": "Головная организация", "BRANCH": "Филиал"}
DADATA_TAX_SYSTEM_RU = {"AUSN": "АУСН", "ESHN": "ЕСХН", "SRP": "СРП", "USN": "УСН"}
# Категории реестра МСП (documents.smb.category, тариф «Максимальный»).
DADATA_SMB_CATEGORY_RU = {
    "MICRO":  "Микропредприятие",
    "SMALL":  "Малое предприятие",
    "MEDIUM": "Среднее предприятие",
}
SMB_NOT_IN_REGISTRY = "Не в реестре"

BASE_ENRICH_HEADERS = [
    "Наименование", "ИНН", "КПП", "КПП крупн.", "ОГРН", "Дата ОГРН",
    "Тип", "ОКВЭД", "Вид деятельности", "Доп. ОКВЭДы", "ОПФ код", "ОПФ полное", "ОПФ кратко",
    "Руководитель", "Должность рук.", "Дата назначения", "Филиалов", "Тип подр.",
    "Адрес", "Регион", "Город", "Статус", "Дата регистрации", "Дата ликвидации", "Числ. сотрудников",
    "МСП категория", "МСП дата включения",
    "Налог. система", "Учредители",
]

# Коды строк бухгалтерской отчётности. 2400 — чистая прибыль/убыток,
# 2110 — выручка, 2120 — себестоимость/расходы.
FINANCE_METRICS = {"profit": "2400", "revenue": "2110", "expense": "2120"}
FINANCE_FIELDS = ("profit", "revenue", "income", "expense")


def _finance_years(results: list[dict]) -> list[int]:
    """Два наиболее свежих года, представленных в финансовой истории ответов."""
    years = {
        year
        for result in results
        for year in (result.get("finance_by_year") or {})
        if isinstance(year, int)
    }
    return sorted(years, reverse=True)[:2]


def _phone_count(results: list[dict]) -> int:
    """Наибольшее число телефонов у одной найденной организации в выгрузке."""
    return max((len(result.get("phones") or []) for result in results), default=0)


def _email_count(results: list[dict]) -> int:
    """Наибольшее число email-адресов у одной найденной организации в выгрузке."""
    return max((len(result.get("emails") or []) for result in results), default=0)


def _enrich_headers(finance_years: list[int], phone_count: int, email_count: int) -> list[str]:
    """Заголовки отчёта, в том числе единые для файла два года финансов."""
    headers = BASE_ENRICH_HEADERS.copy()
    phone_headers = [f"Номер {index}" for index in range(1, phone_count + 1)]
    email_headers = [f"Почта {index}" for index in range(1, email_count + 1)]
    finance_headers = (
        [f"Прибыль {year}" for year in finance_years]
        + [f"Выручка {year}" for year in finance_years]
        + [f"Доходы {year}" for year in finance_years]
        + [f"Расходы {year}" for year in finance_years]
    )
    return (
        headers
        + phone_headers
        + email_headers
        + finance_headers
        + ["Недоимка", "Штрафы"]
    )


class DaDataRateLimiter:
    """Равномерно распределяет старты запросов в пределах заданного RPS."""

    def __init__(self, requests_per_second: int):
        self._interval = 1 / requests_per_second
        self._next_request_at = 0.0
        self._lock = asyncio.Lock()

    async def wait_turn(self):
        async with self._lock:
            now = asyncio.get_running_loop().time()
            wait_seconds = max(0.0, self._next_request_at - now)
            self._next_request_at = max(self._next_request_at, now) + self._interval
            if wait_seconds:
                await asyncio.sleep(wait_seconds)


class DaDataBudget:
    def __init__(self, limit: int, reserve: Callable[[], bool] | None = None):
        self.remaining = limit
        self.used = 0
        self._reserve = reserve

    def take(self) -> bool:
        if not self.remaining or (self._reserve is not None and not self._reserve()):
            self.remaining = 0
            return False
        self.remaining -= 1
        self.used += 1
        return True


def _legacy_usage_count(today: str) -> int:
    if config.DADATA_USAGE_FILE.is_file():
        try:
            data = json.loads(config.DADATA_USAGE_FILE.read_text(encoding="utf-8"))
            if data.get("date") == today:
                return max(0, int(data.get("count", 0)))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    return 0


def _load_dadata_usage() -> dict:
    today = datetime.date.today().isoformat()
    return {"date": today, "count": dadata_store.request_count(today, _legacy_usage_count(today))}


def reserve_dadata_request(limit: int) -> bool:
    today = datetime.date.today().isoformat()
    return dadata_store.reserve_request(today, limit, _legacy_usage_count(today))


def _load_dadata_stats() -> list[dict]:
    """История обработанных ИНН для отчёта администратора."""
    if not config.DADATA_STATS_FILE.is_file():
        return []
    try:
        data = json.loads(config.DADATA_STATS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        logging.error(f"Не удалось прочитать {config.DADATA_STATS_FILE}: {e}")
        return []


def record_dadata_usage(sender: dict, count: int):
    """Добавляет в историю один завершённый запуск обогащения."""
    if count <= 0:
        return
    login = (sender.get("login") or str(sender.get("id") or "неизвестно")).strip()
    entry = {
        "date": datetime.date.today().isoformat(),
        "login": login,
        "display_name": sender.get("display_name") or login,
        "count": count,
    }
    stats = _load_dadata_stats()
    stats.append(entry)
    try:
        config.DADATA_STATS_FILE.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as e:
        logging.error(f"Не удалось сохранить {config.DADATA_STATS_FILE}: {e}")


def dadata_usage_report() -> list[dict]:
    """Возвращает расход DaData по пользователям: день, неделя, месяц и всего."""
    today = datetime.date.today()
    week_start = today - datetime.timedelta(days=today.weekday())
    month_start = today.replace(day=1)
    totals: dict[str, dict] = {}
    for entry in _load_dadata_stats():
        try:
            day = datetime.date.fromisoformat(entry["date"])
            count = int(entry["count"])
        except (KeyError, TypeError, ValueError):
            continue
        login = str(entry.get("login") or "неизвестно")
        row = totals.setdefault(login, {"login": login, "today": 0, "week": 0, "month": 0, "total": 0})
        row["total"] += count
        if day == today:
            row["today"] += count
        if day >= week_start:
            row["week"] += count
        if day >= month_start:
            row["month"] += count
    return sorted(totals.values(), key=lambda row: (-row["total"], row["login"].lower()))


def _normalize_inn(inn: str) -> str:
    """Восстанавливает ведущий ноль у ИНН, сокращённого Excel до 9/11 цифр."""
    return f"0{inn}" if len(inn) in (9, 11) else inn


def _unique(inns: list[str]) -> list[str]:
    """Убирает повторы, сохраняя порядок первого появления ИНН."""
    return list(dict.fromkeys(inns))


def extract_inns_from_txt_with_stats(raw: bytes) -> tuple[list[str], int, int]:
    """Возвращает уникальные ИНН, всего найденных значений и строк без ИНН."""
    text = raw.decode("utf-8-sig", errors="replace")
    inns = []
    invalid_lines = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if INN_RE.fullmatch(line):
            inns.append(_normalize_inn(line))
            continue
        found = [_normalize_inn(m.group()) for m in INN_RE.finditer(line)]
        if found:
            inns.extend(found)
        elif line.upper() not in INN_COLUMN_NAMES:
            invalid_lines += 1
    return _unique(inns), len(inns) + invalid_lines, invalid_lines


def extract_inns_from_txt(raw: bytes) -> list[str]:
    inns, _, _ = extract_inns_from_txt_with_stats(raw)
    return inns


def estimate_seconds(count: int, requests_per_second: int) -> float:
    """Оценка длительности: запросы стартуют равномерно с заданным RPS."""
    return count / max(1, requests_per_second)


def format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    if seconds < 60:
        return "меньше минуты"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"


def extract_inns_from_xlsx_with_stats(path: str) -> tuple[list[str], int, int]:
    """Возвращает уникальные корректные ИНН, всего непустых значений и невалидных."""
    from openpyxl import load_workbook
    wb = load_workbook(path, read_only=True, data_only=True)
    # read_only держит файл открытым до close(): иначе на Windows его нельзя удалить.
    try:
        ws = wb.active

        header_row = next(ws.iter_rows(min_row=1, max_row=1, values_only=True), None)
        col_idx = _find_inn_column(header_row)
        if col_idx is None:
            return [], 0, 0
        start_row = 2

        inns = []
        total_values = 0
        invalid_values = 0
        for row in ws.iter_rows(min_row=start_row, values_only=True):
            if col_idx >= len(row):
                continue
            val = row[col_idx]
            if val is None:
                continue
            s = str(val).strip()
            if not s:
                continue
            total_values += 1
            if s.endswith(".0"):
                s = s[:-2]
            if INN_RE.fullmatch(s):
                inns.append(_normalize_inn(s))
            else:
                invalid_values += 1
    finally:
        wb.close()

    return _unique(inns), total_values, invalid_values


def extract_inns_from_xlsx(path: str) -> list[str]:
    """Возвращает уникальные корректные ИНН из колонки с поддерживаемым заголовком."""
    inns, _, _ = extract_inns_from_xlsx_with_stats(path)
    return inns


def _find_inn_column(header_row) -> int | None:
    """Возвращает индекс колонки ИНН в заголовке первой строки."""
    if not header_row:
        return None
    for idx, val in enumerate(header_row):
        if val is not None and str(val).strip().upper() in INN_COLUMN_NAMES:
            return idx
    return None


def _dd_date(ms) -> str | None:
    if ms is None:
        return None
    try:
        return datetime.datetime.utcfromtimestamp(ms / 1000).strftime("%d.%m.%Y")
    except Exception:
        return None


def _format_phone(value: object) -> str:
    """Убирает плюс и форматирует номер РФ с кодом города из 3, 4 или 5 цифр."""
    raw = str(value or "")
    without_plus = raw.replace("+", "")
    match = re.fullmatch(
        r"7[\s.-]*\(?(\d{3,5})\)?[\s.-]+(\d{5,7})", without_plus
    )
    if match and len(match.group(1)) + len(match.group(2)) == 10:
        city_code, subscriber = match.groups()
        if len(subscriber) == 10 - len(city_code):
            return (
                f"7 ({city_code}) {subscriber[:-4]}-"
                f"{subscriber[-4:-2]}-{subscriber[-2:]}"
            )
    return without_plus


def _smb_fields(data: dict) -> dict:
    """Категория и дата включения в реестр МСП из documents.smb ответа DaData."""
    smb = (data.get("documents") or {}).get("smb") or {}
    if not smb:
        return {"smb_category": SMB_NOT_IN_REGISTRY, "smb_date": None}
    category = smb.get("category")
    return {
        "smb_category": DADATA_SMB_CATEGORY_RU.get(category, category),
        "smb_date": _dd_date(smb.get("issue_date")),
    }


def _enrich_values(result: dict, finance_years: list[int], phone_count: int, email_count: int) -> list:
    if not result.get("found"):
        error = result.get("error")
        status = f"ОШИБКА ЗАПРОСА: {error}" if error else "НЕ НАЙДЕНО"
        return [status] + [""] * (len(_enrich_headers(finance_years, phone_count, email_count)) - 1)
    finance_by_year = result.get("finance_by_year") or {}
    finance_values = [
        (finance_by_year.get(year) or {}).get(metric, "")
        for metric in FINANCE_FIELDS
        for year in finance_years
    ]
    phones = (result.get("phones") or [])[:phone_count]
    phones += [""] * (phone_count - len(phones))
    emails = (result.get("emails") or [])[:email_count]
    emails += [""] * (email_count - len(emails))
    return [
        result.get("name"), result.get("inn"), result.get("kpp"), result.get("kpp_largest"),
        result.get("ogrn"), result.get("ogrn_date"),
        DADATA_TYPE_RU.get(result.get("type"), result.get("type")), result.get("okved"), result.get("okved_name"),
        result.get("additional_okveds"),
        result.get("opf_code"), result.get("opf_full"), result.get("opf_short"),
        result.get("management_name"), result.get("management_post"), result.get("management_start"),
        result.get("branch_count"), DADATA_BRANCH_TYPE_RU.get(result.get("branch_type"), result.get("branch_type")), result.get("address"),
        result.get("region"), result.get("city"),
        DADATA_STATUS_RU.get(result.get("status"), result.get("status")), result.get("reg_date"), result.get("liq_date"),
        result.get("employee_count"), result.get("smb_category"), result.get("smb_date"),
        DADATA_TAX_SYSTEM_RU.get(result.get("tax_system"), result.get("tax_system")),
        result.get("founders"), *phones, *emails,
        *finance_values, result.get("debt"), result.get("penalty"),
    ]


def save_enriched_source_excel(source_path: str, filepath: str, results: list[dict]):
    """Добавляет данные DaData справа от всех заполненных столбцов исходного файла."""
    from openpyxl import load_workbook

    wb = load_workbook(source_path, keep_vba=source_path.lower().endswith(".xlsm"))
    ws = wb.active
    inn_col = _find_inn_column([cell.value for cell in ws[1]])
    if inn_col is None:
        raise ValueError("Не найдена колонка ИНН / ИНН клиента / INN")
    inn_col += 1
    last_data_col = max(
        (cell.column for row in ws.iter_rows() for cell in row if cell.value is not None),
        default=inn_col,
    )
    finance_years = _finance_years(results)
    phone_count = _phone_count(results)
    email_count = _email_count(results)
    headers = _enrich_headers(finance_years, phone_count, email_count)
    for offset, header in enumerate(headers, start=1):
        cell = ws.cell(1, last_data_col + offset, header)
        cell.font = BOLD
        cell.alignment = CENTER

    by_inn = {result["inn"]: result for result in results}
    for row_idx in range(2, ws.max_row + 1):
        value = ws.cell(row_idx, inn_col).value
        if value is None:
            continue
        inn = str(value).strip()
        if inn.endswith(".0"):
            inn = inn[:-2]
        if not INN_RE.fullmatch(inn):
            continue
        result = by_inn.get(_normalize_inn(inn))
        if not result:
            continue
        for offset, value in enumerate(_enrich_values(result, finance_years, phone_count, email_count), start=1):
            ws.cell(row_idx, last_data_col + offset, value)
        if not result.get("found"):
            for offset in range(1, len(headers) + 1):
                ws.cell(row_idx, last_data_col + offset).fill = RED
    ws.freeze_panes = None
    wb.save(filepath)


async def dadata_lookup(session: aiohttp.ClientSession, inn: str,
                        sem: asyncio.Semaphore, rate_limiter: DaDataRateLimiter,
                        budget: DaDataBudget | None = None) -> dict:
    headers = {
        "Content-Type":  "application/json",
        "Accept":        "application/json",
        "Authorization": f"Token {config.DADATA_TOKEN}",
    }
    error = None
    for attempt in range(3):
        pause = 2 ** attempt
        async with sem:
            await rate_limiter.wait_turn()
            if budget is not None and not budget.take():
                return {"inn": inn, "found": False, "error": error, "deferred": error is None}
            try:
                async with session.post(
                    DADATA_URL, json={"query": inn}, headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json(content_type=None)
                        suggestions = data.get("suggestions") if isinstance(data, dict) else None
                        if not isinstance(suggestions, list) or (suggestions and not isinstance(suggestions[0], dict)):
                            return {"inn": inn, "found": False, "error": "некорректный ответ DaData"}
                        received_at = datetime.datetime.now(datetime.UTC).isoformat(timespec="milliseconds")
                        break
                    error = f"HTTP {resp.status}"
                    if resp.status != 429 and not 500 <= resp.status < 600:
                        return {"inn": inn, "found": False, "error": error}
                    retry_after = getattr(resp, "headers", {}).get("Retry-After", "")
                    if retry_after.isdigit():
                        pause = max(pause, min(int(retry_after), 30))
            except (aiohttp.ClientError, TimeoutError) as exc:
                error = type(exc).__name__
            except ValueError:
                return {"inn": inn, "found": False, "error": "некорректный ответ DaData"}
        if attempt < 2 and (budget is None or budget.remaining):
            await asyncio.sleep(pause)
    else:
        logging.warning("[DaData] запрос не выполнен: %s", error)
        return {"inn": inn, "found": False, "error": error}

    result = dadata_result(inn, data)
    if not result.get("error"):
        result.update(response=data, received_at=received_at)
    return result


def dadata_result(inn: str, response: dict) -> dict:
    """Проекция первого совпадения для Excel. История сохраняет полный ответ."""
    suggestions = response["suggestions"]
    if not suggestions:
        return {"inn": inn, "found": False}

    s = suggestions[0]
    d = s.get("data")
    if not isinstance(d, dict):
        return {"inn": inn, "found": False, "error": "некорректный ответ DaData"}
    mgmt   = d.get("management") or {}
    opf    = d.get("opf") or {}
    addr   = d.get("address") or {}
    state  = d.get("state") or {}
    fin    = d.get("finance") or {}
    okveds = d.get("okveds") or []
    okved_main = d.get("okved", "")
    okved_name = ""
    for o in okveds:
        if o.get("main") and o.get("code") == okved_main:
            okved_name = o.get("name", "")
            break
    additional_okveds = "; ".join(
        f"{o.get('code', '')} — {o.get('name', '')}".strip(" —")
        for o in okveds
        if not o.get("main")
    )

    finance_by_year: dict[int, dict[str, object]] = {}
    for report in d.get("finance_history") or []:
        try:
            year = int(report.get("year"))
        except (TypeError, ValueError):
            continue
        values = {metric: "" for metric in FINANCE_FIELDS}
        for item in report.get("metrics") or []:
            code = str(item.get("code", ""))
            for metric, metric_code in FINANCE_METRICS.items():
                if code == metric_code:
                    values[metric] = item.get("value", "")
        finance_by_year[year] = values
    try:
        finance_year = int(fin.get("year"))
    except (TypeError, ValueError):
        finance_year = None
    if finance_year is not None:
        # В кратком объекте finance чистая прибыль отсутствует, но есть выручка,
        # доходы и расходы. Дополняем ими историю, если DaData не вернула её за этот год.
        values = finance_by_year.setdefault(
            finance_year, {metric: "" for metric in FINANCE_FIELDS}
        )
        if values.get("revenue") in ("", None) and fin.get("revenue") is not None:
            values["revenue"] = fin.get("revenue")
        if values.get("income") in ("", None) and fin.get("income") is not None:
            values["income"] = fin.get("income")
        if values.get("expense") in ("", None) and fin.get("expense") is not None:
            values["expense"] = fin.get("expense")

    founders = d.get("founders") or []
    founders_str = "; ".join(
        f"{f.get('name') or ' '.join(filter(None, [
            (f.get('fio') or {}).get('surname'),
            (f.get('fio') or {}).get('name'),
        ]))} (ИНН {f.get('inn') or ''})"
        for f in founders
    )

    phones = [
        _format_phone(phone.get("value", ""))
        for phone in d.get("phones") or []
        if phone.get("value")
    ]

    emails = [
        email.get("value", "")
        for email in d.get("emails") or []
        if email.get("value")
    ]

    return {
        "inn": inn, "found": True,
        "name": s.get("value", ""),
        "kpp": d.get("kpp"), "kpp_largest": d.get("kpp_largest"),
        "ogrn": d.get("ogrn"), "ogrn_date": _dd_date(d.get("ogrn_date")),
        "type": d.get("type"),
        "okved": okved_main, "okved_name": okved_name,
        "additional_okveds": additional_okveds,
        "opf_code": opf.get("code"), "opf_full": opf.get("full"), "opf_short": opf.get("short"),
        "management_name": mgmt.get("name"), "management_post": mgmt.get("post"),
        "management_start": _dd_date(mgmt.get("start_date")),
        "branch_count": d.get("branch_count"), "branch_type": d.get("branch_type"),
        "address": addr.get("value"),
        "region": (addr.get("data") or {}).get("region_with_type") or (addr.get("data") or {}).get("region"),
        "city": (addr.get("data") or {}).get("city_with_type") or (addr.get("data") or {}).get("city"),
        "status": state.get("status"),
        "reg_date": _dd_date(state.get("registration_date")),
        "liq_date": _dd_date(state.get("liquidation_date")),
        "employee_count": d.get("employee_count"),
        **_smb_fields(d),
        "tax_system": fin.get("tax_system"),
        "founders": founders_str, "phones": phones, "emails": emails,
        "finance_by_year": finance_by_year,
        "debt": fin.get("debt"), "penalty": fin.get("penalty"),
    }


def save_dadata_excel(results: list[dict], filepath: str):
    from openpyxl.cell import WriteOnlyCell

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Обогащение ИНН")

    finance_years = _finance_years(results)
    phone_count = _phone_count(results)
    email_count = _email_count(results)
    headers = ["ИНН (запрос)", *_enrich_headers(finance_years, phone_count, email_count)]
    wide_headers = {"Наименование", "Вид деятельности", "Доп. ОКВЭДы", "ОПФ полное", "Руководитель", "Адрес", "Учредители", "Email"}
    for i, header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = 36 if header in wide_headers else 16
    ws.freeze_panes = "A2"
    labels = [WriteOnlyCell(ws, value=header) for header in headers]
    for label in labels:
        label.font = BOLD
        label.alignment = CENTER
    ws.append(labels)

    for result in results:
        row = [result["inn"], *_enrich_values(result, finance_years, phone_count, email_count)]
        if not result.get("found"):
            row = [WriteOnlyCell(ws, value=value) for value in row]
            for cell in row:
                cell.fill = RED
        ws.append(row)
    wb.save(filepath)


def save_remaining_inns(inns: list[str], filepath: Path) -> None:
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Остаток ИНН")
    ws.append(["ИНН"])
    for inn in inns:
        ws.append([inn])
    wb.save(filepath)


def split_enrichment_report(report: Path, target_dir: Path, max_bytes: int) -> list[Path]:
    """Делит активный лист по строкам и проверяет размер каждого готового XLSX."""
    from openpyxl import load_workbook
    from openpyxl.cell import WriteOnlyCell

    workbook = load_workbook(report, read_only=True)
    try:
        source = workbook.active
        if source.max_row is None:
            source.calculate_dimension(force=True)
        last_row = source.max_row or 1
        if last_row < 2 or max_bytes < 4000:
            raise ValueError("Таблицу нельзя разделить на допустимые вложения")
        header, = source.iter_rows(min_row=1, max_row=1, values_only=True)
        part_count = max(2, (report.stat().st_size + max_bytes - 1) // max_bytes)
        rows_per_part = max(1, (last_row - 1 + part_count - 1) // part_count)
        parts: list[Path] = []

        def write_window(start: int, end: int) -> None:
            path = target_dir / f"part-{len(parts) + 1}.xlsx"
            part = Workbook(write_only=True)
            sheet = part.create_sheet(source.title)
            sheet.freeze_panes = "A2"
            labels = []
            for value in header:
                label = WriteOnlyCell(sheet, value=value)
                label.font = BOLD
                labels.append(label)
            sheet.append(labels)
            for row in source.iter_rows(min_row=start, max_row=end):
                values = []
                for cell in row:
                    value = WriteOnlyCell(sheet, value=cell.value)
                    if cell.value is not None:
                        value.number_format = cell.number_format
                        # В автономной части ссылки на отсутствующие листы/строки теряют смысл.
                        if cell.data_type in {"f", "s"}:
                            value.data_type = "s"
                        elif cell.data_type == "e":
                            value.data_type = "e"
                    values.append(value)
                sheet.append(values)
            part.save(path)
            if path.stat().st_size <= max_bytes:
                parts.append(path)
            elif start == end:
                raise ValueError(f"Строка {start} превышает лимит вложения")
            else:
                middle = (start + end) // 2
                write_window(start, middle)
                write_window(middle + 1, end)

        for start in range(2, last_row + 1, rows_per_part):
            write_window(start, min(start + rows_per_part - 1, last_row))
    finally:
        workbook.close()

    named = []
    for index, path in enumerate(parts, 1):
        destination = target_dir / f"{report.stem}_часть_{index:02}_из_{len(parts):02}.xlsx"
        path.replace(destination)
        named.append(destination)
    return named
