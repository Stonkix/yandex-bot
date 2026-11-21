"""
Быстрая проверка парсинга Контур.Экстерн на 5 тестовых регионах (Москва, СПб,
Свердловская обл., Краснодарский край, Новосибирская обл.) — без записи в БД
цен и без отправки чего-либо в Яндекс Мессенджер. Результат сохраняется в
Excel в stat/.

Запуск из корня проекта:
    python scripts/kontur_live_test.py

Тест использует только TEST_REGIONS и не записывает цены в БД.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # корень проекта: пакет yandexbot

from yandexbot.core import config          # обязателен: читает config.toml, настраивает логирование
from yandexbot.modules import prices


async def main():
    print(
        "Запускаю Контур для 5 регионов: "
        + ", ".join(name for _, name in prices.TEST_REGIONS),
        flush=True,
    )
    result = await prices.run_kontur(prices.TEST_REGIONS, test=True)

    print("\n" + "=" * 60)
    if result["ok"]:
        print(f"OK: {result['summary']}")
        print(f"Файл: {result['file']}")
    else:
        print(f"ОШИБКА: {result['summary']}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
