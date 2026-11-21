"""
Управление доступом бота к нескольким чатам:

- чаты панели      — список групповых чатов с двумя НЕЗАВИСИМЫМИ флагами:
                      broadcast (получает плановую/ручную рассылку цен) и
                      commands (в чате можно вызывать /цены, /обогатить всем участникам).
- chats.toml       — прежний список до первого сохранения чатов в панели.
- список доступов админ-панели (admin.db) — логины администраторов и
  сотрудников; [access] из config.toml переносится туда один раз.
"""

import logging

from yandexbot.core import config
from yandexbot.admin import store as admin_store

# ─── chats.toml: список чатов, независимые флаги "рассылка"/"команды" ─────────

_DEFAULT_CHATS_TOML = """\
# Список групповых чатов бота.
# id        — chat.id из апдейта (узнать через команду /whoami в нужном чате)
# title     — просто для читаемости, ни на что не влияет
# broadcast — true: чат получает плановую (раз в 7 дней) и ручную рассылку цен
# commands  — true: в чате разрешены команды /цены и /обогатить всем участникам
# Флаги независимые — можно и то, и то, можно только одно.
#
# [[chat]]
# id = "0/0/xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
# title = "Название чата"
# broadcast = true
# commands = true
"""


def _load_chats() -> tuple[list[dict], bool]:
    stored, saved_in_panel = admin_store.chat_settings(config.ADMIN_DB_PATH)
    if stored or saved_in_panel:
        return [
            {"id": row["chat_id"], "title": row["title"], "broadcast": bool(row["broadcast"]),
             "commands": bool(row["commands"])}
            for row in stored
        ], saved_in_panel
    if not config.CHATS_FILE.is_file():
        try:
            config.CHATS_FILE.write_text(_DEFAULT_CHATS_TOML, encoding="utf-8", newline="\n")
            logging.info(f"Создан пустой {config.CHATS_FILE} — заполните через /whoami в нужных чатах.")
        except Exception as e:
            logging.error(f"Не удалось создать {config.CHATS_FILE}: {e}")
        return [], False
    try:
        import tomllib
        with open(config.CHATS_FILE, "rb") as f:
            data = tomllib.load(f)
        return [c for c in data.get("chat", []) if c.get("id")], False
    except Exception as e:
        logging.error(f"Не удалось прочитать {config.CHATS_FILE}: {e}")
        return [], False


def _chat_ids(chats: list[dict], flag: str, saved_in_panel: bool) -> list[str]:
    ids = [chat["id"] for chat in chats if chat.get(flag)]
    if not ids and not saved_in_panel and config.YANDEX_CHAT_ID:
        return [config.YANDEX_CHAT_ID]
    return ids


def broadcast_chat_ids() -> list[str]:
    """Групповые чаты общей рассылки."""
    chats, saved_in_panel = _load_chats()
    return _chat_ids(chats, "broadcast", saved_in_panel)


def broadcast_targets() -> list[dict]:
    chats, saved_in_panel = _load_chats()
    ids = _chat_ids(chats, "broadcast", saved_in_panel)
    if not ids and not saved_in_panel and config.YANDEX_LOGIN:
        return [{"login": config.YANDEX_LOGIN}]
    return [{"chat_id": chat_id} for chat_id in ids]


def commands_allowed_chat_ids() -> set[str]:
    """Групповые чаты, где команды доступны всем участникам без кода."""
    chats, saved_in_panel = _load_chats()
    return set(_chat_ids(chats, "commands", saved_in_panel))


_MODULE_SETTINGS = {
    "prices": lambda: config.prices_module_settings(),
    "epd_prices": lambda: config.epd_prices_module_settings(),
}


def module_chat_ids(module_code: str) -> list[str]:
    """Чаты рассылки модуля: выбранные в панели, иначе все чаты с флагом «Рассылка».

    Удалённые из списка чатов ID отбрасываются; если от выбора ничего не
    осталось — используется общий список, чтобы итог не пропал.
    """
    chats, saved_in_panel = _load_chats()
    chosen = _MODULE_SETTINGS[module_code]().get("broadcast_chats")
    if chosen is not None:
        known = {chat["id"] for chat in chats}
        ids = [chat_id for chat_id in chosen if chat_id in known]
        if ids:
            return ids
    return _chat_ids(chats, "broadcast", saved_in_panel)


def module_targets(module_code: str) -> list[dict]:
    """Получатели рассылки модуля в формате ym_api (target)."""
    return [{"chat_id": chat_id} for chat_id in module_chat_ids(module_code)]


def chat_title(chat_id: str) -> str | None:
    chats, _ = _load_chats()
    for c in chats:
        if c.get("id") == chat_id:
            return c.get("title")
    return None


# ─── Доступ по логину: единственный источник — список в админ-панели ─────────
# [access] из config.toml переносится туда один раз (import_config_access) и
# дальше прав не выдаёт: иначе такой логин нельзя было бы убрать из панели.

_imported_paths: set = set()


def _roles() -> dict[str, str]:
    path = config.ADMIN_DB_PATH
    if path not in _imported_paths:
        # Бот может работать без панели — переносим и при первой проверке доступа.
        admin_store.import_config_access(path, config.ADMIN_LOGINS, config.ALLOWED_LOGINS)
        _imported_paths.add(path)
    return {row["login"]: row["role"] for row in admin_store.list_access(path)}


def _login(sender: dict) -> str:
    return (sender.get("login") or "").strip().lower()


def is_admin(sender: dict) -> bool:
    login = _login(sender)
    return bool(login) and _roles().get(login) == "admin"


def is_employee(sender: dict) -> bool:
    login = _login(sender)
    return bool(login) and login in _roles()


def admin_logins() -> list[str]:
    return sorted(login for login, role in _roles().items() if role == "admin")


def has_access(sender: dict) -> bool:
    return is_admin(sender) or is_employee(sender)


