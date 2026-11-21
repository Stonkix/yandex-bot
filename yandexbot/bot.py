"""
Главный процесс бота.

Команды (в любом чате, где есть бот):
    /whoami                          — узнать свой логин/id и id/тип текущего чата
    /start                           — (только в личке) приветствие
    /статистика_обогатить            — (только админам) расход DaData по пользователям
    /лимиты [число]                  — (только админам) показать или установить лимит DaData на день
    /цены                            — запустить внеочередной парсинг цен СБИС + Контур
    /эпд-цены                        — выгрузить цены ЭПД СБИС и Контур.Логистики
    /обогатить                       — бот попросит прислать файл (.txt/.xlsx со столбцом "ИНН"),
                                        следующим сообщением пришлите файл — бот обогатит его через DaData

Доступ к командам:
    - админам (роль «Администратор» в админ-панели → Доступы) — всегда, в любом чате;
    - в групповых чатах с commands=true в chats.toml — /цены и /обогатить;
    - в личных сообщениях — только /обогатить для сотрудников из админ-панели → Доступы
      ([access] из config.toml переносится туда один раз при первом запуске).

Рассылка результатов /цены (плановая раз в 7 дней и ручная) уходит во все чаты
с broadcast=true в chats.toml (если файл пуст — в одиночный chat_id из config.toml,
для обратной совместимости).

Запуск (из корня проекта; основной режим — python main.py, см. main.py):
    python -m yandexbot.bot --daemon                        # постоянный процесс: слушает чат
                                                            # И сам следит за расписанием (раз в 7 дней)
    python -m yandexbot.bot --listen                        # только слушать чат, без автовыгрузки по расписанию
    python -m yandexbot.bot --source both                   # плановый (тихий) разовый запуск парсера цен
    python -m yandexbot.bot --source both --announce        # ручной разовый запуск — с сообщением о старте
    python -m yandexbot.bot --source both --announce --force  # ручной запуск без учёта недельного лимита
    python -m yandexbot.bot --source sbis / kontur / kontur-test
    python -m yandexbot.bot --check                         # проверка конфигурации
"""

import argparse
import asyncio
import datetime
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

from yandexbot.core import access
from yandexbot.core import config
from yandexbot.modules import prices
from yandexbot.modules import epd_prices
from yandexbot.modules import enrich
from yandexbot.admin import store as admin_store
from yandexbot.core.module_registry import BotModule, CommandContext, CommandSpec, ModuleRegistry, SettingField
from yandexbot.core import state
from yandexbot.core.state import prices_lock, epd_prices_lock, enrich_lock
from yandexbot.messenger.ym_api import YM_MAX_FILE_BYTES, ym_send_text, ym_send_file, ym_get_updates, ym_download_file
from yandexbot.core.utils import short_error, staged_report

# ─── Ожидание файла после /обогатить ──────────────────────────────────────────
# В Яндекс Мессенджере текст команды и прикреплённый файл часто приходят
# отдельными сообщениями (двумя Update), поэтому реализуем двухшаговый флоу:
# 1) пользователь пишет "/обогатить" -> бот отвечает "жду файл от <имя>"
#    и запоминает, от кого именно ждёт файл;
# 2) следующий файл именно от этого отправителя берём в работу; файлы
#    от других людей, пока идёт ожидание, игнорируем.
COMMAND_MODULES = ModuleRegistry()


def _sender_key(sender: dict) -> str:
    return sender.get("login") or sender.get("id") or "unknown"


def _enrich_request_key(chat: dict, sender: dict) -> tuple[str, str] | None:
    sender_id = sender.get("login") or sender.get("id")
    if not sender_id:
        return None
    chat_id = chat.get("id")
    if chat.get("type") == "private":
        return (f"private:{chat_id or sender_id}", str(sender_id))
    return (str(chat_id), str(sender_id)) if chat_id else None


# ─── Адресация ответов: в приватных чатах chat.id не осмысленный, отвечаем по login ─

def _reply_target(chat: dict, sender: dict) -> dict | None:
    if chat.get("type") == "private":
        login = sender.get("login")
        if login:
            return {"login": login}
    chat_id = chat.get("id")
    return {"chat_id": chat_id} if chat_id else None


def _prices_allowed_here(chat: dict, sender: dict) -> bool:
    """/цены: админам всегда, остальным — только в разрешённом групповом чате."""
    if access.is_admin(sender):
        return True
    return chat.get("type") != "private" and chat.get("id") in access.commands_allowed_chat_ids()


def _enrich_allowed_here(chat: dict, sender: dict) -> bool:
    """/обогатить: админам всегда; в личке — разрешённым логинам; в группах — по настройке."""
    if access.is_admin(sender):
        return True
    if chat.get("type") == "private":
        return access.is_employee(sender)
    return chat.get("id") in access.commands_allowed_chat_ids()


# ─── Смещение (offset) для polling чата ───────────────────────────────────────

def _load_offset() -> int | None:
    if not config.OFFSET_FILE.is_file():
        return None
    try:
        return int(config.OFFSET_FILE.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _save_offset(offset: int):
    try:
        with staged_report(config.OFFSET_FILE) as saved:
            Path(saved).write_text(str(offset), encoding="utf-8", newline="\n")
    except Exception as e:
        logging.error(f"Не удалось сохранить {config.OFFSET_FILE}: {e}")


# ─── Обработчик команды /цены ──────────────────────────────────────────────────

async def _handle_prices(display_name: str, target: dict | None):
    async with prices_lock:
        announce = f'🔄 Принял команду от "{display_name}", запускаю выгрузку СБИС + Контур…'
        try:
            # Выгрузка всех официальных региональных прайсов может занимать
            # больше 30 минут. Прогресс пишется в лог, а отмена по времени
            # здесь недопустима: иначе теряются уже скачанные источники.
            await prices.run("both", announce_text=announce, force=True, target=target,
                             profile="manual")
        except Exception as e:
            logging.error(f"Ошибка выгрузки по команде от '{display_name}': {e}", exc_info=True)
            await ym_send_text(f"❌ Ошибка выгрузки: {short_error(e)}", target=target)


async def _dispatch_prices(context: CommandContext) -> None:
    """Адаптер модуля prices: доступ и запуск команды определены в одном месте."""
    if not config.prices_module_settings()["enabled"]:
        await ym_send_text("⏸ Модуль цен временно отключён администратором.", target=context.target)
        return
    if not _prices_allowed_here(context.chat, context.sender):
        await ym_send_text("❌ В этом чате/у вас нет доступа к этой команде.", target=context.target)
        return
    _enqueue_price_command(context, "prices")


async def _handle_epd_prices(display_name: str, target: dict | None):
    async with epd_prices_lock:
        try:
            await epd_prices.run(
                target=target,
                announce_text=(
                    f'🔄 Принял команду от "{display_name}", запускаю выгрузку цен ЭПД…'
                ),
            )
        except Exception as error:
            logging.error("Ошибка выгрузки ЭПД: %s", error, exc_info=True)
            await ym_send_text(f"❌ Ошибка выгрузки ЭПД: {short_error(error)}", target=target)


async def _dispatch_epd_prices(context: CommandContext) -> None:
    if not config.epd_prices_module_settings()["enabled"]:
        await ym_send_text("⏸ Модуль цен ЭПД временно отключён администратором.", target=context.target)
        return
    if not _prices_allowed_here(context.chat, context.sender):
        await ym_send_text("❌ В этом чате/у вас нет доступа к этой команде.", target=context.target)
        return
    _enqueue_price_command(context, "epd_prices")


def _enqueue_price_command(context: CommandContext, kind: str) -> None:
    if context.update_id is None:
        handler = _handle_prices if kind == "prices" else _handle_epd_prices
        asyncio.create_task(handler(context.display_name, context.target))
        return
    admin_store.enqueue_chat_job(config.ADMIN_DB_PATH, context.update_id, kind, {
        "chat": context.chat, "sender": context.sender, "target": context.target,
        "display_name": context.display_name,
    })


COMMAND_MODULES.register_module(BotModule(
    code="prices", title="Цены", commands=frozenset(config.PRICE_COMMANDS),
    command=CommandSpec("prices", frozenset(config.PRICE_COMMANDS), lambda _context: True, _dispatch_prices),
    settings_schema=(
        SettingField("enabled", "Модуль включён", "checkbox", True),
        SettingField("auto.interval_days", "Интервал, дней", "number", 7, 1),
        SettingField("auto.at", "Время", "time", "02:30"),
        SettingField("auto.sbis_delay_sec", "Пауза СБИС ночью", "number", 1.0, 0),
        SettingField("auto.kontur_region_delay_sec", "Пауза Контур ночью", "number", 10.0, 0),
        SettingField("manual.main_files_only", "Для /цены: только два основных файла", "checkbox", True),
    ),
))

COMMAND_MODULES.register_module(BotModule(
    code="epd_prices", title="Цены ЭПД", commands=frozenset(config.EPD_PRICE_COMMANDS),
    command=CommandSpec(
        "epd_prices", frozenset(config.EPD_PRICE_COMMANDS), lambda _context: True,
        _dispatch_epd_prices,
    ),
    settings_schema=(SettingField("enabled", "Модуль включён", "checkbox", True),),
))


def _admin_job_target(job: dict) -> dict | None:
    if job["delivery"] == "chat":
        return {"chat_id": job["target_chat_id"]}
    if job["delivery"] == "admin":
        admins = access.admin_logins()
        return {"login": admins[0] if admins else config.YANDEX_LOGIN}
    return None


async def _run_admin_job(job: dict) -> tuple[str, str]:
    """Выполняет одно задание панели под той же блокировкой, что и команда из чата."""
    target = _admin_job_target(job)
    if (job["module_code"], job["kind"]) not in admin_store.RUN_KINDS:
        # Например, «archives/send», поставленное до удаления рассылки архивов:
        # иначе оно ушло бы в ветку полной выгрузки 1СО ниже.
        return "failed", f"Тип запуска больше не поддерживается: {job['module_code']}/{job['kind']}"
    if job["module_code"] == "epd_prices":
        async with epd_prices_lock:
            result = await epd_prices.run(
                target=target, announce_text="🔄 Запуск из админ-панели: выгрузка цен ЭПД…",
            )
        files = [result["file"]] if result["file"] else []
    else:
        async with prices_lock:
            result = await prices.run("both", announce=True, force=True, target=target, profile="manual")
        files = result["files"]
    collection = "успешно" if result["ok"] else "частично" if files else "ошибка"
    delivered = "подтверждена" if result.get("sent") else "с ошибками"
    if result.get("delivery_errors"):
        delivered += " · " + ", ".join(dict.fromkeys(result["delivery_errors"]))[:180]
    summary = f"Сбор цен — {collection}. Доставка — {delivered}.\n{result['summary']}"
    return ("success" if result["ok"] and result.get("sent") else "failed"), summary


async def _run_admin_queue():
    """Выполняет задания панели тем же процессом, что слушает мессенджер."""
    interrupted = admin_store.fail_interrupted_runs(config.ADMIN_DB_PATH)
    if interrupted:
        logging.warning("Задания панели, прерванные перезапуском: %s", interrupted)
    while True:
        state.heartbeat["queue"] = time.time()
        job = admin_store.claim_next_run(config.ADMIN_DB_PATH)
        if job is None:
            await asyncio.sleep(3)
            continue
        try:
            status, summary = await _run_admin_job(job)
            admin_store.finish_run(config.ADMIN_DB_PATH, job["id"], status, summary[:2000])
        except Exception as e:
            logging.error("Ошибка задания админ-панели: %s", e, exc_info=True)
            admin_store.finish_run(config.ADMIN_DB_PATH, job["id"], "failed", error=short_error(e))


# ─── Обработчик команды /обогатить ─────────────────────────────────────────────

def _save_enrichment_report(source: str, output: Path, results: list[dict]) -> None:
    # Панель должна видеть только полностью записанные файлы.
    with staged_report(output) as staged:
        if Path(source).suffix.lower() == ".txt":
            enrich.save_dadata_excel(results, staged)
        else:
            enrich.save_enriched_source_excel(source, staged, results)


def _prepare_enrichment_parts(output: Path, max_bytes: int) -> list[Path]:
    with tempfile.TemporaryDirectory(prefix=".enrich_parts_", dir=output.parent) as folder:
        staged = enrich.split_enrichment_report(output, Path(folder), max_bytes)
        parts = []
        for path in staged:
            destination = output.with_name(path.name)
            path.replace(destination)
            parts.append(destination)
        return parts


async def _send_enrichment_report(output: Path, caption: str, target: dict | None) -> str:
    size = output.stat().st_size
    if size <= YM_MAX_FILE_BYTES:
        errors = await ym_send_file(str(output), caption=caption, target=target)
        if not errors:
            return ""
        if size < YM_MAX_FILE_BYTES * 0.75 or not any("HTTP 413" in error for error in errors):
            return "⚠ Яндекс Мессенджер не подтвердил отправку файла."
        max_bytes = YM_MAX_FILE_BYTES // 2
    else:
        max_bytes = YM_MAX_FILE_BYTES

    try:
        parts = await asyncio.to_thread(_prepare_enrichment_parts, output, max_bytes)
    except (OSError, ValueError):
        logging.exception("[DaData] Не удалось разделить %s", output.name)
        return (
            f"📎 Файл весит {size / 1_000_000:.1f} МБ. "
            f"Лимит вложения бота — {YM_MAX_FILE_BYTES / 1_000_000:g} МБ."
        )

    unsuccessful = []
    for number, path in enumerate(parts, 1):
        errors = await ym_send_file(str(path), caption=f"Часть {number} из {len(parts)} · {caption}", target=target)
        if errors:
            unsuccessful.append(str(number))
    if unsuccessful:
        return f"⚠ Не подтверждена отправка частей Excel: {', '.join(unsuccessful)}."
    return f"📎 Активный лист отправлен частями ({len(parts)} файлов Excel)."


async def _handle_enrich(sender: dict, file_id: str, file_name: str, target: dict | None):
    display_name = sender.get("display_name") or sender.get("login") or "пользователь"
    if not config.enrich_module_settings()["enabled"]:
        await ym_send_text("⏸ Модуль обогащения временно отключён администратором.", target=target)
        return
    if not config.DADATA_TOKEN:
        await ym_send_text("❌ Токен DaData не настроен в config.toml ([dadata] token = ...)", target=target)
        return
    async with enrich_lock:
        tmp_dir = tempfile.mkdtemp(prefix="enrich_")
        ext = os.path.splitext(file_name)[1].lower()
        local_path = os.path.join(tmp_dir, f"input{ext}")

        try:
            async with aiohttp.ClientSession() as dl_session:
                await ym_download_file(dl_session, file_id, local_path)

            if ext == ".txt":
                inns, total_inn_values, invalid_inns = enrich.extract_inns_from_txt_with_stats(
                    Path(local_path).read_bytes()
                )
            elif ext in (".xlsx", ".xlsm"):
                inns, total_inn_values, invalid_inns = enrich.extract_inns_from_xlsx_with_stats(local_path)
            else:
                await ym_send_text(f'❌ Неподдерживаемый формат файла "{file_name}" — нужен .txt или .xlsx', target=target)
                return

            if not inns:
                await ym_send_text("❌ В файле не найдено ни одного корректного ИНН.", target=target)
                return

            usage = enrich._load_dadata_usage()
            daily_limit = config.enrich_module_settings()["daily_limit"]
            remaining_today = daily_limit - usage["count"]
            if remaining_today <= 0:
                await ym_send_text(f"❌ Дневной лимит DaData ({daily_limit}) уже исчерпан. Попробуйте завтра.", target=target)
                return

            to_process = inns[:remaining_today]
            skipped = len(inns) - len(to_process)
            # Повторы одного ИНН запрашиваются один раз: результат затем
            # проставляется во все строки исходной таблицы с этим ИНН.
            duplicate_inns = total_inn_values - invalid_inns - len(inns)

            settings = config.enrich_module_settings()
            eta = enrich.format_duration(
                enrich.estimate_seconds(len(to_process), settings["requests_per_second"])
            )
            await ym_send_text(
                f'🔎 Взял в работу файл "{file_name}" от "{display_name}": '
                f'{total_inn_values} ИНН, из них {invalid_inns} невалидных, '
                f'{duplicate_inns} дублей (заполню по одному запросу на ИНН). '
                f'В обработку попадёт {len(to_process)} уникальных ИНН, примерно {eta}.'
                + (f" ({skipped} отложено — не хватает дневного лимита)" if skipped else ""),
                target=target,
            )

            started_at = time.monotonic()
            sem = asyncio.Semaphore(enrich.DADATA_CONCURRENCY)
            rate_limiter = enrich.DaDataRateLimiter(settings["requests_per_second"])
            budget = enrich.DaDataBudget(remaining_today)
            results = []
            progress_step = 10_000
            logging.info(
                f"[DaData] Начато обогащение: {len(to_process)} ИНН, "
                f"лимит {settings['requests_per_second']} запросов/с"
            )
            async with aiohttp.ClientSession() as session:
                # Не создаём десятки тысяч задач сразу: большой файл обрабатываем
                # небольшими порциями, сохраняя общий лимит параллельных запросов.
                batch_size = 100
                for start in range(0, len(to_process), batch_size):
                    if not budget.remaining:
                        break
                    tasks = [
                        enrich.dadata_lookup(session, inn, sem, rate_limiter, budget)
                        for inn in to_process[start:start + batch_size]
                    ]
                    for coro in asyncio.as_completed(tasks):
                        result = await coro
                        if not result.get("deferred"):
                            results.append(result)
                    processed = len(results)
                    if processed % progress_step == 0 or processed == len(to_process):
                        logging.info(
                            f"[DaData] Прогресс: {processed}/{len(to_process)} ИНН"
                        )
                        if processed < len(to_process):
                            await ym_send_text(
                                f"⏳ Обогащение продолжается: {processed} из {len(to_process)} ИНН.",
                                target=target,
                            )

            usage["count"] += budget.used
            enrich._save_dadata_usage(usage)

            order = {inn: i for i, inn in enumerate(to_process)}
            results.sort(key=lambda r: order.get(r["inn"], 0))

            found = sum(1 for r in results if r.get("found"))
            failed = sum(1 for r in results if r.get("error"))
            not_found = len(results) - found - failed
            enrich.record_dadata_usage(sender, len(results) - failed)
            finished_inns = {r["inn"] for r in results if not r.get("error")}
            remaining_inns = [inn for inn in inns if inn not in finished_inns]
            skipped = len(remaining_inns) - failed
            logging.info(
                f"[DaData] Обогащение завершено: {len(results)} ИНН, "
                f"найдено {found}, не найдено {not_found}, ошибок {failed}, отложено {skipped}"
            )

            date_str = datetime.datetime.now().strftime("%d.%m.%y_%H%M%S_%f")
            out_ext = ".xlsm" if ext == ".xlsm" else ".xlsx"
            out_path = config.CONFIG_DIR / f"enrich_{date_str}{out_ext}"
            remainder = None
            if remaining_inns:
                remainder = out_path.with_name(f"{out_path.stem}_остаток.xlsx")
                with staged_report(remainder) as staged:
                    await asyncio.to_thread(enrich.save_remaining_inns, remaining_inns, Path(staged))

            logging.info("[DaData] Формирование отчёта %s", out_path.name)
            await asyncio.to_thread(_save_enrichment_report, local_path, out_path, results)
            size_bytes = out_path.stat().st_size
            logging.info("[DaData] Отчёт сохранён: %s (%d байт)", out_path.name, size_bytes)

            delivery_note = await _send_enrichment_report(
                out_path, f"ИНН: найдено {found}, не найдено {not_found}, ошибок {failed}", target,
            )

            remaining_left = daily_limit - usage["count"]
            elapsed = enrich.format_duration(time.monotonic() - started_at)
            msg = (
                f"{'⚠️' if failed else '✅'} Готово за {elapsed}. Обработано: {found + not_found} уникальных ИНН "
                f"(найдено {found}, не найдено {not_found}). Ошибок запросов: {failed}.\n"
                f"📊 Лимит DaData на сегодня: осталось {remaining_left} из {daily_limit}."
            )
            if duplicate_inns and ext in (".xlsx", ".xlsm"):
                msg += f"\n🔁 Дубли ({duplicate_inns}) заполнены теми же данными без повторных запросов."
            if skipped:
                msg += f"\n⏸ Отложено по дневному лимиту: {skipped} ИНН."
            if remainder is not None:
                remainder_errors = await ym_send_file(str(remainder), target=target)
                location = "Скачайте его в админ-панели → Отчёты → Обогащение ИНН." if remainder_errors else ""
                msg += (f"\n📎 Для продолжения запустите /обогатить и пришлите {remainder.name} "
                        f"({len(remaining_inns)} ИНН). {location}").rstrip()
            if delivery_note:
                msg += (f"\n{delivery_note}\nПолный файл {out_path.name} доступен после входа "
                        "в админ-панель → Отчёты → Обогащение ИНН. "
                        "Если у вас нет доступа, обратитесь к администратору.")
            await ym_send_text(msg, target=target)
        except Exception as e:
            logging.error(f"Ошибка обогащения по команде от '{display_name}': {e}", exc_info=True)
            await ym_send_text(f"❌ Ошибка обогащения: {short_error(e)}", target=target)
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)


async def _dispatch_enrich(context: CommandContext) -> None:
    """Адаптер модуля enrich сохраняет двухшаговый сценарий ожидания файла."""
    if not _enrich_allowed_here(context.chat, context.sender):
        await ym_send_text("❌ В этом чате/у вас нет доступа к этой команде.", target=context.target)
        return
    if not config.enrich_module_settings()["enabled"]:
        await ym_send_text("⏸ Модуль обогащения временно отключён администратором.", target=context.target)
        return
    key = _enrich_request_key(context.chat, context.sender)
    if key is None or context.target is None:
        return
    admin_store.expect_enrich_file(config.ADMIN_DB_PATH, key, time.time() + config.PENDING_ENRICH_TIMEOUT_SEC)
    await ym_send_text("📎 Жду файл от вас с колонкой «ИНН».", target=context.target)


COMMAND_MODULES.register_module(BotModule(
    code="enrich", title="Обогащение ИНН", commands=frozenset(config.ENRICH_COMMANDS),
    command=CommandSpec("enrich", frozenset(config.ENRICH_COMMANDS), lambda _context: True, _dispatch_enrich),
    settings_schema=(
        SettingField("enabled", "Модуль включён", "checkbox", True),
        SettingField("daily_limit", "Дневной лимит DaData", "number", 50000, 1),
    ),
))


# ─── Обработчик /whoami — узнать свой логин/id и id/тип текущего чата ─────────

async def _handle_whoami(chat: dict, sender: dict, target: dict | None):
    lines = [
        f"Логин: {sender.get('login') or '—'}",
        f"ID: {sender.get('id') or '—'}",
        f"Chat ID: {chat.get('id') or '—'}",
        f"Тип чата: {chat.get('type') or '—'}",
    ]
    if access.is_admin(sender):
        lines.append("Статус: 👑 админ")
    elif access.is_employee(sender):
        lines.append("Статус: ✅ разрешённый пользователь")
    await ym_send_text("🪪 " + "\n".join(lines), target=target)


# ─── Обработчик /start в личке ────────────────────────────────────────────────

def _available_commands(sender: dict) -> set[str]:
    """Команды из актуальных настроек модулей: панель может их переименовать."""
    commands = set(config.enrich_module_settings()["commands"])
    if access.is_admin(sender):
        commands |= set(config.prices_module_settings()["commands"])
        commands |= set(config.epd_prices_module_settings()["commands"])
        commands |= {config.ENRICH_STATS_COMMAND, config.DADATA_LIMIT_COMMAND}
    return commands


async def _handle_start(sender: dict, target: dict | None):
    if access.has_access(sender):
        commands = _available_commands(sender)
        await ym_send_text(
            "✅ Готов к работе. Доступные команды:\n"
            + "\n".join(sorted(commands)),
            target=target,
            suggest_buttons={
                "layout": "true",
                "persist": True,
                "buttons": [[{
                    "id": "enrich_dadata",
                    "title": "Обогатить (DaData)",
                    "directives": [{"type": "send_message", "text": "/обогатить"}],
                }]],
            },
        )
        return
    await ym_send_text(
        "👋 Привет! У вас пока нет доступа к командам в личных сообщениях. "
        "Попросите администратора добавить ваш логин в список разрешённых.",
        target=target,
    )


async def _handle_help(sender: dict, target: dict | None):
    commands = _available_commands(sender)
    await ym_send_text(
        "Доступные команды:\n"
        + "\n".join(sorted(commands)),
        target=target,
    )


async def _handle_enrich_stats(target: dict | None):
    rows = enrich.dadata_usage_report()
    if not rows:
        await ym_send_text("📊 Статистика обогащения пока пуста.", target=target)
        return
    lines = ["📊 Статистика обогащения DaData", "Логин — сегодня / неделя / месяц / всего"]
    lines.extend(
        f'{row["login"]} — {row["today"]} / {row["week"]} / {row["month"]} / {row["total"]}'
        for row in rows
    )
    await ym_send_text("\n".join(lines), target=target)


async def _handle_dadata_limit(command_text: str, target: dict | None, actor: str = ""):
    """Показывает или изменяет дневной лимит DaData по команде администратора."""
    parts = command_text.split()
    if len(parts) == 1:
        await ym_send_text(
            f"📊 Дневной лимит обогащения: {config.enrich_module_settings()['daily_limit']}.\n"
            "Чтобы изменить: /лимиты 75000",
            target=target,
        )
        return
    if len(parts) != 2:
        await ym_send_text("❌ Формат команды: /лимиты 75000", target=target)
        return
    try:
        limit = int(parts[1])
        if limit <= 0:
            raise ValueError
    except ValueError:
        await ym_send_text("❌ Лимит должен быть целым положительным числом.", target=target)
        return
    try:
        config.set_dadata_daily_limit(limit, actor=f"мессенджер: {actor}")
    except (OSError, ValueError, sqlite3.Error) as e:
        logging.error(f"Не удалось сохранить дневной лимит DaData: {e}")
        await ym_send_text("❌ Не удалось сохранить новый лимит. Проверьте логи бота.", target=target)
        return
    await ym_send_text(
        f"✅ Дневной лимит обогащения изменён на {limit}.",
        target=target,
    )


# ─── Прослушивание чата ────────────────────────────────────────────────────────

async def _process_update(upd: dict) -> None:
    chat   = upd.get("chat", {}) or {}
    sender = upd.get("from", {}) or {}
    target = _reply_target(chat, sender)

    file_info = upd.get("file")
    text_raw  = (upd.get("text") or "").strip()
    text      = text_raw.lower().rstrip("!.,;:")

    logging.info(
        f"[ЯМ] входящее: chat={chat.get('id')} type={chat.get('type')} "
        f"from={sender.get('login') or sender.get('id')} "
        f"text={text!r} file={bool(file_info)}"
    )

    display_name = sender.get("display_name") or sender.get("login") or "пользователь"
    skey = _sender_key(sender)

    # В личке до выдачи доступа отвечаем на любое входящее сообщение.
    if chat.get("type") == "private" and not access.has_access(sender):
        asyncio.create_task(_handle_start(sender, target))
        return

    # /whoami — доступно всем, в любом чате
    if text in config.WHOAMI_COMMANDS:
        asyncio.create_task(_handle_whoami(chat, sender, target))
        return

    # /start — только в личке
    if text in config.START_COMMANDS and chat.get("type") == "private":
        asyncio.create_task(_handle_start(sender, target))
        return

    if text in config.HELP_COMMANDS:
        if chat.get("type") == "private" and access.has_access(sender):
            asyncio.create_task(_handle_help(sender, target))
        return

    # /статистика_обогатить — только админам
    if text == config.ENRICH_STATS_COMMAND:
        if not access.is_admin(sender):
            asyncio.create_task(
                ym_send_text("❌ Эта команда доступна только админам.", target=target)
            )
            return
        asyncio.create_task(_handle_enrich_stats(target))
        return

    # /лимиты [число] — только админам
    if text == config.DADATA_LIMIT_COMMAND or text.startswith(config.DADATA_LIMIT_COMMAND + " "):
        if not access.is_admin(sender):
            asyncio.create_task(
                ym_send_text("❌ Эта команда доступна только админам.", target=target)
            )
            return
        asyncio.create_task(_handle_dadata_limit(text, target, skey))
        return

    module_context = CommandContext(
        chat=chat,
        sender=sender,
        target=target,
        text=text,
        display_name=display_name,
        update_id=upd["update_id"],
    )
    if text in config.prices_module_settings()["commands"]:
        await _dispatch_prices(module_context)
        return

    if text in config.epd_prices_module_settings()["commands"]:
        await _dispatch_epd_prices(module_context)
        return

    if text in config.enrich_module_settings()["commands"]:
        await _dispatch_enrich(module_context)
        return

    if isinstance(file_info, dict):
        request_key = _enrich_request_key(chat, sender)
        if (
            target and request_key and file_info.get("id") and file_info.get("name")
            and _enrich_allowed_here(chat, sender)
        ):
            admin_store.enqueue_chat_job(config.ADMIN_DB_PATH, upd["update_id"], "enrich", {
                "chat": chat, "sender": sender, "target": target, "file": {"id": file_info["id"], "name": file_info["name"]},
            }, request_key=request_key)
        return

    if text_raw and chat.get("type") == "private" and access.has_access(sender):
        asyncio.create_task(
            ym_send_text("❓ Неизвестная команда. Подробнее: /help или /помощь", target=target)
        )


async def listen_commands():
    """
    Слушает ВСЕ чаты, куда добавлен бот (не только один chat_id из config.toml).
    Кто что может — решает access.py:
      - /whoami           — всем, везде, всегда
      - /цены, /эпд-цены, /обогатить — в группах из chats.toml (commands=true), в личке —
                            только одобренным сотрудникам; админам — всегда и везде
       - /статистика_обогатить, /лимиты — только админам
    """
    offset = _load_offset()
    print(
        f"Слушаю команды {sorted(COMMAND_MODULES.commands() | config.ENRICH_COMMANDS)} "
        f"во всех чатах бота…", flush=True,
    )

    async with aiohttp.ClientSession() as session:
        while True:
            state.heartbeat["listen"] = time.time()
            try:
                data = await ym_get_updates(session, offset=offset, limit=10)
            except Exception as e:
                logging.error(f"[ЯМ] Ошибка getUpdates: {e}")
                await asyncio.sleep(5)
                continue

            if not data.get("ok"):
                logging.error(f"[ЯМ] getUpdates вернул ошибку: {data.get('description')}")
                await asyncio.sleep(5)
                continue

            updates = data.get("updates", [])
            for upd in updates:
                await _process_update(upd)
                offset = upd["update_id"] + 1
                _save_offset(offset)


            if not updates:
                await asyncio.sleep(1)


async def _listen_forever():
    while True:
        try:
            await listen_commands()
            logging.error("listen_commands() неожиданно завершился без исключения")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logging.error(f"listen: ошибка цикла: {e}", exc_info=True)
            print(f"listen: ошибка, перезапуск через 10с: {e}", flush=True)
        await asyncio.sleep(10)


async def _run_chat_jobs(kind: str) -> None:
    while True:
        try:
            job = admin_store.claim_chat_job(config.ADMIN_DB_PATH, kind)
        except sqlite3.Error:
            logging.exception("[ЯМ] Не удалось прочитать задания %s", kind)
            await asyncio.sleep(3)
            continue
        if job is None:
            await asyncio.sleep(1)
            continue
        update_id, payload = job
        ok = False
        try:
            sender, chat, target = payload["sender"], payload["chat"], payload["target"]
            if kind == "enrich" and _enrich_allowed_here(chat, sender):
                await _handle_enrich(sender, payload["file"]["id"], payload["file"]["name"], target)
            elif kind == "prices" and _prices_allowed_here(chat, sender) and config.prices_module_settings()["enabled"]:
                await _handle_prices(payload["display_name"], target)
            elif kind == "epd_prices" and _prices_allowed_here(chat, sender) and config.epd_prices_module_settings()["enabled"]:
                await _handle_epd_prices(payload["display_name"], target)
            else:
                await ym_send_text("⏸ Задание отменено: доступ или настройки модуля изменились.", target=target)
            ok = True
        except Exception:
            logging.exception("[ЯМ] Ошибка задания %s (%s)", update_id, kind)
        # При отмене процесса запись остаётся running и будет восстановлена на старте.
        while True:
            try:
                admin_store.finish_chat_job(config.ADMIN_DB_PATH, update_id, ok)
                break
            except sqlite3.Error:
                logging.exception("[ЯМ] Не удалось сохранить результат задания %s", update_id)
                await asyncio.sleep(3)


async def _listen_with_jobs() -> None:
    recovered = admin_store.recover_chat_jobs(config.ADMIN_DB_PATH)
    if recovered:
        logging.info("[ЯМ] Восстановлено заданий: %s", recovered)
    await asyncio.gather(_listen_forever(), *(_run_chat_jobs(kind) for kind in ("prices", "epd_prices", "enrich")))


async def _daemon_forever():
    """
    Постоянный режим для PM2/автозагрузки: слушает чат
    И сам следит за расписанием парсера цен (раз в 7 дней).
    """
    await asyncio.gather(
        _listen_with_jobs(),
        prices.scheduler_loop(),
        epd_prices.scheduler_loop(),
        _run_admin_queue(),
    )


# ─── Self-check / CLI ────────────────────────────────────────────────────────

def run_self_check():
    print(f"config: {config.CONFIG_FILE}")
    ok = True
    if not config.CONFIG_FILE.is_file():
        print("✗ config not found"); ok = False
    if not config.YANDEX_TOKEN:
        print("✗ yandex token empty"); ok = False
    if not access.broadcast_targets():
        print("⚠ получатели общей рассылки не настроены")
    if not config.DADATA_TOKEN:
        print("⚠ dadata token empty — /обогатить будет недоступна")
    admins = access.admin_logins()
    if not admins:
        print("⚠ в админ-панели нет ни одного администратора бота — /статистика_обогатить недоступна никому")
    else:
        print(f"✓ admins: {', '.join(admins)}")
    broadcast_ids = access.broadcast_chat_ids()
    cmd_ids = access.commands_allowed_chat_ids()
    print(f"✓ chats.toml: {broadcast_ids and len(broadcast_ids) or 0} чат(ов) на рассылку, "
          f"{len(cmd_ids)} чат(ов) с командами" if config.CHATS_FILE.is_file() else "⚠ chats.toml ещё не создан")
    employees = [row for row in admin_store.list_access(config.ADMIN_DB_PATH) if row["role"] == "employee"]
    print(f"✓ сотрудники: {len(employees)} пользователь(ей) с доступом в личке")
    print(f"✓ regions_sbis: {len(prices.REGIONS_SBIS)} регионов (встроено в файл)")
    print(f"✓ regions_kontur: {len(prices.REGIONS_KONTUR)} регионов (встроено в файл)")
    print("status:", "ok" if ok else "failed")
    return 0 if ok else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument(
        "--source",
        choices=["sbis", "kontur", "both", "kontur-test"],
        default="both",
        help="Какой источник парсить (по умолчанию: both)",
    )
    ap.add_argument(
        "--announce",
        action="store_true",
        help="Слать сообщение 'Запускаю выгрузку…' в начале. "
             "Использовать для ручного запуска. Для планового автозапуска не указывать.",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="Игнорировать недельный лимит между плановыми (без --announce) запусками.",
    )
    ap.add_argument(
        "--listen",
        action="store_true",
        help="Не парсить, а бесконечно слушать команды в чате (chat_id из конфига), "
             "без автовыгрузки по расписанию. Для расписания используйте --daemon.",
    )
    ap.add_argument(
        "--daemon",
        action="store_true",
        help="Постоянный режим для PM2/автозагрузки: слушает чат И сам проверяет "
             "расписание парсера цен (раз в 7 дней). Один процесс закрывает обе задачи.",
    )
    args = ap.parse_args()
    if args.check:
        raise SystemExit(run_self_check())

    if args.daemon:
        try:
            asyncio.run(_daemon_forever())
        except KeyboardInterrupt:
            pass
        raise SystemExit(0)

    if args.listen:
        try:
            asyncio.run(_listen_with_jobs())
        except KeyboardInterrupt:
            pass
        raise SystemExit(0)

    try:
        asyncio.run(prices.run(args.source, announce=args.announce, force=args.force))
    except Exception as e:
        logging.error(f"Необработанная ошибка: {e}", exc_info=True)
        try:
            asyncio.run(ym_send_text(f"❌ Критическая ошибка скрипта: {short_error(e)}"))
        except Exception:
            pass
        sys.exit(1)
