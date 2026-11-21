"""
Лог "кто и откуда писал боту" — заполняется прямо основным процессом бота
(в listen_commands, до фильтра по chat_id), без отдельного polling.
scripts/seen_ids.py только читает этот файл (--dump).
"""

import json

from yandexbot.core import config

SEEN_FILE = config.CONFIG_DIR / "test_id_seen.json"


def _load() -> dict:
    if SEEN_FILE.is_file():
        try:
            return json.loads(SEEN_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"people": {}, "chats": {}}


def _save(seen: dict):
    try:
        SEEN_FILE.write_text(json.dumps(seen, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def record(sender: dict, chat: dict, text: str = ""):
    """Запомнить отправителя и чат. Вызывать на КАЖДЫЙ входящий update."""
    seen = _load()

    skey = (sender or {}).get("login") or (sender or {}).get("id")
    if skey:
        seen["people"][str(skey)] = {
            "login": sender.get("login"),
            "id": sender.get("id"),
            "display_name": sender.get("display_name"),
            "last_text": text[:200],
        }

    ckey = (chat or {}).get("id")
    if ckey:
        seen["chats"][str(ckey)] = {
            "chat_id": chat.get("id"),
            "type": chat.get("type"),
            "title": chat.get("title"),
            "last_from": (sender or {}).get("login") or (sender or {}).get("display_name"),
            "last_text": text[:200],
        }

    _save(seen)