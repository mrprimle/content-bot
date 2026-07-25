"""Telegram-бот: 1) коллектор — сохраняет в базу все сообщения из групп, куда
его добавили (privacy mode у бота должен быть выключен через @BotFather);
2) согласование — предлагает черновики по расписанию, кнопки Опубликовать /
Заново / Пропустить, правка через reply в личке.

Запуск: python -m repost.bot  (процесс должен работать постоянно)
"""
import asyncio
import sys
from datetime import time as dtime, timezone
from zoneinfo import ZoneInfo

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config, db, generator, publisher, webingest

PLATFORM_LABELS = {"linkedin": "LinkedIn", "twitter": "X", "threads": "Threads"}


def _is_owner(update: Update) -> bool:
    return bool(config.OWNER_CHAT_ID) and update.effective_chat.id == config.OWNER_CHAT_ID


def _draft_body(draft) -> str:
    """Только чистый финальный текст поста — без служебных приписок."""
    return (draft["edited_text"] or draft["linkedin_text"] or "").strip()[:4000]


def _keyboard(draft_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("✅ Да, пост", callback_data=f"pub:{draft_id}"),
                InlineKeyboardButton("❌ Нет", callback_data=f"skip:{draft_id}"),
            ],
            [
                InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit:{draft_id}"),
            ],
        ]
    )


async def _send_draft(bot, conn, draft_id: int) -> None:
    draft = db.get_draft(conn, draft_id)
    msg = await bot.send_message(config.OWNER_CHAT_ID, _draft_body(draft), reply_markup=_keyboard(draft_id))
    db.set_draft_message(conn, draft_id, msg.message_id)


async def propose(bot) -> None:
    if not config.OWNER_CHAT_ID:
        return
    conn = db.connect()
    post = db.next_new_post(conn)
    if post is None:
        await bot.send_message(config.OWNER_CHAT_ID, "Очередь пуста — все посты обработаны.")
        return
    try:
        out = await asyncio.to_thread(
            generator.generate, post["title"] or post["username"], post["posted_at"][:10], post["text"]
        )
    except Exception as e:  # noqa: BLE001 — показываем ошибку в чат, пост остаётся в очереди
        await bot.send_message(config.OWNER_CHAT_ID, f"⚠️ Ошибка генерации: {e}")
        return
    draft_id = db.create_draft(
        conn, post["id"], config.llm_model(), out.linkedin_text, out.x_text, out.threads_text, out.notes
    )
    author = f" · {post['author']}" if post["author"] else ""
    header = (
        f"📥 {post['title'] or post['username']}{author} · {post['posted_at'][:10]}\n"
        f"{post['url'] or ''}\n\n{post['text']}"
    )[:3700]
    if out.notes:
        header += f"\n\n⚠️ {out.notes[:250]}"
    await bot.send_message(config.OWNER_CHAT_ID, header, disable_web_page_preview=True)
    await _send_draft(bot, conn, draft_id)


def _texts_for_publish(draft) -> dict[str, str]:
    linkedin = draft["edited_text"] or draft["linkedin_text"] or ""
    return {"linkedin": linkedin, "twitter": draft["x_text"] or "", "threads": draft["threads_text"] or ""}


def _ok_platforms(conn, draft_id: int) -> set[str]:
    rows = conn.execute(
        "SELECT DISTINCT platform FROM publication WHERE draft_id=? AND status='ok'", (draft_id,)
    )
    return {r["platform"] for r in rows}


async def _publish(bot, conn, draft_id: int) -> None:
    draft = db.get_draft(conn, draft_id)
    texts = _texts_for_publish(draft)
    done = _ok_platforms(conn, draft_id)
    todo = {p: t for p, t in texts.items() if p not in done}
    results = await asyncio.to_thread(publisher.publish_all, todo)
    lines = []
    for platform, (ok, info) in results.items():
        db.record_publication(conn, draft_id, platform, ok, info if ok else None, None if ok else info)
        label = PLATFORM_LABELS.get(platform, platform)
        lines.append(f"{'✅' if ok else '❌'} {label}" + ("" if ok else f": {info}"))
    failed = [p for p, (ok, _) in results.items() if not ok]
    if failed:
        db.set_draft_status(conn, draft_id, "approved")
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔁 Повторить неудавшиеся", callback_data=f"pub:{draft_id}")]]
        )
    else:
        db.set_draft_status(conn, draft_id, "published")
        db.set_post_status(conn, draft["post_id"], "published")
        markup = None
    await bot.send_message(
        config.OWNER_CHAT_ID, "Публикация:\n" + "\n".join(lines) if lines else "Нечего публиковать",
        reply_markup=markup,
    )


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    if not _is_owner(update):
        return
    action, _, raw_id = query.data.partition(":")
    draft_id = int(raw_id)
    conn = db.connect()
    draft = db.get_draft(conn, draft_id)
    if draft is None:
        return

    if action == "skip":
        db.set_draft_status(conn, draft_id, "skipped")
        db.set_post_status(conn, draft["post_id"], "skipped")
        await query.edit_message_reply_markup(None)
        await context.bot.send_message(config.OWNER_CHAT_ID, f"⏭ Черновик #{draft_id} пропущен.")
    elif action == "regen":
        post = db.get_post(conn, draft["post_id"])
        try:
            out = await asyncio.to_thread(
                generator.generate, post["title"] or post["username"], post["posted_at"][:10], post["text"]
            )
        except Exception as e:  # noqa: BLE001
            await context.bot.send_message(config.OWNER_CHAT_ID, f"⚠️ Ошибка генерации: {e}")
            return
        db.update_draft_texts(conn, draft_id, out.linkedin_text, out.x_text, out.threads_text, None)
        draft = db.get_draft(conn, draft_id)
        await query.edit_message_text(_draft_body(draft), reply_markup=_keyboard(draft_id))
    elif action == "edit":
        prompt = await context.bot.send_message(
            config.OWNER_CHAT_ID,
            f"✏️ Пришли новый текст поста #{draft_id} ответом на это сообщение.",
            reply_markup=ForceReply(selective=True),
        )
        db.set_edit_msg(conn, draft_id, prompt.message_id)
    elif action == "pub":
        await _publish(context.bot, conn, draft_id)


async def on_edit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_owner(update) or update.message.reply_to_message is None:
        return
    conn = db.connect()
    draft = db.draft_by_message(conn, update.message.reply_to_message.message_id)
    if draft is None:
        return
    edited = update.message.text.strip()
    try:
        out = await asyncio.to_thread(generator.adapt, edited)
    except Exception as e:  # noqa: BLE001
        await update.message.reply_text(f"⚠️ Ошибка адаптации: {e}")
        return
    db.update_draft_texts(conn, draft["id"], out.linkedin_text, out.x_text, out.threads_text, edited)
    await _send_draft(context.bot, conn, draft["id"])


async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Коллектор: пишет в базу каждое текстовое сообщение из группы."""
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


async def cmd_next(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_owner(update):
        await propose(context.bot)


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if _is_owner(update):
        s = db.stats(db.connect())
        await update.message.reply_text("\n".join(f"{k}: {v}" for k, v in s.items()) or "База пуста")


async def propose_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await propose(context.bot)


async def sync_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Месячный синк публичных каналов из sources.txt через t.me/s."""
    channels = config.read_sources()
    if not channels:
        return
    try:
        await asyncio.to_thread(webingest.run, channels, config.SYNC_DAYS)
        s = db.stats(db.connect())
        msg = (f"📥 Синк {len(channels)} источников готов. Всего в базе: {s.get('total', 0)}, "
               f"в очереди: {s.get('post:new', 0)}.")
    except Exception as e:  # noqa: BLE001
        msg = f"⚠️ Ошибка синка: {e}"
    if config.OWNER_CHAT_ID:
        await context.bot.send_message(config.OWNER_CHAT_ID, msg)


def main() -> None:
    if not config.BOT_TOKEN:
        sys.exit("BOT_TOKEN не задан в .env — создай бота у @BotFather")
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.add_handler(CommandHandler(["start", "id"], cmd_id, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("chatid", cmd_chatid))
    app.add_handler(CommandHandler("next", cmd_next, filters.ChatType.PRIVATE))
    app.add_handler(CommandHandler("stats", cmd_stats, filters.ChatType.PRIVATE))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.REPLY & filters.TEXT & ~filters.COMMAND, on_edit)
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & (filters.TEXT | filters.CAPTION) & ~filters.COMMAND, on_group_message)
    )
    tz = ZoneInfo(config.TIMEZONE)
    for t in config.POST_TIMES:
        h, m = map(int, t.split(":"))
        app.job_queue.run_daily(propose_job, time=dtime(h, m, tzinfo=tz))
    sh, sm = map(int, config.SYNC_TIME.split(":"))
    app.job_queue.run_monthly(sync_job, when=dtime(sh, sm, tzinfo=tz), day=config.SYNC_DAY)
    print(f"Бот запущен. Слоты: {', '.join(config.POST_TIMES)} ({config.TIMEZONE}), "
          f"синк {config.SYNC_DAY}-го числа в {config.SYNC_TIME}. Ctrl+C — остановить.")
    app.run_polling()


if __name__ == "__main__":
    main()
