"""История ответов DaData, прогресс обогащения и суточный расход запросов."""

import csv
import json
import sqlite3
from contextlib import closing

from yandexbot.core import config


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(config.CONFIG_DIR / "dadata.db", timeout=15)


def init_db() -> None:
    with closing(_connect()) as db:
        db.execute("PRAGMA journal_mode = WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS lookups (
                id INTEGER PRIMARY KEY,
                inn TEXT NOT NULL,
                received_at TEXT,
                report_at TEXT,
                found INTEGER NOT NULL,
                suggestion_count INTEGER,
                payload TEXT NOT NULL,
                job_id INTEGER,
                report_key TEXT,
                UNIQUE(job_id, inn),
                UNIQUE(report_key, inn)
            );
            CREATE INDEX IF NOT EXISTS lookups_inn ON lookups (inn);
            CREATE TABLE IF NOT EXISTS processed_reports (report_key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS request_usage (day TEXT PRIMARY KEY, count INTEGER NOT NULL);
        """)


def request_count(day: str, legacy_count: int) -> int:
    if not (config.CONFIG_DIR / "dadata.db").is_file():
        return legacy_count
    with closing(_connect()) as db:
        row = db.execute("SELECT count FROM request_usage WHERE day=?", (day,)).fetchone()
    return row[0] if row else legacy_count


def reserve_request(day: str, limit: int, legacy_count: int) -> bool:
    """Учитывает попытку до отправки. После сбоя счётчик не откатывается."""
    with closing(_connect()) as db, db:
        db.execute("INSERT OR IGNORE INTO request_usage (day, count) VALUES (?, ?)", (day, legacy_count))
        return db.execute("UPDATE request_usage SET count=count+1 WHERE day=? AND count < ?",
                          (day, limit)).rowcount == 1


def save_response(job_id: int | None, inn: str, received_at: str, response: dict) -> None:
    count = len(response["suggestions"])
    with closing(_connect()) as db, db:
        db.execute("""
            INSERT INTO lookups (job_id, inn, received_at, found, suggestion_count, payload)
            VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (job_id, inn) DO NOTHING
        """, (job_id, inn, received_at, bool(count), count, json.dumps(response, ensure_ascii=False)))


def job_responses(job_id: int):
    with closing(_connect()) as db:
        for inn, payload in db.execute("SELECT inn, payload FROM lookups WHERE job_id=?", (job_id,)):
            yield inn, json.loads(payload)


def processed_reports() -> set[str]:
    with closing(_connect()) as db:
        return {key for key, in db.execute("SELECT report_key FROM processed_reports")}


def store_report(report_key: str, report_at: str | None = None, rows=()) -> None:
    """Отмечает новый отчёт или атомарно переносит строки из старого комплекта."""
    with closing(_connect()) as db, db:
        inserted = db.execute("INSERT OR IGNORE INTO processed_reports VALUES (?)", (report_key,)).rowcount
        if inserted:
            db.executemany("""
                INSERT INTO lookups (report_key, inn, report_at, found, payload) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (report_key, inn) DO NOTHING
            """, ((report_key, inn, report_at, found, json.dumps(fields, ensure_ascii=False, default=str))
                  for inn, found, fields in rows))


def summary() -> dict:
    with closing(_connect()) as db:
        db.row_factory = sqlite3.Row
        row = db.execute("""
            SELECT COUNT(*) AS total, COUNT(DISTINCT inn) AS inns,
                   COALESCE(SUM(found), 0) AS found,
                   COUNT(*) - COALESCE(SUM(found), 0) AS not_found,
                   COUNT(received_at) AS api_responses,
                   COALESCE(SUM(suggestion_count), 0) AS suggestions,
                   COUNT(report_key) AS imported_rows,
                   COUNT(DISTINCT report_key) AS imported_reports,
                   MAX(received_at) AS last_response
            FROM lookups
        """).fetchone()
        return dict(row)


def csv_rows():
    """Потоковый CSV. Все поля ответа, включая несколько совпадений, остаются в JSON."""
    class CsvBuffer:
        def write(self, value: str) -> str:
            return value

    writer = csv.writer(CsvBuffer(), delimiter=";", lineterminator="\n")
    with closing(_connect()) as db:
        rows = db.execute("""
            SELECT inn, received_at, report_at,
                   CASE WHEN received_at IS NULL THEN 'Excel' ELSE 'API DaData' END, report_key,
                   CASE WHEN found THEN 'Найдено' ELSE 'Не найдено' END, suggestion_count, payload
            FROM lookups ORDER BY id
        """)
        yield writer.writerow(("ИНН", "Время ответа UTC", "Время отчёта UTC", "Источник", "Отчёт",
                               "Статус", "Совпадений в ответе API", "Данные JSON"))
        for row in rows:
            yield writer.writerow(row)
