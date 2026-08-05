"""Offline async test: raw delivery must not call the LLM or Buffer."""
import asyncio
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
    def __init__(self, text: str, reply_to_id: int, first_response_id: int = 9_000):
        self.text = text
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
            make = FakeQuery(f"make:{slot['post_id']}")
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
                "✏️ Редактировать руками",
                "🤖 Редактировать с AI",
                "⏭ Другой материал",
                "⏹ Закончить на сегодня",
            ]
            if position == 2:
                edited = "Owner-edited second planning post."
                db.update_draft_texts(conn, draft["id"], "old-li", "old-x", "old-th", edited)
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
        assert set(published[1].values()) == {"Owner-edited second planning post."}
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
    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
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
        assert fake_bot.messages[-1]["text"] == valid_text
        verify = db.connect()
        assert db.get_draft(verify, draft_id)["edited_text"] == valid_text
        assert db.get_draft(verify, draft_id)["status"] == "awaiting_review"
        verify.close()

        unknown = FakeIncomingMessage("Orphan reply", 999_999)
        await bot.on_reply(
            SimpleNamespace(message=unknown, effective_chat=SimpleNamespace(id=123)),
            SimpleNamespace(bot=FakeBot()),
        )
        assert "не нашёл активное редактирование" in unknown.responses[0]["text"]
    finally:
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
        Path(tmp.name).unlink(missing_ok=True)


async def test_anytime_owner_post() -> None:
    """Custom text stays raw until the owner explicitly runs Standard Transform."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_translate = generator.translate_post
    translate_calls: list[tuple[str, str]] = []

    def fake_translate(source: str, date: str, text: str):
        translate_calls.append((source, text))
        return generator.DraftOut(
            linkedin_text="Prepared English owner post.",
            x_text="Prepared English owner post.",
            threads_text="Prepared English owner post.",
            notes="",
        )

    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.BOT_SEND_DELAY = 0
        generator.translate_post = fake_translate

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
        assert menu_labels == ["📚 Накидывать из базы", "✍️ Написать свой текст", "❌ Отменить"]

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
        assert fake_bot.messages[-1]["text"] == source_text
        verify = db.connect()
        draft = verify.execute("SELECT * FROM draft ORDER BY id DESC LIMIT 1").fetchone()
        assert draft["status"] == "awaiting_review"
        raw_labels = [
            button.text
            for row in fake_bot.messages[-1]["reply_markup"].inline_keyboard
            for button in row
        ]
        assert raw_labels == [
            "✅ Опубликовать сейчас",
            "✨ Standard Transform",
            "✏️ Редактировать руками",
            "🤖 Редактировать с AI",
            "❌ Отменить",
        ]
        verify.close()

        transform = FakeQuery(f"transform:{draft['id']}")
        await bot.on_callback(
            SimpleNamespace(
                callback_query=transform,
                effective_chat=SimpleNamespace(id=123),
            ),
            SimpleNamespace(bot=fake_bot),
        )
        assert translate_calls == [("Собственный пост", source_text)]
        assert fake_bot.messages[-1]["text"] == "Prepared English owner post."
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
        assert second_bot.messages[-1]["text"] == expanded
        assert len(translate_calls) == 1, "ручная версия после transform не должна снова вызывать Terra"
        verify = db.connect()
        assert len(db.get_draft(verify, draft["id"])["edited_text"]) == 2_000
        verify.close()
    finally:
        generator.translate_post = original_translate
        config.DB_PATH = old_db_path
        config.OWNER_CHAT_ID = old_owner
        config.BOT_SEND_DELAY = old_delay
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
                    "threads": "Ready image post",
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
    """The menu can start exactly one immediate source-material iteration."""
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
        assert len(application.tasks) == 1
        await application.tasks[0]
        assert "внеплановую итерацию" in fake_bot.messages[0]["text"]
        assert "On demand" in fake_bot.messages[-1]["text"]
        post = conn.execute("SELECT * FROM post WHERE tg_message_id=1").fetchone()
        assert post["status"] == "offered"
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


async def main() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_translate = generator.translate_post
    original_revise = generator.revise_post
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
        assert len(fake.messages) == 1
        assert calls == {"generate": 0, "publish": 0}
        row = conn.execute("SELECT status FROM post WHERE tg_message_id=10").fetchone()
        assert row["status"] == "offered"
        keyboard = fake.messages[0]["reply_markup"].inline_keyboard
        labels = [button.text for row in keyboard for button in row]
        assert labels == ["✨ Создать пост", "⏭ Пропустить"]

        route_calls = {"translate": 0}

        def fake_translate(*args, **kwargs):
            route_calls["translate"] += 1
            return generator.DraftOut(
                linkedin_text="Faithful English translation.",
                x_text="Faithful English translation.",
                threads_text="Faithful English translation.",
                notes="Служебная заметка о заменах",
            )

        generator.translate_post = fake_translate

        text_post_id = conn.execute(
            "SELECT id FROM post WHERE tg_message_id=10"
        ).fetchone()["id"]
        text_query = FakeQuery(f"make:{text_post_id}")
        text_update = SimpleNamespace(
            callback_query=text_query,
            effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
        )
        before_text_generation = len(fake.messages)
        await bot.on_callback(text_update, SimpleNamespace(bot=fake))
        text_generation_messages = fake.messages[before_text_generation:]
        assert route_calls == {"translate": 1}
        assert len(text_generation_messages) == 3
        assert text_generation_messages[0]["text"].startswith("⏳ Пост #")
        assert "10–30 секунд" in text_generation_messages[0]["text"]
        assert text_generation_messages[1]["text"].startswith(
            "⚠️ Заменено / проверить:"
        )
        assert "Служебная заметка о заменах" in text_generation_messages[1]["text"]
        assert text_generation_messages[2]["text"] == "Faithful English translation."
        draft_keyboard = text_generation_messages[2]["reply_markup"].inline_keyboard
        draft_labels = [button.text for row in draft_keyboard for button in row]
        assert draft_labels == [
            "✅ Опубликовать сейчас",
            "✏️ Редактировать руками",
            "🤖 Редактировать с AI",
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

        revise_calls: list[tuple[str, str]] = []

        def fake_revise(current_text: str, instruction: str):
            revise_calls.append((current_text, instruction))
            revised = "Faithful English translation with a sharper joke."
            return generator.DraftOut(
                linkedin_text=revised,
                x_text=revised,
                threads_text=revised,
                notes="Добавлена более острая шутка.",
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
        assert len(fake.messages) == before_ai_messages + 2
        assert fake.messages[-1]["text"] == "Faithful English translation with a sharper joke."
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
        await test_refetch_after_queue_exhaustion()
        await test_edit_retry_loop()
        await test_anytime_owner_post()
        await test_photo_choice_and_publication()
        await test_anytime_database_iteration()
        await test_direct_photo_delivery_captures_file_id()

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

        async def fake_bot_username(_bot):
            return "test_bot"

        async def fake_stage(post, username, token):
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
                "",
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
        assert len(fake.copies) == 1
        assert fake.copies[0]["from_chat_id"] == 999
        assert len(cleanup_calls[0][1]) == 1
        assert len(cleanup_calls[1][1]) == 2
        print("Workflow-тест пройден: raw → выбор, без LLM и Buffer")
    finally:
        generator.translate_post = original_translate
        generator.revise_post = original_revise
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
