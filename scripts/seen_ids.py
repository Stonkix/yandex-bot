"""
Показывает список людей и чатов, которые писали боту.
Данные собирает САМ бот (пока он запущен --daemon/--listen) — этот
скрипт их только читает, отдельного polling не делает (второй поллер
всё равно проигрывает гонку работающему демону за апдейты).

Использование:
    1. Убедись, что бот (main.py) уже запущен.
    2. Напиши боту что угодно из нужных чатов/личек.
    3. python scripts/seen_ids.py            — посмотреть накопленное
    4. python scripts/seen_ids.py --reset    — очистить лог, чтобы начать заново
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта: пакет yandexbot

from yandexbot.messenger import seen_log


def _print_summary(seen: dict):
    print("=== Люди, писавшие боту ===")
    if not seen["people"]:
        print("(пока никто — проверь, что бот запущен и им кто-то писал)")
    for p in seen["people"].values():
        print(f'  login={p.get("login")!r:25} id={p.get("id")!r:15} '
              f'display_name={p.get("display_name")!r:20} last_text={p.get("last_text")!r}')

    print("\n=== Чаты, откуда писали ===")
    if not seen["chats"]:
        print("(пока ни одного)")
    for c in seen["chats"].values():
        print(f'  chat_id={c.get("chat_id")!r:45} type={c.get("type")!r:10} '
              f'title={c.get("title")!r:25} last_from={c.get("last_from")!r}')


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="Очистить накопленный лог")
    args = ap.parse_args()

    if args.reset:
        if seen_log.SEEN_FILE.is_file():
            seen_log.SEEN_FILE.unlink()
        print("Сброшено.")
    else:
        _print_summary(seen_log._load())