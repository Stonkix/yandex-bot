"""Проверка адреса официального регионального прайс-листа Контур."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from yandexbot.modules import prices


class _NoLinksPage:
    def __init__(self):
        self.url = None
        self.request = None

    async def goto(self, url, **_kwargs):
        self.url = url

    async def evaluate(self, _script):
        return []


class KonturRegionalFilesTests(unittest.TestCase):
    def test_reports_missing_official_files_for_the_requested_region(self):
        page = _NoLinksPage()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "нет ссылок"):
                asyncio.run(prices.kontur_download_regional_price_files(
                    page, 1, Path(directory),
                ))
        self.assertEqual(page.url, "https://www.kontur-extern.ru/price-download/1")


if __name__ == "__main__":
    unittest.main()
