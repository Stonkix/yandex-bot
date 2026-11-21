"""
Общие мелкие утилиты, используемые и парсером цен, и обогащением ИНН.
"""

import tempfile
from contextlib import contextmanager
from pathlib import Path

from openpyxl.styles import Font, Alignment, PatternFill

# ─── Стили Excel (общие для всех отчётов) ─────────────────────────────────────

RED    = PatternFill("solid", start_color="FFC7CE", end_color="FFC7CE")
GREEN  = PatternFill("solid", start_color="C6EFCE", end_color="C6EFCE")
BOLD   = Font(bold=True)
CENTER = Alignment(horizontal="center", vertical="center")
LEFT   = Alignment(horizontal="left",   vertical="center")


@contextmanager
def staged_report(path: Path):
    """Готовый отчёт заменяет старый файл только после успешного сохранения."""
    with tempfile.TemporaryDirectory(prefix=".report_", dir=path.parent) as directory:
        staged = Path(directory) / path.name
        yield str(staged)
        staged.replace(path)


def apply_diff_fill(cell, old_price, new_price):
    """Подсветка изменения цены: рост — красным, падение — зелёным."""
    if old_price is None or new_price is None:
        return
    if new_price > old_price:
        cell.fill = RED
    elif new_price < old_price:
        cell.fill = GREEN


def short_error(e: Exception, limit: int = 300) -> str:
    text = " ".join(str(e).split())
    if len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def progress_text(label: str, done: int, total: int, region: str) -> str:
    pct = int(done / total * 100)
    filled = pct // 5
    bar = "█" * filled + "░" * (20 - filled)
    return f"{label}\n{bar} {pct}%  [{done}/{total}]\n📍 {region}"

# ─── История цен: плановые и ручные выгрузки ──────────────────────────────────

# Плановая выгрузка сравнивается только с прошлой плановой: иначе ручной запуск
# (команда чата, «Запуски» в панели) между ними забирал бы изменение цен себе,
# а плановая видела «без изменений» и молчала в чатах рассылки. Строки до
# появления колонки (run_kind IS NULL) считаются годными для обоих видов.
RUN_KIND_AUTO = "auto"
RUN_KIND_MANUAL = "manual"


def ensure_run_kind_column(conn, table: str) -> None:
    columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    if "run_kind" not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN run_kind TEXT")


def previous_run_filter(run_kind: str | None) -> tuple[str, tuple]:
    """Условие SQL для выбора прошлой выгрузки: ручная сравнивается с любой,
    плановая — только с плановой (или со старой, без отметки)."""
    if run_kind == RUN_KIND_AUTO:
        return " AND (run_kind IS NULL OR run_kind = ?)", (RUN_KIND_AUTO,)
    return "", ()
