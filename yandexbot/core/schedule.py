"""Расписание плановых выгрузок «раз в N дней, не раньше HH:MM».

Общая логика для цен 1СО и ЭПД. Срок считается по календарным датам, а не
по прошедшим часам: иначе каждый запуск сдвигается на время между
проверками планировщика (02:30 → 03:30 → 04:30 …).
"""

import datetime
import json
import logging
import re
from pathlib import Path

TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")


def validate(interval_days, at) -> tuple[int, str]:
    """Проверяет интервал и время из формы или конфига."""
    interval = int(interval_days)
    at = str(at)
    if interval <= 0:
        raise ValueError("Интервал должен быть положительным числом дней")
    if not TIME_RE.fullmatch(at):
        raise ValueError("Время должно быть в формате ЧЧ:ММ")
    return interval, at


def _at_time(at: str) -> datetime.time:
    hour, minute = map(int, at.split(":"))
    return datetime.time(hour, minute)


def next_run(interval_days: int, at: str, last: datetime.datetime | None,
             now: datetime.datetime | None = None) -> datetime.datetime:
    """Когда плановый запуск станет разрешён (может быть уже в прошлом)."""
    now = now or datetime.datetime.now()
    day = now.date() if last is None else last.date() + datetime.timedelta(days=interval_days)
    return datetime.datetime.combine(day, _at_time(at))


def is_due(interval_days: int, at: str, last: datetime.datetime | None,
           now: datetime.datetime | None = None) -> bool:
    now = now or datetime.datetime.now()
    return now >= next_run(interval_days, at, last, now)


def load_last_run(path: Path) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(json.loads(path.read_text(encoding="utf-8"))["last_auto_run"])
    except (OSError, KeyError, ValueError, TypeError):
        return None


def save_last_run(path: Path, moment: datetime.datetime) -> None:
    try:
        path.write_text(json.dumps({"last_auto_run": moment.isoformat()}), encoding="utf-8")
    except OSError as error:
        logging.error("Не удалось сохранить %s: %s", path, error)
