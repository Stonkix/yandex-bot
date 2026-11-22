"""Импорт готовых отчётов DaData и недельное хранение файлов обогащения."""

import datetime
import logging
import re
import sqlite3
import time
import uuid
from collections import defaultdict
from pathlib import Path

from openpyxl import load_workbook

from yandexbot.core import config
from yandexbot.modules import dadata_store, enrich

FILE_TTL_SEC = 7 * 24 * 3600
REPORT_TAIL = re.compile(r"_(?:остаток|часть_[0-9]+_из_[0-9]+)$")
UPLOAD_NAME = re.compile(r"(?:job-[0-9]+|upload-[0-9a-f]{32})\.(?:txt|xlsx|xlsm)(?:\.part)?")


def upload_path(extension: str, job_id: int | None) -> Path:
    directory = config.CONFIG_DIR / "enrich_uploads"
    directory.mkdir(exist_ok=True)
    if directory.resolve() != config.CONFIG_DIR.resolve() / directory.name:
        raise OSError("Недопустимый каталог входных файлов обогащения")
    stem = f"job-{job_id}" if job_id is not None else f"upload-{uuid.uuid4().hex}"
    return directory / f"{stem}{extension}"


def _read_report(path: Path):
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = iter(workbook.active.values)
        headers = tuple(str(value or "").strip() for value in next(rows, ()))
        # Отчёт дописывает блок DaData справа от исходных данных. Если книгу
        # обогащали несколько раз, берём последний блок.
        starts = [i for i in range(len(headers)) if headers[i:i + 3] == ("Наименование", "ИНН", "КПП")]
        if not starts:
            raise ValueError("Не найден блок колонок DaData")
        start = starts[-1]
        end = headers.index("Штрафы", start) + 1 if "Штрафы" in headers[start:] else len(headers)
        query_col = (0 if headers[0] == "ИНН (запрос)" else enrich._find_inn_column(headers[:start]))
        if query_col is None:
            raise ValueError("Не найдена колонка исходного ИНН")

        for row in rows:
            values = row[start:end]
            if not values or all(value in (None, "") for value in values):
                continue
            label = str(values[0] or "").strip().upper()
            if label.startswith("ОШИБКА ЗАПРОСА"):
                continue
            inn = str(row[query_col] or "").strip().removesuffix(".0")
            if not enrich.INN_RE.fullmatch(inn):
                continue
            fields = {header: value for header, value in zip(headers[start:end], values) if header}
            yield enrich._normalize_inn(inn), label != "НЕ НАЙДЕНО", fields
    finally:
        workbook.close()


def _report_rows(paths: list[Path]):
    for path in paths:
        yield from _read_report(path)


def _report_date(key: str, fallback: float) -> str:
    for format_string in ("enrich_%d.%m.%y_%H%M%S_%f", "enrich_%d.%m.%y_%H%M%S"):
        try:
            return datetime.datetime.strptime(key, format_string).astimezone(datetime.UTC).isoformat()
        except ValueError:
            continue
    return datetime.datetime.fromtimestamp(fallback, datetime.UTC).isoformat()


def _remove_expired(path: Path, cutoff: float) -> None:
    try:
        if path.stat().st_mtime < cutoff:
            path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        logging.exception("[DaData] Не удалось удалить просроченный файл %s", path.name)


def maintain_files() -> None:
    """Импортирует прежние отчёты перед очисткой. Вызывается под блокировкой обогащения."""
    cutoff = time.time() - FILE_TTL_SEC
    dadata_store.init_db()
    processed = dadata_store.processed_reports()
    reports = defaultdict(list)
    for path in config.CONFIG_DIR.glob("enrich_*"):
        if path.suffix.lower() in {".xlsx", ".xlsm"} and not path.is_symlink() and path.is_file():
            reports[REPORT_TAIL.sub("", path.stem)].append(path)

    for key, files in reports.items():
        if key not in processed:
            full = next((path for path in files if path.stem == key), None)
            parts = sorted(path for path in files if "_часть_" in path.stem[len(key):])
            sources = [full] if full else parts
            try:
                report_at = _report_date(key, sources[0].stat().st_mtime) if sources else None
                dadata_store.store_report(key, report_at, _report_rows(sources))
            except sqlite3.Error:
                raise
            except Exception:
                logging.exception("[DaData] Не удалось импортировать комплект %s", key)
                continue
        try:
            expired = max(path.stat().st_mtime for path in files) < cutoff
        except FileNotFoundError:
            continue
        if expired:
            for path in files:
                _remove_expired(path, cutoff)

    uploads = config.CONFIG_DIR / "enrich_uploads"
    if uploads.is_dir() and uploads.resolve() == config.CONFIG_DIR.resolve() / uploads.name:
        for path in uploads.iterdir():
            if UPLOAD_NAME.fullmatch(path.name) and not path.is_symlink() and path.is_file():
                _remove_expired(path, cutoff)
