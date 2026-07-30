"""Offline async test: raw delivery must not call the LLM or Buffer."""
import asyncio
import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import bot, config, db, generator, ingest, publisher  # noqa: E402


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.messages: list[dict] = []
        self.copies: list[dict] = []
        self.deleted: list[tuple[int, int]] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append({"chat_id": chat_id, "text": text, **kwargs})
        return FakeMessage(len(self.messages))

    async def copy_message(self, **kwargs):
        self.copies.append(kwargs)
        return FakeMessage(10_000 + len(self.copies))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.answers: list[tuple[tuple, dict]] = []
        self.markup_edits: list[object] = []

    async def answer(self, *args, **kwargs):
        self.answers.append((args, kwargs))

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


async def test_daily_schedule_and_skip(calls: dict[str, int]) -> None:
    """Four scheduled items per day; a raw skip immediately adds one replacement."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    config.DB_PATH = tmp.name
    conn = db.connect()
    try:
        post_ids: dict[str, int] = {}
        for index, username in enumerate(
            ("@skip-alpha", "@skip-bravo", "@skip-charlie", "@skip-delta", "@skip-echo"),
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
            post_ids[username] = conn.execute(
                "SELECT id FROM post WHERE source_id=? AND tg_message_id=?",
                (source_id, index),
            ).fetchone()["id"]

        fake = FakeBot()
        await bot.propose_job(
            SimpleNamespace(bot=fake, job=SimpleNamespace(data={"slot": "10:00"}))
        )
        assert len(fake.messages) == 2, "утренний слот должен показать ровно два материала"
        assert "@skip-alpha" in fake.messages[0]["text"]
        assert "@skip-bravo" in fake.messages[1]["text"]

        skipped_id = post_ids["@skip-alpha"]
        query = FakeQuery(f"drop:{skipped_id}")
        update = SimpleNamespace(
            callback_query=query,
            effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
        )
        context = SimpleNamespace(bot=fake)
        before_skip = len(fake.messages)
        await bot.on_callback(update, context)
        assert query.markup_edits == [None]
        assert len(fake.messages) == before_skip + 2
        assert "Показываю следующий" in fake.messages[-2]["text"]
        assert "@skip-charlie" in fake.messages[-1]["text"]
        assert db.get_post(conn, skipped_id)["status"] == "skipped"
        assert calls == {"generate": 0, "publish": 0}, (
            "пропуск сырого материала не должен вызывать LLM или Buffer"
        )

        duplicate = FakeQuery(f"drop:{skipped_id}")
        duplicate_update = SimpleNamespace(
            callback_query=duplicate,
            effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
        )
        before_duplicate = len(fake.messages)
        await bot.on_callback(duplicate_update, context)
        assert len(fake.messages) == before_duplicate, (
            "повторное нажатие той же кнопки не должно выдать ещё одну замену"
        )
        assert calls == {"generate": 0, "publish": 0}

        await bot.propose_job(
            SimpleNamespace(bot=fake, job=SimpleNamespace(data={"slot": "18:00"}))
        )
        assert "@skip-delta" in fake.messages[-2]["text"]
        assert "@skip-echo" in fake.messages[-1]["text"]

        daily_counts = [
            row["n"]
            for row in conn.execute(
                "SELECT COUNT(*) n FROM delivery_item di "
                "JOIN delivery_batch b ON b.id=di.batch_id "
                "WHERE b.slot_key LIKE 'daily:%' GROUP BY b.slot_key ORDER BY b.slot_key"
            )
        ]
        replacement_count = conn.execute(
            "SELECT COUNT(*) n FROM delivery_item di "
            "JOIN delivery_batch b ON b.id=di.batch_id "
            "WHERE b.slot_key LIKE 'replacement:%'"
        ).fetchone()["n"]
        assert daily_counts == [2, 2] and sum(daily_counts) == 4
        assert replacement_count == 1, (
            "замена после пропуска идёт сверх четырёх базовых материалов дня"
        )
        assert calls == {"generate": 0, "publish": 0}
    finally:
        conn.close()
        config.DB_PATH = old_db_path
        Path(tmp.name).unlink(missing_ok=True)


async def main() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    old_db_path = config.DB_PATH
    old_owner = config.OWNER_CHAT_ID
    old_delay = config.BOT_SEND_DELAY
    original_translate = generator.translate_post
    original_voice_idea = generator.voice_idea
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
        generator.voice_idea = forbidden_generate
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

        route_calls = {"translate": 0, "voice": 0}

        def fake_translate(*args, **kwargs):
            route_calls["translate"] += 1
            return generator.DraftOut(
                linkedin_text="Faithful English translation.",
                x_text="Faithful English translation.",
                threads_text="Faithful English translation.",
                notes="Служебная заметка о заменах",
            )

        def fake_voice_idea(*args, **kwargs):
            route_calls["voice"] += 1
            return generator.DraftOut(
                linkedin_text="English post from the voice idea.",
                x_text="English post from the voice idea.",
                threads_text="English post from the voice idea.",
                notes="голосовое о проверке продуктовой гипотезы",
            )

        generator.translate_post = fake_translate
        generator.voice_idea = fake_voice_idea

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
        assert route_calls == {"translate": 1, "voice": 0}
        assert len(text_generation_messages) == 2
        assert text_generation_messages[0]["text"].startswith(
            "⚠️ Заменено / проверить:"
        )
        assert "Служебная заметка о заменах" in text_generation_messages[0]["text"]
        assert text_generation_messages[1]["text"] == "Faithful English translation."
        assert all(
            "Идея:" not in message["text"]
            for message in text_generation_messages
        ), "обычный текст не должен получать отдельное сообщение с идеей"

        voice_source = db.upsert_source(conn, "@voice-route", "Voice route")
        assert db.insert_post(
            conn,
            voice_source,
            12,
            "2026-04-30T10:00:00+00:00",
            "",
            "https://t.me/voice-route/12",
            status="offered",
            media_kind="voice",
            media_mime="audio/ogg",
        )
        voice_post_id = conn.execute(
            "SELECT id FROM post WHERE source_id=? AND tg_message_id=12",
            (voice_source,),
        ).fetchone()["id"]
        db.set_transcript(
            conn,
            voice_post_id,
            "Подробная расшифровка голосового сообщения.",
            "Краткое содержание.",
        )
        voice_query = FakeQuery(f"makeauto:{voice_post_id}")
        voice_update = SimpleNamespace(
            callback_query=voice_query,
            effective_chat=SimpleNamespace(id=config.OWNER_CHAT_ID),
        )
        before_voice_generation = len(fake.messages)
        await bot.on_callback(voice_update, SimpleNamespace(bot=fake))
        voice_generation_messages = fake.messages[before_voice_generation:]
        assert route_calls == {"translate": 1, "voice": 1}
        assert len(voice_generation_messages) == 2
        assert voice_generation_messages[0]["text"].startswith("ℹ️ Идея:")
        assert voice_generation_messages[1]["text"] == "English post from the voice idea."

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

        await test_daily_schedule_and_skip(calls)

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
        assert waiter.result() == (321, 654)
        bot._STAGING_WAITERS.pop("unit-token", None)

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
        generator.voice_idea = original_voice_idea
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
