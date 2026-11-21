"""Данные для страниц админ-панели: состояние бота, расписание, отчёты, лог.

Только чтение: ничего из этого модуля не меняет настройки и рабочие файлы.
"""

import datetime
import sqlite3
import time
from pathlib import Path

from yandexbot.core import config, schedule, state

# Сколько секунд без прохода цикла считаем нормой. getUpdates ждёт до 30 с,
# очередь спит 3 с, планировщики просыпаются раз в SCHEDULER_CHECK_INTERVAL_SEC.
# Плановая выгрузка 1СО идёт внутри тика планировщика и длится до часа.
HEARTBEAT_LIMITS = {
    "listen": 120, "queue": 60,
    "scheduler": config.SCHEDULER_CHECK_INTERVAL_SEC + 2 * 3600,
    "epd_scheduler": config.SCHEDULER_CHECK_INTERVAL_SEC + 900,
}
HEARTBEAT_TITLES = {"listen": "Чат", "queue": "Очередь панели", "scheduler": "Расписание 1СО",
                    "epd_scheduler": "Расписание ЭПД"}

REPORT_SUFFIXES = {".xlsx", ".xlsm", ".zip"}
# Порядок важен: более длинные префиксы раньше коротких.
REPORT_KINDS = (
    ("kontur_test_sources_", "Исходники Контура (тест)"),
    ("kontur_sources_", "Архив исходников · Отчётность (Контур)"),
    ("prices_sources_", "Архив исходников · Отчётность"),
    ("epd_sources_", "Архив исходников · ЭПД"),
    ("kontur_test_", "Цены 1СО · Контур (тест)"),
    ("kontur_", "Цены 1СО · Контур"),
    ("sbis_", "Цены 1СО · СБИС"),
    ("epd_prices_test_", "ЭПД · тестовый прогон"),
    ("epd_prices_", "Цены ЭПД"),
    ("enrich_", "Обогащение ИНН"),
)


def bot_status() -> list[dict]:
    """Состояние фоновых циклов; пусто, если бот не запущен в этом процессе."""
    now = time.time()
    result = []
    for key, title in HEARTBEAT_TITLES.items():
        seen = state.heartbeat.get(key)
        if seen is None:
            continue
        age = now - seen
        result.append({"title": title, "ok": age <= HEARTBEAT_LIMITS[key], "age": _age(age)})
    return result


def _age(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} с назад"
    if seconds < 3600:
        return f"{seconds // 60} мин назад"
    return f"{seconds // 3600} ч назад"


def _schedule_info(module_enabled: bool, auto: dict, last: datetime.datetime | None) -> dict:
    enabled = module_enabled and auto["enabled"]
    next_run = schedule.next_run(auto["interval_days"], auto["at"], last) if enabled else None
    return {
        "module_enabled": module_enabled,
        "auto_enabled": auto["enabled"],
        "interval_days": auto["interval_days"],
        "at": auto["at"],
        "last": last,
        "next": next_run,
        "overdue": next_run is not None and next_run <= datetime.datetime.now(),
    }


def prices_schedule() -> dict:
    """Расписание плановой выгрузки цен 1СО."""
    settings = config.prices_module_settings()
    return _schedule_info(settings["enabled"], settings["auto"], schedule.load_last_run(config.STATE_FILE))


def epd_schedule() -> dict:
    """Расписание плановой выгрузки цен ЭПД."""
    settings = config.epd_prices_module_settings()
    return _schedule_info(settings["enabled"], settings["auto"], schedule.load_last_run(config.EPD_STATE_FILE))


def last_fetch(table: str) -> datetime.datetime | None:
    """Время последней записи истории цен (1СО — prices, ЭПД — epd_prices)."""
    if table not in {"prices", "epd_prices"} or not config.DB_PATH.is_file():
        return None
    try:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            row = conn.execute(f"SELECT MAX(fetched_at) FROM {table}").fetchone()
        finally:
            conn.close()
        return datetime.datetime.fromisoformat(row[0]) if row and row[0] else None
    except (sqlite3.Error, ValueError):
        return None


def dadata_usage() -> dict:
    from yandexbot.modules import enrich

    usage = enrich._load_dadata_usage()
    limit = config.enrich_module_settings()["daily_limit"]
    used = int(usage.get("count", 0))
    return {"used": used, "limit": limit, "percent": min(100, round(used * 100 / limit)) if limit else 0,
            "token": bool(config.DADATA_TOKEN)}


def dadata_by_user() -> list[dict]:
    from yandexbot.modules import enrich

    return enrich.dadata_usage_report()


def report_kind(name: str) -> str:
    for prefix, title in REPORT_KINDS:
        if name.startswith(prefix):
            return title
    return "Другое"


def list_reports(limit: int = 300) -> list[dict]:
    """Файлы отчётов в stat/ и архивы в stat/archive/, новые сверху.

    name — путь относительно stat/ (архивы: archive/<файл>), по нему и скачиваем.
    """
    items = []
    archive_dir = config.CONFIG_DIR / "archive"
    paths = list(config.CONFIG_DIR.iterdir()) + (list(archive_dir.iterdir()) if archive_dir.is_dir() else [])
    for path in paths:
        if not path.is_file() or path.suffix.lower() not in REPORT_SUFFIXES or path.name.startswith("~$"):
            continue
        info = path.stat()
        items.append({
            "name": path.relative_to(config.CONFIG_DIR).as_posix(),
            "kind": report_kind(path.name),
            "size": info.st_size,
            "modified": datetime.datetime.fromtimestamp(info.st_mtime),
        })
    items.sort(key=lambda item: item["modified"], reverse=True)
    return items[:limit]


def report_path(name: str) -> Path | None:
    """Путь к отчёту только если имя есть в списке отчётов (без обхода каталогов)."""
    if name in {item["name"] for item in list_reports(limit=10_000)}:
        return config.CONFIG_DIR / name
    return None


def log_tail(lines: int = 300, query: str = "", max_bytes: int = 1_000_000) -> list[str]:
    """Последние строки stat/bot_log.log, при запросе — только совпадающие."""
    try:
        with open(config.LOG_FILE, "rb") as stream:
            stream.seek(0, 2)
            size = stream.tell()
            stream.seek(max(0, size - max_bytes))
            text = stream.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    rows = text.splitlines()
    if size > max_bytes and rows:
        rows = rows[1:]  # первая строка обрезана посередине
    if query:
        needle = query.lower()
        rows = [row for row in rows if needle in row.lower()]
    return rows[-lines:]


def is_error_line(line: str) -> bool:
    lowered = line.lower()
    return any(marker in lowered for marker in ("ошибка", "error", "traceback", "exception", "❌"))
