"""Telegram review bot with an evening three-post plan and next-day publishing.

At 21:00 London time the owner prepares three drafts sequentially. Approved
drafts remain durable in PostgreSQL and publish automatically the next day.
LLM generation still starts only after the owner presses "Создать пост".
"""
import asyncio
import fcntl
import json
import logging
import re
import secrets
import sys
import tempfile
import time
import uuid
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from telegram import (
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db, generator, ingest, publisher

LOGGER = logging.getLogger("repost.bot")
PLATFORM_LABELS = {"linkedin": "LinkedIn", "twitter": "X", "threads": "Threads"}
_SEND_LOCK = asyncio.Lock()
_SYNC_LOCK = asyncio.Lock()
_DELIVERY_LOCK = asyncio.Lock()
_LAST_SEND_AT = 0.0
_BOT_USERNAME: str | None = None
_STAGING_WAITERS: dict[str, asyncio.Future] = {}
_STAGING_PREFIX = "repost-staging:"
_STARTUP_RECOVERY_NOTICE_KEY = "startup_recovery_notice_pending"
_BOT_PROCESS_LOCK = None
NEW_POST_BUTTON = "✍️ Создать пост"
STATS_BUTTON = "📊 Статус"


def _is_owner(update: Update) -> bool:
    chat_id = getattr(update.effective_chat, "id", None)
    return bool(config.OWNER_CHAT_ID) and chat_id == config.OWNER_CHAT_ID


def _public_error_text(error: BaseException) -> str:
    """Return a useful error without leaking credentials or token-bearing URLs."""
    message = str(error).strip() or type(error).__name__
    for secret in (
        config.BOT_TOKEN,
        config.OPENAI_API_KEY,
        config.BUFFER_TOKEN,
        config.TELEGRAM_SESSION_STRING,
    ):
        if secret:
            message = message.replace(secret, "[secret]")
    message = re.sub(
        r"https://api\.telegram\.org/bot[^/\s]+",
        "https://api.telegram.org/bot[secret]",
        message,
    )
    return message[:350]


async def _send(call, *args, **kwargs):
    """Respect Telegram's per-chat pace and retry explicit flood limits."""
    global _LAST_SEND_AT
    async with _SEND_LOCK:
        wait = config.BOT_SEND_DELAY - (time.monotonic() - _LAST_SEND_AT)
        if wait > 0:
            await asyncio.sleep(wait)
        while True:
            for value in kwargs.values():
                if hasattr(value, "seek"):
                    try:
                        value.seek(0)
                    except (OSError, ValueError):
                        pass
            try:
                result = await call(*args, **kwargs)
                _LAST_SEND_AT = time.monotonic()
                return result
            except RetryAfter as exc:
                retry_after = exc.retry_after
                seconds = (
                    retry_after.total_seconds()
                    if hasattr(retry_after, "total_seconds")
                    else float(retry_after)
                )
                await asyncio.sleep(seconds + 0.5)


def _draft_body(draft) -> str:
    return (draft["edited_text"] or draft["linkedin_text"] or "").strip()


def _thread_items_for_draft(draft) -> list[str]:
    raw = draft["threads_json"] if "threads_json" in draft.keys() else None
    if raw:
        try:
            parsed = json.loads(raw)
            items = [str(item).strip() for item in parsed if str(item).strip()]
            if items:
                return items
        except (TypeError, ValueError):
            LOGGER.warning("invalid threads_json draft_id=%s", draft["id"])
    return publisher.split_for_thread(_draft_body(draft), config.THREAD_ITEM_CHARS)


def _threads_preview(draft) -> str:
    items = _thread_items_for_draft(draft)
    total = len(items)
    parts = [f"🧵 Threads preview · {total} частей · до {config.THREAD_ITEM_CHARS} символов"]
    for index, item in enumerate(items, start=1):
        parts.append(f"{index}/{total} · {len(item)} chars\n{item}")
    return "\n\n".join(parts)


def _message_media_file_id(message, media_kind: str) -> str | None:
    """Extract the reusable Bot API file_id from a delivered Telegram message."""
    if media_kind == "photo" and getattr(message, "photo", None):
        return message.photo[-1].file_id
    media = getattr(message, media_kind, None)
    return getattr(media, "file_id", None)


def _remember_bot_media(conn, post, file_id: str | None) -> None:
    if not file_id:
        return
    access_token = post["media_access_token"] or secrets.token_urlsafe(32)
    db.set_post_bot_media(conn, post["id"], file_id, access_token)


def _draft_keyboard(conn, draft_id: int) -> InlineKeyboardMarkup:
    planning_slot = db.planning_slot_for_draft(conn, draft_id)
    if planning_slot is not None and planning_slot["session_status"] == "active":
        return InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        f"✅ Готово на завтра ({planning_slot['position']}/{planning_slot['target_count']})",
                        callback_data=f"planready:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "✏️ Редактировать руками",
                        callback_data=f"edit:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        "🤖 Редактировать с AI",
                        callback_data=f"aiedit:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "🧵 Пересобрать Threads с AI",
                        callback_data=f"threadify:{draft_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        "⏭ Другой материал",
                        callback_data=f"plandiscard:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        "⏹ Закончить на сегодня",
                        callback_data=f"plancancel:{draft_id}",
                    ),
                ],
            ]
        )
    draft = db.get_draft(conn, draft_id)
    post = db.get_post(conn, draft["post_id"]) if draft is not None else None
    final_row = [
        InlineKeyboardButton(
            "❌ Отменить" if post is not None and post["media_kind"] == "manual" else "⏹ Закончить итерацию",
            callback_data=f"draftskip:{draft_id}",
        )
    ]
    if post is not None and post["media_kind"] != "manual":
        final_row.insert(
            0,
            InlineKeyboardButton(
                "⏭ Другой материал",
                callback_data=f"draftnext:{draft_id}",
            ),
        )
    rows = [
        [InlineKeyboardButton("✅ Опубликовать сейчас", callback_data=f"pub:{draft_id}")],
    ]
    if post is not None and post["media_kind"] == "manual":
        rows.append(
            [
                InlineKeyboardButton(
                    "✨ Standard Transform",
                    callback_data=f"transform:{draft_id}",
                )
            ]
        )
    rows.extend(
        [
            [
                InlineKeyboardButton(
                    "✏️ Редактировать руками",
                    callback_data=f"edit:{draft_id}",
                ),
                InlineKeyboardButton(
                    "🤖 Редактировать с AI",
                    callback_data=f"aiedit:{draft_id}",
                ),
            ],
            [
                InlineKeyboardButton(
                    "🧵 Пересобрать Threads с AI",
                    callback_data=f"threadify:{draft_id}",
                )
            ],
            final_row,
        ]
    )
    return InlineKeyboardMarkup(rows)


def _media_choice_keyboard(draft_id: int, *, planning: bool) -> InlineKeyboardMarkup:
    prefix = "plan" if planning else "pub"
    destination = "в очередь" if planning else "сейчас"
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    f"🖼 С картинкой {destination}",
                    callback_data=f"{prefix}with:{draft_id}",
                ),
                InlineKeyboardButton(
                    f"📝 Без картинки {destination}",
                    callback_data=f"{prefix}without:{draft_id}",
                ),
            ],
            [InlineKeyboardButton("↩️ Назад", callback_data=f"mediaresume:{draft_id}")],
        ]
    )


def _draft_has_photo(conn, draft) -> bool:
    post = db.get_post(conn, draft["post_id"])
    return post is not None and post["media_kind"] == "photo"


def _photo_publish_error(conn, draft) -> str | None:
    post = db.get_post(conn, draft["post_id"])
    if post is None or post["media_kind"] != "photo":
        return "У этого поста нет исходной картинки"
    if not post["bot_media_file_id"] or not post["media_access_token"]:
        return "Фото не удалось сохранить. Можно опубликовать без картинки."
    if post["media_size"] and post["media_size"] >= 10_000_000:
        return "Фото превышает лимит Buffer 10 МБ. Можно опубликовать без картинки."
    if not config.PUBLIC_BASE_URL:
        return "PUBLIC_BASE_URL не настроен. Buffer не сможет забрать картинку."
    return None


def _main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [[KeyboardButton(NEW_POST_BUTTON), KeyboardButton(STATS_BUTTON)]],
        resize_keyboard=True,
        is_persistent=True,
    )


def _new_post_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📚 Накидывать из базы", callback_data="newdb:0")],
            [InlineKeyboardButton("✍️ Написать свой текст", callback_data="newcustom:0")],
            [InlineKeyboardButton("❌ Отменить", callback_data="newcancel:0")],
        ]
    )


def _raw_keyboard(post_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✨ Создать пост", callback_data=f"make:{post_id}"),
                InlineKeyboardButton("⏭ Пропустить", callback_data=f"drop:{post_id}"),
            ]
        ]
    )


def _raw_body(post, *, include_text: bool = True) -> str:
    author = f" · {post['author']}" if post["author"] else ""
    media = "" if post["media_kind"] == "text" else f" · {post['media_kind']}"
    header = (
        f"📥 {post['title'] or post['username']}{author} · {post['posted_at'][:10]}{media}\n"
        f"{post['url'] or ''}"
    )
    if include_text and post["text"]:
        return f"{header}\n\n{post['text']}"
    return header[:4000]


def _text_chunks(text: str, limit: int = 4000) -> list[str]:
    if not text:
        return []
    return [text[index : index + limit] for index in range(0, len(text), limit)]


def _raw_parts(post) -> list[str]:
    """Keep the source text byte-for-byte visible instead of silently truncating it."""
    header = _raw_body(post, include_text=False)
    text = post["text"] or ""
    combined = f"{header}\n\n{text}" if text else header
    if len(combined) <= 4000:
        return [combined]
    return [header, *_text_chunks(text)]


def _finish_delivery(conn, post, bot_message_id: int) -> None:
    if not db.mark_delivery_sent(
        conn,
        post["id"],
        bot_message_id,
        post["claim_token"],
    ):
        raise RuntimeError("Аренда доставки уже истекла")


async def _send_raw_parts(bot, post, *, keyboard_on_last: bool) -> int:
    messages = _raw_parts(post)
    last_message_id = 0
    for index, text in enumerate(messages):
        kwargs = {"disable_web_page_preview": True}
        if keyboard_on_last and index == len(messages) - 1:
            kwargs["reply_markup"] = _raw_keyboard(post["id"])
        sent = await _send(bot.send_message, config.OWNER_CHAT_ID, text, **kwargs)
        last_message_id = sent.message_id
    return last_message_id


async def _send_draft(bot, conn, draft_id: int) -> None:
    draft = db.get_draft(conn, draft_id)
    master = _draft_body(draft)
    for chunk in _text_chunks("📄 LinkedIn / X:\n\n" + master):
        await _send(bot.send_message, config.OWNER_CHAT_ID, chunk)
    msg = await _send(
        bot.send_message,
        config.OWNER_CHAT_ID,
        _threads_preview(draft),
        reply_markup=_draft_keyboard(conn, draft_id),
    )
    db.set_draft_message(conn, draft_id, msg.message_id)
    db.set_draft_status(conn, draft_id, "awaiting_review")


async def _prepare_missing_threads(bot, conn, draft) -> bool:
    """Build a durable AI thread plan and require the owner to review it once."""
    if draft["threads_json"]:
        return True
    if not db.transition_draft(
        conn,
        draft["id"],
        ("awaiting_review", "approved", "delivery_failed"),
        "ai_editing",
    ):
        return False
    await _send(
        bot.send_message,
        config.OWNER_CHAT_ID,
        f"⏳ Перед финальным подтверждением Terra собирает Threads-план: каждый "
        f"story/value point до {config.THREAD_ITEM_CHARS} символов.",
    )
    try:
        plan = await asyncio.to_thread(generator.threadify_post, _draft_body(draft))
        db.set_draft_thread_items(conn, draft["id"], plan.thread_items)
        db.set_draft_status(conn, draft["id"], "awaiting_review")
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            "🧵 Threads-план готов. Проверь карточки и нажми финальную кнопку ещё раз."
            + (f"\n\n{plan.notes[:500]}" if plan.notes else ""),
        )
        await _send_draft(bot, conn, draft["id"])
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("missing Threads plan preparation failed draft_id=%s", draft["id"])
        db.set_draft_status(conn, draft["id"], "awaiting_review")
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            f"⚠️ Threads-план не создан: {_public_error_text(exc)}",
            reply_markup=_draft_keyboard(conn, draft["id"]),
        )
    return False


async def _send_generation_note(bot, draft) -> None:
    notes = (draft["notes"] or "").strip()
    if not notes:
        return
    await _send(
        bot.send_message,
        config.OWNER_CHAT_ID,
        f"⚠️ Заменено / проверить:\n{notes[:500]}",
    )


async def _send_media(bot, post, path: Path):
    markup = _raw_keyboard(post["id"])
    with path.open("rb") as media:
        common = {
            "chat_id": config.OWNER_CHAT_ID,
            "reply_markup": markup,
            "read_timeout": 300,
            "write_timeout": 300,
        }
        if post["media_kind"] == "voice":
            return await _send(bot.send_voice, voice=media, **common)
        if post["media_kind"] == "video":
            return await _send(bot.send_video, video=media, supports_streaming=True, **common)
        if post["media_kind"] == "video_note":
            return await _send(bot.send_video_note, video_note=media, **common)
        if post["media_kind"] == "audio":
            return await _send(bot.send_audio, audio=media, **common)
        if post["media_kind"] == "photo":
            return await _send(bot.send_photo, photo=media, **common)
        return await _send(bot.send_document, document=media, **common)


async def _bot_username(bot) -> str:
    global _BOT_USERNAME
    if _BOT_USERNAME:
        return _BOT_USERNAME
    me = await bot.get_me()
    if not me.username:
        raise RuntimeError("У бота нет username")
    _BOT_USERNAME = me.username
    return _BOT_USERNAME


async def _send_raw(bot, conn, post) -> None:
    if post["media_kind"] == "text":
        message_id = await _send_raw_parts(bot, post, keyboard_on_last=True)
        _finish_delivery(conn, post, message_id)
        return

    # Photos are small enough for a direct Bot API upload. This avoids relying
    # on an in-memory staging waiter across separate Vercel webhook instances
    # and guarantees that the reusable file_id is durable before review starts.
    if post["media_kind"] == "photo":
        await _send_raw_parts(bot, post, keyboard_on_last=False)
        try:
            if post["media_size"] and post["media_size"] > config.BOT_MEDIA_MAX_BYTES:
                raise RuntimeError(
                    f"файл больше лимита отправки бота ({config.BOT_MEDIA_MAX_BYTES // 1_000_000} МБ)"
                )
            async with asyncio.timeout(300):
                with tempfile.TemporaryDirectory(prefix="repost-photo-") as tmp:
                    path = await ingest.download_post_media(post, tmp)
                    msg = await _send_media(bot, post, path)
                    _remember_bot_media(conn, post, _message_media_file_id(msg, "photo"))
        except Exception as exc:  # noqa: BLE001
            msg = await _send(
                bot.send_message,
                config.OWNER_CHAT_ID,
                f"⚠️ Фото не удалось переслать: {_public_error_text(exc)}\n"
                f"Открыть оригинал: {post['url']}",
                reply_markup=_raw_keyboard(post["id"]),
                disable_web_page_preview=True,
            )
        _finish_delivery(conn, post, msg.message_id)
        return

    staging = None
    staging_event = None
    token = secrets.token_urlsafe(18)
    loop = asyncio.get_running_loop()
    waiter = loop.create_future()
    _STAGING_WAITERS[token] = waiter
    username = None
    try:
        async with asyncio.timeout(config.MEDIA_STAGE_TIMEOUT):
            username = await _bot_username(bot)
            staging = await ingest.stage_post_for_bot(post, username, token)
            staging_event = await waiter
    except Exception:  # upload/link fallback below
        pass
    finally:
        _STAGING_WAITERS.pop(token, None)
        if not waiter.done():
            waiter.cancel()
    if staging is not None and staging_event is not None:
        sender_id, marker_user_id, media_user_id = staging
        bot_chat_id, inbound_bot_message_id, bot_media_file_id = staging_event
        _remember_bot_media(conn, post, bot_media_file_id)
        copied = None
        try:
            if bot_chat_id != sender_id:
                raise RuntimeError("Получено staging-медиа не от Telethon-пользователя")
            if sender_id != config.OWNER_CHAT_ID:
                copied = await _send(
                    bot.copy_message,
                    chat_id=config.OWNER_CHAT_ID,
                    from_chat_id=bot_chat_id,
                    message_id=inbound_bot_message_id,
                    read_timeout=300,
                    write_timeout=300,
                )
            message_id = await _send_raw_parts(bot, post, keyboard_on_last=True)
            _finish_delivery(conn, post, message_id)
        except Exception:
            message_id = None
            if copied is not None:
                try:
                    await bot.delete_message(config.OWNER_CHAT_ID, copied.message_id)
                except Exception:
                    pass
        try:
            cleanup = [marker_user_id]
            if sender_id != config.OWNER_CHAT_ID or message_id is None:
                cleanup.append(media_user_id)
            await ingest.delete_bot_staging_messages(username, cleanup)
        except Exception:
            pass
        if message_id is not None:
            return

    if staging is not None and username:
        try:
            await ingest.delete_bot_staging_messages(username, [staging[1], staging[2]])
        except Exception:
            pass
    await _send_raw_parts(bot, post, keyboard_on_last=False)
    try:
        if post["media_size"] and post["media_size"] > config.BOT_MEDIA_MAX_BYTES:
            raise RuntimeError(
                f"файл больше лимита отправки бота ({config.BOT_MEDIA_MAX_BYTES // 1_000_000} МБ)"
            )
        async with asyncio.timeout(300):
            with tempfile.TemporaryDirectory(prefix="repost-media-") as tmp:
                path = await ingest.download_post_media(post, tmp)
                if path.stat().st_size > config.BOT_MEDIA_MAX_BYTES:
                    raise RuntimeError(
                        f"файл больше лимита отправки бота ({config.BOT_MEDIA_MAX_BYTES // 1_000_000} МБ)"
                    )
                msg = await _send_media(bot, post, path)
                _remember_bot_media(conn, post, _message_media_file_id(msg, post["media_kind"]))
    except Exception as exc:  # noqa: BLE001 — ссылка остаётся рабочим fallback
        msg = await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            f"⚠️ Медиа не удалось переслать: {str(exc)[:300]}\nОткрыть оригинал: {post['url']}",
            reply_markup=_raw_keyboard(post["id"]),
            disable_web_page_preview=True,
        )
    _finish_delivery(conn, post, msg.message_id)


async def propose_batch(
    bot,
    *,
    slot_key: str,
    source_username: str | None = None,
    max_items: int | None = None,
    announce_empty: bool = False,
    planning_slot_id: int | None = None,
) -> int:
    if not config.OWNER_CHAT_ID:
        return 0
    LOGGER.info(
        "delivery started slot=%s source=%s max_items=%s",
        slot_key,
        source_username or "all",
        config.ITEMS_PER_SLOT if max_items is None else max_items,
    )
    async with _DELIVERY_LOCK:
        conn = db.connect()
        try:
            refetch_result = None
            # Do not form a source round from a half-finished Telegram sync.
            async with _SYNC_LOCK:
                posts = db.claim_oldest_posts(
                    conn,
                    slot_key,
                    source_username=source_username,
                    max_items=config.ITEMS_PER_SLOT if max_items is None else max_items,
                )
                if not posts and source_username is None:
                    refetch_result = await _incremental_refetch_on_exhaustion()
                    posts = db.claim_oldest_posts(
                        conn,
                        slot_key,
                        source_username=source_username,
                        max_items=config.ITEMS_PER_SLOT if max_items is None else max_items,
                    )
            if not posts:
                LOGGER.info("delivery empty slot=%s refetch=%s", slot_key, refetch_result is not None)
                if announce_empty:
                    detail = ""
                    if refetch_result is not None:
                        detail = (
                            f" После обновления добавлено {refetch_result['added']};"
                            f" ошибок источников: {len(refetch_result['errors'])}."
                        )
                    await _send(
                        bot.send_message,
                        config.OWNER_CHAT_ID,
                        "Очередь пуста — новых материалов нет." + detail,
                    )
                return 0
            LOGGER.info(
                "delivery claimed slot=%s post_ids=%s",
                slot_key,
                ",".join(str(post["id"]) for post in posts),
            )
            if planning_slot_id is not None:
                if len(posts) != 1 or not db.assign_planning_post(
                    conn,
                    planning_slot_id,
                    posts[0]["id"],
                ):
                    for post in posts:
                        db.release_delivery(conn, post["id"], post["claim_token"])
                    LOGGER.warning(
                        "planning slot assignment lost slot_id=%s post_ids=%s",
                        planning_slot_id,
                        ",".join(str(post["id"]) for post in posts),
                    )
                    return 0
            sent = 0
            for post in posts:
                try:
                    await _send_raw(bot, conn, post)
                    sent += 1
                    LOGGER.info("delivery sent slot=%s post_id=%s", slot_key, post["id"])
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("delivery failed slot=%s post_id=%s", slot_key, post["id"])
                    db.release_delivery(conn, post["id"], post["claim_token"])
                    if planning_slot_id is not None:
                        db.clear_planning_post(conn, planning_slot_id, post["id"])
                    try:
                        await _send(
                            bot.send_message,
                            config.OWNER_CHAT_ID,
                            f"⚠️ Не удалось показать "
                            f"{post['username']}/{post['tg_message_id']}: {str(exc)[:300]}",
                        )
                    except Exception:
                        pass
            return sent
        finally:
            conn.close()


async def _incremental_refetch_on_exhaustion() -> dict | None:
    """Fetch every Telegram message after each source's durable last_message_id."""
    lease_conn = db.connect()
    if not db.claim_incremental_refetch(lease_conn):
        lease_conn.close()
        return None
    result = None
    queue_lock_acquired = False
    try:
        await asyncio.to_thread(db.acquire_queue_sync_lock, lease_conn)
        queue_lock_acquired = True
        rows = lease_conn.execute(
            "SELECT username FROM source WHERE active=1 ORDER BY lower(username)"
        ).fetchall()
        sources = [row["username"] for row in rows]
        if not sources:
            sources = config.read_sources()
        result = await ingest.run_fetch(
            sources,
            window_start=None,
            window_end=datetime.now(timezone.utc),
            incremental=True,
        )
        return result
    finally:
        db.finish_incremental_refetch(lease_conn, result)
        if queue_lock_acquired:
            await asyncio.to_thread(db.release_queue_sync_lock, lease_conn)
        lease_conn.close()


async def _offer_replacement(bot, skipped_post_id: int) -> int:
    """Immediately replace a skipped material with the next queue item."""
    return await propose_batch(
        bot,
        slot_key=f"replacement:{skipped_post_id}",
        max_items=1,
        announce_empty=True,
    )


def _planning_publish_datetimes(local_now: datetime) -> tuple[str, list[str]]:
    target_date = local_now.date() + timedelta(days=1)
    tz = ZoneInfo(config.TIMEZONE)
    publish_at: list[str] = []
    for slot in config.PUBLISH_TIMES:
        hour, minute = map(int, slot.split(":"))
        local_publish = datetime.combine(
            target_date,
            dtime(hour, minute),
            tzinfo=tz,
        )
        publish_at.append(local_publish.astimezone(timezone.utc).isoformat())
    return target_date.isoformat(), publish_at


async def _continue_planning(bot, session_id: int) -> int:
    """Offer exactly one candidate for the next unfinished planning slot."""
    conn = db.connect()
    try:
        session = db.get_planning_session(conn, session_id)
        if session is None or session["status"] != "active":
            return 0
        slot = db.next_planning_slot(conn, session_id)
        if slot is None or slot["status"] != "selecting" or slot["post_id"] is not None:
            return 0
        reserved = db.reserve_planning_attempt(conn, slot["id"])
        if reserved is None:
            return 0
        attempt = reserved["attempt_count"]
        position = reserved["position"]
    finally:
        conn.close()
    await _send(
        bot.send_message,
        config.OWNER_CHAT_ID,
        f"📌 Итерация {position}/{session['target_count']}: выбираем материал для поста.",
    )
    sent = await propose_batch(
        bot,
        slot_key=f"planning:{session_id}:{position}:{attempt}",
        max_items=1,
        announce_empty=True,
        planning_slot_id=slot["id"],
    )
    return sent


async def start_evening_planning(bot, *, now: datetime | None = None) -> dict:
    """Start the single 21:00 London review flow for tomorrow's posts."""
    local_now = now or datetime.now(ZoneInfo(config.TIMEZONE))
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=ZoneInfo(config.TIMEZONE))
    planning_date = local_now.date().isoformat()
    target_date, publish_at = _planning_publish_datetimes(local_now)
    conn = db.connect()
    try:
        closed = db.close_stale_planning_sessions(conn, planning_date)
        session, created = db.create_planning_session(
            conn,
            planning_date,
            target_date,
            publish_at,
        )
    finally:
        conn.close()
    if session["status"] != "active":
        return {"created": created, "closed": closed, "sent": 0, "status": session["status"]}
    if created:
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            "🌙 Вечерняя сессия началась. Последовательно подготовим "
            f"{session['target_count']} поста на завтра. После каждого готового "
            "черновика я сразу запущу следующую итерацию.\n\n"
            "Публикация завтра: " + ", ".join(config.PUBLISH_TIMES) + " по Лондону.",
        )
    sent = await _continue_planning(bot, session["id"])
    LOGGER.info(
        "planning started session_id=%s date=%s target=%s created=%s closed_stale=%s sent=%s",
        session["id"],
        planning_date,
        target_date,
        created,
        closed,
        sent,
    )
    return {"created": created, "closed": closed, "sent": sent, "status": session["status"]}


async def _generate_from_post(
    bot,
    conn,
    post,
    source_text: str,
) -> bool:
    started_at = time.monotonic()
    LOGGER.info(
        "generation started post_id=%s source=%s chars=%s model=%s",
        post["id"],
        post["username"],
        len(source_text),
        config.llm_model(),
    )
    existing = db.active_draft_for_post(conn, post["id"])
    if existing is not None:
        try:
            db.attach_planning_draft(conn, post["id"], existing["id"])
            await _send_generation_note(bot, existing)
            await _send_draft(bot, conn, existing["id"])
            db.set_post_status(conn, post["id"], "drafted")
            return True
        except Exception as exc:  # noqa: BLE001
            db.set_draft_status(conn, existing["id"], "delivery_failed")
            db.set_post_status(conn, post["id"], "offered")
            try:
                await _send(bot.send_message, config.OWNER_CHAT_ID, f"⚠️ Не удалось отправить черновик: {exc}")
            except Exception:
                pass
            return False
    try:
        out = await asyncio.to_thread(
            generator.translate_post,
            post["title"] or post["username"],
            post["posted_at"][:10],
            source_text,
        )
        draft_id = db.create_draft(
            conn,
            post["id"],
            config.llm_model(),
            out.linkedin_text,
            out.x_text,
            out.threads_text,
            out.notes,
            out.thread_items,
        )
        db.attach_planning_draft(conn, post["id"], draft_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("generation failed post_id=%s", post["id"])
        db.set_post_status(conn, post["id"], "offered")
        try:
            await _send(bot.send_message, config.OWNER_CHAT_ID, f"⚠️ Ошибка генерации: {str(exc)[:500]}")
        except Exception:
            pass
        return False
    try:
        draft = db.get_draft(conn, draft_id)
        await _send_generation_note(bot, draft)
        await _send_draft(bot, conn, draft_id)
        LOGGER.info(
            "generation completed post_id=%s draft_id=%s chars=%s duration_ms=%s",
            post["id"],
            draft_id,
            len(_draft_body(draft)),
            round((time.monotonic() - started_at) * 1000),
        )
        return True
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("draft delivery failed post_id=%s draft_id=%s", post["id"], draft_id)
        db.set_draft_status(conn, draft_id, "delivery_failed")
        db.set_post_status(conn, post["id"], "offered")
        try:
            await _send(bot.send_message, config.OWNER_CHAT_ID, f"⚠️ Не удалось отправить черновик: {exc}")
        except Exception:
            pass
        return False


def _texts_for_publish(draft) -> dict[str, str | list[str]]:
    if draft["edited_text"]:
        master = draft["edited_text"]
        return {
            "linkedin": master,
            "twitter": master,
            "threads": _thread_items_for_draft(draft),
        }
    return {
        "linkedin": draft["linkedin_text"] or "",
        "twitter": draft["x_text"] or "",
        "threads": _thread_items_for_draft(draft),
    }


def _image_url_for_draft(conn, draft) -> str | None:
    if not draft["include_media"]:
        return None
    post = db.get_post(conn, draft["post_id"])
    if post is None or post["media_kind"] != "photo":
        raise RuntimeError("Для черновика выбрана картинка, но исходное фото не найдено")
    if not post["bot_media_file_id"] or not post["media_access_token"]:
        raise RuntimeError("Фото не удалось сохранить в Telegram Bot API; выбери публикацию без картинки")
    if not config.PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL не настроен — Buffer не сможет получить картинку")
    return f"{config.PUBLIC_BASE_URL.rstrip('/')}/api/media/{post['media_access_token']}"


def _ok_platforms(conn, draft_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT platform FROM publication WHERE draft_id=? AND status='ok'",
        (draft_id,),
    )
    return {row["platform"] for row in rows}


async def _publish(bot, conn, draft_id: int, *, notify: bool = True) -> None:
    if not db.claim_draft_publish(conn, draft_id):
        if notify:
            await _send(
                bot.send_message,
                config.OWNER_CHAT_ID,
                "Этот черновик уже публикуется или обработан.",
            )
        return
    draft = db.get_draft(conn, draft_id)
    texts = _texts_for_publish(draft)
    try:
        image_url = _image_url_for_draft(conn, draft)
    except Exception as exc:
        db.set_draft_status(conn, draft_id, "approved")
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            f"⚠️ Не могу подготовить картинку: {_public_error_text(exc)}",
            reply_markup=_draft_keyboard(conn, draft_id) if notify else None,
        )
        return
    done = _ok_platforms(conn, draft_id)
    todo = {platform: text for platform, text in texts.items() if platform not in done}
    if notify:
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            "⏳ Отправляю пост в Buffer: "
            + ", ".join(PLATFORM_LABELS.get(platform, platform) for platform in todo)
            + ". Обычно это занимает несколько секунд.",
        )
    LOGGER.info(
        "publication started draft_id=%s platforms=%s",
        draft_id,
        ",".join(todo),
    )
    try:
        results = await asyncio.to_thread(publisher.publish_all, todo, image_url)
    except BaseException as exc:
        LOGGER.exception("publication result unknown draft_id=%s", draft_id)
        db.set_draft_status(conn, draft_id, "publish_unknown")
        if isinstance(exc, asyncio.CancelledError):
            raise
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            f"⚠️ Статус публикации неизвестен. Проверь Buffer перед любым повтором: {str(exc)[:300]}",
        )
        return
    if todo and not results:
        db.set_draft_status(conn, draft_id, "approved")
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            "⚠️ Buffer не вернул ни одной публикации. Черновик сохранён; проверь настройки каналов.",
        )
        return
    lines = []
    for platform, (ok, info) in results.items():
        db.record_publication(conn, draft_id, platform, ok, info if ok else None, None if ok else info)
        label = PLATFORM_LABELS.get(platform, platform)
        lines.append(f"{'✅' if ok else '❌'} {label}" + ("" if ok else f": {info}"))
    LOGGER.info(
        "publication completed draft_id=%s results=%s",
        draft_id,
        ",".join(f"{platform}:{'ok' if ok else 'error'}" for platform, (ok, _) in results.items()),
    )
    failed = [platform for platform, (ok, _) in results.items() if not ok]
    unknown = [platform for platform, (ok, info) in results.items() if not ok and info.startswith("UNKNOWN:")]
    if unknown:
        db.set_draft_status(conn, draft_id, "publish_unknown")
        markup = None
        lines.append("⚠️ Проверь Buffer вручную: автоматический повтор отключён, чтобы не создать дубль.")
    elif failed:
        db.set_draft_status(conn, draft_id, "approved")
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔁 Повторить неудавшиеся", callback_data=f"pub:{draft_id}")]]
        )
    else:
        db.set_draft_status(conn, draft_id, "published")
        db.set_post_status(conn, draft["post_id"], "published")
        markup = None
    if notify or failed or unknown:
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            "Публикация:\n" + "\n".join(lines) if lines else "Нечего публиковать",
            reply_markup=markup,
        )


async def publish_due_planned(bot, *, now: datetime | None = None) -> dict[str, int]:
    """Publish all durable planning slots due by the current UTC instant."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    conn = db.connect()
    summary = {"claimed": 0, "published": 0, "retry": 0, "unknown": 0, "failed": 0}
    try:
        slots = db.claim_due_planning_slots(conn, current.astimezone(timezone.utc).isoformat())
        summary["claimed"] = len(slots)
        for slot in slots:
            draft = db.get_draft(conn, slot["draft_id"])
            if draft is None:
                db.finish_planning_publication(conn, slot["id"], "missing", "draft missing")
                summary["failed"] += 1
                await _send(
                    bot.send_message,
                    config.OWNER_CHAT_ID,
                    f"❌ Плановый слот {slot['position']}/{config.DAILY_POSTS}: "
                    "черновик не найден, публикация не выполнена.",
                )
                continue
            try:
                await _publish(bot, conn, draft["id"], notify=False)
            except BaseException as exc:
                refreshed = db.get_draft(conn, draft["id"])
                status = refreshed["status"] if refreshed is not None else "missing"
                db.finish_planning_publication(
                    conn,
                    slot["id"],
                    status,
                    _public_error_text(exc),
                )
                if isinstance(exc, asyncio.CancelledError):
                    raise
                LOGGER.exception(
                    "scheduled publication crashed slot_id=%s draft_id=%s",
                    slot["id"],
                    draft["id"],
                )
                summary["unknown" if status == "publish_unknown" else "failed"] += 1
                continue
            refreshed = db.get_draft(conn, draft["id"])
            status = refreshed["status"] if refreshed is not None else "missing"
            slot_status = db.finish_planning_publication(conn, slot["id"], status)
            key = {
                "published": "published",
                "ready": "retry",
                "publish_unknown": "unknown",
            }.get(slot_status, "failed")
            summary[key] += 1
            if slot_status == "published":
                post = db.get_post(conn, draft["post_id"])
                source = (post["title"] or post["username"]) if post is not None else "без источника"
                excerpt = re.sub(r"\s+", " ", _draft_body(draft)).strip()
                if len(excerpt) > 180:
                    excerpt = excerpt[:177].rstrip() + "…"
                planned_time = config.PUBLISH_TIMES[slot["position"] - 1]
                await _send(
                    bot.send_message,
                    config.OWNER_CHAT_ID,
                    f"✅ {planned_time} — пост #{draft['id']} опубликован в LinkedIn, X и Threads.\n"
                    f"Источник: {source}\n\n{excerpt}",
                )
        LOGGER.info("scheduled publication sweep now=%s summary=%s", current.isoformat(), summary)
        return summary
    finally:
        conn.close()


async def on_staging_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Resolve the Bot API ID of media staged by the Telethon user."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None or msg.reply_to_message is None:
        return
    marker = (msg.reply_to_message.text or msg.reply_to_message.caption or "").strip()
    if not marker.startswith(_STAGING_PREFIX):
        return
    token = marker.removeprefix(_STAGING_PREFIX).strip()
    waiter = _STAGING_WAITERS.get(token)
    if waiter is not None and not waiter.done():
        media_kind = (
            "photo"
            if getattr(msg, "photo", None)
            else "video"
            if getattr(msg, "video", None)
            else "document"
        )
        waiter.set_result((chat.id, msg.message_id, _message_media_file_id(msg, media_kind)))


async def _finalize_planning_draft(query, context, conn, draft_id: int, include_media: bool) -> None:
    draft = db.get_draft(conn, draft_id)
    if draft is None:
        await query.answer("Черновик не найден", show_alert=True)
        return
    if not draft["threads_json"]:
        try:
            await query.answer("Готовлю Threads-план")
        except Exception:
            pass
        await _prepare_missing_threads(context.bot, conn, draft)
        return
    if include_media:
        error = _photo_publish_error(conn, draft)
        if error:
            await query.answer(error, show_alert=True)
            return
    db.set_draft_include_media(conn, draft_id, include_media)
    progress = db.mark_planning_draft_ready(conn, draft_id)
    if progress is None:
        await query.answer("Слот уже сохранён или вечерняя сессия закрыта", show_alert=True)
        return
    try:
        await query.answer("Сохранено на завтра")
        await query.edit_message_reply_markup(None)
    except Exception:
        pass
    media_note = " с картинкой" if include_media else ""
    if progress["ready"] < progress["target"]:
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"✅ Пост {progress['ready']}/{progress['target']} сохранён на завтра{media_note}. "
            "Запускаю следующую итерацию.",
        )
        await _continue_planning(context.bot, progress["session_id"])
    else:
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"✅ Вечерняя сессия завершена: {progress['ready']}/{progress['target']} "
            f"поста готовы на {progress['target_date']}. Они выйдут в "
            + ", ".join(config.PUBLISH_TIMES)
            + f" по {config.TIMEZONE}.",
        )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = getattr(update.effective_chat, "id", None)
    user_id = getattr(getattr(query, "from_user", None), "id", None)
    LOGGER.info(
        "callback received chat_id=%s user_id=%s data=%r",
        chat_id,
        user_id,
        query.data,
    )
    if not _is_owner(update):
        LOGGER.warning(
            "callback rejected: chat_id=%s does not match configured owner",
            chat_id,
        )
        await query.answer()
        return
    action, _, raw_id = query.data.partition(":")
    object_id = int(raw_id)
    conn = db.connect()

    if action in {"make", "drop"}:
        post = db.get_post(conn, object_id)
        if post is None:
            await query.answer("Материал не найден", show_alert=True)
            return
        planning_slot = db.planning_slot_for_post(conn, post["id"])
        if action == "drop":
            try:
                await query.answer()
            except Exception:
                pass
            transitioned = db.transition_post(
                conn,
                post["id"],
                ("offered", "awaiting_manual"),
                "skipped",
            )
            LOGGER.info(
                "raw skip post_id=%s previous_status=%s transitioned=%s",
                post["id"],
                post["status"],
                transitioned,
            )
            if transitioned:
                try:
                    await query.edit_message_reply_markup(None)
                except Exception:
                    pass
                if planning_slot is not None and planning_slot["session_status"] == "active":
                    db.clear_planning_post(conn, planning_slot["id"], post["id"])
                    try:
                        await _send(
                            context.bot.send_message,
                            config.OWNER_CHAT_ID,
                            f"⏭ Материал пропущен. Итерация {planning_slot['position']}/"
                            f"{planning_slot['target_count']} продолжается — показываю следующий.",
                        )
                    except Exception:
                        pass
                    replacement_count = await _continue_planning(
                        context.bot,
                        planning_slot["session_id"],
                    )
                    LOGGER.info(
                        "planning raw replacement post_id=%s slot_id=%s sent=%s",
                        post["id"],
                        planning_slot["id"],
                        replacement_count,
                    )
                    return
                try:
                    await _send(
                        context.bot.send_message,
                        config.OWNER_CHAT_ID,
                        "⏭ Материал пропущен. Показываю следующий из очереди.",
                    )
                except Exception:
                    pass
                replacement_count = await _offer_replacement(context.bot, post["id"])
                LOGGER.info(
                    "raw skip replacement post_id=%s sent=%s",
                    post["id"],
                    replacement_count,
                )
            else:
                try:
                    await query.edit_message_reply_markup(None)
                except Exception:
                    pass
            return
        needs_manual_text = (
            post["media_kind"] in {"voice", "audio", "video", "video_note"}
            or not post["text"]
        )
        if action == "make" and needs_manual_text:
            await query.answer()
            if not db.transition_post(conn, post["id"], ("offered",), "awaiting_manual"):
                return
            try:
                prompt = await _send(
                    context.bot.send_message,
                    config.OWNER_CHAT_ID,
                    "✍️ Напиши свой текст поста ответом на это сообщение. "
                    f"Финальная версия будет не длиннее {config.MAX_POST_CHARS} символов.",
                    reply_markup=ForceReply(selective=True),
                )
            except Exception as exc:
                db.set_post_status(conn, post["id"], "offered")
                try:
                    await query.edit_message_reply_markup(_raw_keyboard(post["id"]))
                except Exception:
                    pass
                await _send(
                    context.bot.send_message,
                    config.OWNER_CHAT_ID,
                    f"⚠️ Не удалось открыть ручной ввод: {str(exc)[:300]}",
                )
                return
            db.set_manual_prompt(conn, post["id"], prompt.message_id)
            await query.edit_message_reply_markup(None)
            return
        source_text = post["text"]
        if not source_text:
            await query.answer("Нет текста для генерации", show_alert=True)
            return
        await query.answer("Создаю пост")
        if not db.transition_post(conn, post["id"], ("offered", "awaiting_manual"), "generating"):
            return
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"⏳ Пост #{post['id']}: перевожу на английский, проверяю факты и "
            f"укладываю master в {config.MAX_POST_CHARS} символов; отдельно собираю "
            f"Threads-карточки до {config.THREAD_ITEM_CHARS}. Обычно это занимает 10–30 секунд.",
        )
        success = await _generate_from_post(
            context.bot,
            conn,
            post,
            source_text,
        )
        if success:
            await query.edit_message_reply_markup(None)
        else:
            refreshed = db.get_post(conn, post["id"])
            await query.edit_message_reply_markup(
                _raw_keyboard(refreshed["id"])
            )
        return

    if action == "newcancel":
        await query.answer("Отменено")
        await query.edit_message_reply_markup(None)
        return
    if action == "newcustom":
        await query.answer()
        await query.edit_message_reply_markup(None)
        await _open_custom_post(context.bot)
        return
    if action == "newdb":
        await query.answer("Ищу материал")
        await query.edit_message_reply_markup(None)
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            "📚 Запускаю одну внеплановую итерацию. Готовый пост можно будет "
            "опубликовать сразу.",
        )
        context.application.create_task(
            propose_batch(
                context.bot,
                slot_key=f"ondemand:{uuid.uuid4().hex}",
                max_items=1,
                announce_empty=True,
            ),
            update=update,
            name="ondemand-delivery",
        )
        return

    draft = db.get_draft(conn, object_id)
    if draft is None:
        await query.answer("Черновик не найден", show_alert=True)
        return
    if action == "skip":  # backwards compatibility for already-sent old keyboards
        action = "draftskip"
    if action == "planready":
        planning_slot = db.planning_slot_for_draft(conn, object_id)
        if planning_slot is None:
            await query.answer("Этот черновик не относится к вечернему плану", show_alert=True)
            return
        if planning_slot["session_status"] != "active" or planning_slot["status"] != "reviewing":
            await query.answer("Слот уже сохранён или вечерняя сессия закрыта", show_alert=True)
            return
        if not draft["threads_json"]:
            await query.answer("Готовлю Threads-план")
            await _prepare_missing_threads(context.bot, conn, draft)
            return
        if _draft_has_photo(conn, draft):
            await query.answer("Выбери вариант картинки")
            await query.edit_message_reply_markup(
                _media_choice_keyboard(object_id, planning=True)
            )
            return
        await _finalize_planning_draft(query, context, conn, object_id, False)
        return
    if action in {"planwith", "planwithout"}:
        await _finalize_planning_draft(
            query,
            context,
            conn,
            object_id,
            action == "planwith",
        )
        return
    if action == "mediaresume":
        await query.answer()
        await query.edit_message_reply_markup(_draft_keyboard(conn, object_id))
        return
    if action == "plandiscard":
        progress = db.discard_planning_draft(conn, object_id)
        if progress is None:
            await query.answer("Черновик уже закрыт или сессия завершена", show_alert=True)
            return
        try:
            await query.answer("Ищу другой материал")
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"⏭ Черновик отклонён. Итерация {progress['position']}/{progress['target']} "
            "продолжается — показываю следующий материал.",
        )
        await _continue_planning(context.bot, progress["session_id"])
        return
    if action == "plancancel":
        planning_slot = db.planning_slot_for_draft(conn, object_id)
        if planning_slot is None:
            await query.answer("Вечерняя сессия не найдена", show_alert=True)
            return
        result = db.cancel_planning_session(conn, planning_slot["session_id"])
        if result is None:
            await query.answer("Сессия уже закрыта", show_alert=True)
            return
        try:
            await query.answer()
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"⏹ Вечерняя сессия остановлена. Уже готово {result['ready']}/"
            f"{result['target']}; готовые посты останутся в расписании, остальные слоты отменены.",
        )
        return
    if action in {"edit", "aiedit", "threadify"}:
        planning_slot = db.planning_slot_for_draft(conn, object_id)
        if planning_slot is not None and planning_slot["status"] != "reviewing":
            await query.answer("Пост уже сохранён в расписание; редактирование закрыто", show_alert=True)
            return
    if draft["status"] in ("expired", "skipped", "published", "publishing", "publish_unknown"):
        await query.answer()
        await query.edit_message_reply_markup(None)
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"Черновик #{object_id} уже неактуален ({draft['status']}).",
        )
        return

    if action in {
        "draftskip",
        "draftnext",
        "plandiscard",
        "edit",
        "aiedit",
        "threadify",
        "transform",
    } and _ok_platforms(
        conn,
        object_id,
    ):
        await query.answer(
            "Часть площадок уже опубликована. Можно только повторить оставшиеся.",
            show_alert=True,
        )
        return

    try:
        await query.answer()
    except Exception:
        pass
    if action == "draftskip":
        if not db.transition_draft(
            conn,
            object_id,
            ("awaiting_review", "approved", "delivery_failed"),
            "skipped",
        ):
            return
        db.set_post_status(conn, draft["post_id"], "skipped")
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        try:
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                f"⏹ Итерация с черновиком #{object_id} завершена. "
                "Следующий материал придёт по расписанию.",
            )
        except Exception:
            pass
    elif action == "draftnext":
        if not db.transition_draft(
            conn,
            object_id,
            ("awaiting_review", "approved", "delivery_failed"),
            "skipped",
        ):
            return
        db.set_post_status(conn, draft["post_id"], "skipped")
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"⏭ Черновик #{object_id} отклонён. Показываю следующий материал.",
        )
        await _offer_replacement(context.bot, draft["post_id"])
    elif action == "edit":
        current_text = _draft_body(draft)
        edit_instruction = (
            "Пришли полный изменённый текст ответом на это сообщение. "
            f"AI-лимит {config.MAX_POST_CHARS} уже не применяется; вручную можно до "
            f"{config.MANUAL_MAX_POST_CHARS} символов."
        )
        if len(current_text) + len(edit_instruction) > 3900:
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                f"✏️ Текущая версия поста #{object_id}:",
            )
            for chunk in _text_chunks(current_text):
                await _send(context.bot.send_message, config.OWNER_CHAT_ID, chunk)
            prompt_text = edit_instruction
        else:
            prompt_text = f"✏️ Текущая версия поста #{object_id}:\n\n{current_text}\n\n{edit_instruction}"
        prompt = await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            prompt_text,
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Вставь сюда полную изменённую версию",
            ),
        )
        db.set_edit_msg(conn, object_id, prompt.message_id)
    elif action == "aiedit":
        prompt = await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"🤖 AI-редактор открыт для поста #{object_id}. Напиши одной репликой, "
            "что изменить: убрать фрагмент, переписать формулировку, добавить шутку, "
            "усилить hook и т.д. Terra изменит текущую версию и вернёт полный пост.",
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Например: сократи второй абзац и добавь шутку",
            ),
        )
        db.set_ai_prompt(conn, object_id, prompt.message_id)
    elif action == "threadify":
        if not db.transition_draft(
            conn,
            object_id,
            ("awaiting_review", "approved", "delivery_failed"),
            "ai_editing",
        ):
            return
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"⏳ Terra пересобирает Threads-план для поста #{object_id}: цельный hook → "
            f"story/value points → payoff, каждый до {config.THREAD_ITEM_CHARS} символов.",
        )
        try:
            plan = await asyncio.to_thread(generator.threadify_post, _draft_body(draft))
            db.set_draft_thread_items(conn, object_id, plan.thread_items)
            db.set_draft_status(conn, object_id, "awaiting_review")
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                "🧵 Threads-план готов."
                + (f"\n\n{plan.notes[:500]}" if plan.notes else ""),
            )
            await _send_draft(context.bot, conn, object_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("Threads regeneration failed draft_id=%s", object_id)
            db.set_draft_status(conn, object_id, "awaiting_review")
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                f"⚠️ Threads-план не обновлён: {_public_error_text(exc)}",
                reply_markup=_draft_keyboard(conn, object_id),
            )
    elif action == "transform":
        post = db.get_post(conn, draft["post_id"])
        if post is None or post["media_kind"] != "manual":
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                "Standard Transform доступен только для собственного текста.",
            )
            return
        if not db.transition_draft(
            conn,
            object_id,
            ("awaiting_review", "approved", "delivery_failed"),
            "ai_editing",
        ):
            return
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            f"⏳ Standard Transform для поста #{object_id}: English → факты Mike/Vahue → "
            f"смысловое сжатие только при необходимости до {config.MAX_POST_CHARS} → "
            f"Threads-план по {config.THREAD_ITEM_CHARS} символов. "
            "Обычно это занимает 10–30 секунд.",
        )
        try:
            out = await asyncio.to_thread(
                generator.translate_post,
                "Собственный пост",
                datetime.now(ZoneInfo(config.TIMEZONE)).date().isoformat(),
                _draft_body(draft),
            )
            db.update_draft_texts(
                conn,
                object_id,
                out.linkedin_text,
                out.x_text,
                out.threads_text,
                None,
                out.thread_items,
            )
            db.set_draft_status(conn, object_id, "awaiting_review")
            transformed = db.get_draft(conn, object_id)
            await _send_generation_note(context.bot, transformed)
            await _send_draft(context.bot, conn, object_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("standard transform failed draft_id=%s", object_id)
            db.set_draft_status(conn, object_id, "awaiting_review")
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                f"⚠️ Standard Transform не завершён: {_public_error_text(exc)}. "
                "Исходная версия сохранена.",
                reply_markup=_draft_keyboard(conn, object_id),
            )
    elif action == "pub":
        if not await _prepare_missing_threads(context.bot, conn, draft):
            return
        if _draft_has_photo(conn, draft):
            await query.edit_message_reply_markup(
                _media_choice_keyboard(object_id, planning=False)
            )
        else:
            await _publish(context.bot, conn, object_id)
    elif action in {"pubwith", "pubwithout"}:
        if not await _prepare_missing_threads(context.bot, conn, draft):
            return
        include_media = action == "pubwith"
        if include_media:
            error = _photo_publish_error(conn, draft)
            if error:
                await _send(
                    context.bot.send_message,
                    config.OWNER_CHAT_ID,
                    f"⚠️ {error}",
                    reply_markup=_media_choice_keyboard(object_id, planning=False),
                )
                return
        db.set_draft_include_media(conn, object_id, include_media)
        try:
            await query.edit_message_reply_markup(None)
        except Exception:
            pass
        await _publish(context.bot, conn, object_id)


async def _apply_ai_instruction(update, context, conn, draft, instruction: str) -> None:
    planning_slot = db.planning_slot_for_draft(conn, draft["id"])
    if planning_slot is not None and planning_slot["status"] != "reviewing":
        await update.message.reply_text(
            "Этот пост уже сохранён в расписание или вечерняя сессия закрыта; "
            "AI-редактирование больше не применяется."
        )
        return
    if _ok_platforms(conn, draft["id"]):
        await update.message.reply_text(
            "Часть площадок уже опубликована — AI не меняет текст, чтобы версии не разошлись."
        )
        return
    if not db.transition_draft(
        conn,
        draft["id"],
        ("awaiting_review", "approved"),
        "ai_editing",
    ):
        await update.message.reply_text("⚠️ Этот черновик уже обрабатывается или закрыт.")
        return
    await update.message.reply_text(
        f"⏳ Terra редактирует пост #{draft['id']} по твоей инструкции и проверяет "
        f"лимит {config.MAX_POST_CHARS}; затем обновляет Threads-план. "
        "Обычно это занимает 10–30 секунд."
    )
    current_text = _draft_body(draft)
    try:
        out = await asyncio.to_thread(generator.revise_post, current_text, instruction)
        db.update_draft_texts(
            conn,
            draft["id"],
            out.linkedin_text,
            out.x_text,
            out.threads_text,
            out.linkedin_text,
            out.thread_items,
        )
        db.set_draft_status(conn, draft["id"], "awaiting_review")
        db.set_ai_prompt(conn, draft["id"], None)
        await _send(
            context.bot.send_message,
            config.OWNER_CHAT_ID,
            "🤖 Готово. Теперь пост выглядит так:"
            + (f"\n\nИзменения: {out.notes[:500]}" if out.notes else ""),
        )
        await _send_draft(context.bot, conn, draft["id"])
        LOGGER.info(
            "AI edit completed draft_id=%s instruction_chars=%s result_chars=%s",
            draft["id"],
            len(instruction),
            len(out.linkedin_text),
        )
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("AI edit failed draft_id=%s", draft["id"])
        db.set_draft_status(conn, draft["id"], "awaiting_review")
        retry_prompt = await update.message.reply_text(
            f"⚠️ AI-редактирование не завершилось: {_public_error_text(exc)}\n\n"
            "Текущая версия сохранена. Ответь на это сообщение инструкцией, чтобы повторить.",
            reply_markup=ForceReply(
                selective=True,
                input_field_placeholder="Повтори или уточни инструкцию для Terra",
            ),
        )
        db.set_ai_prompt(conn, draft["id"], retry_prompt.message_id)


async def on_reply(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or update.message.reply_to_message is None:
        return
    conn = db.connect()
    try:
        reply_to = update.message.reply_to_message.message_id
        text = (update.message.text or "").strip()
        if not text:
            return

        post = db.post_by_manual_prompt(conn, reply_to)
        if post is not None and post["status"] != "awaiting_manual":
            post = None
        ai_draft = None if post is not None else db.draft_by_ai_prompt(conn, reply_to)
        draft = None if post is not None or ai_draft is not None else db.draft_by_message(conn, reply_to)
        LOGGER.info(
            "reply received reply_to=%s chars=%s target=%s target_id=%s",
            reply_to,
            len(text),
            "manual"
            if post is not None
            else "ai"
            if ai_draft is not None
            else "draft"
            if draft is not None
            else "none",
            post["id"]
            if post is not None
            else ai_draft["id"]
            if ai_draft is not None
            else draft["id"]
            if draft is not None
            else "none",
        )

        if post is None and ai_draft is None and draft is None:
            await update.message.reply_text(
                "⚠️ Я получил сообщение, но не нашёл активное редактирование для этого reply. "
                "Нажми «✏️ Редактировать» под актуальным черновиком и ответь на новый prompt."
            )
            return

        if ai_draft is not None:
            await _apply_ai_instruction(update, context, conn, ai_draft, text)
            return

        target_limit = (
            None
            if post is not None and post["media_kind"] == "manual"
            else config.MANUAL_MAX_POST_CHARS
        )
        if target_limit is not None and len(text) > target_limit:
            retry_prompt = await update.message.reply_text(
                f"⚠️ Текст содержит {len(text)} символов; максимум для публикации во все "
                f"площадки — {target_limit}. "
                "Я ничего не обрезал. Сократи текст и ответь прямо на это сообщение — "
                "я продолжу тот же процесс.",
                reply_markup=ForceReply(selective=True),
            )
            if post is not None:
                db.set_manual_prompt(conn, post["id"], retry_prompt.message_id)
            else:
                db.set_edit_msg(conn, draft["id"], retry_prompt.message_id)
            LOGGER.info(
                "reply rejected over limit target=%s target_id=%s chars=%s retry_prompt=%s",
                "manual" if post is not None else "draft",
                post["id"] if post is not None else draft["id"],
                len(text),
                retry_prompt.message_id,
            )
            return

        if post is not None and post["media_kind"] == "manual":
            await update.message.reply_text(
                f"✅ Текст принят: {len(text)} символов. Сохраняю как есть, без AI."
            )
        else:
            await update.message.reply_text(
                f"⏳ Текст принят: {len(text)}/{config.MANUAL_MAX_POST_CHARS}. "
                "Обновляю LinkedIn/X без AI-сжатия и отдельно пересобираю Threads-план…"
            )

        if post is not None:
            if not db.transition_post(conn, post["id"], ("awaiting_manual",), "generating"):
                await update.message.reply_text("⚠️ Этот материал уже обрабатывается или закрыт.")
                return
            existing = db.active_draft_for_post(conn, post["id"])
            if existing is not None:
                try:
                    await _send_draft(context.bot, conn, existing["id"])
                    db.set_draft_status(conn, existing["id"], "awaiting_review")
                    db.set_post_status(conn, post["id"], "drafted")
                except Exception as exc:  # noqa: BLE001
                    LOGGER.exception("manual draft delivery failed post_id=%s", post["id"])
                    db.set_draft_status(conn, existing["id"], "delivery_failed")
                    db.set_post_status(conn, post["id"], "awaiting_manual")
                    await update.message.reply_text(
                        f"⚠️ Не удалось отправить черновик: {_public_error_text(exc)}"
                    )
                return
            try:
                if post["media_kind"] == "manual":
                    draft_id = db.create_draft(
                        conn,
                        post["id"],
                        "manual/raw",
                        text,
                        text,
                        text,
                        "",
                    )
                else:
                    out = await asyncio.to_thread(generator.adapt, text)
                    thread_plan = await asyncio.to_thread(generator.threadify_post, text)
                    draft_id = db.create_draft(
                        conn,
                        post["id"],
                        config.llm_model(),
                        out.linkedin_text,
                        out.x_text,
                        out.threads_text,
                        out.notes,
                        thread_plan.thread_items,
                    )
                db.attach_planning_draft(conn, post["id"], draft_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("manual draft preparation failed post_id=%s", post["id"])
                db.set_post_status(conn, post["id"], "awaiting_manual")
                retry_prompt = await update.message.reply_text(
                    f"⚠️ Ошибка подготовки: {_public_error_text(exc)}\n\n"
                    "Состояние сохранено. Ответь на это сообщение исходным текстом, "
                    "чтобы повторить попытку.",
                    reply_markup=ForceReply(selective=True),
                )
                db.set_manual_prompt(conn, post["id"], retry_prompt.message_id)
                return
            try:
                await _send_draft(context.bot, conn, draft_id)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("manual draft send failed post_id=%s draft_id=%s", post["id"], draft_id)
                db.set_draft_status(conn, draft_id, "delivery_failed")
                db.set_post_status(conn, post["id"], "awaiting_manual")
                retry_prompt = await update.message.reply_text(
                    f"⚠️ Не удалось отправить черновик: {_public_error_text(exc)}\n\n"
                    "Состояние сохранено. Ответь на это сообщение исходным текстом, "
                    "чтобы повторить попытку.",
                    reply_markup=ForceReply(selective=True),
                )
                db.set_manual_prompt(conn, post["id"], retry_prompt.message_id)
            return

        planning_slot = db.planning_slot_for_draft(conn, draft["id"])
        if planning_slot is not None and planning_slot["status"] != "reviewing":
            await update.message.reply_text(
                "Этот пост уже сохранён в расписание или вечерняя сессия закрыта; "
                "редактирование больше не применяется."
            )
            return
        if draft["status"] not in ("awaiting_review", "approved"):
            await update.message.reply_text(f"Черновик уже нельзя редактировать ({draft['status']}).")
            return
        if _ok_platforms(conn, draft["id"]):
            await update.message.reply_text(
                "Часть площадок уже опубликована — менять текст нельзя, чтобы версии не разошлись."
            )
            return
        try:
            out = await asyncio.to_thread(generator.adapt, text)
            thread_plan = await asyncio.to_thread(generator.threadify_post, text)
            db.update_draft_texts(
                conn,
                draft["id"],
                out.linkedin_text,
                out.x_text,
                out.threads_text,
                text,
                thread_plan.thread_items,
            )
            db.set_draft_status(conn, draft["id"], "awaiting_review")
            await _send_draft(context.bot, conn, draft["id"])
            LOGGER.info("draft edited draft_id=%s chars=%s", draft["id"], len(text))
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("draft edit failed draft_id=%s", draft["id"])
            await update.message.reply_text(f"⚠️ Ошибка обновления: {_public_error_text(exc)}")
    finally:
        conn.close()


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Legacy group collector for text/captions; channel media uses Telethon ingest."""
    msg = update.effective_message
    chat = update.effective_chat
    if msg is None or chat is None:
        return
    text = (msg.text or msg.caption or "").strip()
    if not text:
        return
    conn = db.connect()
    source_id = db.upsert_source(conn, f"chat:{chat.id}", chat.title)
    if msg.from_user and not msg.from_user.is_bot:
        author = msg.from_user.full_name
    elif msg.sender_chat:
        author = msg.sender_chat.title
    else:
        author = None
    internal_id = str(chat.id).removeprefix("-100")
    url = f"https://t.me/c/{internal_id}/{msg.message_id}"
    status = "new" if len(text) >= config.MIN_POST_CHARS else "short"
    posted_at = msg.date.astimezone(timezone.utc).isoformat()
    db.insert_post(conn, source_id, msg.message_id, posted_at, text, url, author=author, status=status)


async def cmd_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(f"chat id: {update.effective_chat.id}")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        f"chat id: {update.effective_chat.id}\n"
        + ("(этот id уже в OWNER_CHAT_ID)" if _is_owner(update) else "Впиши его в .env как OWNER_CHAT_ID")
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        await cmd_id(update, context)
        return
    await update.message.reply_text(
        "Бот готов. Можно дождаться идеи из Telegram-очереди или в любой момент "
        "создать собственный пост кнопкой ниже.",
        reply_markup=_main_keyboard(),
    )


async def _open_custom_post(bot) -> None:
    conn = db.connect()
    try:
        existing = db.pending_owner_post(conn, config.OWNER_CHAT_ID)
        prompt = await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            "✍️ Пришли свой текст ответом на это сообщение. Я сначала сохраню его как есть — "
            "без AI и без автоматического перевода. Затем можно сразу опубликовать, применить "
            "Standard Transform, отредактировать вручную или через AI.",
            reply_markup=ForceReply(selective=True),
        )
        if existing is not None:
            db.set_manual_prompt(conn, existing["id"], prompt.message_id)
            LOGGER.info("owner post input reopened post_id=%s prompt=%s", existing["id"], prompt.message_id)
            return
        source_id = db.upsert_source(
            conn,
            f"manual:{config.OWNER_CHAT_ID}",
            "Собственные посты",
        )
        if not db.insert_post(
            conn,
            source_id,
            prompt.message_id,
            datetime.now(timezone.utc).isoformat(),
            "",
            None,
            author="Mike Doroshenko",
            status="awaiting_manual",
            media_kind="manual",
        ):
            raise RuntimeError("Не удалось создать durable manual-post session")
        row = conn.execute(
            "SELECT id FROM post WHERE source_id=? AND tg_message_id=?",
            (source_id, prompt.message_id),
        ).fetchone()
        db.set_manual_prompt(conn, row["id"], prompt.message_id)
        LOGGER.info("owner post input created post_id=%s prompt=%s", row["id"], prompt.message_id)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("failed to start owner post input")
        await _send(
            bot.send_message,
            config.OWNER_CHAT_ID,
            f"⚠️ Не удалось начать новый пост: {_public_error_text(exc)}",
        )
    finally:
        conn.close()


async def cmd_new_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    await update.message.reply_text(
        "Что создаём? Это отдельный режим: он не влияет на вечерние три черновика.",
        reply_markup=_new_post_menu(),
    )


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_owner(update):
        context.application.create_task(
            propose_batch(
                context.bot,
                slot_key=f"manual:{uuid.uuid4().hex}",
                announce_empty=True,
            ),
            update=update,
            name="manual-delivery",
        )


async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update):
        return
    source = context.args[0] if context.args else None
    if source and not source.startswith("@"):
        source = "@" + source
    context.application.create_task(
            propose_batch(
                context.bot,
                slot_key=f"test:{uuid.uuid4().hex}",
                source_username=source,
                max_items=1,
                announce_empty=True,
            ),
        update=update,
        name="test-delivery",
    )


async def cmd_resend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Re-deliver a saved draft after a Telegram delivery/UI failure."""
    if not _is_owner(update):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Использование: /resend <draft_id>")
        return
    draft_id = int(context.args[0])
    conn = db.connect()
    try:
        draft = db.get_draft(conn, draft_id)
        if draft is None:
            await update.message.reply_text(f"⚠️ Черновик #{draft_id} не найден.")
            return
        planning_slot = db.planning_slot_for_draft(conn, draft_id)
        if planning_slot is not None and planning_slot["status"] != "reviewing":
            await update.message.reply_text(
                f"⚠️ Черновик #{draft_id} уже сохранён в план ({planning_slot['status']}); "
                "повторная review-карточка не создаётся."
            )
            return
        if draft["status"] in ("publishing", "published"):
            await update.message.reply_text(
                f"⚠️ Черновик #{draft_id} уже {draft['status']}; повторно не отправляю."
            )
            return
        await update.message.reply_text(f"⏳ Повторно отправляю сохранённый черновик #{draft_id}…")
        await _send_draft(context.bot, conn, draft_id)
        LOGGER.info("draft redelivered draft_id=%s chars=%s", draft_id, len(_draft_body(draft)))
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("draft redelivery failed draft_id=%s", draft_id)
        await update.message.reply_text(
            f"⚠️ Не удалось повторно отправить черновик #{draft_id}: {_public_error_text(exc)}"
        )
    finally:
        conn.close()


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_owner(update):
        conn = db.connect()
        try:
            stats = db.stats(conn)
        finally:
            conn.close()
        await update.message.reply_text(
            "\n".join(f"{key}: {value}" for key, value in stats.items()) or "База пуста",
            reply_markup=_main_keyboard(),
        )


async def propose_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = datetime.now(ZoneInfo(config.TIMEZONE))
    slot = context.job.data["slot"]
    await propose_batch(context.bot, slot_key=f"daily:{now.date().isoformat()}:{slot}")


async def planning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await start_evening_planning(context.bot)


async def publication_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await publish_due_planned(context.bot)


def _startup_recovery_message(deliveries: int, work: dict[str, int]) -> str | None:
    lines: list[str] = []
    if deliveries:
        lines.append(f"• незавершённых доставок возвращено в очередь: {deliveries}")
    if work["generating_reoffered"]:
        lines.append(
            f"• генераций без готового черновика снова доступны: "
            f"{work['generating_reoffered']}"
        )
    if work["generating_manual"]:
        lines.append(
            f"• ручных вводов снова ожидают ответа: {work['generating_manual']}"
        )
    if work["generating_reconciled"]:
        lines.append(
            f"• генераций согласовано с уже сохранёнными черновиками: "
            f"{work['generating_reconciled']}"
        )
    if work["undelivered_drafts_reopened"]:
        lines.append(
            f"• сохранённых, но не доставленных черновиков возвращено "
            f"на повторную доставку: {work['undelivered_drafts_reopened']}"
        )
    if work["manual_without_prompt_reoffered"]:
        lines.append(
            f"• ручных вводов без Telegram-prompt возвращено к исходной карточке: "
            f"{work['manual_without_prompt_reoffered']}"
        )
    if work["publishing_unknown"]:
        lines.append(
            f"• публикаций с неизвестным результатом: {work['publishing_unknown']}. "
            "Автоповтор отключён — проверь Buffer вручную."
        )
    if work["ai_edits_reopened"]:
        lines.append(
            f"• AI-редактирований возвращено в review после рестарта: "
            f"{work['ai_edits_reopened']}"
        )
    if not lines:
        return None
    return "⚠️ После перезапуска восстановлено состояние:\n" + "\n".join(lines)


async def startup_recovery_notice_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(
        context.bot.send_message,
        config.OWNER_CHAT_ID,
        context.job.data,
    )
    conn = db.connect()
    try:
        # Keep a newer/different notice durable if state changed unexpectedly.
        if db.get_meta(conn, _STARTUP_RECOVERY_NOTICE_KEY) == context.job.data:
            db.delete_meta(conn, _STARTUP_RECOVERY_NOTICE_KEY)
    finally:
        conn.close()


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log handler failures without logging message contents or credentials."""
    error = context.error
    update_id = getattr(update, "update_id", None)
    if error is None:
        LOGGER.error("Telegram update failed without an exception; update_id=%s", update_id)
        return
    LOGGER.error(
        "Telegram update failed; update_id=%s error=%s",
        update_id,
        type(error).__name__,
        exc_info=(type(error), error, error.__traceback__),
    )
    effective_chat = getattr(update, "effective_chat", None)
    if getattr(effective_chat, "id", None) == config.OWNER_CHAT_ID:
        try:
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                f"⚠️ Процесс завершился ошибкой ({type(error).__name__}): "
                f"{_public_error_text(error)}. Состояние сохранено; действие можно повторить.",
            )
        except Exception:
            LOGGER.exception("failed to report Telegram handler error to owner")


_SYNC_RETRY_KEYS = (
    "sync_retry_sources",
    "sync_retry_window_start",
    "sync_retry_window_end",
    "sync_retry_attempt",
    "next_source_retry_at",
)


def _meta_due(conn, key: str, now: datetime, *, missing_is_due: bool = False) -> bool:
    raw = db.get_meta(conn, key)
    if not raw:
        return missing_is_due
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return now >= value


def _clear_sync_retry(conn) -> None:
    db.delete_meta(conn, *_SYNC_RETRY_KEYS)


def _set_sync_retry(
    conn,
    sources: list[str],
    window_start: datetime,
    window_end: datetime,
    *,
    attempt: int,
    next_retry: datetime,
) -> None:
    db.set_meta(conn, "sync_retry_sources", json.dumps(sources, ensure_ascii=False))
    db.set_meta(conn, "sync_retry_window_start", window_start.isoformat())
    db.set_meta(conn, "sync_retry_window_end", window_end.isoformat())
    db.set_meta(conn, "sync_retry_attempt", str(attempt))
    db.set_meta(conn, "next_source_retry_at", next_retry.isoformat())


async def _fetch_sync_window(
    sources: list[str],
    window_start: datetime,
    window_end: datetime,
) -> dict:
    try:
        return await ingest.run_fetch(
            sources,
            window_start=window_start,
            window_end=window_end,
            incremental=False,
        )
    except Exception as exc:  # noqa: BLE001
        return {
            "errors": {source: str(exc) for source in sources},
            "added": 0,
            "stats": {},
        }


async def sync_due_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not config.AUTO_SYNC:
        return
    async with _SYNC_LOCK:
        conn = db.connect()
        now = datetime.now(timezone.utc)
        full_due = _meta_due(conn, "next_full_sync_at", now, missing_is_due=True)
        if full_due:
            channels = config.read_sources()
            if not channels:
                return
            window_end = now
            window_start = ingest.subtract_months(window_end, config.SYNC_MONTHS)
            await _send(
                context.bot.send_message,
                config.OWNER_CHAT_ID,
                f"📥 Начинаю трёхмесячный сбор из {len(channels)} источников.",
            )
            result = await _fetch_sync_window(channels, window_start, window_end)
            errors = result["errors"]
            next_full = ingest.add_months(now, config.SYNC_MONTHS)
            db.set_meta(conn, "last_full_sync_at", now.isoformat())
            db.set_meta(conn, "next_full_sync_at", next_full.isoformat())
            if errors:
                failed = sorted(errors, key=str.casefold)
                _set_sync_retry(
                    conn,
                    failed,
                    window_start,
                    window_end,
                    attempt=0,
                    next_retry=now + timedelta(days=1),
                )
                message = (
                    f"⚠️ Сбор завершён частично: добавлено {result['added']}, "
                    f"ошибок {len(failed)}. Повторю только эти источники через сутки; "
                    f"полный сбор — {next_full.date().isoformat()}."
                )
            else:
                _clear_sync_retry(conn)
                message = (
                    f"✅ Сбор завершён: добавлено {result['added']}. "
                    f"Следующий полный запуск: {next_full.date().isoformat()}."
                )
            await _send(context.bot.send_message, config.OWNER_CHAT_ID, message)
            return

        if not _meta_due(conn, "next_source_retry_at", now):
            return
        try:
            sources = json.loads(db.get_meta(conn, "sync_retry_sources") or "[]")
            window_start = datetime.fromisoformat(db.get_meta(conn, "sync_retry_window_start") or "")
            window_end = datetime.fromisoformat(db.get_meta(conn, "sync_retry_window_end") or "")
            attempt = int(db.get_meta(conn, "sync_retry_attempt") or "0") + 1
        except (TypeError, ValueError, json.JSONDecodeError):
            _clear_sync_retry(conn)
            return
        if not sources:
            _clear_sync_retry(conn)
            return
        result = await _fetch_sync_window(sources, window_start, window_end)
        failed = sorted(result["errors"], key=str.casefold)
        if failed:
            delay_days = min(2**attempt, 7)
            _set_sync_retry(
                conn,
                failed,
                window_start,
                window_end,
                attempt=attempt,
                next_retry=now + timedelta(days=delay_days),
            )
            message = (
                f"⚠️ Повторный сбор: добавлено {result['added']}, всё ещё ошибок {len(failed)}. "
                f"Следующий повтор только для них через {delay_days} дн."
            )
        else:
            _clear_sync_retry(conn)
            message = f"✅ Повторный сбор завершён: добавлено {result['added']}; ошибок больше нет."
        await _send(context.bot.send_message, config.OWNER_CHAT_ID, message)


def _acquire_process_lock() -> None:
    global _BOT_PROCESS_LOCK
    path = Path(f"{config.DB_PATH}.bot.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        sys.exit(f"Бот уже запущен для базы {config.DB_PATH}")
    _BOT_PROCESS_LOCK = lock_file


def create_application() -> Application:
    """Build the Telegram update router for polling locally or webhooks on Vercel."""
    app = Application.builder().token(config.BOT_TOKEN).concurrent_updates(8).build()
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.ATTACHMENT, on_staging_media),
        group=-1,
    )
    app.add_handler(CommandHandler("start", cmd_start, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("id", cmd_id, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("next", cmd_next, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("test", cmd_test, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("resend", cmd_resend, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("stats", cmd_stats, filters.ChatType.PRIVATE))
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Regex(f"^{re.escape(NEW_POST_BUTTON)}$"),
            cmd_new_post,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.Regex(f"^{re.escape(STATS_BUTTON)}$"),
            cmd_stats,
        )
    )
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.REPLY & filters.TEXT & ~filters.COMMAND, on_reply)
    )
    return app


def main() -> None:
    if not config.BOT_TOKEN:
        sys.exit("BOT_TOKEN не задан в .env — создай бота у @BotFather")
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    LOGGER.setLevel(logging.INFO)
    _acquire_process_lock()
    startup_conn = db.connect()
    source_state = db.reconcile_active_sources(startup_conn, config.read_sources())
    recovered_deliveries = db.recover_incomplete_deliveries(startup_conn)
    recovered_work = db.recover_stranded_work(startup_conn)
    recovered_planning = db.recover_planning_publications(startup_conn)
    new_recovery_notice = _startup_recovery_message(recovered_deliveries, recovered_work)
    recovery_notice = db.get_meta(startup_conn, _STARTUP_RECOVERY_NOTICE_KEY)
    if new_recovery_notice:
        recovery_notice = (
            f"{recovery_notice}\n\n{new_recovery_notice}"
            if recovery_notice
            else new_recovery_notice
        )
        db.set_meta(startup_conn, _STARTUP_RECOVERY_NOTICE_KEY, recovery_notice)
    startup_conn.close()
    app = create_application()

    tz = ZoneInfo(config.TIMEZONE)
    planning_hour, planning_minute = map(int, config.PLANNING_TIME.split(":"))
    app.job_queue.run_daily(
        planning_job,
        time=dtime(planning_hour, planning_minute, tzinfo=tz),
        name=f"planning-{config.PLANNING_TIME}",
    )
    for slot in config.PUBLISH_TIMES:
        hour, minute = map(int, slot.split(":"))
        app.job_queue.run_daily(
            publication_job,
            time=dtime(hour, minute, tzinfo=tz),
            data={"slot": slot},
            name=f"publication-{slot}",
        )
    sync_hour, sync_minute = map(int, config.SYNC_TIME.split(":"))
    app.job_queue.run_daily(
        sync_due_job,
        time=dtime(sync_hour, sync_minute, tzinfo=tz),
        name="quarterly-sync-check",
    )
    if recovery_notice and config.OWNER_CHAT_ID:
        app.job_queue.run_once(
            startup_recovery_notice_job,
            when=2,
            data=recovery_notice,
            name="startup-recovery-notice",
        )
    app.job_queue.run_once(sync_due_job, when=10, name="startup-sync-check")
    recovered_generation_count = sum(
        recovered_work[key]
        for key in (
            "generating_reoffered",
            "generating_manual",
            "generating_reconciled",
            "undelivered_drafts_reopened",
            "manual_without_prompt_reoffered",
            "ai_edits_reopened",
        )
    )
    print(
        f"Бот запущен. Вечернее планирование: {config.PLANNING_TIME}; "
        f"публикация: {', '.join(config.PUBLISH_TIMES)} ({config.TIMEZONE}); "
        f"по {config.DAILY_POSTS} поста в день; "
        f"сбор раз в {config.SYNC_MONTHS} мес. {'включён' if config.AUTO_SYNC else 'выключен'}. "
        f"Активных источников: {source_state['configured']}; "
        f"восстановлено доставок: {recovered_deliveries}; "
        f"зависших генераций: {recovered_generation_count}; "
        f"неизвестных публикаций: {recovered_work['publishing_unknown']}; "
        f"восстановлено planning-слотов: {sum(recovered_planning.values())}."
    )
    # Telegram keeps the previous allowed_updates filter when the parameter is
    # omitted. Explicitly request every update type so inline button callbacks
    # cannot remain disabled by an older bot deployment.
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
