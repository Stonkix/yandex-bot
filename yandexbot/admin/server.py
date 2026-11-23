"""Защищённая Flask-панель управления ботом для локальной сети.

Страницы рендерятся Jinja-шаблонами из templates/ (автоэкранирование),
оформление — static/admin.css. Задания запуска панель только ставит в
очередь admin.db; выполняет их основной процесс бота.
"""

import argparse
import datetime
import logging
import re
import secrets
import threading
import time
import tomllib

from flask import Flask, Response, abort, flash, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from yandexbot.admin import status
from yandexbot.admin import store as admin_store
from yandexbot.core import access as bot_access
from yandexbot.core import config
from yandexbot.modules import archives, dadata_store

MIN_PASSWORD_LENGTH = 10
LOGIN_ATTEMPTS = 5
LOGIN_LOCK_SEC = 600
LOGIN_RE = re.compile(r"^[\w.@+-]{2,100}$")
PUBLIC_ENDPOINTS = {"login", "setup", "static"}
ROLE_TITLES = {"employee": "Сотрудник", "admin": "Администратор"}
RUN_STATUS = {
    "queued": ("В очереди", "wait"),
    "running": ("Выполняется", "info"),
    "success": ("Готово", "ok"),
    "failed": ("Ошибка", "bad"),
    "cancelled": ("Отменено", "muted"),
}
MODULES = {
    "prices": ("Цены 1СО", "Выгрузка цен СБИС и Контур.Экстерн по регионам.",
               "prices_module_settings", "save_prices_module_settings"),
    "epd_prices": ("Цены ЭПД", "Пакеты и дополнения ЭПД СБИС и Контур.Логистики.",
                   "epd_prices_module_settings", "save_epd_prices_module_settings"),
    "enrich": ("Обогащение ИНН", "Обогащение файлов с ИНН через DaData.",
               "enrich_module_settings", "save_enrich_module_settings"),
}


class LoginThrottle:
    """Не больше LOGIN_ATTEMPTS неудачных входов с одного адреса за LOGIN_LOCK_SEC."""

    def __init__(self):
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, address: str) -> list[float]:
        border = time.monotonic() - LOGIN_LOCK_SEC
        failures = [moment for moment in self._failures.get(address, []) if moment > border]
        self._failures[address] = failures
        return failures

    def locked_for(self, address: str) -> int:
        with self._lock:
            failures = self._recent(address)
            if len(failures) < LOGIN_ATTEMPTS:
                return 0
            return int(failures[0] + LOGIN_LOCK_SEC - time.monotonic()) + 1

    def fail(self, address: str) -> None:
        with self._lock:
            self._recent(address).append(time.monotonic())

    def reset(self, address: str) -> None:
        with self._lock:
            self._failures.pop(address, None)


def _legacy_chats() -> list[dict]:
    if not config.CHATS_FILE.is_file():
        return []
    try:
        with open(config.CHATS_FILE, "rb") as stream:
            return [chat for chat in tomllib.load(stream).get("chat", []) if chat.get("id")]
    except (OSError, tomllib.TOMLDecodeError):
        logging.warning("Не удалось прочитать прежний список чатов %s", config.CHATS_FILE)
        return []


def _secret() -> str:
    path = config.CONFIG_DIR / "admin_session_secret.txt"
    if path.is_file():
        return path.read_text(encoding="utf-8").strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value, encoding="utf-8")
    return value


def _parse_dt(value) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        return value
    try:
        return datetime.datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _format_dt(value, with_seconds: bool = False) -> str:
    moment = _parse_dt(value)
    if moment is None:
        return "—"
    return moment.strftime("%d.%m.%Y %H:%M:%S" if with_seconds else "%d.%m.%Y %H:%M")


def _format_size(size: int) -> str:
    for unit in ("Б", "КБ", "МБ"):
        if size < 1024 or unit == "МБ":
            return f"{size:.0f} {unit}" if unit == "Б" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return str(size)


def _format_number(value) -> str:
    if value is None:
        return "—"
    number = float(value)
    text = f"{number:,.0f}" if number.is_integer() else f"{number:,.2f}"
    return text.replace(",", " ").replace(".", ",")


def _run_view(run: dict) -> dict:
    title, tone = RUN_STATUS.get(run["status"], (run["status"], "muted"))
    started, finished = _parse_dt(run.get("started_at")), _parse_dt(run.get("finished_at"))
    duration = ""
    if started:
        seconds = int(((finished or datetime.datetime.now()) - started).total_seconds())
        duration = f"{seconds // 60} мин {seconds % 60:02d} с" if seconds >= 60 else f"{seconds} с"
    delivery = admin_store.DELIVERIES.get(run["delivery"], run["delivery"])
    if run["delivery"] == "chat":
        delivery = access_chat_title(run.get("target_chat_id")) or run.get("target_chat_id") or delivery
    return {
        **run,
        "title": admin_store.RUN_KINDS.get((run["module_code"], run["kind"]), f"{run['module_code']}/{run['kind']}"),
        "status_title": title,
        "tone": tone,
        "duration": duration,
        "delivery_title": delivery,
        "result": run.get("error") or run.get("summary") or "",
    }


def access_chat_title(chat_id: str | None) -> str | None:
    for chat in admin_store.list_chats(config.ADMIN_DB_PATH):
        if chat["chat_id"] == chat_id:
            return chat["title"]
    return None


def _selected_chats(current: list[str] | None) -> list[str] | None:
    """Чаты рассылки из формы, если в панели есть список для выбора."""
    known = {chat["chat_id"] for chat in admin_store.list_chats(config.ADMIN_DB_PATH)}
    if not known:
        return current
    selected = request.form.getlist("broadcast_chats")
    if not selected:
        raise ValueError("Выберите хотя бы один чат для рассылки")
    if set(selected) - known:
        raise ValueError("В списке рассылки есть неизвестный чат — обновите страницу")
    return selected


def _chat_picker(module_code: str) -> dict:
    return {"chats": admin_store.list_chats(config.ADMIN_DB_PATH),
            "selected": set(bot_access.module_chat_ids(module_code))}


def _form_rows(*fields: str) -> list[dict]:
    """Строки динамической таблицы: одинаковые имена полей в порядке строк."""
    columns = [request.form.getlist(field) for field in fields]
    return [dict(zip(fields, values)) for values in zip(*columns)]


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=_secret(),
        SESSION_COOKIE_NAME="yandexbot_admin",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12),
    )
    admin_store.init_db(config.ADMIN_DB_PATH)
    admin_store.migrate_legacy(config.ADMIN_DB_PATH, _legacy_chats(), config.ADMIN_LOGINS, config.ALLOWED_LOGINS)
    throttle = LoginThrottle()
    db = config.ADMIN_DB_PATH
    archives.migrate_old_archives()

    app.jinja_env.filters.update(dt=_format_dt, size=_format_size, num=_format_number)

    @app.before_request
    def protect():
        # Токен создаётся на любом запросе, в том числе сразу после входа:
        # раньше его не было в сессии после логина, и все страницы падали с 500.
        session.setdefault("csrf", secrets.token_urlsafe(32))
        if request.method == "POST" and not secrets.compare_digest(
            request.form.get("csrf", ""), session["csrf"],
        ):
            abort(403)
        if request.endpoint in PUBLIC_ENDPOINTS:
            return None
        if not session.get("user"):
            return redirect(url_for("login" if admin_store.has_users(db) else "setup"))
        return None

    @app.context_processor
    def layout_context():
        return {"csrf": session.get("csrf"), "user": session.get("user"), "bot_status": status.bot_status()}

    def _start_session(username: str) -> None:
        session.clear()
        session.permanent = True
        session["user"] = username
        session["csrf"] = secrets.token_urlsafe(32)

    # ─── Вход ─────────────────────────────────────────────────────────────

    @app.route("/setup", methods=["GET", "POST"])
    def setup():
        if admin_store.has_users(db):
            return redirect(url_for("login"))
        if request.method == "POST":
            password = request.form.get("password", "")
            if len(password) < MIN_PASSWORD_LENGTH:
                flash(f"Пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов.", "error")
            elif password != request.form.get("password2", password):
                flash("Пароли не совпадают.", "error")
            else:
                admin_store.create_user(db, "admin", generate_password_hash(password))
                _start_session("admin")
                return redirect(url_for("dashboard"))
        return render_template("auth.html", mode="setup", title="Первичная настройка")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if session.get("user"):
            return redirect(url_for("dashboard"))
        if request.method == "POST":
            address = request.remote_addr or "unknown"
            wait = throttle.locked_for(address)
            if wait:
                flash(f"Слишком много неудачных попыток. Повторите через {wait // 60 + 1} мин.", "error")
                return render_template("auth.html", mode="login", title="Вход"), 429
            user = admin_store.get_user(db, request.form.get("username", "").strip())
            if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
                throttle.reset(address)
                _start_session(user["username"])
                admin_store.mark_login(db, user["username"])
                return redirect(url_for("dashboard"))
            throttle.fail(address)
            logging.warning("Админ-панель: неудачный вход с %s", address)
            flash("Неверный логин или пароль.", "error")
        return render_template("auth.html", mode="login", title="Вход")

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ─── Обзор ────────────────────────────────────────────────────────────

    @app.get("/")
    def dashboard():
        runs = [_run_view(run) for run in admin_store.list_runs(db, 6)]
        chats = admin_store.list_chats(db)
        access_rows = admin_store.list_access(db)
        return render_template(
            "dashboard.html", title="Обзор",
            schedule=status.prices_schedule(),
            epd_schedule=status.epd_schedule(),
            modules={code: getattr(config, getter)() for code, (_, _, getter, _) in MODULES.items()},
            module_titles={code: item[0] for code, item in MODULES.items()},
            last_prices=status.last_fetch("prices"),
            last_epd=status.last_fetch("epd_prices"),
            usage=status.dadata_usage(),
            runs=runs,
            reports=status.list_reports(6),
            chat_counts={"prices": len(bot_access.module_chat_ids("prices")),
                         "epd": len(bot_access.module_chat_ids("epd_prices")),
                         "commands": sum(chat["commands"] for chat in chats), "total": len(chats)},
            access_counts={"admin": sum(row["role"] == "admin" for row in access_rows),
                           "employee": sum(row["role"] == "employee" for row in access_rows)},
            auto_refresh=15 if any(run["status"] in {"queued", "running"} for run in runs) else None,
        )

    # ─── Настройки ────────────────────────────────────────────────────────

    @app.route("/modules", methods=["GET", "POST"])
    def modules():
        if request.method == "POST":
            code = request.form.get("module")
            if code not in MODULES:
                abort(400)
            title, _, getter, saver = MODULES[code]
            try:
                current = getattr(config, getter)()
                getattr(config, saver)({
                    **current, "enabled": "enabled" in request.form,
                    "commands": request.form.get("commands", ""),
                }, session["user"])
                flash(f"Модуль «{title}» сохранён.", "success")
            except ValueError as error:
                flash(f"«{title}»: {error}", "error")
            return redirect(url_for("modules"))
        cards = [
            {"code": code, "title": title, "description": description, "settings": getattr(config, getter)()}
            for code, (title, description, getter, _) in MODULES.items()
        ]
        return render_template("modules.html", title="Модули", cards=cards)

    @app.route("/prices", methods=["GET", "POST"])
    def prices():
        if request.method == "POST":
            form = request.form
            try:
                current = config.prices_module_settings()
                config.save_prices_module_settings({
                    **current,
                    "enabled": "enabled" in form,
                    "auto": {
                        "enabled": "auto_enabled" in form, "interval_days": form["interval_days"],
                        "at": form["at"], "only_changes": "only_changes" in form,
                        "sbis_delay_sec": form["auto_sbis_delay_sec"],
                        "kontur_region_delay_sec": form["auto_kontur_region_delay_sec"],
                        "kontur_region_delay_max_sec": form.get("auto_kontur_region_delay_max_sec", form["auto_kontur_region_delay_sec"]),
                    },
                    "manual": {
                        "sbis_delay_sec": form["manual_sbis_delay_sec"],
                        "kontur_region_delay_sec": form["manual_kontur_region_delay_sec"],
                        "kontur_region_delay_max_sec": form.get("manual_kontur_region_delay_max_sec", form["manual_kontur_region_delay_sec"]),
                        "main_files_only": "main_files_only" in form,
                    },
                    "broadcast_chats": _selected_chats(current["broadcast_chats"]),
                }, session["user"])
                flash("Настройки выгрузки цен сохранены.", "success")
            except (KeyError, ValueError) as error:
                flash(f"Ошибка: {error}", "error")
            return redirect(url_for("prices"))
        return render_template(
            "prices.html", title="Цены 1СО",
            settings=config.prices_module_settings(), schedule=status.prices_schedule(),
            last_prices=status.last_fetch("prices"), picker=_chat_picker("prices"),
            price_gaps=admin_store.list_price_gaps(db),
        )

    @app.post("/prices/withdrawals")
    def prices_withdrawals():
        try:
            count = admin_store.confirm_price_withdrawal(
                db, request.form.get("source", ""), request.form.get("tariff", ""),
                request.form.get("region_codes", ""), request.form.get("evidence", "").strip(), session["user"],
            )
            flash(f"Снятие тарифа подтверждено для {count} регионов. История обновится при следующей выгрузке.", "success")
        except ValueError as error:
            flash(str(error), "error")
        return redirect(url_for("prices"))

    @app.route("/epd-prices", methods=["GET", "POST"])
    def epd_prices_page():
        if request.method == "POST":
            form = request.form
            try:
                current = config.epd_prices_module_settings()
                config.save_epd_prices_module_settings({
                    **current,
                    "enabled": "enabled" in form,
                    "auto": {"enabled": "auto_enabled" in form, "interval_days": form["interval_days"],
                             "at": form["at"], "only_changes": "only_changes" in form},
                    "broadcast_chats": _selected_chats(current["broadcast_chats"]),
                }, session["user"])
                flash("Настройки выгрузки цен ЭПД сохранены.", "success")
            except (KeyError, ValueError) as error:
                flash(f"Ошибка: {error}", "error")
            return redirect(url_for("epd_prices_page"))
        return render_template(
            "epd_prices.html", title="Цены ЭПД",
            settings=config.epd_prices_module_settings(), schedule=status.epd_schedule(),
            last_epd=status.last_fetch("epd_prices"), picker=_chat_picker("epd_prices"),
        )

    @app.route("/enrich", methods=["GET", "POST"])
    def enrich_page():
        if request.method == "POST":
            try:
                config.save_enrich_module_settings({
                    **config.enrich_module_settings(),
                    "enabled": "enabled" in request.form,
                    "daily_limit": request.form["daily_limit"],
                    "requests_per_second": request.form["requests_per_second"],
                }, session["user"])
                flash("Настройки обогащения сохранены.", "success")
            except (KeyError, ValueError) as error:
                flash(f"Ошибка: {error}", "error")
            return redirect(url_for("enrich_page"))
        return render_template(
            "enrich.html", title="Обогащение ИНН",
            settings=config.enrich_module_settings(), usage=status.dadata_usage(),
            by_user=status.dadata_by_user(),
        )

    @app.get("/dadata")
    def dadata_page():
        dadata_store.init_db()
        totals = dadata_store.summary()
        last_response = _parse_dt(totals["last_response"])
        return render_template("dadata.html", title="База DaData", totals=totals,
                               last_response=last_response.astimezone() if last_response else None)

    @app.get("/dadata/export.csv")
    def dadata_export():
        dadata_store.init_db()
        return Response(dadata_store.csv_rows(), content_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": 'attachment; filename="dadata.csv"',
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })

    @app.route("/chats", methods=["GET", "POST"])
    def chats():
        if request.method == "POST":
            rows = [
                {"chat_id": row["chat_id"].strip(), "title": row["title"].strip() or row["chat_id"].strip(),
                 "broadcast": row["broadcast"] == "1", "commands": row["commands"] == "1"}
                for row in _form_rows("chat_id", "title", "broadcast", "commands")
                if row["chat_id"].strip()
            ]
            if len({row["chat_id"] for row in rows}) != len(rows):
                flash("ID чатов не должны повторяться.", "error")
            else:
                admin_store.replace_chats(db, rows, session["user"])
                flash(f"Чаты сохранены: {len(rows)}.", "success")
            return redirect(url_for("chats"))
        chat_modules: dict[str, list[str]] = {}
        for code, title in (("prices", "Цены 1СО"), ("epd_prices", "Цены ЭПД")):
            for chat_id in bot_access.module_chat_ids(code):
                chat_modules.setdefault(chat_id, []).append(title)
        return render_template("chats.html", title="Чаты", chats=admin_store.list_chats(db),
                               chat_modules=chat_modules)

    @app.route("/access", methods=["GET", "POST"])
    def access():
        if request.method == "POST":
            rows = [
                {"login": row["login"].strip().lower(), "role": row["role"]}
                for row in _form_rows("login", "role") if row["login"].strip()
            ]
            bad = [row["login"] for row in rows if not LOGIN_RE.fullmatch(row["login"])]
            if any(row["role"] not in ROLE_TITLES for row in rows):
                abort(400)
            if bad:
                flash(f"Некорректный логин: {', '.join(bad)}", "error")
            elif len({row["login"] for row in rows}) != len(rows):
                flash("Логины не должны повторяться.", "error")
            else:
                admin_store.replace_access(db, rows, session["user"])
                flash(f"Доступы сохранены: {len(rows)}.", "success")
            return redirect(url_for("access"))
        return render_template(
            "access.html", title="Доступы", rows=admin_store.list_access(db),
            pending=admin_store.pending_config_access(db, config.ADMIN_LOGINS, config.ALLOWED_LOGINS),
        )

    @app.post("/access/add")
    def access_add():
        login = request.form.get("login", "").strip().lower()
        role = request.form.get("role", "")
        if role not in ROLE_TITLES:
            abort(400)
        if not LOGIN_RE.fullmatch(login):
            flash(f"Некорректный логин: {login or 'пусто'}", "error")
        elif admin_store.set_access(db, login, role, session["user"]):
            flash(f"Доступ выдан: {login} — {ROLE_TITLES[role]}.", "success")
        else:
            flash(f"Роль изменена: {login} — {ROLE_TITLES[role]}.", "success")
        return redirect(url_for("access"))

    @app.post("/access/import-config")
    def access_import_config():
        added = admin_store.add_access(db, config.ADMIN_LOGINS, config.ALLOWED_LOGINS, session["user"])
        flash(f"Перенесено из config.toml: {added}.", "success")
        return redirect(url_for("access"))

    # ─── Работа ───────────────────────────────────────────────────────────

    @app.route("/runs", methods=["GET", "POST"])
    def runs():
        if request.method == "POST":
            module_code, _, kind = request.form.get("job", "").partition(":")
            delivery = request.form.get("delivery", "")
            target = request.form.get("target") if delivery == "chat" else None
            try:
                run_id = admin_store.enqueue_run(db, module_code, kind, delivery, target, session["user"])
                flash(f"Задание №{run_id} поставлено в очередь.", "success")
            except ValueError as error:
                flash(str(error), "error")
            return redirect(url_for("runs"))
        items = [_run_view(run) for run in admin_store.list_runs(db, 50)]
        return render_template(
            "runs.html", title="Запуски", runs=items,
            kinds=admin_store.RUN_KINDS, deliveries=admin_store.DELIVERIES,
            chats=admin_store.list_chats(db),
            bot_running=bool(status.bot_status()),
            auto_refresh=10 if any(run["status"] in {"queued", "running"} for run in items) else None,
        )

    @app.post("/runs/<int:run_id>/cancel")
    def cancel_run(run_id: int):
        if admin_store.cancel_run(db, run_id, session["user"]):
            flash(f"Задание №{run_id} отменено.", "success")
        else:
            flash(f"Задание №{run_id} уже выполняется или завершено — отменить нельзя.", "error")
        return redirect(url_for("runs"))

    @app.get("/reports")
    def reports():
        items = status.list_reports()
        kinds = sorted({item["kind"] for item in items})
        kind = request.args.get("kind", "")
        if kind:
            items = [item for item in items if item["kind"] == kind]
        return render_template("reports.html", title="Отчёты", reports=items, kinds=kinds, kind=kind)

    @app.get("/reports/<path:name>")
    def download_report(name: str):
        path = status.report_path(name)
        if path is None:
            abort(404)
        try:
            return send_file(path, as_attachment=True, download_name=path.name)
        except FileNotFoundError:
            abort(404)

    @app.get("/logs")
    def logs():
        try:
            lines = min(max(int(request.args.get("lines", 300)), 50), 2000)
        except ValueError:
            lines = 300
        query = request.args.get("q", "").strip()
        rows = [{"text": line, "error": status.is_error_line(line)} for line in status.log_tail(lines, query)]
        rows.reverse()
        return render_template(
            "logs.html", title="Журнал", log_rows=rows, lines=lines, query=query,
            audit=admin_store.list_audit(db, 100),
        )

    @app.route("/account", methods=["GET", "POST"])
    def account():
        if request.method == "POST":
            user = admin_store.get_user(db, session["user"])
            new = request.form.get("new_password", "")
            if not user or not check_password_hash(user["password_hash"], request.form.get("current_password", "")):
                flash("Текущий пароль указан неверно.", "error")
            elif len(new) < MIN_PASSWORD_LENGTH:
                flash(f"Новый пароль должен содержать не менее {MIN_PASSWORD_LENGTH} символов.", "error")
            elif new != request.form.get("new_password2", ""):
                flash("Новые пароли не совпадают.", "error")
            else:
                admin_store.update_password(db, session["user"], generate_password_hash(new))
                flash("Пароль изменён.", "success")
            return redirect(url_for("account"))
        return render_template("account.html", title="Аккаунт", account=admin_store.get_user(db, session["user"]))

    # ─── Ошибки ───────────────────────────────────────────────────────────

    @app.errorhandler(403)
    def forbidden(_error):
        return render_template(
            "error.html", title="Форма устарела",
            message="Страница была открыта слишком давно или в другой вкладке вышли из панели. "
                    "Обновите страницу и повторите действие.",
        ), 403

    @app.errorhandler(404)
    def not_found(_error):
        return render_template("error.html", title="Не найдено", message="Такой страницы или файла нет."), 404

    @app.errorhandler(500)
    def server_error(_error):
        return render_template(
            "error.html", title="Ошибка панели",
            message="Что-то пошло не так. Подробности записаны в журнал бота (stat/bot_log.log).",
        ), 500

    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5001)
    args = parser.parse_args()
    create_app().run(host=args.host, port=args.port, debug=False)
