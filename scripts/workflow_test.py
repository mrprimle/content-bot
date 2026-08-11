"""Offline async test: raw delivery must not call the LLM or Buffer."""
import asyncio
import json
import tempfile
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import bot, config, db, generator, ingest, publisher  # noqa: E402


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeIncomingMessage:
    def __init__(
        self,
        text: str | None,
        reply_to_id: int,
        first_response_id: int = 9_000,
        *,
        caption: str | None = None,
        photo: list | None = None,
    ):
        self.text = text
        self.caption = caption
        self.photo = photo or []
        self.reply_to_message = SimpleNamespace(message_id=reply_to_id)
        self.first_response_id = first_response_id
        self.responses: list[dict] = []

    async def reply_text(self, text: str, **kwargs):
        self.responses.append({"text": text, **kwargs})
        return FakeMessage(self.first_response_id + len(self.responses))


class FakeBot:
    def __init__(self):
        self.messages: list[dict] = []
        self.copies: list[dict] = []
        self.deleted: list[tuple[int, int]] = []
        self.photos: list[dict] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return FakeMessage(len(self.messages))

    async def copy_message(self, **kwargs):
        self.copies.append(kwargs)
        return FakeMessage(10_000 + len(self.copies))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return SimpleNamespace(
            message_id=20_000 + len(self.photos),
            photo=[SimpleNamespace(file_id=f"bot-photo-{len(self.photos)}")],
        )


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.answers: list[tuple[tuple, dict]] = []
        self.markup_edits: list[object] = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


class FakeApplication:
    def __init__(self):
        self.tasks: list[asyncio.Task] = []

    def create_task(self, coroutine, **_kwargs):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task


def test_shelf_status_plan() -> None:
    """Status must turn the FIFO shelf into today's concrete three-slot plan."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    try:
        source_id = db.upsert_source(conn, "@status", "Status source")
        queue_ids: list[int] = []
        for index, text in enumerate(
            ("First shelf story.", "Second shelf story.", "Third shelf story."),
            start=1,
        ):
            assert db.insert_post(
                conn,
                source_id,
                index,
                f"2026-05-0{index}T10:00:00+00:00",
                text,
                f"https://t.me/status/{index}",
            )
            post_id = conn.execute(
                "SELECT id FROM post WHERE source_id=? AND tg_message_id=?",
                (source_id, index),
            ).fetchone()["id"]
            draft_id = db.create_draft(
                conn,
                post_id,
                "test",
                text,
                text,
                text,
                "",
                [text],
            )
            queue_ids.append(db.enqueue_ready_draft(conn, draft_id)["queue_id"])

        conn.execute(
            "UPDATE ready_queue SET status='published', "
            "published_at='2026-08-07 08:00:00' WHERE id=?",
            (queue_ids[0],),
        )
        conn.commit()
        report = bot._status_report(
            conn,
            now=datetime(2026, 8, 7, 10, 0, tzinfo=ZoneInfo(config.TIMEZONE)),
        )
        assert "✅ 09:00 · опубликован — First shelf story." in report
        assert "🕒 14:00 · запланирован — Second shelf story." in report
        assert "🕒 19:00 · запланирован — Third shelf story." in report

        conn.execute("UPDATE ready_queue SET status='cancelled' WHERE id=?", (queue_ids[2],))
        conn.commit()
        short_report = bot._status_report(
            conn,
            now=datetime(2026, 8, 7, 10, 0, tzinfo=ZoneInfo(config.TIMEZONE)),
        )
        assert "⚪ 19:00 · поста не хватает — полка пуста" in short_report
        assert "Не хватает готовых постов на будущие слоты: 1" in short_report
    finally:
        conn.close()
        Path(tmp.name).unlink(missing_ok=True)


async def test_evening_planning_and_next_day_publish() -> None:
    """One 21:00 session must prepare three drafts and publish them next day."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_translate = generator.translate_post
    original_publish = publisher.publish_all
    config.DB_PATH = tmp.name
    config.OWNER_CHAT_ID = 123
    config.BOT_SEND_DELAY = 0
    conn = db.connect()
    try:
        for index, username in enumerate(
            (
                "@plan-alpha",
                "@plan-bravo",
                "@plan-charlie",
                "@plan-delta",
                "@plan-echo",
                "@plan-foxtrot",
            ),
            start=1,
        ):
            source_id = db.upsert_source(conn, username, username)
            assert db.insert_post(
                conn,
                source_id,
                index,
                f"2026-05-0{index}T10:00:00+00:00",
                f"Сырой материал {username} " * 20,
                f"https://t.me/{username[1:]}/{index}",
            )

        generated = 0
        published: list[dict[str, str]] = []

        def fake_translate(*_args, **_kwargs):
            nonlocal generated
            generated += 1
            text = f"Prepared planning draft {generated}."
            return generator.DraftOut(
                linkedin_text=text,
                x_text=text,
                threads_text=text,
                notes="",
                thread_items=[f"Planning hook {generated}.", f"Planning payoff {generated}."],
            )

        def fake_publish(texts, image_url=None):
            assert image_url is None
            published.append(dict(texts))
            return {platform: (True, f"buffer-{len(published)}-{platform}") for platform in texts}

        generator.translate_post = fake_translate
        publisher.publish_all = fake_publish

        fake = FakeBot()
        planning_now = datetime(2026, 8, 5, 21, 0, tzinfo=ZoneInfo(config.TIMEZONE))
        started = await bot.start_evening_planning(fake, now=planning_now)
        assert started == {"created": True, "closed": 0, "sent": 1, "status": "active"}
        assert "Вечерняя сессия началась" in fake.messages[0]["text"]
        assert "Итерация 1/3" in fake.messages[1]["text"]
        assert "@plan-alpha" in fake.messages[2]["text"]

        session = db.planning_session_for_date(conn, "2026-08-05")
        assert session["target_date"] == "2026-08-06" and session["target_count"] == 3
        slots = conn.execute(
            "SELECT * FROM planning_slot WHERE session_id=? ORDER BY position",
            (session["id"],),
        ).fetchall()
        assert [row["publish_at"] for row in slots] == [
            "2026-08-06T08:00:00+00:00",
            "2026-08-06T13:00:00+00:00",
            "2026-08-06T18:00:00+00:00",
        ]

        first_post_id = slots[0]["post_id"]
        skip = FakeQuery(f"drop:{first_post_id}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=skip,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        assert skip.markup_edits == [None]
        assert db.get_post(conn, first_post_id)["status"] == "skipped"
        assert "@plan-bravo" in fake.messages[-1]["text"]

        for position in range(1, 4):
            slot = conn.execute(
                "SELECT * FROM planning_slot WHERE session_id=? AND position=?",
                (session["id"], position),
            ).fetchone()
            make = FakeQuery(f"make1500:{slot['post_id']}")
            await bot.on_callback(
                SimpleNamespace(
                    callback_query=make,
                    effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
                ),
                SimpleNamespace(bot=fake),
            )
            slot = conn.execute(
                "SELECT * FROM planning_slot WHERE session_id=? AND position=?",
                (session["id"], position),
            ).fetchone()
            draft = db.get_draft(conn, slot["draft_id"])
            keyboard = fake.messages[-1]["reply_markup"].inline_keyboard
            labels = [button.text for row in keyboard for button in row]
            assert labels == [
                f"✅ Готово на завтра ({position}/3)",
                "✨ Короткий · EN ≤1500",
                "📖 Длинный · EN ≤3000",
                "✏️ Редактировать руками",
                "🤖 Редактировать с AI",
                "🧵 Пересобрать Threads с AI",
                "⏭ Другой материал",
                "⏹ Закончить на сегодня",
            ]
            if position == 2:
                edited = "Owner-edited second planning post."
                db.update_draft_texts(conn, draft["id"], "old-li", "old-x", "old-th", edited)
                db.set_draft_thread_items(
                    conn,
                    draft["id"],
                    ["Edited planning hook.", "Edited planning payoff."],
                )
            ready = FakeQuery(f"planready:{draft['id']}")
            await bot.on_callback(
                SimpleNamespace(
                    callback_query=ready,
                    effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
                ),
                SimpleNamespace(bot=fake),
            )
            assert ready.markup_edits == [None]

        session = db.get_planning_session(conn, session["id"])
        assert session["status"] == "scheduled"
        assert generated == 3 and published == []
        assert conn.execute(
            "SELECT COUNT(*) n FROM planning_slot WHERE session_id=? AND status='ready'",
            (session["id"],),
        ).fetchone()["n"] == 3
        assert "Нео" in fake.messages[-1]["text"]
        assert "Горжусь тобой" in fake.messages[-1]["text"]

        status_report = bot._status_report(
            conn,
            now=datetime(2026, 8, 6, 0, 30, tzinfo=ZoneInfo(config.TIMEZONE)),
        )
        assert "Всего в пуле: 6" in status_report
        assert "Осталось: 2" in status_report
        assert "Уже отправлено тебе: 4" in status_report
        assert "Проверка: 2 + 4 = 6" in status_report
        assert "Подготовлено: 3/3" in status_report
        assert "1. 09:00 · готов — Prepared planning draft 1." in status_report
        assert "2. 14:00 · готов — Owner-edited second planning post." in status_report
        assert "3. 19:00 · готов — Prepared planning draft 3." in status_report
        assert "Ошибок не обнаружено" in status_report

        conn.execute(
            "UPDATE planning_slot SET status='failed', last_error='Buffer timeout' "
            "WHERE session_id=? AND position=2",
            (session["id"],),
        )
        conn.commit()
        broken_report = bot._status_report(
            conn,
            now=datetime(2026, 8, 6, 0, 30, tzinfo=ZoneInfo(config.TIMEZONE)),
        )
        assert "🚨 Что не так:" in broken_report
        assert "слот 2: ошибка" in broken_report
        assert "слот 2: Buffer timeout" in broken_report
        conn.execute(
            "UPDATE planning_slot SET status='ready', last_error=NULL "
            "WHERE session_id=? AND position=2",
            (session["id"],),
        )
        conn.commit()

        duplicate_start = await bot.start_evening_planning(fake, now=planning_now)
        assert duplicate_start["created"] is False and duplicate_start["sent"] == 0

        for hour in (9, 14, 19):
            before_publish_messages = len(fake.messages)
            result = await bot.publish_due_planned(
                fake,
                now=datetime(2026, 8, 6, hour, 0, tzinfo=ZoneInfo(config.TIMEZONE)),
            )
            assert result["claimed"] == 1 and result["published"] == 1
            assert len(fake.messages) == before_publish_messages + 1
            assert "опубликован в LinkedIn, X и Threads" in fake.messages[-1]["text"]
        assert len(published) == 3
        assert published[0]["threads"] == ["Planning hook 1.", "Planning payoff 1."]
        assert published[1]["linkedin"] == "Owner-edited second planning post."
        assert published[1]["twitter"] == "Owner-edited second planning post."
        assert published[1]["threads"] == ["Edited planning hook.", "Edited planning payoff."]
        session = db.get_planning_session(conn, session["id"])
        assert session["status"] == "published"
    finally:
        conn.close()
        generator.translate_post = original_translate
        publisher.publish_all = original_publish
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_unbounded_curation_shelf_and_fifo_publish() -> None:
    """Manual curation can save any count; each cron tick publishes one FIFO item."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_translate = generator.translate_post
    original_publish = publisher.publish_all
    config.DB_PATH = tmp.name
    config.OWNER_CHAT_ID = 123
    config.BOT_SEND_DELAY = 0
    conn = db.connect()
    published: list[str] = []
    try:
        for index in range(1, 6):
            source_id = db.upsert_source(conn, f"@shelf-{index}", f"Shelf {index}")
            assert db.insert_post(
                conn,
                source_id,
                index,
                f"2026-05-0{index}T10:00:00+00:00",
                f"Source shelf material {index}. " * 20,
                f"https://t.me/shelf_{index}/{index}",
            )

        def fake_translate(*args, **_kwargs):
            source_text = args[-1]
            number = source_text.split("material ", 1)[1].split(".", 1)[0]
            text = f"Shelf-ready draft {number}."
            return generator.DraftOut(
                linkedin_text=text,
                x_text=text,
                threads_text=text,
                notes="",
                thread_items=[f"Shelf hook {number}.", f"Shelf payoff {number}."],
            )

        def fake_publish(texts, image_url=None):
            assert image_url is None
            published.append(str(texts["linkedin"]))
            return {
                platform: (True, f"buffer-{len(published)}-{platform}")
                for platform in texts
            }

        generator.translate_post = fake_translate
        publisher.publish_all = fake_publish
        fake = FakeBot()

        started = await bot.start_curation(fake)
        assert started["created"] is True and started["sent"] == 1
        session = db.active_curation_session(conn)
        assert session is not None and session["saved_count"] == 0
        first_item = db.current_curation_item(conn, session["id"])
        assert first_item["status"] == "reviewing"

        skipped = FakeQuery(f"drop:{first_item['post_id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=skipped,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        second_item = db.current_curation_item(conn, session["id"])
        assert second_item["id"] == first_item["id"]
        assert second_item["post_id"] != first_item["post_id"]

        make = FakeQuery(f"make:{second_item['post_id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=make,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        reviewing = db.current_curation_item(conn, session["id"])
        draft = db.get_draft(conn, reviewing["draft_id"])
        labels = [
            button.text
            for row in bot._draft_keyboard(conn, draft["id"]).inline_keyboard
            for button in row
        ]
        assert "📥 Сохранить на полку" in labels
        assert "🔴 Закончить накидывать" in labels

        shelf = FakeQuery(f"shelf:{draft['id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=shelf,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        assert db.ready_queue_stats(conn)["ready"] == 1
        assert db.active_curation_session(conn)["saved_count"] == 1
        current = db.current_curation_item(conn, session["id"])
        assert current["position"] == 2 and current["status"] == "reviewing"

        stop = FakeQuery(f"curstoppost:{current['post_id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=stop,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        assert db.active_curation_session(conn) is None
        assert db.ready_queue_stats(conn)["ready"] == 1

        manual_source = db.upsert_source(conn, "manual:123", "Own posts")
        assert db.insert_post(
            conn,
            manual_source,
            999,
            "2026-08-06T12:00:00+00:00",
            "Custom shelf post.",
            None,
            status="drafted",
            media_kind="manual",
        )
        manual_post = conn.execute(
            "SELECT * FROM post WHERE source_id=? AND tg_message_id=999",
            (manual_source,),
        ).fetchone()
        manual_draft_id = db.create_draft(
            conn,
            manual_post["id"],
            "manual/raw",
            "Custom shelf post.",
            "Custom shelf post.",
            "Custom shelf post.",
            "",
            ["Custom shelf post."],
        )
        before_custom_shelf = len(fake.messages)
        custom_shelf = FakeQuery(f"shelf:{manual_draft_id}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=custom_shelf,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        assert db.ready_queue_stats(conn)["ready"] == 2
        assert len(fake.messages) == before_custom_shelf + 1
        shelf_menu = fake.messages[-1]["reply_markup"].keyboard
        assert [[button.text for button in row] for row in shelf_menu] == [
            [bot.CURATION_BUTTON],
            [bot.NEW_POST_BUTTON, bot.STATS_BUTTON],
        ]

        first_tick = await bot.publish_scheduled_tick(
            fake,
            now=datetime(2026, 8, 7, 9, 0, tzinfo=ZoneInfo(config.TIMEZONE)),
        )
        assert first_tick["source"] == "shelf" and first_tick["published"] == 1
        assert db.ready_queue_stats(conn)["ready"] == 1
        second_tick = await bot.publish_scheduled_tick(
            fake,
            now=datetime(2026, 8, 7, 14, 0, tzinfo=ZoneInfo(config.TIMEZONE)),
        )
        assert second_tick["published"] == 1
        assert db.ready_queue_stats(conn).get("ready", 0) == 0
        assert published == [draft["linkedin_text"], "Custom shelf post."]
    finally:
        conn.close()
        generator.translate_post = original_translate
        publisher.publish_all = original_publish
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_refetch_after_queue_exhaustion() -> None:
    """An empty durable slot is retried after a pointer-based Telegram refetch."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    original_run_fetch = ingest.run_fetch
    calls = 0

    async def fake_run_fetch(sources, *, window_start, window_end, incremental):
        nonlocal calls
        calls += 1
        assert sources == ["@fresh"]
        assert window_start is None and window_end is not None and incremental is True
        fetch_conn = db.connect()
        try:
            source_id = db.upsert_source(fetch_conn, "@fresh", "Fresh")
            assert db.insert_post(
                fetch_conn,
                source_id,
                42,
                "2026-08-01T10:00:00+00:00",
                "Материал, появившийся после последнего указателя " * 10,
                "https://t.me/fresh/42",
            )
            db.set_last_message_id(fetch_conn, source_id, 42)
        finally:
            fetch_conn.close()
        return {"sources": 1, "seen": 1, "added": 1, "errors": {}}

    try:
        config.DB_PATH = tmp.name
        conn = db.connect()
        db.reconcile_active_sources(conn, ["@fresh"])
        conn.close()
        ingest.run_fetch = fake_run_fetch
        fake = FakeBot()
        assert await bot.propose_batch(fake, slot_key="empty-then-refetch") == 1
        assert calls == 1
        assert any("Материал, появившийся" in message["text"] for message in fake.messages)
        verify = db.connect()
        try:
            source = verify.execute(
                "SELECT last_message_id FROM source WHERE username='@fresh'"
            ).fetchone()
            assert source["last_message_id"] == 42
            assert db.get_meta(verify, "last_incremental_refetch_added") == "1"
        finally:
            verify.close()
    finally:
        ingest.run_fetch = original_run_fetch
        config.DB_PATH = old_db_path
        Path(tmp.name).unlink(missing_ok=True)


async def test_edit_retry_loop() -> None:
    """An over-limit edit must create a new durable ForceReply target."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_threadify = generator.threadify_post

    def fake_threadify(text: str):
        return generator.ThreadPlanOut(
            thread_items=["Edited hook.", f"Edited payoff: {text}"],
            notes="Тестовый Threads-план",
        )

    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        generator.threadify_post = fake_threadify
        conn = db.connect()
        source_id = db.upsert_source(conn, "@edit-loop", "Edit loop")
        assert db.insert_post(
            conn,
            source_id,
            1,
            "2026-08-03T12:00:00+00:00",
            "Исходный текст",
            "https://t.me/edit-loop/1",
        )
        claimed = db.claim_oldest_posts(conn, "edit-loop", max_items=1)[0]
        assert db.mark_delivery_sent(conn, claimed["id"], 100, claimed["claim_token"])
        assert db.transition_post(conn, claimed["id"], ("offered",), "generating")
        draft_id = db.create_draft(
            conn,
            claimed["id"],
            "test-model",
            "Generated text",
            "Generated text",
            "Generated text",
            "",
        )
        db.set_draft_message(conn, draft_id, 101)
        db.set_edit_msg(conn, draft_id, 200)
        db.set_draft_status(conn, draft_id, "awaiting_review")
        conn.close()

        too_long = FakeIncomingMessage("x" * (config.MANUAL_MAX_POST_CHARS + 1), 200)
        await bot.on_reply(
            SimpleNamespace(message=too_long, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=FakeBot()),
        )
        assert len(too_long.responses) == 1
        assert "ответь прямо на это сообщение" in too_long.responses[0]["text"]
        assert too_long.responses[0]["reply_markup"].force_reply is True
        retry_prompt_id = too_long.first_response_id + 1
        verify = db.connect()
        assert db.get_draft(verify, draft_id)["edit_msg_id"] == retry_prompt_id
        assert db.get_draft(verify, draft_id)["edited_text"] is None
        verify.close()

        valid_text = "Final edited text that is safely under the limit."
        valid = FakeIncomingMessage(valid_text, retry_prompt_id, first_response_id=9_100)
        fake_bot = FakeBot()
        await bot.on_reply(
            SimpleNamespace(message=valid, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert valid.responses[0]["text"].startswith("⏳ Текст принят:")
        assert any(message["text"] == valid_text for message in fake_bot.messages)
        assert any(message["text"].startswith("🧵 Threads preview") for message in fake_bot.messages)
        verify = db.connect()
        assert db.get_draft(verify, draft_id)["edited_text"] == valid_text
        assert json.loads(db.get_draft(verify, draft_id)["threads_json"]) == [
            "Edited hook.",
            f"Edited payoff: {valid_text}",
        ]
        assert db.get_draft(verify, draft_id)["status"] == "awaiting_review"
        verify.close()

        unknown = FakeIncomingMessage("Orphan reply", 999_999)
        await bot.on_reply(
            SimpleNamespace(message=unknown, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=FakeBot()),
        )
        assert "не нашла активное редактирование" in unknown.responses[0]["text"]
    finally:
        generator.threadify_post = original_threadify
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_anytime_owner_post() -> None:
    """Custom text stays raw until the owner explicitly chooses an AI action."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_translate = generator.translate_post
    original_threadify = generator.threadify_post
    translate_calls: list[tuple[str, str]] = []
    threadify_calls: list[str] = []

    def fake_translate(source: str, date: str, text: str, **_kwargs):
        translate_calls.append((source, text))
        return generator.DraftOut(
            linkedin_text="Prepared English owner post.",
            x_text="Prepared English owner post.",
            threads_text="Prepared English owner post.",
            notes="",
            thread_items=["A strong owner-post hook.", "The owner-post payoff."],
        )

    def fake_threadify(text: str):
        threadify_calls.append(text)
        return generator.ThreadPlanOut(
            thread_items=["Manual edit hook.", "Manual edit payoff."],
            notes="",
        )

    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        generator.translate_post = fake_translate
        generator.threadify_post = fake_threadify

        start_message = FakeIncomingMessage(bot.NEW_POST_BUTTON, 0)
        await bot.cmd_new_post(
            SimpleNamespace(message=start_message, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(),
        )
        assert "Что создаём?" in start_message.responses[0]["text"]
        menu_labels = [
            button.text
            for row in start_message.responses[0]["reply_markup"].inline_keyboard
            for button in row
        ]
        assert menu_labels == ["📚 Начать накидывать", "✍️ Написать свой текст", "❌ Отменить"]

        fake_bot = FakeBot()
        custom = FakeQuery("newcustom:0")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=custom,
                effective_chat=SimpleNamespace(id=123),
            ),
            SimpleNamespace(bot=fake_bot),
        )
        assert "без AI" in fake_bot.messages[-1]["text"]
        assert fake_bot.messages[-1]["reply_markup"].force_reply is True
        prompt_id = 1
        verify = db.connect()
        pending = db.pending_owner_post(verify, 123)
        assert pending is not None
        assert pending["manual_prompt_id"] == prompt_id
        assert pending["media_kind"] == "manual"
        verify.close()

        source_text = "Моя новая идея для собственного поста."
        submission = FakeIncomingMessage(source_text, prompt_id, first_response_id=9_100)
        await bot.on_reply(
            SimpleNamespace(message=submission, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert "без AI" in submission.responses[0]["text"]
        assert translate_calls == []
        assert fake_bot.messages[-3]["text"] == f"📄 LinkedIn / X · {len(source_text)} символов"
        assert fake_bot.messages[-2]["text"] == source_text
        assert fake_bot.messages[-1]["text"].startswith("🧵 Threads-версия появится")
        verify = db.connect()
        draft = verify.execute("SELECT * FROM draft ORDER BY id DESC LIMIT 1").fetchone()
        stored_post = db.get_post(verify, draft["post_id"])
        assert stored_post["text"] == source_text
        assert draft["status"] == "awaiting_review"
        raw_labels = [
            button.text
            for row in fake_bot.messages[-1]["reply_markup"].inline_keyboard
            for button in row
        ]
        assert raw_labels == [
            "✅ Опубликовать сейчас",
            "📥 На полку",
            "✨ Короткий · EN ≤1500",
            "📖 Длинный · EN ≤3000",
            "✏️ Редактировать руками",
            "🤖 Редактировать с AI",
            "🧵 Пересобрать Threads с AI",
            "❌ Отменить",
        ]
        verify.close()

        raw_publish = FakeQuery(f"pub:{draft['id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=raw_publish,
                effective_chat=SimpleNamespace(id=123),
            ),
            SimpleNamespace(bot=fake_bot),
        )
        assert threadify_calls == [source_text]
        verify = db.connect()
        assert db.get_draft(verify, draft["id"])["status"] == "awaiting_review"
        assert json.loads(db.get_draft(verify, draft["id"])["threads_json"]) == [
            "Manual edit hook.",
            "Manual edit payoff.",
        ]
        verify.close()
        assert any(
            "нажми финальную кнопку ещё раз" in message["text"]
            for message in fake_bot.messages
        )

        transform = FakeQuery(f"transform:{draft['id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=transform,
                effective_chat=SimpleNamespace(id=123),
            ),
            SimpleNamespace(bot=fake_bot),
        )
        assert translate_calls == [("Собственный пост", source_text)]
        assert any(message["text"] == "Prepared English owner post." for message in fake_bot.messages)
        assert any(message["text"] == "A strong owner-post hook." for message in fake_bot.messages)
        verify = db.connect()
        draft_message_id = db.get_draft(verify, draft["id"])["tg_message_id"]
        verify.close()

        expanded = "E" * 2_000
        edit = FakeIncomingMessage(expanded, draft_message_id, first_response_id=9_200)
        second_bot = FakeBot()
        await bot.on_reply(
            SimpleNamespace(message=edit, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=second_bot),
        )
        assert "2000/3000" in edit.responses[0]["text"]
        assert any(message["text"] == expanded for message in second_bot.messages)
        assert any(message["text"].startswith("🧵 Threads preview") for message in second_bot.messages)
        assert len(translate_calls) == 1, "ручная версия не должна снова запускать полный transform"
        assert threadify_calls == [source_text, expanded]
        verify = db.connect()
        assert len(db.get_draft(verify, draft["id"])["edited_text"]) == 2_000
        verify.close()
    finally:
        generator.translate_post = original_translate
        generator.threadify_post = original_threadify
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_anytime_owner_post_with_photo_caption() -> None:
    """A custom Telegram photo+caption must create a media-aware raw draft."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    old_public_url = config.PUBLIC_BASE_URL
    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        config.PUBLIC_BASE_URL = "https://content.example"
        fake_bot = FakeBot()
        await bot._open_custom_post(fake_bot)
        prompt_id = fake_bot.messages[-1]["chat_id"] and 1
        caption = "software engineers before vs after agents"
        submission = FakeIncomingMessage(
            None,
            prompt_id,
            caption=caption,
            photo=[SimpleNamespace(file_id="owner-photo-large", file_size=456_789)],
        )
        await bot.on_reply(
            SimpleNamespace(message=submission, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )

        conn = db.connect()
        draft = conn.execute("SELECT * FROM draft ORDER BY id DESC LIMIT 1").fetchone()
        assert draft is not None and draft["linkedin_text"] == caption
        post = db.get_post(conn, draft["post_id"])
        assert post["media_kind"] == "manual"
        assert post["bot_media_file_id"] == "owner-photo-large"
        assert post["media_size"] == 456_789
        assert post["media_mime"] == "image/jpeg"
        assert post["media_access_token"]
        assert bot._draft_has_photo(conn, draft)
        assert bot._photo_publish_error(conn, draft) is None
        conn.close()
        assert submission.responses[0]["text"].startswith("✅ Текст принят")

        await bot._open_custom_post(fake_bot)
        second_prompt_id = len(fake_bot.messages)
        photo_only = FakeIncomingMessage(
            None,
            second_prompt_id,
            first_response_id=9_500,
            photo=[SimpleNamespace(file_id="owner-photo-first", file_size=123_456)],
        )
        await bot.on_reply(
            SimpleNamespace(message=photo_only, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert photo_only.responses[0]["text"].startswith("🖼 Картинку сохранила")
        text_after_photo = FakeIncomingMessage("Text sent after the photo.", 9_501)
        await bot.on_reply(
            SimpleNamespace(message=text_after_photo, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        conn = db.connect()
        staged_draft = conn.execute("SELECT * FROM draft ORDER BY id DESC LIMIT 1").fetchone()
        staged_post = db.get_post(conn, staged_draft["post_id"])
        assert staged_draft["linkedin_text"] == "Text sent after the photo."
        assert staged_post["bot_media_file_id"] == "owner-photo-first"
        assert bot._draft_has_photo(conn, staged_draft)
        conn.close()
    finally:
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        config.PUBLIC_BASE_URL = old_public_url
        Path(tmp.name).unlink(missing_ok=True)


async def test_photo_choice_and_publication() -> None:
    """A source photo is optional and the chosen mode survives until publication."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    old_public_url = config.PUBLIC_BASE_URL
    original_publish = publisher.publish_all
    published: list[tuple[dict[str, str], str | None]] = []

    def fake_publish(texts, image_url=None):
        published.append((dict(texts), image_url))
        return {platform: (True, f"buffer-{platform}") for platform in texts}

    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        config.PUBLIC_BASE_URL = "https://content.example"
        publisher.publish_all = fake_publish
        conn = db.connect()
        source_id = db.upsert_source(conn, "@photo", "Photo")
        assert db.insert_post(
            conn,
            source_id,
            1,
            "2026-08-01T10:00:00+00:00",
            "Photo source text",
            "https://t.me/photo/1",
            status="offered",
            media_kind="photo",
            media_mime="image/jpeg",
            media_size=100_000,
        )
        post = conn.execute("SELECT * FROM post WHERE tg_message_id=1").fetchone()
        db.set_post_bot_media(conn, post["id"], "telegram-photo-file", "media-token")
        draft_id = db.create_draft(
            conn,
            post["id"],
            "test",
            "Ready image post",
            "Ready image post",
            "Ready image post",
            "",
            ["Ready image hook.", "Ready image payoff."],
        )
        fake_bot = FakeBot()
        choose = FakeQuery(f"pub:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=choose, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert published == []
        choice_labels = [
            button.text for row in choose.markup_edits[-1].inline_keyboard for button in row
        ]
        assert choice_labels == [
            "🖼 С картинкой сейчас",
            "📝 Без картинки сейчас",
            "↩️ Назад",
        ]

        with_photo = FakeQuery(f"pubwith:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=with_photo, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert published == [
            (
                {
                    "linkedin": "Ready image post",
                    "twitter": "Ready image post",
                    "threads": ["Ready image hook.", "Ready image payoff."],
                },
                "https://content.example/api/media/media-token",
            )
        ]
        assert db.get_draft(conn, draft_id)["include_media"] == 1

        assert db.insert_post(
            conn,
            source_id,
            2,
            "2026-08-02T10:00:00+00:00",
            "Planning photo source",
            "https://t.me/photo/2",
            status="offered",
            media_kind="photo",
            media_mime="image/jpeg",
            media_size=100_000,
        )
        second = conn.execute("SELECT * FROM post WHERE tg_message_id=2").fetchone()
        db.set_post_bot_media(conn, second["id"], "telegram-planning-photo", "planning-token")
        second_draft = db.create_draft(
            conn,
            second["id"],
            "test",
            "Planning image post",
            "Planning image post",
            "Planning image post",
            "",
            ["Planning image hook.", "Planning image payoff."],
        )
        session, _ = db.create_planning_session(
            conn,
            "2026-08-05",
            "2026-08-06",
            ["2026-08-06T08:00:00+00:00"],
        )
        slot = db.next_planning_slot(conn, session["id"])
        assert db.assign_planning_post(conn, slot["id"], second["id"])
        assert db.attach_planning_draft(conn, second["id"], second_draft)
        planning_choice = FakeQuery(f"planready:{second_draft}")
        await bot.on_callback(
            SimpleNamespace(callback_query=planning_choice, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert db.planning_slot_for_draft(conn, second_draft)["status"] == "reviewing"
        with_planning_photo = FakeQuery(f"planwith:{second_draft}")
        await bot.on_callback(
            SimpleNamespace(callback_query=with_planning_photo, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert db.planning_slot_for_draft(conn, second_draft)["status"] == "ready"
        assert db.get_draft(conn, second_draft)["include_media"] == 1
        scheduled_result = await bot.publish_due_planned(
            fake_bot,
            now=datetime(2026, 8, 6, 9, 0, tzinfo=ZoneInfo(config.TIMEZONE)),
        )
        assert scheduled_result["published"] == 1
        assert published[-1][1] == "https://content.example/api/media/planning-token"
        conn.close()
    finally:
        publisher.publish_all = original_publish
        config.PUBLIC_BASE_URL = old_public_url
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_anytime_database_iteration() -> None:
    """The menu starts a durable unbounded shelf-curation session."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        conn = db.connect()
        source_id = db.upsert_source(conn, "@ondemand", "On demand")
        assert db.insert_post(
            conn,
            source_id,
            1,
            "2026-08-01T10:00:00+00:00",
            "Материал для внепланового поста",
            "https://t.me/ondemand/1",
        )
        fake_bot = FakeBot()
        application = FakeApplication()
        query = FakeQuery("newdb:0")
        await bot.on_callback(
            SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot, application=application),
        )
        assert application.tasks == []
        assert "Начинаем накидывать" in fake_bot.messages[0]["text"]
        assert "Накидывание #1" in fake_bot.messages[1]["text"]
        assert fake_bot.messages[-1]["text"] == "Материал для внепланового поста"
        labels = [
            button.text
            for row in fake_bot.messages[-1]["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "🔴 Закончить накидывать" in labels
        post = conn.execute("SELECT * FROM post WHERE tg_message_id=1").fetchone()
        assert post["status"] == "offered"
        assert db.active_curation_session(conn) is not None
        conn.close()
    finally:
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_direct_photo_delivery_captures_file_id() -> None:
    """Photo review bypasses cross-instance staging and persists Bot API media."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_download = ingest.download_post_media

    async def fake_download(_post, destination):
        path = Path(destination) / "photo.jpg"
        path.write_bytes(b"fake-jpeg")
        return path

    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        ingest.download_post_media = fake_download
        conn = db.connect()
        source_id = db.upsert_source(conn, "@photo-delivery", "Photo delivery")
        assert db.insert_post(
            conn,
            source_id,
            1,
            "2026-08-01T10:00:00+00:00",
            "Photo caption",
            "https://t.me/photo-delivery/1",
            media_kind="photo",
            media_mime="image/jpeg",
            media_size=9,
        )
        fake_bot = FakeBot()
        assert await bot.propose_batch(fake_bot, slot_key="photo-delivery", max_items=1) == 1
        post = conn.execute("SELECT * FROM post WHERE tg_message_id=1").fetchone()
        assert fake_bot.photos and post["bot_media_file_id"] == "bot-photo-1"
        assert post["media_access_token"] and len(post["media_access_token"]) > 30
        conn.close()
    finally:
        ingest.download_post_media = original_download
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_send_retries_connect_timeout() -> None:
    """A pre-connect Telegram timeout is safe to retry inside the webhook request."""
    class ConnectTimeout(Exception):
        pass

    attempts = 0
    sleeps: list[float] = []
    original_sleep = bot.asyncio.sleep
    old_delay = config.BOT_SEND_DELAY

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def flaky_send():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            try:
                raise ConnectTimeout("connect failed")
            except ConnectTimeout as exc:
                raise bot.TimedOut("timed out") from exc
        return "sent"

    try:
        config.BOT_SEND_DELAY = 0
        bot.asyncio.sleep = fake_sleep
        assert await bot._send(flaky_send) == "sent"
        assert attempts == 2
        assert sleeps == [0.5]
    finally:
        bot.asyncio.sleep = original_sleep
        config.BOT_SEND_DELAY = old_delay


async def test_long_draft_controls_and_chunked_preview() -> None:
    """1500 is optional; oversized previews are lossless and 3000 is the publish gate."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_compress = generator.compress_post
    original_translate = generator.translate_post
    original_publish = publisher.publish_all
    compress_targets: list[int] = []
    translate_targets: list[int] = []
    publish_calls = 0

    def fake_compress(text: str, target_chars: int):
        compress_targets.append(target_chars)
        result = "C" * target_chars
        return generator.DraftOut(
            linkedin_text=result,
            x_text=result,
            threads_text=result,
            notes="",
            thread_items=["C" * min(config.THREAD_ITEM_CHARS, target_chars)],
        )

    def fake_translate(source: str, date: str, text: str, *, max_chars: int):
        translate_targets.append(max_chars)
        result = "E" * min(2_900, max_chars)
        return generator.DraftOut(
            linkedin_text=result,
            x_text=result,
            threads_text=result,
            notes="",
            thread_items=["English Threads card."],
        )

    def forbidden_publish(*_args, **_kwargs):
        nonlocal publish_calls
        publish_calls += 1
        raise AssertionError("oversized draft reached Buffer")

    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        generator.compress_post = fake_compress
        generator.translate_post = fake_translate
        publisher.publish_all = forbidden_publish
        conn = db.connect()
        source_id = db.upsert_source(conn, "manual:123", "Own posts")
        long_master = "L" * 4_500
        assert db.insert_post(
            conn,
            source_id,
            1,
            "2026-08-07T12:00:00+00:00",
            long_master,
            None,
            status="drafted",
            media_kind="manual",
        )
        post = conn.execute("SELECT * FROM post WHERE tg_message_id=1").fetchone()
        draft_id = db.create_draft(
            conn,
            post["id"],
            "manual/raw",
            long_master,
            long_master,
            long_master,
            "",
            [str(index) + ("T" * 470) for index in range(10)],
        )
        fake_bot = FakeBot()
        await bot._send_draft(fake_bot, conn, draft_id)
        assert all(len(message["text"]) <= 4_000 for message in fake_bot.messages)
        assert len(fake_bot.messages) >= 4
        labels = [
            button.text
            for row in fake_bot.messages[-1]["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "✨ Короткий · EN ≤1500" in labels
        assert "📖 Длинный · EN ≤3000" in labels

        publish_query = FakeQuery(f"pub:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=publish_query, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert publish_calls == 0
        assert "максимум — 3000" in fake_bot.messages[-1]["text"]

        fit_query = FakeQuery(f"fitplatform:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=fit_query, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert compress_targets == [config.PLATFORM_SAFE_CHARS]
        assert len(bot._draft_body(db.get_draft(conn, draft_id))) == config.PLATFORM_SAFE_CHARS
        fitted_labels = [
            button.text
            for row in fake_bot.messages[-1]["reply_markup"].inline_keyboard
            for button in row
        ]
        assert "📖 Длинный · EN ≤3000" in fitted_labels

        long_query = FakeQuery(f"transform3000:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=long_query, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert translate_targets == [config.PLATFORM_SAFE_CHARS]

        short_query = FakeQuery(f"transform1500:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=short_query, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert translate_targets[-1] == config.MAX_POST_CHARS

        translate_query = FakeQuery(f"translateonly:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=translate_query, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert translate_targets == [
            config.PLATFORM_SAFE_CHARS,
            config.MAX_POST_CHARS,
            config.PLATFORM_SAFE_CHARS,
        ]

        compact_query = FakeQuery(f"compress1500:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(callback_query=compact_query, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=fake_bot),
        )
        assert compress_targets == [config.PLATFORM_SAFE_CHARS, config.MAX_POST_CHARS]
        assert len(bot._draft_body(db.get_draft(conn, draft_id))) == config.MAX_POST_CHARS

        raw_source_id = db.upsert_source(conn, "@raw-compress", "Raw compress")
        assert db.insert_post(
            conn,
            raw_source_id,
            2,
            "2026-08-07T13:00:00+00:00",
            "Русский текст без перевода " * 100,
            "https://t.me/raw_compress/2",
        )
        raw_post = conn.execute("SELECT * FROM post WHERE tg_message_id=2").fetchone()
        db.set_post_status(conn, raw_post["id"], "offered")
        raw_compress_query = FakeQuery(f"rawcompress:{raw_post['id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=raw_compress_query,
                effective_chat=SimpleNamespace(id=123),
            ),
            SimpleNamespace(bot=fake_bot),
        )
        assert compress_targets[-1] == config.MAX_POST_CHARS
        assert db.active_draft_for_post(conn, raw_post["id"]) is not None

        assert db.insert_post(
            conn,
            raw_source_id,
            3,
            "2026-08-07T14:00:00+00:00",
            "Ещё один полный русский текст " * 100,
            "https://t.me/raw_compress/3",
        )
        translate_post = conn.execute("SELECT * FROM post WHERE tg_message_id=3").fetchone()
        db.set_post_status(conn, translate_post["id"], "offered")
        raw_translate_query = FakeQuery(f"make3000:{translate_post['id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=raw_translate_query,
                effective_chat=SimpleNamespace(id=123),
            ),
            SimpleNamespace(bot=fake_bot),
        )
        assert translate_targets[-1] == config.PLATFORM_SAFE_CHARS
        assert db.active_draft_for_post(conn, translate_post["id"]) is not None
        conn.close()
    finally:
        generator.compress_post = original_compress
        generator.translate_post = original_translate
        publisher.publish_all = original_publish
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def main() -> None:
    test_shelf_status_plan()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_translate = generator.translate_post
    original_revise = generator.revise_post
    original_threadify = generator.threadify_post
    original_publish = publisher.publish_all
    original_stage = ingest.stage_post_for_bot
    original_cleanup = ingest.delete_bot_staging_messages
    original_bot_username = bot._bot_username
    calls = {"generate": 0, "publish": 0}
    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        conn = db.connect()
        source_id = db.upsert_source(conn, "@source", "Source")
        db.insert_post(
            conn,
            source_id,
            10,
            "2026-04-28T10:00:00+00:00",
            "Сырой материал " * 30,
            "https://t.me/source/10",
        )

        def forbidden_generate(*args, **kwargs):
            calls["generate"] += 1
            raise AssertionError("LLM был вызван до кнопки")

        def forbidden_publish(*args, **kwargs):
            calls["publish"] += 1
            raise AssertionError("Buffer был вызван до кнопки")

        generator.translate_post = forbidden_generate
        publisher.publish_all = forbidden_publish
        fake = FakeBot()
        sent = await bot.propose_batch(fake, slot_key="offline-test")
        assert sent == 1
        assert len(fake.messages) == 2
        assert calls == {"generate": 0, "publish": 0}
        row = conn.execute("SELECT status FROM post WHERE tg_message_id=10").fetchone()
        assert row["status"] == "offered"
        assert fake.messages[0]["text"].startswith("📥 Source")
        assert fake.messages[1]["text"].startswith("Сырой материал")
        keyboard = fake.messages[1]["reply_markup"].inline_keyboard
        labels = [button.text for row in keyboard for button in row]
        assert labels == [
            "🟢 Двигаемся с этим постом",
            "🟡 Скипнуть",
        ]

        route_calls = {"translate": 0}

        def fake_translate(*args, **kwargs):
            route_calls["translate"] += 1
            return generator.DraftOut(
                linkedin_text="Faithful English translation.",
                x_text="Faithful English translation.",
                threads_text="Faithful English translation.",
                notes="Служебная заметка о заменах",
                thread_items=[
                    "Why do faithful translations still fail on Threads?",
                    "Because structure matters as much as wording.",
                ],
            )

        generator.translate_post = fake_translate

        text_post_id = conn.execute(
            "SELECT id FROM post WHERE tg_message_id=10"
        ).fetchone()["id"]
        text_query = FakeQuery(f"select:{text_post_id}")
        text_update = SimpleNamespace(
            callback_query=text_query,
            effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
        )
        before_text_generation = len(fake.messages)
        await bot.on_callback(text_update, SimpleNamespace(bot=fake))
        text_generation_messages = fake.messages[before_text_generation:]
        assert route_calls == {"translate": 0}
        assert len(text_generation_messages) == 4
        assert text_generation_messages[0]["text"].startswith("💜 Отлично, с этим постом")
        assert text_generation_messages[1]["text"].startswith("📄 LinkedIn / X ·")
        assert text_generation_messages[2]["text"].startswith("Сырой материал")
        assert text_generation_messages[3]["text"].startswith("🧵 Threads-версия появится")
        draft_keyboard = text_generation_messages[3]["reply_markup"].inline_keyboard
        draft_labels = [button.text for row in draft_keyboard for button in row]
        assert draft_labels == [
            "✅ Опубликовать сейчас",
            "📥 На полку",
            "✨ Короткий · EN ≤1500",
            "📖 Длинный · EN ≤3000",
            "✏️ Редактировать руками",
            "🤖 Редактировать с AI",
            "🧵 Пересобрать Threads с AI",
            "⏭ Другой материал",
            "⏹ Закончить итерацию",
        ]
        assert all(
            "Идея:" not in message["text"]
            for message in text_generation_messages
        ), "обычный текст не должен получать отдельное сообщение с идеей"

        draft_id = conn.execute(
            "SELECT id FROM draft WHERE post_id=?",
            (text_post_id,),
        ).fetchone()["id"]
        assert db.get_draft(conn, draft_id)["threads_json"] is None

        transform_query = FakeQuery(f"transform1500:{draft_id}")
        before_transform = len(fake.messages)
        await bot.on_callback(
            SimpleNamespace(
                callback_query=transform_query,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        transformed_messages = fake.messages[before_transform:]
        assert route_calls == {"translate": 1}
        assert transformed_messages[0]["text"].startswith("⏳ Пост #")
        assert any(
            message["text"].startswith("⚠️ Заменено / проверить:")
            for message in transformed_messages
        )
        assert any(
            message["text"] == "Faithful English translation."
            for message in transformed_messages
        )
        assert any(
            message["text"] == "Why do faithful translations still fail on Threads?"
            for message in transformed_messages
        )
        assert json.loads(db.get_draft(conn, draft_id)["threads_json"]) == [
            "Why do faithful translations still fail on Threads?",
            "Because structure matters as much as wording.",
        ]

        manual_edit_button = FakeQuery(f"edit:{draft_id}")
        before_manual_edit = len(fake.messages)
        await bot.on_callback(
            SimpleNamespace(
                callback_query=manual_edit_button,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        assert len(fake.messages) == before_manual_edit + 1
        assert fake.messages[-1]["text"] == "waiting for edited text:"
        assert fake.messages[-1]["reply_markup"].force_reply is True
        assert "Faithful English translation" not in fake.messages[-1]["text"]
        assert db.get_draft(conn, draft_id)["edit_msg_id"] == len(fake.messages)

        threadify_calls: list[str] = []

        def fake_threadify(text: str):
            threadify_calls.append(text)
            return generator.ThreadPlanOut(
                thread_items=["A rebuilt Threads hook.", "A rebuilt Threads payoff."],
                notes="Hook и payoff пересобраны",
            )

        generator.threadify_post = fake_threadify
        threadify_button = FakeQuery(f"threadify:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=threadify_button,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        assert threadify_calls == ["Faithful English translation."]
        assert json.loads(db.get_draft(conn, draft_id)["threads_json"]) == [
            "A rebuilt Threads hook.",
            "A rebuilt Threads payoff.",
        ]
        assert any(
            message["text"] == "A rebuilt Threads hook."
            for message in fake.messages
        )

        revise_calls: list[tuple[str, str]] = []

        def fake_revise(current_text: str, instruction: str):
            revise_calls.append((current_text, instruction))
            revised = "Faithful English translation with a sharper joke."
            return generator.DraftOut(
                linkedin_text=revised,
                x_text=revised,
                threads_text=revised,
                notes="Добавлена более острая шутка.",
                thread_items=[
                    "A sharper hook for the revised story.",
                    "And the joke lands only in the payoff.",
                ],
            )

        generator.revise_post = fake_revise
        ai_button = FakeQuery(f"aiedit:{draft_id}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=ai_button,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        ai_prompt_id = db.get_draft(conn, draft_id)["ai_prompt_id"]
        assert ai_prompt_id is not None and "AI-редактор открыт" in fake.messages[-1]["text"]
        instruction = FakeIncomingMessage("Добавь более острую шутку", ai_prompt_id)
        before_ai_messages = len(fake.messages)
        await bot.on_reply(
            SimpleNamespace(
                message=instruction,
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        assert instruction.responses[0]["text"].startswith("⏳ Terra редактирует")
        assert revise_calls == [("Faithful English translation.", "Добавь более острую шутку")]
        assert len(fake.messages) > before_ai_messages + 3
        assert any(
            message["text"] == "Faithful English translation with a sharper joke."
            for message in fake.messages[before_ai_messages:]
        )
        assert any("A sharper hook" in message["text"] for message in fake.messages[before_ai_messages:])
        assert db.get_draft(conn, draft_id)["ai_prompt_id"] is None

        finish_query = FakeQuery(f"draftskip:{draft_id}")
        finish_update = SimpleNamespace(
            callback_query=finish_query,
            effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
        )
        before_finish = len(fake.messages)
        await bot.on_callback(finish_update, SimpleNamespace(bot=fake))
        assert len(fake.messages) == before_finish + 1, (
            "завершение готового черновика не должно подбрасывать замену"
        )
        assert "Следующий материал придёт по расписанию" in fake.messages[-1]["text"]
        assert db.get_draft(conn, draft_id)["status"] == "skipped"
        assert db.get_post(conn, text_post_id)["status"] == "skipped"

        long_text = "Полный исходный текст. " * 500
        long_source = db.upsert_source(conn, "@long", "Long")
        db.insert_post(
            conn,
            long_source,
            11,
            "2026-04-29T10:00:00+00:00",
            long_text,
            "https://t.me/long/11",
        )
        before = len(fake.messages)
        assert await bot.propose_batch(fake, slot_key="offline-long") == 1
        delivered = fake.messages[before:]
        assert len(delivered) > 2
        assert "".join(part["text"] for part in delivered[1:]) == long_text
        assert "reply_markup" not in delivered[0]
        assert "reply_markup" in delivered[-1]

        await test_evening_planning_and_next_day_publish()
        await test_unbounded_curation_shelf_and_fifo_publish()
        await test_refetch_after_queue_exhaustion()
        await test_edit_retry_loop()
        await test_anytime_owner_post()
        await test_anytime_owner_post_with_photo_caption()
        await test_photo_choice_and_publication()
        await test_anytime_database_iteration()
        await test_direct_photo_delivery_captures_file_id()
        await test_send_retries_connect_timeout()
        await test_long_draft_controls_and_chunked_preview()

        waiter = asyncio.get_running_loop().create_future()
        bot._STAGING_WAITERS["unit-token"] = waiter
        staging_update = SimpleNamespace(
            effective_chat=SimpleNamespace(id=321),
            effective_message=SimpleNamespace(
                message_id=654,
                reply_to_message=SimpleNamespace(
                    text="repost-staging:unit-token",
                    caption=None,
                ),
            ),
        )
        await bot.on_staging_media(staging_update, None)
        assert waiter.result() == (321, 654, None)
        bot._STAGING_WAITERS.pop("unit-token", None)

        photo_waiter = asyncio.get_running_loop().create_future()
        bot._STAGING_WAITERS["photo-token"] = photo_waiter
        await bot.on_staging_media(
            SimpleNamespace(
                effective_chat=SimpleNamespace(id=321),
                effective_message=SimpleNamespace(
                    message_id=655,
                    photo=[SimpleNamespace(file_id="photo-small"), SimpleNamespace(file_id="photo-large")],
                    video=None,
                    reply_to_message=SimpleNamespace(
                        text="repost-staging:photo-token",
                        caption=None,
                    ),
                ),
            ),
            None,
        )
        assert photo_waiter.result() == (321, 655, "photo-large")
        bot._STAGING_WAITERS.pop("photo-token", None)

        cleanup_calls: list[tuple[str, list[int]]] = []
        stage_calls: list[int] = []

        async def fake_bot_username(_bot):
            return "test_bot"

        async def fake_stage(post, username, token):
            stage_calls.append(post["id"])
            sender_id = 123 if post["username"] == "@media-owner" else 999
            inbound = SimpleNamespace(
                effective_chat=SimpleNamespace(id=sender_id),
                effective_message=SimpleNamespace(
                    message_id=700 + post["id"],
                    reply_to_message=SimpleNamespace(
                        text=f"repost-staging:{token}",
                        caption=None,
                    ),
                ),
            )
            await bot.on_staging_media(inbound, None)
            return sender_id, 800 + post["id"], 900 + post["id"]

        async def fake_cleanup(username, message_ids):
            cleanup_calls.append((username, list(message_ids)))

        bot._bot_username = fake_bot_username
        ingest.stage_post_for_bot = fake_stage
        ingest.delete_bot_staging_messages = fake_cleanup
        for username, message_id in (("@media-owner", 21), ("@media-other", 22)):
            source_id = db.upsert_source(conn, username, username)
            db.insert_post(
                conn,
                source_id,
                message_id,
                "2026-05-01T10:00:00+00:00",
                f"YouTube caption {message_id}",
                f"https://t.me/{username[1:]}/{message_id}",
                media_kind="video",
                media_mime="video/mp4",
                media_size=200_000_000,
            )
            assert await bot.propose_batch(
                fake,
                slot_key=f"media-{message_id}",
                source_username=username,
                max_items=1,
            ) == 1
        assert stage_calls == [], "video must never enter Telethon staging"
        assert fake.copies == [], "video must never be copied through Telegram"
        assert cleanup_calls == [], "no video staging means no media cleanup"
        assert any(message["text"] == "YouTube caption 21" for message in fake.messages)
        assert any(message["text"] == "YouTube caption 22" for message in fake.messages)
        assert all("Медиа пока не удалось переслать" not in message["text"] for message in fake.messages)

        video_post = conn.execute(
            "SELECT * FROM post WHERE tg_message_id=21"
        ).fetchone()
        before_select = len(fake.messages)
        await bot.on_callback(
            SimpleNamespace(
                callback_query=FakeQuery(f"select:{video_post['id']}"),
                effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
            ),
            SimpleNamespace(bot=fake),
        )
        selected_messages = fake.messages[before_select:]
        video_draft = conn.execute(
            "SELECT * FROM draft WHERE post_id=? ORDER BY id DESC LIMIT 1",
            (video_post["id"],),
        ).fetchone()
        assert video_draft["linkedin_text"] == "YouTube caption 21"
        assert all("Напиши свой текст" not in message["text"] for message in selected_messages)
        print("Workflow-тест пройден: raw → выбор, без LLM и Buffer")
    finally:
        generator.translate_post = original_translate
        generator.revise_post = original_revise
        generator.threadify_post = original_threadify
        publisher.publish_all = original_publish
        ingest.stage_post_for_bot = original_stage
        ingest.delete_bot_staging_messages = original_cleanup
        bot._bot_username = original_bot_username
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
