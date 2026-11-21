"""SQLite-хранилище админ-панели: настройки, доступы, чаты, задания и аудит."""

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit


class _ClosingConnection(sqlite3.Connection):
    """`with` для SQLite должен закрывать файл и на Windows, а не только commit."""

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=_ClosingConnection)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path: Path) -> None:
    with _connect(path) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS admin_users (
                id INTEGER PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_login_at TEXT
            );
            CREATE TABLE IF NOT EXISTS module_settings (
                module_code TEXT PRIMARY KEY,
                value_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                updated_by TEXT
            );
            CREATE TABLE IF NOT EXISTS chats (
                chat_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                broadcast INTEGER NOT NULL DEFAULT 0,
                commands INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS access_logins (
                login TEXT PRIMARY KEY,
                role TEXT NOT NULL CHECK(role IN ('admin', 'employee')),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS module_runs (
                id INTEGER PRIMARY KEY,
                module_code TEXT NOT NULL,
                kind TEXT NOT NULL,
                delivery TEXT NOT NULL,
                target_chat_id TEXT,
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'success', 'failed', 'cancelled')),
                requested_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                summary TEXT,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY,
                actor TEXT NOT NULL,
                action TEXT NOT NULL,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_jobs (
                update_id INTEGER PRIMARY KEY,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued',
                finished_at REAL
            );
            CREATE INDEX IF NOT EXISTS chat_jobs_ready ON chat_jobs(kind, status, update_id);
            CREATE TABLE IF NOT EXISTS enrich_pending (
                chat_key TEXT NOT NULL,
                sender_key TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY (chat_key, sender_key)
            );
            CREATE TABLE IF NOT EXISTS price_gaps (
                source TEXT NOT NULL,
                region_code TEXT NOT NULL,
                tariff_key TEXT NOT NULL,
                confirmed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (source, region_code, tariff_key)
            );
        """)


_CHATS_SAVED = "panel_chats_saved"


def _chats_saved(conn: sqlite3.Connection) -> bool:
    # В старой базе сохранение списка видно по записи аудита, даже если список пуст.
    return conn.execute(
        "SELECT 1 FROM meta WHERE key = ? "
        "UNION ALL SELECT 1 FROM audit_log WHERE action = 'chats_saved' LIMIT 1",
        (_CHATS_SAVED,),
    ).fetchone() is not None


def migrate_legacy(path: Path, chats: list[dict], admins: set[str], employees: set[str]) -> None:
    """Переносит прежние TOML-настройки, сохраняя правки панели."""
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        has_chats = conn.execute("SELECT 1 FROM chats LIMIT 1").fetchone()
        if _chats_saved(conn):
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (_CHATS_SAVED, _now()))
        elif not has_chats and chats:
            now = _now()
            conn.executemany("INSERT INTO chats(chat_id,title,broadcast,commands,updated_at) VALUES (?,?,?,?,?)",
                             [(c["id"], c.get("title") or c["id"], int(bool(c.get("broadcast"))), int(bool(c.get("commands"))), now) for c in chats])
    import_config_access(path, admins, employees)


_CONFIG_ACCESS_IMPORTED = "config_access_imported"


def import_config_access(path: Path, admins: set[str], employees: set[str]) -> int:
    """Один раз переносит [access] из config.toml в список доступов панели.

    После переноса доступами управляет только панель: config.toml больше не
    выдаёт прав, поэтому любой логин можно понизить или удалить. Роли
    сохраняются такими, какими они фактически действовали: админ из
    config.toml остаётся админом, даже если в панели он был сотрудником.
    """
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM meta WHERE key=?", (_CONFIG_ACCESS_IMPORTED,)).fetchone():
            return 0
        added = _merge_access(conn, admins, employees)
        conn.execute("INSERT INTO meta(key, value) VALUES (?, ?)", (_CONFIG_ACCESS_IMPORTED, _now()))
        _audit(conn, "система", "config_access_imported", {"count": added})
        return added


def add_access(path: Path, admins: set[str], employees: set[str], actor: str) -> int:
    """Добавляет логины к списку, не удаляя существующие (кнопка «Перенести» в панели)."""
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        added = _merge_access(conn, admins, employees)
        _audit(conn, actor, "access_added", {"count": added})
        return added


def _merge_access(conn: sqlite3.Connection, admins: set[str], employees: set[str]) -> int:
    now = _now()
    current = {row["login"]: row["role"] for row in conn.execute("SELECT login, role FROM access_logins")}
    changed = 0
    for login in sorted(admins):
        if current.get(login) != "admin":
            conn.execute(
                "INSERT INTO access_logins(login, role, updated_at) VALUES (?, 'admin', ?) "
                "ON CONFLICT(login) DO UPDATE SET role='admin', updated_at=excluded.updated_at",
                (login, now),
            )
            changed += 1
    for login in sorted(employees - admins):
        if login not in current:
            conn.execute("INSERT INTO access_logins(login, role, updated_at) VALUES (?, 'employee', ?)", (login, now))
            changed += 1
    return changed


def set_access(path: Path, login: str, role: str, actor: str) -> bool:
    """Выдаёт доступ одному логину или меняет его роль. True — логин новый."""
    if role not in {"admin", "employee"}:
        raise ValueError("Неизвестная роль")
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        is_new = conn.execute("SELECT 1 FROM access_logins WHERE login=?", (login,)).fetchone() is None
        conn.execute(
            "INSERT INTO access_logins(login, role, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(login) DO UPDATE SET role=excluded.role, updated_at=excluded.updated_at",
            (login, role, _now()),
        )
        _audit(conn, actor, "access_granted", {"login": login, "role": role})
        return is_new


def pending_config_access(path: Path, admins: set[str], employees: set[str]) -> dict[str, list[str]]:
    """Логины из config.toml, которых нет в списке панели (или с меньшей ролью)."""
    current = {row["login"]: row["role"] for row in list_access(path)}
    return {
        "admin": sorted(login for login in admins if current.get(login) != "admin"),
        "employee": sorted(login for login in employees - admins if login not in current),
    }


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def get_module_settings(path: Path, module_code: str) -> dict:
    init_db(path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT value_json FROM module_settings WHERE module_code = ?", (module_code,),
        ).fetchone()
    return json.loads(row["value_json"]) if row else {}


def save_module_settings(path: Path, module_code: str, value: dict, actor: str) -> None:
    init_db(path)
    serialized = json.dumps(value, ensure_ascii=False, sort_keys=True)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("""INSERT INTO module_settings(module_code, value_json, updated_at, updated_by)
                     VALUES (?, ?, ?, ?)
                     ON CONFLICT(module_code) DO UPDATE SET value_json=excluded.value_json,
                     updated_at=excluded.updated_at, updated_by=excluded.updated_by""",
                     (module_code, serialized, _now(), actor))
        _audit(conn, actor, "module_settings_saved", {"module": module_code})


def _audit(conn: sqlite3.Connection, actor: str, action: str, details: dict) -> None:
    conn.execute("INSERT INTO audit_log(actor, action, details_json, created_at) VALUES (?, ?, ?, ?)",
                 (actor, action, json.dumps(details, ensure_ascii=False), _now()))


def list_chats(path: Path) -> list[dict]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM chats ORDER BY title, chat_id").fetchall()
    return [dict(row) for row in rows]


def chat_settings(path: Path) -> tuple[list[dict], bool]:
    """Один снимок списка чатов и признака сохранения списка в панели."""
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN")
        rows = conn.execute("SELECT * FROM chats ORDER BY title, chat_id").fetchall()
        saved = _chats_saved(conn)
    return [dict(row) for row in rows], saved


def replace_chats(path: Path, chats: list[dict], actor: str) -> None:
    init_db(path)
    now = _now()
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM chats")
        conn.executemany("INSERT INTO chats(chat_id,title,broadcast,commands,updated_at) VALUES (?,?,?,?,?)",
                         [(c["chat_id"], c["title"], int(c["broadcast"]), int(c["commands"]), now) for c in chats])
        conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES (?, ?)", (_CHATS_SAVED, now))
        _audit(conn, actor, "chats_saved", {"count": len(chats)})


def list_access(path: Path) -> list[dict]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT login, role FROM access_logins ORDER BY role, login").fetchall()
    return [dict(row) for row in rows]


def replace_access(path: Path, rows: list[dict], actor: str) -> None:
    init_db(path)
    now = _now()
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM access_logins")
        conn.executemany("INSERT INTO access_logins(login,role,updated_at) VALUES (?,?,?)",
                         [(r["login"].lower(), r["role"], now) for r in rows])
        _audit(conn, actor, "access_saved", {"count": len(rows)})


def has_users(path: Path) -> bool:
    init_db(path)
    with _connect(path) as conn:
        return conn.execute("SELECT 1 FROM admin_users LIMIT 1").fetchone() is not None


def get_user(path: Path, username: str):
    init_db(path)
    with _connect(path) as conn:
        return conn.execute("SELECT * FROM admin_users WHERE username=?", (username,)).fetchone()


def create_user(path: Path, username: str, password_hash: str) -> None:
    init_db(path)
    with _connect(path) as conn:
        conn.execute("INSERT INTO admin_users(username,password_hash,created_at) VALUES (?,?,?)",
                     (username, password_hash, _now()))
        _audit(conn, username, "admin_user_created", {"username": username})


def mark_login(path: Path, username: str) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE admin_users SET last_login_at=? WHERE username=?", (_now(), username))


def update_password(path: Path, username: str, password_hash: str) -> None:
    init_db(path)
    with _connect(path) as conn:
        conn.execute("UPDATE admin_users SET password_hash=? WHERE username=?", (password_hash, username))
        _audit(conn, username, "password_changed", {"username": username})


# Какие задания панель умеет ставить в очередь: (module_code, kind) → подпись.
RUN_KINDS = {
    ("prices", "full"): "Цены 1СО: все регионы",
    ("epd_prices", "full"): "Цены ЭПД",
}
DELIVERIES = {"broadcast": "В чаты рассылки модуля", "admin": "Только администратору", "chat": "В выбранный чат"}


def enqueue_run(path: Path, module_code: str, kind: str, delivery: str,
                target_chat_id: str | None, actor: str) -> int:
    if (module_code, kind) not in RUN_KINDS:
        raise ValueError(f"Неизвестный тип запуска: {module_code}/{kind}")
    if delivery not in DELIVERIES or (delivery == "chat") != bool(target_chat_id):
        raise ValueError("Неверный способ доставки")
    init_db(path)
    with _connect(path) as conn:
        cursor = conn.execute("""INSERT INTO module_runs(module_code,kind,delivery,target_chat_id,status,requested_by,created_at)
                               VALUES (?,?,?,?, 'queued', ?, ?)""",
                              (module_code, kind, delivery, target_chat_id, actor, _now()))
        _audit(conn, actor, "run_enqueued", {"module": module_code, "run_id": cursor.lastrowid})
        return cursor.lastrowid


def list_runs(path: Path, limit: int = 50) -> list[dict]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM module_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def claim_next_run(path: Path):
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM module_runs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
        if row is None:
            return None
        conn.execute("UPDATE module_runs SET status='running',started_at=? WHERE id=?", (_now(), row["id"]))
        return dict(row)


def finish_run(path: Path, run_id: int, status: str, summary: str = "", error: str = "") -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE module_runs SET status=?,finished_at=?,summary=?,error=? WHERE id=?",
                     (status, _now(), summary, error, run_id))


def cancel_run(path: Path, run_id: int, actor: str) -> bool:
    """Отменяет задание, пока оно в очереди; уже запущенное не трогает."""
    init_db(path)
    with _connect(path) as conn:
        cursor = conn.execute(
            "UPDATE module_runs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
            (_now(), run_id),
        )
        if cursor.rowcount:
            _audit(conn, actor, "run_cancelled", {"run_id": run_id})
        return bool(cursor.rowcount)


def fail_interrupted_runs(path: Path) -> int:
    """При старте бота: задания в статусе running остались от прерванного процесса."""
    init_db(path)
    with _connect(path) as conn:
        cursor = conn.execute(
            "UPDATE module_runs SET status='failed', finished_at=?, error=? WHERE status='running'",
            (_now(), "Прервано перезапуском бота"),
        )
        return cursor.rowcount


def list_audit(path: Path, limit: int = 100) -> list[dict]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(row) for row in rows]


def expect_enrich_file(path: Path, key: tuple[str, str], expires: float) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM enrich_pending WHERE expires_at <= ?", (time.time(),))
        conn.execute("INSERT OR REPLACE INTO enrich_pending VALUES (?, ?, ?)", (*key, expires))


def enqueue_chat_job(path: Path, update_id: int, kind: str, payload: dict,
                     request_key: tuple[str, str] | None = None) -> bool:
    """Файл и погашение запроса на него записываются одной транзакцией."""
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM chat_jobs WHERE update_id=?", (update_id,)).fetchone():
            return False
        if request_key is not None:
            deleted = conn.execute(
                "DELETE FROM enrich_pending WHERE chat_key=? AND sender_key=? AND expires_at > ?",
                (*request_key, time.time()),
            )
            if not deleted.rowcount:
                return False
        cursor = conn.execute(
            "INSERT INTO chat_jobs (update_id, kind, payload) VALUES (?, ?, ?)",
            (update_id, kind, json.dumps(payload, ensure_ascii=False)),
        )
        return bool(cursor.rowcount)


def recover_chat_jobs(path: Path) -> int:
    init_db(path)
    with _connect(path) as conn:
        conn.execute("DELETE FROM chat_jobs WHERE finished_at < ?", (time.time() - 7 * 86400,))
        return conn.execute("UPDATE chat_jobs SET status='queued' WHERE status='running'").rowcount


def claim_chat_job(path: Path, kind: str) -> tuple[int, dict] | None:
    with _connect(path) as conn:
        row = conn.execute("""
            UPDATE chat_jobs SET status='running' WHERE update_id=(
                SELECT update_id FROM chat_jobs WHERE kind=? AND status='queued'
                ORDER BY update_id LIMIT 1
            ) RETURNING update_id, payload
        """, (kind,)).fetchone()
        return (row["update_id"], json.loads(row["payload"])) if row else None


def finish_chat_job(path: Path, update_id: int, ok: bool) -> None:
    with _connect(path) as conn:
        conn.execute("UPDATE chat_jobs SET status=?, payload='{}', finished_at=? WHERE update_id=?",
                     ("done" if ok else "failed", time.time(), update_id))


def reconcile_price_gaps(path: Path, source: str, missing: set[tuple[str, str]],
                         observed: set[tuple[str, str]], coverage_ok: bool) -> set[tuple[str, str]]:
    """Сохраняет пропуски для проверки и снимает подтверждение с вернувшейся цены."""
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        existing = {(row["region_code"], row["tariff_key"]): row["confirmed"] for row in conn.execute(
            "SELECT region_code, tariff_key, confirmed FROM price_gaps WHERE source=?", (source,),
        )}
        obsolete = observed & existing.keys()
        if coverage_ok:
            obsolete |= {key for key, confirmed in existing.items() if not confirmed and key not in missing}
        conn.executemany("DELETE FROM price_gaps WHERE source=? AND region_code=? AND tariff_key=?",
                         [(source, *key) for key in obsolete])
        if coverage_ok:
            conn.executemany("INSERT OR IGNORE INTO price_gaps(source, region_code, tariff_key) VALUES(?,?,?)",
                             [(source, *key) for key in missing])
        return {key for key, confirmed in existing.items() if confirmed and key not in observed}


def list_price_gaps(path: Path) -> list[dict]:
    init_db(path)
    with _connect(path) as conn:
        rows = conn.execute("""
            SELECT source, tariff_key, COUNT(*) AS regions, GROUP_CONCAT(region_code, ', ') AS region_codes
            FROM price_gaps WHERE confirmed=0
            GROUP BY source, tariff_key ORDER BY source, tariff_key
        """)
        return [dict(row) for row in rows]


def confirm_price_withdrawal(path: Path, source: str, tariff: str, region_codes: str, evidence: str, actor: str) -> int:
    hosts = {"sbis": ("saby.ru", "sbis.ru"), "kontur": ("kontur.ru", "kontur-extern.ru")}
    url = urlsplit(evidence)
    hostname = url.hostname or ""
    if (
        not tariff or len(evidence) > 1000 or url.scheme != "https" or url.username or url.password
        or not any(hostname == host or hostname.endswith(f".{host}") for host in hosts.get(source, ()))
    ):
        raise ValueError("Укажите HTTPS-ссылку на официальный источник выбранного поставщика.")
    displayed = {code.strip() for code in region_codes.split(",") if code.strip()}
    if len(displayed) > 100 or any(not code.isascii() or not code.isdecimal() or len(code) > 2 for code in displayed):
        raise ValueError("Некорректный список регионов.")
    init_db(path)
    with _connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = {row[0] for row in conn.execute("""
            SELECT region_code FROM price_gaps WHERE source=? AND tariff_key=? AND confirmed=0
        """, (source, tariff))}
        if not current or displayed != current:
            raise ValueError("Список пропусков изменился. Сверьте текущие регионы и подтвердите заново.")
        conn.execute("UPDATE price_gaps SET confirmed=1 WHERE source=? AND tariff_key=? AND confirmed=0", (source, tariff))
        _audit(conn, actor, "price_withdrawal_confirmed", {
            "source": source, "tariff": tariff, "regions": sorted(current), "evidence": evidence,
        })
        return len(current)
