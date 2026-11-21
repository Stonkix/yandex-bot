"""Проверки Flask-панели и SQLite-хранилища."""

import re
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from werkzeug.security import generate_password_hash

from yandexbot.admin import server as admin_server
from yandexbot.admin import store as admin_store
from yandexbot.core import access, config

PASSWORD = "достаточно-длинный"
PAGES = ["/", "/modules", "/prices", "/epd-prices", "/enrich", "/chats", "/access", "/runs", "/reports", "/logs", "/account"]


def _csrf(response) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', response.get_data(as_text=True))
    assert match, "в форме нет CSRF-токена"
    return match.group(1)


class PanelTestCase(unittest.TestCase):
    """Панель на временных admin.db и stat/: рабочие файлы не трогаются."""

    def setUp(self):
        self._stack = ExitStack()
        self.dir = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        self.db = self.dir / "admin.db"
        for name, value in {
            "ADMIN_DB_PATH": self.db, "CONFIG_DIR": self.dir, "DB_PATH": self.dir / "prices.db",
            "LOG_FILE": self.dir / "bot_log.log", "STATE_FILE": self.dir / "last_run.json",
            "EPD_STATE_FILE": self.dir / "epd_last_run.json",
            "DADATA_USAGE_FILE": self.dir / "usage.json", "DADATA_STATS_FILE": self.dir / "stats.json",
            "CHATS_FILE": self.dir / "chats.toml",
            "ADMIN_LOGINS": {"boss"}, "ALLOWED_LOGINS": {"boss", "worker"},
        }.items():
            self._stack.enter_context(patch.object(config, name, value))
        self.app = admin_server.create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        self._stack.close()

    def login(self, password: str = PASSWORD):
        if not admin_store.has_users(self.db):
            admin_store.create_user(self.db, "admin", generate_password_hash(PASSWORD))
        token = _csrf(self.client.get("/login"))
        return self.client.post("/login", data={"username": "admin", "password": password, "csrf": token})

    def post(self, url: str, data: dict, page: str | None = None):
        token = _csrf(self.client.get(page or url))
        return self.client.post(url, data={**data, "csrf": token})


class SettingsRestartTests(PanelTestCase):
    def test_settings_access_and_explicit_empty_chats_survive_restart(self):
        from yandexbot.messenger import ym_api

        legacy = '[[chat]]\nid = "old-chat"\nbroadcast = true\ncommands = true\n'
        config.CHATS_FILE.write_text(legacy, encoding="utf-8", newline="\n")
        admin_server.create_app()
        self.assertEqual(access.broadcast_chat_ids(), ["old-chat"])
        self.login()
        self.post("/prices", {"enabled": "on", "auto_enabled": "on", "interval_days": "3", "at": "09:15",
                              "auto_sbis_delay_sec": "1", "auto_kontur_region_delay_sec": "10",
                              "manual_sbis_delay_sec": "0.3", "manual_kontur_region_delay_sec": "1",
                              "main_files_only": "on", "broadcast_chats": ["old-chat"]})
        self.post("/epd-prices", {"enabled": "on", "auto_enabled": "on", "interval_days": "4", "at": "11:20",
                                  "only_changes": "on", "broadcast_chats": ["old-chat"]})
        self.post("/enrich", {"enabled": "on", "daily_limit": "4321", "requests_per_second": "15"})
        self.post("/access", {"login": ["worker"], "role": ["employee"]})
        self.post("/chats", {})
        readers = (config.prices_module_settings, config.epd_prices_module_settings, config.enrich_module_settings)
        before = [reader() for reader in readers]
        self.assertEqual(before[0]["auto"]["interval_days"], 3)
        self.assertEqual(before[1]["auto"]["interval_days"], 4)
        self.assertEqual(before[2]["daily_limit"], 4321)
        self.assertEqual(before[1]["broadcast_chats"], ["old-chat"])
        with patch.object(config, "YANDEX_CHAT_ID", "fallback-chat"), patch.object(config, "YANDEX_LOGIN", "fallback"):
            admin_server.create_app()
            self.assertEqual(admin_store.list_chats(self.db), [])
            self.assertEqual(access.commands_allowed_chat_ids(), set())
            self.assertEqual(ym_api._resolve_targets([]), [])
        self.assertEqual([reader() for reader in readers], before)
        self.assertEqual(admin_store.list_access(self.db), [{"login": "worker", "role": "employee"}])
        self.assertTrue(admin_store.has_users(self.db))
        self.post("/epd-prices", {"enabled": "on", "auto_enabled": "on", "interval_days": "5", "at": "11:20"})
        self.assertEqual(config.epd_prices_module_settings()["auto"]["interval_days"], 5)
        self.assertEqual(config.epd_prices_module_settings()["broadcast_chats"], ["old-chat"])

    def test_upgrade_preserves_empty_chat_list_saved_before_new_marker(self):
        config.CHATS_FILE.write_text('[[chat]]\nid = "old-chat"\n', encoding="utf-8", newline="\n")
        admin_store.replace_chats(self.db, [], "test")
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("DELETE FROM meta WHERE key = 'panel_chats_saved'")
            connection.commit()
        finally:
            connection.close()
        admin_server.create_app()
        self.assertEqual(admin_store.chat_settings(self.db), ([], True))

    def test_invalid_legacy_chat_file_does_not_prevent_startup(self):
        config.CHATS_FILE.write_text("invalid = [", encoding="utf-8", newline="\n")
        admin_store.replace_chats(self.db, [], "test")
        with self.assertLogs(level="WARNING"):
            app = admin_server.create_app()
        self.assertEqual(app.test_client().get("/login").status_code, 200)


class AuthTests(PanelTestCase):
    def test_first_setup_creates_user_and_opens_dashboard(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        token = _csrf(self.client.get("/setup"))
        response = self.client.post("/setup", data={"password": PASSWORD, "password2": PASSWORD, "csrf": token},
                                    follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Обзор", response.get_data(as_text=True))
        self.assertTrue((self.dir / "admin_session_secret.txt").is_file())

    def test_every_page_opens_right_after_password_login(self):
        # Регрессия: после входа в сессии не было CSRF-токена, и все страницы отдавали 500.
        self.assertEqual(self.login().status_code, 302)
        for page in PAGES:
            with self.subTest(page=page):
                self.assertEqual(self.client.get(page).status_code, 200)

    def test_post_without_csrf_is_rejected(self):
        self.login()
        self.assertEqual(self.client.post("/prices", data={}).status_code, 403)

    def test_repeated_wrong_password_is_throttled(self):
        for _ in range(admin_server.LOGIN_ATTEMPTS):
            self.assertEqual(self.login("неверный-пароль").status_code, 200)
        self.assertEqual(self.login().status_code, 429)

    def test_password_change(self):
        self.login()
        response = self.post("/account", {"current_password": PASSWORD, "new_password": "новый-пароль-123",
                                          "new_password2": "новый-пароль-123"})
        self.assertEqual(response.status_code, 302)
        self.client.post("/logout", data={"csrf": _csrf(self.client.get("/account"))})
        self.assertEqual(self.login("новый-пароль-123").status_code, 302)


class SettingsTests(PanelTestCase):
    def test_module_command_conflict_is_reported_not_saved(self):
        self.login()
        self.post("/modules", {"module": "epd_prices", "enabled": "on", "commands": "/цены"})
        page = self.client.get("/modules").get_data(as_text=True)
        self.assertIn("уже назначена модулю", page)
        self.assertNotIn("/цены", config.epd_prices_module_settings()["commands"])

    def test_epd_module_can_be_disabled_from_panel(self):
        self.login()
        self.post("/modules", {"module": "epd_prices", "commands": "/эпд-цены"})
        self.assertFalse(config.epd_prices_module_settings()["enabled"])

    def test_epd_schedule_is_saved_from_panel(self):
        self.login()
        self.post("/epd-prices", {"enabled": "on", "auto_enabled": "on", "interval_days": "3", "at": "07:30"})
        self.assertEqual(config.epd_prices_module_settings()["auto"],
                         {"enabled": True, "interval_days": 3, "at": "07:30", "only_changes": False})
        self.post("/epd-prices", {"enabled": "on", "auto_enabled": "on", "interval_days": "3", "at": "07:30",
                                  "only_changes": "on"})
        self.assertTrue(config.epd_prices_module_settings()["auto"]["only_changes"])
        page = self.client.get("/epd-prices").get_data(as_text=True)
        self.assertIn('value="07:30"', page)
        self.post("/epd-prices", {"enabled": "on", "interval_days": "0", "at": "07:30"})
        self.assertIn("Интервал должен быть положительным", self.client.get("/epd-prices").get_data(as_text=True))

    def test_chats_are_saved_and_escaped(self):
        self.login()
        self.post("/chats", {"chat_id": ["0/0/1", "0/0/2"], "title": ["<b>Чат</b>", ""],
                             "broadcast": ["1", "0"], "commands": ["0", "1"]})
        chats = admin_store.list_chats(self.db)
        self.assertEqual([(chat["chat_id"], chat["title"]) for chat in chats],
                         [("0/0/2", "0/0/2"), ("0/0/1", "<b>Чат</b>")])
        self.assertIn("&lt;b&gt;Чат&lt;/b&gt;", self.client.get("/chats").get_data(as_text=True))

    def test_dadata_limit_from_messenger_and_panel_is_one_value(self):
        config.set_dadata_daily_limit(1234, actor="test")
        self.assertEqual(config.enrich_module_settings()["daily_limit"], 1234)


class ModuleBroadcastTests(PanelTestCase):
    """У «Цен 1СО» и «Цен ЭПД» свои списки чатов рассылки."""

    def setUp(self):
        super().setUp()
        admin_store.replace_chats(self.db, [
            {"chat_id": "0/0/general", "title": "Ботики", "broadcast": True, "commands": True},
            {"chat_id": "0/0/epd", "title": "Конкуренты ЭПД", "broadcast": True, "commands": False},
            {"chat_id": "0/0/1co", "title": "Метрики 1СО", "broadcast": True, "commands": False},
        ], "test")

    def test_default_is_all_chats_with_broadcast_flag(self):
        self.assertEqual(sorted(access.module_chat_ids("prices")), ["0/0/1co", "0/0/epd", "0/0/general"])
        self.assertEqual(sorted(access.module_chat_ids("epd_prices")), ["0/0/1co", "0/0/epd", "0/0/general"])

    def test_each_module_gets_its_own_chats(self):
        self.login()
        self.post("/epd-prices", {"enabled": "on", "interval_days": "7", "at": "09:00",
                                  "broadcast_chats": ["0/0/epd"]})
        self.post("/prices", {"enabled": "on", "auto_enabled": "on", "interval_days": "7", "at": "02:30",
                              "auto_sbis_delay_sec": "1", "auto_kontur_region_delay_sec": "10",
                              "manual_sbis_delay_sec": "0.3", "manual_kontur_region_delay_sec": "1",
                              "broadcast_chats": ["0/0/1co", "0/0/general"]})
        # Переключатель «только при изменении» не отмечен — сохраняется выключенным.
        self.assertFalse(config.prices_auto_schedule()["only_changes"])
        self.assertEqual(access.module_targets("epd_prices"), [{"chat_id": "0/0/epd"}])
        self.assertEqual(access.module_chat_ids("prices"), ["0/0/1co", "0/0/general"])
        page = self.client.get("/chats").get_data(as_text=True)
        self.assertIn("Цены ЭПД", page)
        # Смена команд на странице «Модули» не сбрасывает выбор чатов.
        self.post("/modules", {"module": "epd_prices", "enabled": "on", "commands": "/эпд-цены"})
        self.assertEqual(access.module_chat_ids("epd_prices"), ["0/0/epd"])

    def test_empty_selection_is_rejected(self):
        self.login()
        self.post("/epd-prices", {"enabled": "on", "interval_days": "7", "at": "09:00"})
        self.assertIn("Выберите хотя бы один чат", self.client.get("/epd-prices").get_data(as_text=True))
        self.assertIsNone(config.epd_prices_module_settings()["broadcast_chats"])

    def test_deleted_chat_falls_back_to_default_list(self):
        self.login()
        self.post("/epd-prices", {"enabled": "on", "interval_days": "7", "at": "09:00",
                                  "broadcast_chats": ["0/0/epd"]})
        admin_store.replace_chats(self.db, [
            {"chat_id": "0/0/general", "title": "Ботики", "broadcast": True, "commands": True},
        ], "test")
        self.assertEqual(access.module_chat_ids("epd_prices"), ["0/0/general"])

    def test_messenger_sends_to_every_module_chat(self):
        from yandexbot.messenger import ym_api

        targets = [{"chat_id": "0/0/epd"}, {"chat_id": "0/0/1co"}]
        self.assertEqual(ym_api._resolve_targets(targets), targets)
        self.assertEqual(ym_api._resolve_targets({"login": "a"}), [{"login": "a"}])
        # Пустой список — как раньше None: общая рассылка.
        self.assertEqual(len(ym_api._resolve_targets([])), 3)

    def test_picker_shows_current_selection(self):
        self.login()
        self.post("/epd-prices", {"enabled": "on", "interval_days": "7", "at": "09:00",
                                  "broadcast_chats": ["0/0/epd"]})
        page = self.client.get("/epd-prices").get_data(as_text=True)
        self.assertIn('value="0/0/epd" checked', page)
        self.assertNotIn('value="0/0/1co" checked', page)


class ArchivesPanelTests(PanelTestCase):
    def test_archive_is_listed_in_reports_and_downloadable(self):
        from yandexbot.modules import archives

        self.login()
        source = self.dir / "src"
        archives.keep_source(source, "sbis/api.json", "{}")
        archive = archives.make_archive("epd", source)
        name = f"archive/{archive.name}"
        page = self.client.get("/reports").get_data(as_text=True)
        self.assertIn("Архив исходников · ЭПД", page)
        self.assertIn(archive.name, page)
        response = self.client.get(f"/reports/{name}")
        self.assertEqual(response.status_code, 200)
        response.close()
        self.assertEqual(self.client.get("/reports/archive/..%2Fadmin.db").status_code, 404)

    def test_archive_sending_is_removed(self):
        self.login()
        self.assertEqual(self.client.get("/archives").status_code, 404)
        with self.assertRaises(ValueError):
            admin_store.enqueue_run(self.db, "archives", "send", "broadcast", None, "boss")


class AccessTests(PanelTestCase):
    """Доступами управляет только панель; [access] из config.toml переносится один раз."""

    def test_config_logins_are_imported_once_with_their_roles(self):
        roles = {row["login"]: row["role"] for row in admin_store.list_access(self.db)}
        self.assertEqual(roles, {"boss": "admin", "worker": "employee"})
        with patch.object(config, "ADMIN_LOGINS", {"boss", "new-admin"}):
            self.assertEqual(admin_store.import_config_access(self.db, config.ADMIN_LOGINS, config.ALLOWED_LOGINS), 0)
        self.assertNotIn("new-admin", {row["login"] for row in admin_store.list_access(self.db)})

    def test_config_admin_can_be_demoted_and_removed_in_panel(self):
        self.login()
        self.assertTrue(access.is_admin({"login": "boss"}))
        self.post("/access", {"login": ["boss"], "role": ["employee"]})
        self.assertFalse(access.is_admin({"login": "boss"}))
        self.assertTrue(access.is_employee({"login": "boss"}))
        self.assertFalse(access.is_employee({"login": "worker"}))

    def test_quick_grant_adds_login_and_changes_role(self):
        self.login()
        self.post("/access/add", {"login": " New.User@Company.ru ", "role": "employee"}, page="/access")
        self.assertTrue(access.is_employee({"login": "new.user@company.ru"}))
        self.post("/access/add", {"login": "new.user@company.ru", "role": "admin"}, page="/access")
        self.assertTrue(access.is_admin({"login": "new.user@company.ru"}))
        self.assertEqual(len(admin_store.list_access(self.db)), 3)

    def test_pending_config_logins_can_be_imported_from_panel(self):
        self.login()
        with patch.object(config, "ALLOWED_LOGINS", {"boss", "worker", "late"}):
            page = self.client.get("/access").get_data(as_text=True)
            self.assertIn("не действуют", page)
            self.assertIn("late", page)
            self.post("/access/import-config", {}, page="/access")
        self.assertTrue(access.is_employee({"login": "late"}))


class RunsAndReportsTests(PanelTestCase):
    def test_enqueue_and_cancel_run(self):
        self.login()
        self.post("/runs", {"job": "epd_prices:full", "delivery": "admin"})
        [run] = admin_store.list_runs(self.db)
        self.assertEqual((run["module_code"], run["status"]), ("epd_prices", "queued"))
        self.post(f"/runs/{run['id']}/cancel", {}, page="/runs")
        self.assertEqual(admin_store.list_runs(self.db)[0]["status"], "cancelled")

    def test_unknown_job_is_rejected(self):
        self.login()
        self.post("/runs", {"job": "prices:everything", "delivery": "admin"})
        self.assertEqual(admin_store.list_runs(self.db), [])

    def test_interrupted_runs_are_marked_failed(self):
        run_id = admin_store.enqueue_run(self.db, "prices", "full", "admin", None, "test")
        admin_store.claim_next_run(self.db)
        self.assertEqual(admin_store.fail_interrupted_runs(self.db), 1)
        [run] = admin_store.list_runs(self.db)
        self.assertEqual((run["id"], run["status"]), (run_id, "failed"))

    def test_report_download_only_for_listed_files(self):
        self.login()
        (self.dir / "epd_prices_18.09.26.xlsx").write_bytes(b"xlsx")
        (self.dir / "admin_session_secret.txt").write_text("secret", encoding="utf-8")
        response = self.client.get("/reports/epd_prices_18.09.26.xlsx")
        self.assertEqual((response.status_code, response.data), (200, b"xlsx"))
        response.close()
        self.assertEqual(self.client.get("/reports/admin_session_secret.txt").status_code, 404)
        self.assertEqual(self.client.get("/reports/..%2Fadmin.db").status_code, 404)

    def test_logs_page_filters_and_highlights_errors(self):
        self.login()
        config.LOG_FILE.write_text("2026-09-18 - старт\n2026-09-18 - Ошибка Контур: HTTP 403\n", encoding="utf-8")
        page = self.client.get("/logs?q=контур").get_data(as_text=True)
        self.assertIn('class="error">2026-09-18 - Ошибка Контур', page)
        self.assertNotIn("старт", page)


if __name__ == "__main__":
    unittest.main()
