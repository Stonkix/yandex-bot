"""
Тонкая обёртка над Bot API Яндекс Мессенджера: отправка текста/файлов,
получение обновлений (polling) и скачивание вложений.
Документация: https://yandex.ru/dev/messenger/doc/ru/
"""

import logging
import os

import aiohttp

from yandexbot.core import access
from yandexbot.core import config

YM_API_BASE = "https://botapi.messenger.yandex.net/bot/v1"
YM_MAX_LEN  = 4000  # с запасом от лимита API на длину текста
YM_MAX_FILE_BYTES = 51_000_000  # 51 МБ на вложение по данным Яндекс 360


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


async def ym_send_text(text: str, target: dict | list[dict] | None = None,
                       suggest_buttons: dict | None = None) -> list[str]:
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
                try:
                    async with session.post(url, json=payload, headers=headers,
                                            timeout=aiohttp.ClientTimeout(total=30)) as resp:
                        try:
                            data = await resp.json(content_type=None)
                        except ValueError:
                            data = None
                        if 200 <= resp.status < 300 and isinstance(data, dict) and data.get("ok"):
                            continue
                        description = data.get("description") if isinstance(data, dict) else None
                        error = f"HTTP {resp.status}"
                        if description:
                            error += f" — {description}"
                        logging.error("[ЯМ] Ошибка отправки текста в %s: %s", tgt, error)
                        errors.append(error)
                except Exception as e:
                    logging.error(f"[ЯМ] Ошибка отправки текста в {tgt}: {e}")
                    errors.append(type(e).__name__)
    return errors


def _upload_timeout(size_bytes: int) -> aiohttp.ClientTimeout:
    """Большие архивы грузятся долго: минимум 2 минуты плюс время на ~200 КБ/с."""
    return aiohttp.ClientTimeout(total=max(120, 60 + size_bytes / (200 * 1024)))


async def ym_send_file(filepath: str, caption: str | None = None,
                       target: dict | list[dict] | None = None) -> list[str]:
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
            form = aiohttp.FormData()
            for k, v in tgt.items():
                form.add_field(k, v)
            if caption:
                form.add_field("text", caption)
            try:
                with open(filepath, "rb") as document:
                    form.add_field("document", document, filename=filename,
                                   content_type="application/octet-stream")
                    async with session.post(url, data=form, headers=headers,
                                            timeout=_upload_timeout(size_bytes)) as resp:
                        try:
                            data = await resp.json(content_type=None)
                        except ValueError:
                            data = None
                        if 200 <= resp.status < 300 and isinstance(data, dict) and data.get("ok"):
                            continue
                        description = data.get("description") if isinstance(data, dict) else None
                        reason = f"HTTP {resp.status}"
                        if description:
                            reason += f" — {description}"
            except Exception as e:
                reason = f"{type(e).__name__}: {e}".rstrip(": ")
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
