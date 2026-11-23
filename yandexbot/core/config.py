"""
Общая конфигурация проекта: пути, чтение config.toml, логирование.
Импортируется всеми остальными модулями пакета yandexbot.

Файлы настроек лежат в <корень>/config/; для совместимости со старой
раскладкой они ищутся и в корне проекта. Рабочие данные (БД, лог, отчёты)
по-прежнему в <корень>/stat/.
"""

import logging
import json
import os
import re
import sys
from pathlib import Path

import tomllib
from yandexbot.admin import store as admin_store
from yandexbot.core import schedule

# ─── Пути ───────────────────────────────────────────────────────────────────

PROJECT_DIR = Path(__file__).resolve().parents[2]  # yandexbot/core/config.py → корень
SETTINGS_DIR = PROJECT_DIR / "config"             # config.toml, chats.toml
CONFIG_DIR  = PROJECT_DIR / "stat"                 # runtime-данные и отчёты (путь не меняется)
os.makedirs(CONFIG_DIR, exist_ok=True)

DB_PATH           = CONFIG_DIR / "prices.db"
LOG_FILE          = CONFIG_DIR / "bot_log.log"
STATE_FILE        = CONFIG_DIR / "last_run.json"       # последний плановый запуск цен 1СО
EPD_STATE_FILE    = CONFIG_DIR / "epd_last_run.json"   # последний плановый запуск цен ЭПД
OFFSET_FILE       = CONFIG_DIR / "ym_offset.txt"
DADATA_USAGE_FILE = CONFIG_DIR / "dadata_usage.json"
DADATA_STATS_FILE = CONFIG_DIR / "dadata_stats.json"
DADATA_LIMIT_FILE = CONFIG_DIR / "dadata_daily_limit.json"
MODULE_SETTINGS_FILE = CONFIG_DIR / "module_settings.json"
ADMIN_DB_PATH = CONFIG_DIR / "admin.db"
PROGRAMM_NAME = "Бот Астрал: цены конкурентов + обогащение ИНН"


def _settings_file(name: str) -> Path:
    """config/<name>; если его ещё нет, а в корне лежит старый — берём старый."""
    new, legacy = SETTINGS_DIR / name, PROJECT_DIR / name
    return legacy if not new.is_file() and legacy.is_file() else new


CHATS_FILE = _settings_file("chats.toml")  # список чатов: рассылка/команды (независимые флаги)


def _cfg_file() -> Path:
    for p in [
        os.getenv("PARSER_CONFIG"),
        SETTINGS_DIR / "config.toml",
        PROJECT_DIR / "config.toml",
        CONFIG_DIR  / "config.toml",
        Path.cwd()  / "config.toml",
    ]:
        if p and Path(p).is_file():
            return Path(p)
    return SETTINGS_DIR / "config.toml"


CONFIG_FILE = _cfg_file()

# ─── Кодировка stdout/stderr ──────────────────────────────────────────────────
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

# ─── Логирование ─────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    filename=LOG_FILE,
    # Дозапись: тестовые скрипты и CLI импортируют config и не должны
    # обнулять лог работающего бота.
    filemode="a",
    encoding="utf-8",
    format="%(asctime)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logging.info(f"СТАРТ '{PROGRAMM_NAME}'")

# ─── Конфиг ──────────────────────────────────────────────────────────────────

if not CONFIG_FILE.is_file():
    logging.error(f"Конфиг не найден: {CONFIG_FILE}")
    sys.exit(1)

with open(CONFIG_FILE, "rb") as f:
    DATA = tomllib.load(f)

YANDEX_TOKEN   = DATA.get("yandex", {}).get("token", "")
YANDEX_CHAT_ID = DATA.get("yandex", {}).get("chat_id", "")
YANDEX_LOGIN   = DATA.get("yandex", {}).get("login", "")

if not YANDEX_TOKEN:
    logging.error("Нет токена бота Яндекс Мессенджера в конфиге!")
    sys.exit(1)

if not YANDEX_CHAT_ID and not YANDEX_LOGIN:
    logging.error("В конфиге не указан ни chat_id, ни login для отправки в Яндекс Мессенджер!")
    sys.exit(1)

# DaData — опционально: если токена нет, команда /обогатить будет отвечать отказом,
# но остальной бот (парсер цен) продолжит работать.
DADATA_TOKEN       = DATA.get("dadata", {}).get("token", "")
DADATA_DEFAULT_DAILY_LIMIT = int(DATA.get("dadata", {}).get("daily_limit", 50000))
DADATA_DAILY_LIMIT = DADATA_DEFAULT_DAILY_LIMIT


def _load_dadata_daily_limit() -> int:
    """Читает лимит, заданный администратором, не меняя config.toml."""
    if not DADATA_LIMIT_FILE.is_file():
        return DADATA_DEFAULT_DAILY_LIMIT
    try:
        value = int(json.loads(DADATA_LIMIT_FILE.read_text(encoding="utf-8"))["limit"])
        return value if value > 0 else DADATA_DEFAULT_DAILY_LIMIT
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError) as e:
        logging.warning(f"Не удалось прочитать лимит DaData: {e}")
        return DADATA_DEFAULT_DAILY_LIMIT


def set_dadata_daily_limit(limit: int, actor: str = "messenger") -> int:
    """Команда /лимиты: пишет в те же настройки модуля enrich, что и админ-панель.

    Раньше лимит жил в отдельном dadata_daily_limit.json, и сохранение в
    панели молча перекрывало значение из чата. Файл теперь только значение
    по умолчанию для старых установок.
    """
    if limit <= 0:
        raise ValueError("Лимит должен быть положительным числом")
    save_enrich_module_settings({**enrich_module_settings(), "daily_limit": limit}, actor)
    return limit


DADATA_DAILY_LIMIT = _load_dadata_daily_limit()

if not DADATA_TOKEN:
    logging.warning("Токен DaData не указан в конфиге — команда /обогатить будет недоступна.")

# ─── Доступ: только для первоначального переноса в админ-панель ───────────────
# [access] admins / allowed_logins из config.toml один раз переносятся в список
# доступов панели (admin_store.import_config_access); дальше права выдаёт
# и отзывает только панель. Логины сравниваются регистронезависимо.

ADMIN_LOGINS = {
    str(x).strip().lower()
    for x in DATA.get("access", {}).get("admins", [])
    if str(x).strip()
}

# Логины пользователей, которым разрешена работа с ботом в личных сообщениях.
# Сравнение регистронезависимое.
ALLOWED_LOGINS = {
    str(x).strip().lower()
    for x in DATA.get("access", {}).get("allowed_logins", [])
    if str(x).strip()
}

# ─── Команды бота ─────────────────────────────────────────────────────────────

_PRICES_MODULE_CONFIG = DATA.get("modules", {}).get("prices", {})
_PRICES_MANUAL_CONFIG = _PRICES_MODULE_CONFIG.get("manual", {})
_PRICES_AUTO_CONFIG   = _PRICES_MODULE_CONFIG.get("auto", {})
_EPD_PRICES_MODULE_CONFIG = DATA.get("modules", {}).get("epd_prices", {})

PRICE_COMMANDS = {
    str(command).strip().lower()
    for command in _PRICES_MODULE_CONFIG.get("commands", ["/цены", "/цена"])
    if str(command).strip()
}
EPD_PRICE_COMMANDS = {
    str(command).strip().lower()
    for command in _EPD_PRICES_MODULE_CONFIG.get("commands", ["/эпд-цены"])
    if str(command).strip()
}
ENRICH_COMMANDS     = {"/обогатить"}
WHOAMI_COMMANDS      = {"/whoami", "/ктоя"}
START_COMMANDS       = {"/start", "/начать"}
ENRICH_STATS_COMMAND = "/статистика_обогатить"
DADATA_LIMIT_COMMAND = "/лимиты"
HELP_COMMANDS        = {"/help", "/помощь"}

AUTO_RUN_INTERVAL_DAYS       = int(_PRICES_AUTO_CONFIG.get("interval_days", 7))
PRICES_AUTO_ENABLED          = bool(_PRICES_AUTO_CONFIG.get("enabled", True))
PRICES_AUTO_RUN_AT           = str(_PRICES_AUTO_CONFIG.get("at", "02:30"))
SCHEDULER_CHECK_INTERVAL_SEC = 300    # как часто планировщики проверяют расписание (5 мин)
PENDING_ENRICH_TIMEOUT_SEC   = 900    # сколько ждать файл после /обогатить (15 мин)

if AUTO_RUN_INTERVAL_DAYS <= 0:
    raise ValueError("[modules.prices.auto] interval_days должен быть положительным")
if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", PRICES_AUTO_RUN_AT):
    raise ValueError("[modules.prices.auto] at должен быть в формате HH:MM")


def prices_run_settings(profile: str) -> dict[str, float]:
    """Настройки скорости парсера для ручного или планового запуска."""
    if profile not in {"manual", "auto"}:
        raise ValueError(f"Неизвестный профиль парсинга: {profile}")
    overrides = _load_module_settings().get("prices", {})
    source_defaults = _PRICES_MANUAL_CONFIG if profile == "manual" else _PRICES_AUTO_CONFIG
    source = {**source_defaults, **overrides.get(profile, {})}
    defaults = (
        {"sbis_delay_sec": 0.3, "kontur_region_delay_sec": 1.0, "kontur_region_delay_max_sec": 2.0}
        if profile == "manual"
        else {"sbis_delay_sec": 1.0, "kontur_region_delay_sec": 5.0, "kontur_region_delay_max_sec": 10.0}
    )
    settings = {}
    for key, default in defaults.items():
        value = float(source.get(key, default))
        if value < 0:
            raise ValueError(f"[modules.prices.{profile}] {key} не может быть отрицательным")
        settings[key] = value
    if settings["kontur_region_delay_max_sec"] < settings["kontur_region_delay_sec"]:
        settings["kontur_region_delay_max_sec"] = settings["kontur_region_delay_sec"]
    return settings


def _load_module_settings() -> dict:
    """Читает изменяемые в админ-панели параметры, не затрагивая секреты."""
    return {
        "prices": admin_store.get_module_settings(ADMIN_DB_PATH, "prices"),
        "enrich": admin_store.get_module_settings(ADMIN_DB_PATH, "enrich"),
        "epd_prices": admin_store.get_module_settings(ADMIN_DB_PATH, "epd_prices"),
    }


def enrich_module_settings() -> dict:
    """Безопасные runtime-настройки обогащения, без токена DaData."""
    saved = _load_module_settings().get("enrich", {})
    enabled = bool(saved.get("enabled", True))
    daily_limit = int(saved.get("daily_limit", DADATA_DAILY_LIMIT))
    requests_per_second = int(saved.get("requests_per_second", 25))
    if daily_limit <= 0:
        raise ValueError("Лимит DaData должен быть положительным")
    if not 1 <= requests_per_second <= 25:
        raise ValueError("Скорость DaData должна быть от 1 до 25 запросов в секунду")
    return {
        "enabled": enabled,
        "commands": _module_commands(saved.get("commands"), ENRICH_COMMANDS),
        "daily_limit": daily_limit,
        "requests_per_second": requests_per_second,
    }


def save_enrich_module_settings(value: dict, actor: str) -> None:
    daily_limit = int(value["daily_limit"])
    requests_per_second = int(value["requests_per_second"])
    if daily_limit <= 0:
        raise ValueError("Лимит DaData должен быть положительным")
    if not 1 <= requests_per_second <= 25:
        raise ValueError("Скорость DaData должна быть от 1 до 25 запросов в секунду")
    commands = _parse_commands(value.get("commands", ENRICH_COMMANDS))
    _check_command_conflicts("enrich", commands)
    admin_store.save_module_settings(
        ADMIN_DB_PATH, "enrich", {
            "enabled": bool(value["enabled"]), "daily_limit": daily_limit,
            "requests_per_second": requests_per_second,
            "commands": commands,
        }, actor,
    )


def prices_module_settings() -> dict:
    """Возвращает полную безопасную конфигурацию модуля prices для панели."""
    overrides = _load_module_settings().get("prices", {})
    auto = {**_PRICES_AUTO_CONFIG, **overrides.get("auto", {})}
    manual = {**_PRICES_MANUAL_CONFIG, **overrides.get("manual", {})}
    return {
        "enabled": bool(overrides.get("enabled", _PRICES_MODULE_CONFIG.get("enabled", True))),
        "commands": _module_commands(overrides.get("commands"), PRICE_COMMANDS),
        "auto": {
            "enabled": bool(auto.get("enabled", True)),
            "interval_days": int(auto.get("interval_days", AUTO_RUN_INTERVAL_DAYS)),
            "at": str(auto.get("at", PRICES_AUTO_RUN_AT)),
            "only_changes": bool(auto.get("only_changes", True)),
            **prices_run_settings("auto"),
        },
        "manual": {
            **prices_run_settings("manual"),
            "main_files_only": bool(manual.get("main_files_only", True)),
        },
        "broadcast_chats": _broadcast_chats(overrides.get("broadcast_chats")),
    }


_EPD_AUTO_CONFIG = _EPD_PRICES_MODULE_CONFIG.get("auto", {})


def epd_prices_module_settings() -> dict:
    """Настройки самостоятельного модуля ЭПД: config.toml плюс правки панели.

    Плановый запуск по умолчанию выключен: его включают в панели («Цены ЭПД»).
    """
    overrides = _load_module_settings().get("epd_prices", {})
    auto = {**_EPD_AUTO_CONFIG, **overrides.get("auto", {})}
    return {
        "enabled": bool(overrides.get("enabled", _EPD_PRICES_MODULE_CONFIG.get("enabled", True))),
        "commands": _module_commands(overrides.get("commands"), EPD_PRICE_COMMANDS),
        "auto": {
            "enabled": bool(auto.get("enabled", False)),
            "interval_days": int(auto.get("interval_days", 7)),
            "at": str(auto.get("at", "09:00")),
            "only_changes": bool(auto.get("only_changes", True)),
        },
        "broadcast_chats": _broadcast_chats(overrides.get("broadcast_chats")),
    }


def save_epd_prices_module_settings(value: dict, actor: str) -> None:
    commands = _parse_commands(value.get("commands", EPD_PRICE_COMMANDS))
    _check_command_conflicts("epd_prices", commands)
    auto = value.get("auto") or epd_prices_module_settings()["auto"]
    interval_days, at = schedule.validate(auto["interval_days"], auto["at"])
    admin_store.save_module_settings(
        ADMIN_DB_PATH, "epd_prices", {
            "enabled": bool(value["enabled"]),
            "commands": commands,
            "auto": {"enabled": bool(auto["enabled"]), "interval_days": interval_days, "at": at,
                     "only_changes": bool(auto.get("only_changes", True))},
            "broadcast_chats": _broadcast_chats(value.get("broadcast_chats")),
        }, actor,
    )


def _broadcast_chats(value: object) -> list[str] | None:
    """Чаты рассылки модуля. None — не выбраны: рассылка во все чаты с флагом «Рассылка»."""
    if value is None:
        return None
    if isinstance(value, str) or not all(isinstance(item, str) for item in value):
        raise ValueError("Список чатов рассылки должен быть списком ID")
    return list(dict.fromkeys(item.strip() for item in value if item.strip()))


# Служебные команды бота нельзя отдавать модулям.
_RESERVED_COMMANDS = WHOAMI_COMMANDS | START_COMMANDS | HELP_COMMANDS | {ENRICH_STATS_COMMAND, DADATA_LIMIT_COMMAND}


def _check_command_conflicts(module_code: str, commands: list[str]) -> None:
    """Одна команда — один модуль, иначе бот молча выберет первый по порядку."""
    reserved = sorted(set(commands) & _RESERVED_COMMANDS)
    if reserved:
        raise ValueError(f"Служебные команды бота нельзя назначать модулю: {', '.join(reserved)}")
    others = {
        "prices": ("Цены", prices_module_settings),
        "epd_prices": ("Цены ЭПД", epd_prices_module_settings),
        "enrich": ("Обогащение ИНН", enrich_module_settings),
    }
    for code, (title, settings) in others.items():
        if code == module_code:
            continue
        taken = sorted(set(commands) & set(settings()["commands"]))
        if taken:
            raise ValueError(f"Команда {', '.join(taken)} уже назначена модулю «{title}»")


def save_prices_module_settings(value: dict, actor: str = "runtime") -> None:
    """Проверяет и атомарно сохраняет редактируемые параметры модуля prices."""
    auto = value.get("auto", {})
    manual = value.get("manual", {})
    interval_days, at = schedule.validate(auto["interval_days"], auto["at"])
    commands = _parse_commands(value.get("commands", PRICE_COMMANDS))
    _check_command_conflicts("prices", commands)
    normalized = {
        "enabled": bool(value["enabled"]),
        "commands": commands,
        "auto": {
            "enabled": bool(auto["enabled"]),
            "interval_days": interval_days,
            "at": at,
            "only_changes": bool(auto.get("only_changes", True)),
            "sbis_delay_sec": _nonnegative_float(auto["sbis_delay_sec"], "Пауза СБИС"),
            "kontur_region_delay_sec": _nonnegative_float(
                auto["kontur_region_delay_sec"], "Пауза Контур от",
            ),
            "kontur_region_delay_max_sec": _nonnegative_float(
                auto.get("kontur_region_delay_max_sec", auto["kontur_region_delay_sec"]),
                "Пауза Контур до",
            ),
        },
        "manual": {
            "sbis_delay_sec": _nonnegative_float(manual["sbis_delay_sec"], "Пауза СБИС"),
            "kontur_region_delay_sec": _nonnegative_float(
                manual["kontur_region_delay_sec"], "Пауза Контур от",
            ),
            "kontur_region_delay_max_sec": _nonnegative_float(
                manual.get("kontur_region_delay_max_sec", manual["kontur_region_delay_sec"]),
                "Пауза Контур до",
            ),
            "main_files_only": bool(manual.get("main_files_only", True)),
        },
        "broadcast_chats": _broadcast_chats(value.get("broadcast_chats")),
    }
    admin_store.save_module_settings(ADMIN_DB_PATH, "prices", normalized, actor)


def _module_commands(value: object, defaults: set[str]) -> list[str]:
    """Возвращает команды из панели или безопасные команды из config.toml."""
    return _parse_commands(value) if value is not None else sorted(defaults)


def _parse_commands(value: object) -> list[str]:
    """Проверяет команды: одна команда на строку или через запятую."""
    if isinstance(value, (set, list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = str(value or "").replace(",", "\n").splitlines()
    commands = sorted({item.strip().lower() for item in raw if item.strip()})
    if not commands:
        raise ValueError("Укажите хотя бы одну команду")
    if any(not command.startswith("/") or " " in command for command in commands):
        raise ValueError("Команда должна начинаться с / и не содержать пробелов")
    return commands


def prices_auto_schedule() -> dict:
    """Текущее расписание: config.toml плюс допустимые правки админ-панели."""
    settings = prices_module_settings()["auto"]
    return {
        "module_enabled": prices_module_settings()["enabled"],
        "enabled": settings["enabled"],
        "interval_days": settings["interval_days"],
        "at": settings["at"],
        "only_changes": settings["only_changes"],
    }


def _nonnegative_float(value, label: str) -> float:
    result = float(value)
    if result < 0:
        raise ValueError(f"{label} не может быть отрицательной")
    return result
