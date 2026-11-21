"""Единая точка запуска: бот, планировщик, очередь панели и Flask-админка."""

import argparse
import asyncio
import logging
import threading

from werkzeug.serving import make_server

from yandexbot.admin import server as admin_server
from yandexbot import bot


class AdminWebThread(threading.Thread):
    """Flask работает в одном процессе с ботом, но не блокирует asyncio-цикл."""

    def __init__(self, host: str, port: int):
        super().__init__(name="admin-web", daemon=True)
        self.server = make_server(host, port, admin_server.create_app())

    def run(self) -> None:
        logging.info("Админ-панель запущена на http://%s:%s", *self.server.server_address)
        self.server.serve_forever()

    def stop(self) -> None:
        self.server.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Проверить конфигурацию бота")
    parser.add_argument("--admin-host", default="0.0.0.0", help="Адрес админ-панели в LAN")
    parser.add_argument("--admin-port", default=5001, type=int, help="Порт админ-панели")
    args = parser.parse_args()
    if args.check:
        return bot.run_self_check()

    web = AdminWebThread(args.admin_host, args.admin_port)
    web.start()
    print(f"Админ-панель: http://{args.admin_host}:{args.admin_port}", flush=True)
    try:
        asyncio.run(bot._daemon_forever())
    except KeyboardInterrupt:
        pass
    finally:
        web.stop()
        web.join(timeout=5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
