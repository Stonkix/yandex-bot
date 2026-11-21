"""
Тонкая обёртка над Bot API Яндекс Мессенджера: отправка текста/файлов,
получение обновлений (polling) и скачивание вложений.
Документация: https://yandex.ru/dev/messenger/doc/ru/
"""

import asyncio
import logging
import os

import aiohttp

from yandexbot.core import access
from yandexbot.core import config

YM_API_BASE = "https://botapi.messenger.yandex.net/bot/v1"
YM_MAX_LEN  = 4000  # с запасом от лимита API на длину текста
YM_MAX_FILE_BYTES = 51_000_000  # 51 МБ на вложение по данным Яндекс 360
PRICE_DELIVERY_RETRY_DELAYS = (2, 8)


def _resolve_targets(target: dict | list[dict] | None) -> list[dict]:
    """
    target=None            -> общая рассылка в чаты с флагом «Рассылка».
                               До сохранения чатов в панели возможен резерв из config.toml.
    target={"chat_id":...} -> ответ в конкретный групповой чат
    target={"login":...}   -> ответ конкретному человеку в личку (для приватных чатов,
                               где chat.id не осмысленный)
    target=[{...}, ...]    -> несколько получателей (чаты рассылки модуля);
                               пустой список — как None
    """
    if isinstance(target, list):
        if target:
            return target
        target = None
    if target:
        return [target]

    return access.broadcast_targets()


async def _post_delivery(session, url: str, **kwargs) -> tuple[str, bool, int]:
    try:
        async with session.post(url, **kwargs) as response:
            try:
                data = await response.json(content_type=None)
            except ValueError:
                data = None
            status = response.status
            if 200 <= status < 300 and isinstance(data, dict) and data.get("ok"):
                return "", False, 0
            description = data.get("description") if isinstance(data, dict) else None
            response_code = data.get("error_code") if isinstance(data, dict) else None
            if status < 400 and isinstance(response_code, int):
                status = response_code
            reason = f"HTTP {status}" + (f" — {description}" if description else "")
            retry_after = response.headers.get("Retry-After", "")
            delay = min(int(retry_after), 30) if retry_after.isascii() and retry_after.isdecimal() else 0
            retryable = status == 429 or 500 <= status < 600
            return reason, retryable, delay
    except (aiohttp.ClientError, TimeoutError) as error:
        return type(error).__name__, True, 0


async def _retry_delivery(send, delays: tuple[int, ...]) -> str:
    for pause in (*delays, None):
        reason, retryable, retry_after = await send()
        if not retryable or pause is None:
            return reason
        await asyncio.sleep(max(pause, retry_after))
    return reason


async def ym_send_text(text: str, target: dict | list[dict] | None = None,
                       suggest_buttons: dict | None = None, *, retry_delays: tuple[int, ...] = ()) -> list[str]:
    """
    target=None -> уходит всем broadcast-чатам (рассылка цен и т.п.).
    target={"chat_id"|"login": ...} -> ответ конкретно туда, откуда пришла команда.
    """
    headers = {
        "Authorization": f"OAuth {config.YANDEX_TOKEN}",
        "Content-Type":  "application/json",
    }
    url = f"{YM_API_BASE}/messages/sendText/"

    if len(text) <= YM_MAX_LEN:
        chunks = [text]
    else:
        chunks, chunk = [], ""
        for line in text.split("\n"):
            if len(chunk) + len(line) + 1 > YM_MAX_LEN:
                if chunk:
                    chunks.append(chunk)
                chunk = line
            else:
                chunk = f"{chunk}\n{line}" if chunk else line
        if chunk:
            chunks.append(chunk)

    targets = _resolve_targets(target)
    if not targets:
        logging.error("[ЯМ] Нет ни одного получателя (broadcast_chat_ids пуст и login не задан)")
        return ["нет получателей"]

    errors = []
    async with aiohttp.ClientSession() as session:
        for tgt in targets:
            for part in chunks:
                payload = {**tgt, "text": part}
                if suggest_buttons and part == chunks[-1]:
                    payload["suggest_buttons"] = suggest_buttons
                async def send():
                    return await _post_delivery(session, url, json=payload, headers=headers,
                                                timeout=aiohttp.ClientTimeout(total=30))

                error = await _retry_delivery(send, retry_delays)
                if error:
                    logging.error("[ЯМ] Ошибка отправки текста в %s: %s", tgt, error)
                    errors.append(error)
    return errors


def _upload_timeout(size_bytes: int) -> aiohttp.ClientTimeout:
    """Большие архивы грузятся долго: минимум 2 минуты плюс время на ~200 КБ/с."""
    return aiohttp.ClientTimeout(total=max(120, 60 + size_bytes / (200 * 1024)))


async def ym_send_file(filepath: str, caption: str | None = None,
                       target: dict | list[dict] | None = None, *, retry_delays: tuple[int, ...] = ()) -> list[str]:
    """Отправляет файл; возвращает список ошибок по получателям (пустой — всё дошло)."""
    headers = {"Authorization": f"OAuth {config.YANDEX_TOKEN}"}
    url = f"{YM_API_BASE}/messages/sendFile/"

    targets = _resolve_targets(target)
    if not targets:
        logging.error("[ЯМ] Нет ни одного получателя (broadcast_chat_ids пуст и login не задан)")
        return ["нет ни одного получателя"]

    filename = os.path.basename(filepath)
    size_bytes = os.path.getsize(filepath)
    errors: list[str] = []

    async with aiohttp.ClientSession() as session:
        for tgt in targets:
            async def send():
                form = aiohttp.FormData()
                for key, value in tgt.items():
                    form.add_field(key, value)
                if caption:
                    form.add_field("text", caption)
                with open(filepath, "rb") as document:
                    form.add_field("document", document, filename=filename,
                                   content_type="application/octet-stream")
                    return await _post_delivery(session, url, data=form, headers=headers,
                                                timeout=_upload_timeout(size_bytes))

            try:
                reason = await _retry_delivery(send, retry_delays)
            except OSError as error:
                reason = f"{type(error).__name__}: {error}"
            if reason:
                logging.error("[ЯМ] Ошибка отправки файла %s (%d КБ) в %s: %s",
                              filename, size_bytes // 1024, tgt, reason)
                errors.append(reason)
    return errors


async def ym_get_updates(session: aiohttp.ClientSession,
                         offset: int | None, limit: int = 10) -> dict:
    headers = {"Authorization": f"OAuth {config.YANDEX_TOKEN}"}
    params = {"limit": limit}
    if offset is not None:
        params["offset"] = offset
    url = f"{YM_API_BASE}/messages/getUpdates/"
    async with session.get(url, headers=headers, params=params,
                           timeout=aiohttp.ClientTimeout(total=30)) as resp:
        return await resp.json(content_type=None)


async def ym_download_file(session: aiohttp.ClientSession, file_id: str, dest_path: str):
    """Скачивает вложение чата (Update.file.id) по file_id в dest_path."""
    headers = {"Authorization": f"OAuth {config.YANDEX_TOKEN}"}
    url = f"{YM_API_BASE}/messages/getFile/"
    async with session.get(url, headers=headers, params={"file_id": file_id},
                           timeout=aiohttp.ClientTimeout(total=120)) as resp:
        resp.raise_for_status()
        with open(dest_path, "wb") as f:
            async for chunk in resp.content.iter_chunked(1 << 16):
                f.write(chunk)
