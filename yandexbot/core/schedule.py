"""Расписание плановых выгрузок «раз в N дней, не раньше HH:MM».

Общая логика для цен 1СО и ЭПД. Срок считается по календарным датам, а не
по прошедшим часам: иначе каждый запуск сдвигается на время между
проверками планировщика (02:30 → 03:30 → 04:30 …).
"""

import datetime
import json
import logging
import re
import tempfile
from pathlib import Path

TIME_RE = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d")
RETRY_DELAY = datetime.timedelta(hours=1)
MAX_ATTEMPTS_PER_DAY = 3


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


def _load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _load_time(state: dict, key: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(state[key])
    except (KeyError, ValueError, TypeError):
        return None


def load_last_run(path: Path) -> datetime.datetime | None:
    return _load_time(_load_state(path), "last_auto_run")


def next_attempt(path: Path, interval_days: int, at: str, now: datetime.datetime | None = None) -> datetime.datetime:
    state = _load_state(path)
    success = _load_time(state, "last_auto_run")
    attempt = _load_time(state, "last_attempt")
    regular = next_run(interval_days, at, success, now)
    if not attempt or (success and attempt <= success):
        return regular
    retry = attempt + RETRY_DELAY
    if state.get("attempt_count", 1) >= MAX_ATTEMPTS_PER_DAY or retry.date() != attempt.date():
        tomorrow = datetime.datetime.combine(attempt.date() + datetime.timedelta(days=1), _at_time(at))
        retry = max(tomorrow, retry)
    return max(regular, retry)


def should_run(path: Path, interval_days: int, at: str, now: datetime.datetime) -> bool:
    return now >= next_attempt(path, interval_days, at, now)


def _save_state(path: Path, state: dict) -> None:
    try:
        with tempfile.TemporaryDirectory(prefix=".schedule_", dir=path.parent) as folder:
            tmp = Path(folder) / path.name
            tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8", newline="\n")
            tmp.replace(path)
    except OSError as error:
        logging.error("Не удалось сохранить %s: %s", path, error)


def save_last_run(path: Path, moment: datetime.datetime) -> None:
    state = _load_state(path)
    state["last_auto_run"] = moment.isoformat()
    _save_state(path, state)


def save_attempt(path: Path, moment: datetime.datetime) -> None:
    state = _load_state(path)
    previous = _load_time(state, "last_attempt")
    same_day = previous and previous.date() == moment.date()
    count = state.get("attempt_count", 1) if same_day else 0
    state["last_attempt"] = moment.isoformat()
    state["attempt_count"] = count + 1
    _save_state(path, state)
