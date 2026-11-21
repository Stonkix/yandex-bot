r"""Живой тест выгрузки публичных цен ЭПД — обёртка над CLI модуля epd_prices.

Сообщения в Яндекс Мессенджер не отправляются, история в БД по умолчанию не пишется.

Запуск из корня проекта:
    $env:PYTHONUTF8='1'
    .\venv\Scripts\python.exe scripts\epd_prices_live_test.py
    .\venv\Scripts\python.exe scripts\epd_prices_live_test.py --source kontur
Аргументы: --source sbis|kontur|both, --save-db, --no-excel, --out файл.xlsx.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта: пакет yandexbot

from yandexbot.modules import epd_prices


if __name__ == "__main__":
    raise SystemExit(asyncio.run(epd_prices._cli()))
