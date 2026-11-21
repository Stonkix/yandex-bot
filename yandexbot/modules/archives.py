"""Архивы исходников выгрузок.

Первая полная выгрузка за календарный месяц складывает в ZIP в stat/archive/ Excel с ценами
СБИС и Контура (в корне архива) и скачанные исходники поставщиков (ответы API,
страницы, PDF/DOCX — папки sbis/ и kontur/):

- epd_sources_<ГГГГММ>.zip    — ЭПД;
- prices_sources_<ГГГГММ>.zip — Отчётность (цены 1СО).

В чаты архивы не отправляются: их скачивают в админ-панели → «Отчёты».
"""

import datetime
import logging
import os
import shutil
import tempfile
from pathlib import Path

from yandexbot.core import config

# kind → подпись и префиксы файлов (первый — для новых архивов).
ARCHIVE_KINDS = {
    "epd": {"title": "ЭПД", "prefixes": ("epd_sources_",)},
    # kontur_sources_ — старые архивы 1СО только с Контуром.
    "prices": {"title": "Отчётность (цены 1СО)", "prefixes": ("prices_sources_", "kontur_sources_")},
}
ARCHIVE_SUFFIX = ".zip"


def archive_dir() -> Path:
    path = config.CONFIG_DIR / "archive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def kind_of(name: str) -> str | None:
    for kind, item in ARCHIVE_KINDS.items():
        if name.startswith(item["prefixes"]) and name.endswith(ARCHIVE_SUFFIX):
            return kind
    return None


def _monthly_path(kind: str, run_date: datetime.date) -> Path:
    prefix = ARCHIVE_KINDS[kind]["prefixes"][0]
    return config.CONFIG_DIR / "archive" / f"{prefix}{run_date:%Y%m}{ARCHIVE_SUFFIX}"


def needs_archive(kind: str, run_date: datetime.date) -> bool:
    return not _monthly_path(kind, run_date).is_file()


def make_archive(kind: str, source_dir: Path, extra_files: list | tuple = (), *,
                 run_date: datetime.date | None = None) -> Path | None:
    """Упаковывает исходники выгрузки и extra_files (Excel с ценами) в корень ZIP.

    None — архив месяца уже сохранён или упаковывать нечего.
    """
    path = _monthly_path(kind, run_date or datetime.date.today())
    if path.is_file():
        return None
    for extra in extra_files:
        if extra and Path(extra).is_file():
            source_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extra, source_dir / Path(extra).name)
    if not source_dir.is_dir() or not any(path.is_file() for path in source_dir.rglob("*")):
        return None
    with tempfile.TemporaryDirectory(prefix=".packing_", dir=archive_dir()) as directory:
        staged = shutil.make_archive(str(Path(directory) / "sources"), "zip", source_dir)
        try:
            # Публикуем готовый ZIP атомарно. Параллельный запуск или CLI
            # не перезапишет уже сохранённый архив месяца.
            os.link(staged, path)
        except FileExistsError:
            return None
    logging.info("Архив исходников %s: %s", kind, path.name)
    return path


def keep_source(sources_dir: Path | None, relative: str, data: bytes | str) -> None:
    """Сохраняет скачанный исходник для архива; без sources_dir ничего не делает."""
    if sources_dir is None:
        return
    path = sources_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8", newline="\n")
    else:
        path.write_bytes(data)


def migrate_old_archives() -> int:
    """Переносит архивы, лежащие прямо в stat/, в stat/archive/."""
    moved = 0
    for path in config.CONFIG_DIR.glob(f"*{ARCHIVE_SUFFIX}"):
        if path.is_file() and kind_of(path.name):
            target = archive_dir() / path.name
            if not target.exists():
                shutil.move(str(path), target)
                moved += 1
    if moved:
        logging.info("Перенесено архивов в %s: %s", archive_dir(), moved)
    return moved


def list_archives(kind: str | None = None) -> list[Path]:
    """Архивы в stat/archive/, новые сверху."""
    items = [path for path in archive_dir().glob(f"*{ARCHIVE_SUFFIX}")
             if path.is_file() and (kind_of(path.name) == kind if kind else kind_of(path.name))]
    return sorted(items, key=lambda path: path.stat().st_mtime, reverse=True)
