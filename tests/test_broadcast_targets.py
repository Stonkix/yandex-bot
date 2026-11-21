"""Итог выгрузки уходит в чаты, выбранные в панели на момент отправки.

Регрессия: список получателей фиксировался при старте выгрузки. Выгрузка 1СО
идёт до часа, и если в это время снять галочку с чата в панели, итог всё
равно уходил в него.
"""

import asyncio
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from yandexbot.admin import store as admin_store
from yandexbot.core import config
from yandexbot.modules import epd_prices, prices

CHATS = [
    {"chat_id": "0/0/general", "title": "Ботики", "broadcast": True, "commands": True},
    {"chat_id": "0/0/epd", "title": "Конкуренты ЭПД", "broadcast": True, "commands": False},
    {"chat_id": "0/0/1co", "title": "Метрики 1СО", "broadcast": True, "commands": False},
]


class BroadcastTargetTests(unittest.TestCase):
    def setUp(self):
        self._stack = ExitStack()
        directory = Path(self._stack.enter_context(tempfile.TemporaryDirectory()))
        for name, value in {
            "ADMIN_DB_PATH": directory / "admin.db", "CONFIG_DIR": directory,
            "DB_PATH": directory / "prices.db", "STATE_FILE": directory / "last_run.json",
            "EPD_STATE_FILE": directory / "epd_last_run.json",
        }.items():
            self._stack.enter_context(patch.object(config, name, value))
        admin_store.replace_chats(config.ADMIN_DB_PATH, CHATS, "test")
        self.sent_text = self._stack.enter_context(patch.object(prices, "ym_send_text", new=AsyncMock()))
        self.sent_file = self._stack.enter_context(patch.object(prices, "ym_send_file", new=AsyncMock()))

    def tearDown(self):
        self._stack.close()

    def _choose_prices_chats(self, chat_ids: list[str]) -> None:
        config.save_prices_module_settings(
            {**config.prices_module_settings(), "broadcast_chats": chat_ids}, "test",
        )

    def test_selection_changed_during_run_is_respected(self):
        async def slow_sbis(_delay, **_kwargs):
            # Пока идёт выгрузка, администратор снимает галочку с «Конкуренты ЭПД».
            self._choose_prices_chats(["0/0/1co"])
            return {"ok": True, "summary": "СБИС: ок", "file": "sbis.xlsx"}

        with patch.object(prices, "run_sbis", new=slow_sbis):
            asyncio.run(prices.run("sbis", force=True))

        summary_targets = self.sent_text.await_args_list[-1].kwargs["target"]
        self.assertEqual(summary_targets, [{"chat_id": "0/0/1co"}])
        self.assertEqual(self.sent_file.await_args.kwargs["target"], [{"chat_id": "0/0/1co"}])

    def test_command_reply_stays_in_command_chat(self):
        async def sbis(_delay, **_kwargs):
            return {"ok": True, "summary": "СБИС: ок", "file": None}

        self._choose_prices_chats(["0/0/1co"])
        with patch.object(prices, "run_sbis", new=sbis):
            asyncio.run(prices.run("sbis", announce_text="старт", force=True, target={"chat_id": "0/0/epd"}))
        self.assertTrue(all(call.kwargs["target"] == {"chat_id": "0/0/epd"}
                            for call in self.sent_text.await_args_list))

    def test_epd_uses_selection_at_send_time(self):
        rows = [epd_prices._package_row(1000, 5000.0, epd_prices.SBIS_URL, "")]

        async def slow_collect(*_args, **_kwargs):
            config.save_epd_prices_module_settings(
                {**config.epd_prices_module_settings(), "broadcast_chats": ["0/0/epd"]}, "test",
            )
            return {"results": {"epd_sbis": rows}, "errors": {}, "notes": []}

        with patch.object(epd_prices, "collect", new=slow_collect), \
                patch.object(epd_prices, "ym_send_text", new=AsyncMock()) as sent, \
                patch.object(epd_prices, "ym_send_file", new=AsyncMock()) as sent_file:
            asyncio.run(epd_prices.run())
        self.assertEqual(sent.await_args.kwargs["target"], [{"chat_id": "0/0/epd"}])
        self.assertEqual(sent_file.await_args.kwargs["target"], [{"chat_id": "0/0/epd"}])


if __name__ == "__main__":
    unittest.main()
