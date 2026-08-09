"""Offline smoke test: DB migration, round-robin pool, workflow and hard limits."""
import asyncio
import json
import sys
import sqlite3
import tempfile
from contextlib import redirect_stderr
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import bot, config, db, generator, ingest, prompts, publisher  # noqa: E402


def test_legacy_media_migration() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    raw = sqlite3.connect(tmp.name)
    raw.executescript(
        """
        CREATE TABLE source(
          id INTEGER PRIMARY KEY,
          username TEXT UNIQUE NOT NULL,
          title TEXT,
          last_message_id INTEGER NOT NULL DEFAULT 0,
          last_synced_at TEXT,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE post(
          id INTEGER PRIMARY KEY,
          source_id INTEGER NOT NULL REFERENCES source(id),
          tg_message_id INTEGER NOT NULL,
          posted_at TEXT NOT NULL,
          author TEXT,
          text TEXT NOT NULL,
          text_hash TEXT NOT NULL,
          url TEXT,
          status TEXT NOT NULL DEFAULT 'new',
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(source_id, tg_message_id)
        );
        """
    )
    raw.execute("INSERT INTO source(id, username, title) VALUES(1, '@legacy', 'Legacy')")
    caption = "Старая подпись к voice"
    raw.execute(
        "INSERT INTO post(source_id, tg_message_id, posted_at, text, text_hash, url, status) "
        "VALUES(1, 10, '2026-05-01T10:00:00+00:00', ?, ?, 'https://t.me/legacy/10', 'short')",
        (caption, db.text_hash(caption)),
    )
    raw.commit()
    raw.close()

    try:
        conn = db.connect(tmp.name)
        assert not db.insert_post(
            conn,
            1,
            10,
            "2026-05-01T10:00:00+00:00",
            caption,
            "https://t.me/legacy/10",
            media_kind="voice",
            media_mime="audio/ogg",
            media_size=1234,
        )
        migrated = db.get_post(conn, 1)
        assert migrated["media_kind"] == "voice"
        assert migrated["media_mime"] == "audio/ogg"
        assert migrated["media_size"] == 1234
        assert migrated["status"] == "new"
        conn.close()
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def test_candidate_pool_schedule() -> None:
    """Two London slots consume one persistent source round oldest-first."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    try:
        source_ids: dict[str, int] = {}
        for username in ("@alpha", "@bravo", "@charlie", "@delta"):
            source_ids[username] = db.upsert_source(conn, username, username)

        first_round_dates = {
            "@alpha": "2026-04-28T10:00:00+00:00",
            "@bravo": "2026-04-29T10:00:00+00:00",
            "@charlie": "2026-04-30T10:00:00+00:00",
            "@delta": "2026-05-01T10:00:00+00:00",
        }
        second_round_dates = {
            "@alpha": "2026-05-02T10:00:00+00:00",
            "@bravo": "2026-05-03T10:00:00+00:00",
            "@charlie": "2026-05-04T10:00:00+00:00",
            "@delta": "2026-05-05T10:00:00+00:00",
        }
        for username, posted_at in first_round_dates.items():
            assert db.insert_post(
                conn,
                source_ids[username],
                1,
                posted_at,
                f"Первый материал {username} " * 20,
                f"https://t.me/{username[1:]}/1",
            )
        for username, posted_at in second_round_dates.items():
            assert db.insert_post(
                conn,
                source_ids[username],
                2,
                posted_at,
                f"Второй материал {username} " * 20,
                f"https://t.me/{username[1:]}/2",
            )

        morning = db.claim_oldest_posts(conn, "daily:2026-07-28:10:00", max_items=2)
        assert [(row["username"], row["tg_message_id"]) for row in morning] == [
            ("@alpha", 1),
            ("@bravo", 1),
        ]
        assert db.claim_oldest_posts(conn, "daily:2026-07-28:10:00", max_items=2) == [], (
            "повторный запуск того же слота не должен дублировать выдачу"
        )
        for row in morning:
            assert db.mark_delivery_sent(conn, row["id"], 1_000 + row["id"], row["claim_token"])

        evening = db.claim_oldest_posts(conn, "daily:2026-07-28:18:00", max_items=2)
        assert [(row["username"], row["tg_message_id"]) for row in evening] == [
            ("@charlie", 1),
            ("@delta", 1),
        ], "вечер должен продолжить тот же пул, а не открыть новый круг"
        assert len(morning) + len(evening) == 4, "за два плановых слота должно быть четыре материала"

        assert db.recover_incomplete_deliveries(conn) == 2
        recovered = db.claim_oldest_posts(conn, "daily:2026-07-29:recovery", max_items=2)
        assert [(row["username"], row["tg_message_id"]) for row in recovered] == [
            ("@charlie", 1),
            ("@delta", 1),
        ], "после рестарта незавершённый хвост должен вернуться в тот же круг"
        for row in recovered:
            assert db.mark_delivery_sent(conn, row["id"], 2_000 + row["id"], row["claim_token"])

        next_round = db.claim_oldest_posts(conn, "daily:2026-07-29:10:00", max_items=2)
        assert [(row["username"], row["tg_message_id"]) for row in next_round] == [
            ("@alpha", 2),
            ("@bravo", 2),
        ], "новый круг начинается только после исчерпания предыдущего"
    finally:
        conn.close()
        Path(tmp.name).unlink(missing_ok=True)


def test_pool_boundary_top_up() -> None:
    """An odd round still yields two items without breaking chronology."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    try:
        sources = [db.upsert_source(conn, f"@boundary{i}") for i in range(1, 4)]
        dates = (
            ("2026-04-28T10:00:00+00:00", sources[0], 1),
            ("2026-04-29T10:00:00+00:00", sources[1], 1),
            ("2026-04-30T10:00:00+00:00", sources[2], 1),
            ("2026-05-01T10:00:00+00:00", sources[2], 2),
            ("2026-05-02T10:00:00+00:00", sources[0], 2),
            ("2026-05-03T10:00:00+00:00", sources[1], 2),
        )
        for posted_at, source_id, message_id in dates:
            assert db.insert_post(
                conn,
                source_id,
                message_id,
                posted_at,
                f"Граница круга {source_id}/{message_id}",
                None,
            )
        first = db.claim_oldest_posts(conn, "boundary:10:00", max_items=2)
        for row in first:
            assert db.mark_delivery_sent(conn, row["id"], 3_000 + row["id"], row["claim_token"])
        crossing = db.claim_oldest_posts(conn, "boundary:18:00", max_items=2)
        assert [(row["username"], row["tg_message_id"]) for row in crossing] == [
            ("@boundary3", 1),
            ("@boundary3", 2),
        ], "слот должен добрать второй материал из нового круга и сохранить порядок дат"
    finally:
        conn.close()
        Path(tmp.name).unlink(missing_ok=True)


def test_legacy_delivery_constraint_migration() -> None:
    """Existing installs lose the old per-source-per-slot UNIQUE safely."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    raw = sqlite3.connect(tmp.name)
    raw.executescript(
        """
        CREATE TABLE source(
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, title TEXT,
          last_message_id INTEGER NOT NULL DEFAULT 0, last_synced_at TEXT,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE post(
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL REFERENCES source(id),
          tg_message_id INTEGER NOT NULL, posted_at TEXT NOT NULL, author TEXT,
          text TEXT NOT NULL, text_hash TEXT NOT NULL, url TEXT,
          status TEXT NOT NULL DEFAULT 'new',
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          UNIQUE(source_id, tg_message_id)
        );
        CREATE TABLE delivery_batch(
          id INTEGER PRIMARY KEY, slot_key TEXT UNIQUE NOT NULL,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE delivery_item(
          id INTEGER PRIMARY KEY,
          batch_id INTEGER NOT NULL REFERENCES delivery_batch(id),
          source_id INTEGER NOT NULL REFERENCES source(id),
          post_id INTEGER UNIQUE NOT NULL REFERENCES post(id),
          status TEXT NOT NULL DEFAULT 'claimed',
          bot_message_id INTEGER, sent_at TEXT,
          claim_token TEXT, claimed_at TEXT,
          UNIQUE(batch_id, source_id)
        );
        INSERT INTO source(id, username) VALUES(1, '@legacy-delivery');
        INSERT INTO post(
          id, source_id, tg_message_id, posted_at, text, text_hash, status
        ) VALUES(
          1, 1, 1, '2026-05-01T00:00:00+00:00', 'legacy', 'legacy-hash', 'offered'
        );
        INSERT INTO delivery_batch(id, slot_key) VALUES(1, 'legacy-slot');
        INSERT INTO delivery_item(
          id, batch_id, source_id, post_id, status, claim_token
        ) VALUES(1, 1, 1, 1, 'sending', 'legacy-token');
        """
    )
    raw.commit()
    raw.close()
    try:
        conn = db.connect(tmp.name)
        table_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_item'"
        ).fetchone()["sql"]
        assert "UNIQUE(batch_id, source_id)" not in table_sql
        preserved = conn.execute(
            "SELECT post_id, status, claim_token FROM delivery_item WHERE id=1"
        ).fetchone()
        assert tuple(preserved) == (1, "sending", "legacy-token")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def test_generation_retries_incomplete_boundary_text() -> None:
    """A schema-boundary result must be regenerated, never stored as a cut tail."""
    original_parse = generator._parse
    schema_limits: list[int] = []
    editorial_targets: list[int] = []

    def fake_parse(system: str, _user: str, max_chars: int):
        schema_limits.append(max_chars)
        marker = "editorial target of "
        editorial_targets.append(int(system.split(marker, 1)[1].split(" ", 1)[0]))
        if len(schema_limits) == 1:
            full_text = ("A" * (max_chars - 5)) + ", but"
        else:
            full_text = ("B" * (editorial_targets[-1] - 20)) + " Complete ending."
        return generator.TranslationOut(
            full_text=full_text,
            thread_items=["A complete Threads item."],
            notes="",
        )

    try:
        generator._parse = fake_parse
        result = generator.translate_post(
            "Test source",
            "2026-08-07",
            "Полный исходный текст",
            max_chars=config.PLATFORM_SAFE_CHARS,
        )
        assert schema_limits == [config.PLATFORM_SAFE_CHARS, config.PLATFORM_SAFE_CHARS]
        assert editorial_targets == [2_970, 2_700]
        assert result.linkedin_text.endswith("Complete ending.")
        assert len(result.linkedin_text) < 2_700
    finally:
        generator._parse = original_parse


def test_generation_accepts_complete_text_at_editorial_target() -> None:
    """The soft target is not the schema boundary and must not cause false failure."""
    original_parse = generator._parse
    calls = 0

    def fake_parse(system: str, _user: str, max_chars: int):
        nonlocal calls
        calls += 1
        marker = "editorial target of "
        target = int(system.split(marker, 1)[1].split(" ", 1)[0])
        ending = " Complete ending."
        return generator.TranslationOut(
            full_text=("A" * (target - len(ending))) + ending,
            thread_items=["A complete Threads item."],
            notes="",
        )

    try:
        generator._parse = fake_parse
        result = generator.translate_post(
            "Test source",
            "2026-08-09",
            "Полный исходный текст",
            max_chars=config.PLATFORM_SAFE_CHARS,
        )
        assert calls == 1
        assert len(result.linkedin_text) == 2_970
        assert result.linkedin_text.endswith("Complete ending.")
    finally:
        generator._parse = original_parse


def test_stranded_work_recovery() -> None:
    """Startup recovery is idempotent and never retries an uncertain publish."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    try:
        source_id = db.upsert_source(conn, "@recovery", "Recovery")

        def add_post(message_id: int) -> int:
            assert db.insert_post(
                conn,
                source_id,
                message_id,
                f"2026-05-{message_id:02d}T10:00:00+00:00",
                f"Recovery material {message_id}",
                f"https://t.me/recovery/{message_id}",
                status="generating",
            )
            return conn.execute(
                "SELECT id FROM post WHERE source_id=? AND tg_message_id=?",
                (source_id, message_id),
            ).fetchone()["id"]

        raw_post = add_post(1)
        manual_post = add_post(2)
        conn.execute(
            "UPDATE post SET manual_prompt_id=? WHERE id=?",
            (7_002, manual_post),
        )
        conn.commit()

        delivered_draft_post = add_post(3)
        delivered_draft = db.create_draft(
            conn,
            delivered_draft_post,
            "test",
            "ready",
            "ready",
            "ready",
            "",
        )
        db.set_draft_message(conn, delivered_draft, 8_003)
        db.set_post_status(conn, delivered_draft_post, "generating")

        failed_draft_post = add_post(4)
        failed_draft = db.create_draft(
            conn,
            failed_draft_post,
            "test",
            "not delivered",
            "not delivered",
            "not delivered",
            "",
        )
        db.set_draft_status(conn, failed_draft, "delivery_failed")
        db.set_post_status(conn, failed_draft_post, "generating")

        publishing_post = add_post(5)
        publishing_draft = db.create_draft(
            conn,
            publishing_post,
            "test",
            "may be live",
            "may be live",
            "may be live",
            "",
        )
        db.set_draft_message(conn, publishing_draft, 8_005)
        db.set_draft_status(conn, publishing_draft, "publishing")

        generating_publish_post = add_post(6)
        generating_publish_draft = db.create_draft(
            conn,
            generating_publish_post,
            "test",
            "may also be live",
            "may also be live",
            "may also be live",
            "",
        )
        db.set_draft_message(conn, generating_publish_draft, 8_006)
        db.set_draft_status(conn, generating_publish_draft, "publishing")
        db.set_post_status(conn, generating_publish_post, "generating")

        undelivered_draft_post = add_post(7)
        undelivered_draft = db.create_draft(
            conn,
            undelivered_draft_post,
            "test",
            "saved but not sent",
            "saved but not sent",
            "saved but not sent",
            "",
        )

        manual_undelivered_post = add_post(8)
        conn.execute(
            "UPDATE post SET manual_prompt_id=? WHERE id=?",
            (7_008, manual_undelivered_post),
        )
        conn.commit()
        manual_undelivered_draft = db.create_draft(
            conn,
            manual_undelivered_post,
            "test",
            "manual saved but not sent",
            "manual saved but not sent",
            "manual saved but not sent",
            "",
        )

        delivered_control_post = add_post(9)
        delivered_control_draft = db.create_draft(
            conn,
            delivered_control_post,
            "test",
            "already sent",
            "already sent",
            "already sent",
            "",
        )
        db.set_draft_message(conn, delivered_control_draft, 8_009)

        missing_prompt_post = add_post(10)
        db.set_post_status(conn, missing_prompt_post, "awaiting_manual")

        existing_prompt_post = add_post(11)
        db.set_manual_prompt(conn, existing_prompt_post, 7_011)

        recovered = db.recover_stranded_work(conn)
        assert recovered == {
            "generating_reoffered": 2,
            "generating_manual": 1,
            "generating_reconciled": 2,
            "undelivered_drafts_reopened": 2,
            "manual_without_prompt_reoffered": 1,
            "publishing_unknown": 2,
            "ai_edits_reopened": 0,
        }
        assert db.get_post(conn, raw_post)["status"] == "offered"
        assert db.get_post(conn, manual_post)["status"] == "awaiting_manual"
        assert db.get_post(conn, delivered_draft_post)["status"] == "drafted"
        assert db.get_post(conn, failed_draft_post)["status"] == "offered"
        assert db.get_post(conn, generating_publish_post)["status"] == "drafted"
        assert db.get_draft(conn, publishing_draft)["status"] == "publish_unknown"
        assert db.get_draft(conn, generating_publish_draft)["status"] == "publish_unknown"
        assert db.get_post(conn, undelivered_draft_post)["status"] == "offered"
        assert db.get_draft(conn, undelivered_draft)["status"] == "delivery_failed"
        assert db.get_post(conn, manual_undelivered_post)["status"] == "awaiting_manual"
        assert db.get_post(conn, manual_undelivered_post)["manual_prompt_id"] == 7_008
        assert db.get_draft(conn, manual_undelivered_draft)["status"] == "delivery_failed"
        assert db.get_post(conn, delivered_control_post)["status"] == "drafted"
        assert db.get_draft(conn, delivered_control_draft)["status"] == "awaiting_review"
        assert db.get_draft(conn, delivered_control_draft)["tg_message_id"] == 8_009
        assert db.get_post(conn, missing_prompt_post)["status"] == "offered"
        assert db.get_post(conn, existing_prompt_post)["status"] == "awaiting_manual"
        assert db.get_post(conn, existing_prompt_post)["manual_prompt_id"] == 7_011
        assert not db.claim_draft_publish(conn, publishing_draft), (
            "неизвестную публикацию нельзя автоматически повторять"
        )
        assert db.recover_stranded_work(conn) == {
            "generating_reoffered": 0,
            "generating_manual": 0,
            "generating_reconciled": 0,
            "undelivered_drafts_reopened": 0,
            "manual_without_prompt_reoffered": 0,
            "publishing_unknown": 0,
            "ai_edits_reopened": 0,
        }, "startup recovery должен быть идемпотентным"
        notice = bot._startup_recovery_message(1, recovered)
        assert notice and "Автоповтор отключён" in notice and "Buffer" in notice
        assert bot._startup_recovery_message(
            0,
            {
                "generating_reoffered": 0,
                "generating_manual": 0,
                "generating_reconciled": 0,
                "undelivered_drafts_reopened": 0,
                "manual_without_prompt_reoffered": 0,
                "publishing_unknown": 0,
                "ai_edits_reopened": 0,
            },
        ) is None

        class FakeBot:
            def __init__(self):
                self.messages: list[tuple[int, str]] = []

            async def send_message(self, chat_id: int, text: str):
                self.messages.append((chat_id, text))
                return SimpleNamespace(message_id=1)

        old_db_path = config.DB_PATH
        old_owner = config.OWNER_CHAT_ID
        try:
            config.DB_PATH = tmp.name
            config.OWNER_CHAT_ID = 123
            db.set_meta(conn, bot._STARTUP_RECOVERY_NOTICE_KEY, notice)
            fake = FakeBot()
            asyncio.run(
                bot.startup_recovery_notice_job(
                    SimpleNamespace(bot=fake, job=SimpleNamespace(data=notice))
                )
            )
            assert fake.messages == [(123, notice)]
            assert db.get_meta(conn, bot._STARTUP_RECOVERY_NOTICE_KEY) is None
        finally:
            config.DB_PATH = old_db_path
            config.OWNER_CHAT_ID = old_owner
    finally:
        conn.close()
        Path(tmp.name).unlink(missing_ok=True)


def test_cleanup_is_disabled_and_non_destructive() -> None:
    """Neither the DB helper nor the legacy CLI can physically delete rows."""
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)
    try:
        source_id = db.upsert_source(conn, "@retention", "Retention")
        assert db.insert_post(
            conn,
            source_id,
            1,
            "2000-01-01T00:00:00+00:00",
            "Permanent tombstone",
            "https://t.me/retention/1",
        )
        delivery = db.claim_oldest_posts(conn, "retention-slot", max_items=1)[0]
        assert db.mark_delivery_sent(
            conn,
            delivery["id"],
            9_001,
            delivery["claim_token"],
        )
        assert db.transition_post(conn, delivery["id"], ("offered",), "generating")
        draft_id = db.create_draft(
            conn,
            delivery["id"],
            "test",
            "published",
            "published",
            "published",
            "",
        )
        db.record_publication(conn, draft_id, "linkedin", True, "external-id", None)
        db.set_draft_status(conn, draft_id, "published")
        db.set_post_status(conn, delivery["id"], "published")

        tables = ("post", "draft", "publication", "delivery_item", "delivery_batch")

        def snapshot() -> dict[str, list[tuple]]:
            return {
                table: [
                    tuple(row)
                    for row in conn.execute(f"SELECT * FROM {table} ORDER BY id")
                ]
                for table in tables
            }

        before = snapshot()
        try:
            db.cleanup(conn, keep_days=0)
        except RuntimeError as exc:
            assert "Физическое удаление отключено" in str(exc)
        else:
            raise AssertionError("db.cleanup не должен иметь destructive path")
        assert snapshot() == before

        original_argv = sys.argv
        try:
            sys.argv = ["repost.ingest", "cleanup", "--keep-days", "0"]
            try:
                ingest.main()
            except SystemExit as exc:
                assert "cleanup отключена" in str(exc.code)
            else:
                raise AssertionError("legacy cleanup CLI должен завершаться отказом")
        finally:
            sys.argv = original_argv
        assert snapshot() == before
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        conn.close()
        Path(tmp.name).unlink(missing_ok=True)


def main() -> None:
    test_legacy_media_migration()
    test_legacy_delivery_constraint_migration()
    test_candidate_pool_schedule()
    test_pool_boundary_top_up()
    test_stranded_work_recovery()
    test_cleanup_is_disabled_and_non_destructive()
    test_generation_retries_incomplete_boundary_text()
    test_generation_accepts_complete_text_at_editorial_target()
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    conn = db.connect(tmp.name)

    diagnostic_id = db.record_diagnostic_event(
        conn,
        level="error",
        component="smoke",
        event="test_failure",
        entity_type="draft",
        entity_id=42,
        error=RuntimeError("sanitized test error"),
        details={"action": "transform3000", "source_chars": 4_058},
    )
    diagnostic = db.recent_diagnostic_events(conn, 1)[0]
    assert diagnostic["id"] == diagnostic_id
    assert diagnostic["error_type"] == "RuntimeError"
    assert json.loads(diagnostic["details_json"])["source_chars"] == 4_058

    s1 = db.upsert_source(conn, "@one", "One")
    s2 = db.upsert_source(conn, "@two", "Two")
    s3 = db.upsert_source(conn, "@three", "Three")
    assert db.upsert_source(conn, "@one", None) == s1

    assert db.insert_post(
        conn, s1, 1, "2026-04-28T10:00:00+00:00", "Одинаковый текст " * 20, "https://t.me/one/1"
    )
    assert not db.insert_post(
        conn, s1, 1, "2026-04-28T10:00:00+00:00", "другой", "https://t.me/one/1"
    ), "дубликат Telegram message id должен отсекаться"
    assert not db.insert_post(
        conn,
        s2,
        1,
        "2026-04-29T10:00:00+00:00",
        "одинаковый   текст\n" * 20,
        "https://t.me/two/1",
    ), "одинаковые нормализованные тексты из разных источников должны схлопываться"
    assert db.insert_post(
        conn,
        s3,
        1,
        "2026-04-30T10:00:00+00:00",
        "",
        "https://t.me/three/1",
        media_kind="voice",
        media_mime="audio/ogg",
        media_size=1234,
    )
    assert db.insert_post(
        conn,
        s3,
        2,
        "2026-05-01T10:00:00+00:00",
        "",
        "https://t.me/three/2",
        media_kind="video",
        media_mime="video/mp4",
        media_size=4321,
    ), "media без текста не должны схлопываться по пустому hash"
    assert db.insert_post(
        conn, s1, 2, "2026-05-02T10:00:00+00:00", "Следующий пост " * 20, "https://t.me/one/2"
    )
    assert db.insert_post(
        conn,
        s2,
        2,
        "2026-05-03T10:00:00+00:00",
        "Следующий пост второго источника " * 20,
        "https://t.me/two/2",
    )

    first = db.claim_oldest_posts(
        conn,
        "workflow-one",
        source_username="@one",
        max_items=1,
    )
    second = db.claim_oldest_posts(
        conn,
        "workflow-two",
        source_username="@two",
        max_items=1,
    )
    voice = db.claim_oldest_posts(
        conn,
        "workflow-three",
        source_username="@three",
        max_items=1,
    )
    assert [(row["username"], row["tg_message_id"]) for row in first] == [("@one", 1)]
    assert [(row["username"], row["tg_message_id"]) for row in second] == [("@two", 2)]
    assert [(row["username"], row["tg_message_id"]) for row in voice] == [("@three", 1)]
    for row in first + second + voice:
        assert row["claim_token"]
        assert db.mark_delivery_sent(conn, row["id"], 1000 + row["id"], row["claim_token"])

    stale_source = db.upsert_source(conn, "@stale", "Stale")
    assert db.insert_post(
        conn,
        stale_source,
        1,
        "2026-05-04T10:00:00+00:00",
        "Проверка устаревшей аренды " * 20,
        "https://t.me/stale/1",
    )
    stale_claim = db.claim_oldest_posts(
        conn,
        "stale-owner-a",
        source_username="@stale",
        max_items=1,
    )[0]
    assert db.release_delivery(conn, stale_claim["id"], stale_claim["claim_token"])
    current_conn = db.connect(tmp.name)
    current_claim = db.claim_oldest_posts(
        current_conn,
        "stale-owner-b",
        source_username="@stale",
        max_items=1,
    )[0]
    assert current_claim["claim_token"] != stale_claim["claim_token"]
    assert not db.mark_delivery_sent(conn, stale_claim["id"], 9001, stale_claim["claim_token"])
    assert not db.release_delivery(conn, stale_claim["id"], stale_claim["claim_token"])
    owned = current_conn.execute(
        "SELECT status, claim_token FROM delivery_item WHERE post_id=?",
        (stale_claim["id"],),
    ).fetchone()
    assert (owned["status"], owned["claim_token"]) == ("sending", current_claim["claim_token"])
    assert db.get_post(current_conn, stale_claim["id"])["raw_message_id"] is None
    assert db.mark_delivery_sent(current_conn, current_claim["id"], 9002, current_claim["claim_token"])
    assert db.get_post(current_conn, stale_claim["id"])["raw_message_id"] == 9002
    current_conn.close()

    post = db.get_post(conn, first[0]["id"])
    assert db.transition_post(conn, post["id"], ("offered",), "generating")
    assert not db.transition_post(conn, post["id"], ("offered",), "generating")
    draft_id = db.create_draft(conn, post["id"], "test-model", "EN text", "x", "threads", "")
    assert db.create_draft(conn, post["id"], "test-model", "duplicate", "x", "t", "") == draft_id
    db.set_draft_message(conn, draft_id, 777)
    assert db.draft_by_message(conn, 777)["id"] == draft_id
    db.update_draft_texts(conn, draft_id, "edited full", "x2", "t2", "edited full")
    assert db.transition_draft(conn, draft_id, ("awaiting_review",), "skipped")
    assert not db.transition_draft(conn, draft_id, ("awaiting_review",), "skipped")
    db.set_draft_status(conn, draft_id, "awaiting_review")
    assert db.claim_draft_publish(conn, draft_id)
    assert not db.claim_draft_publish(conn, draft_id)
    db.set_draft_status(conn, draft_id, "awaiting_review")

    db.set_manual_prompt(conn, second[0]["id"], 888)
    assert db.post_by_manual_prompt(conn, 888)["id"] == second[0]["id"]

    db.set_transcript(conn, voice[0]["id"], "Расшифровка", "Краткое содержание")
    media = db.get_post(conn, voice[0]["id"])
    assert media["transcript"] == "Расшифровка" and media["summary"] == "Краткое содержание"

    assert config.PLANNING_TIME == "21:00"
    assert config.PUBLISH_TIMES == ["09:00", "14:00", "19:00"]
    assert config.DAILY_POSTS == 3
    assert config.ITEMS_PER_SLOT == 1
    assert config.TIMEZONE == "Europe/London"
    assert config.OPENAI_MODEL == "gpt-5.6-terra"
    assert config.MAX_POST_CHARS == 1500
    assert config.MANUAL_MAX_POST_CHARS == 3000
    assert config.PLATFORM_SAFE_CHARS == 3000
    assert config.THREAD_ITEM_CHARS == 500
    assert config.THREAD_MAX_ITEMS == 10
    assert config.THREADS_TOTAL_CHARS == 5000
    assert config.LIMITS == {"linkedin": 3000, "twitter": 25000, "threads": 500}
    assert config.X_PREMIUM is True
    sources = config.read_sources()
    assert sources and len(sources) == len(set(sources)), "источники должны быть уникальными"
    assert "Do NOT summarize" in prompts.TRANSLATE_SYSTEM
    assert "Preserve book titles" in prompts.TRANSLATE_SYSTEM
    assert "must not be replaced" in prompts.TRANSLATE_SYSTEM
    assert "editorial target of 1500" in prompts.TRANSLATE_SYSTEM
    assert "absolute platform limit is 1500" in prompts.TRANSLATE_SYSTEM
    assert "Preserve the original first-person perspective" in prompts.TRANSLATE_SYSTEM
    assert "company is Vahue" in prompts.TRANSLATE_SYSTEM
    assert "STAGE 1 — ENGLISH" in prompts.TRANSLATE_SYSTEM
    assert "STAGE 2 — TRUTH" in prompts.TRANSLATE_SYSTEM
    assert "STAGE 3 — COMPRESSION" in prompts.TRANSLATE_SYSTEM
    assert "Mike lives in London" in prompts.TRANSLATE_SYSTEM
    assert "editorial target of 3000" in prompts.translation_system(config.PLATFORM_SAFE_CHARS)
    assert "Preserve its current language: do not translate" in prompts.compression_system(1500)
    assert "Mike is a man" in prompts.TRANSLATE_SYSTEM
    assert "building SMM automation" in prompts.TRANSLATE_SYSTEM
    assert "third-party fact is content" in prompts.TRANSLATE_SYSTEM
    assert ingest.subtract_months(
        datetime(2026, 7, 31, tzinfo=timezone.utc), 3
    ) == datetime(2026, 4, 30, tzinfo=timezone.utc)
    assert ingest.add_months(
        datetime(2026, 11, 30, tzinfo=timezone.utc), 3
    ) == datetime(2027, 2, 28, tzinfo=timezone.utc)
    captured: dict[str, str] = {}
    original_channels = publisher.config.buffer_channels
    original_create_post = publisher.create_post
    try:
        publisher.config.buffer_channels = lambda: {"linkedin": "channel-id"}

        def fake_create_post(
            channel_id: str,
            text: str,
            *,
            thread_platform: str | None = None,
            thread: list[str] | None = None,
        ) -> str:
            captured["channel_id"] = channel_id
            captured["text"] = text
            return "post-id"

        publisher.create_post = fake_create_post
        result = publisher.publish_all({"linkedin": "x" * 3001})
    finally:
        publisher.config.buffer_channels = original_channels
        publisher.create_post = original_create_post

    assert result["linkedin"][0] is False
    assert "без обрезания" in result["linkedin"][1]
    assert not captured

    db.set_meta(conn, "next_full_sync_at", "2026-10-28T00:00:00+00:00")
    assert db.get_meta(conn, "next_full_sync_at") == "2026-10-28T00:00:00+00:00"

    original_run_backfill = ingest.run_backfill
    original_argv = sys.argv

    async def failed_backfill(*args, **kwargs):
        return {
            "sources": 1,
            "added": 0,
            "seen": 0,
            "errors": {"@broken": "test failure"},
            "window_end": datetime.now(timezone.utc).isoformat(),
            "stats": {},
        }

    try:
        ingest.run_backfill = failed_backfill
        sys.argv = [
            "repost.ingest",
            "backfill",
            "--days",
            "1",
            "--sources",
            "@broken",
        ]
        stderr = StringIO()
        with redirect_stderr(stderr):
            try:
                ingest.main()
            except SystemExit as exc:
                assert exc.code == 1, "ручной backfill с ошибками должен завершаться exit 1"
            else:
                raise AssertionError("ручной backfill с ошибками не должен завершаться успешно")
        assert "1 из 1 источников" in stderr.getvalue()
    finally:
        ingest.run_backfill = original_run_backfill
        sys.argv = original_argv

    legacy = conn.execute(
        "INSERT INTO source(username, title) VALUES('@LegacyCase', 'Legacy')"
    ).lastrowid
    duplicate_case = conn.execute(
        "INSERT INTO source(username, title) VALUES('@CaseDup', 'Old duplicate')"
    ).lastrowid
    canonical_case = conn.execute(
        "INSERT INTO source(username, title) VALUES('@casedup', 'Canonical duplicate')"
    ).lastrowid
    chat_source = db.upsert_source(conn, "chat:123", "Legacy group")
    conn.commit()
    for source_id, message_id in (
        (legacy, 101),
        (duplicate_case, 102),
        (canonical_case, 103),
        (chat_source, 104),
    ):
        assert db.insert_post(
            conn,
            source_id,
            message_id,
            "2026-05-05T10:00:00+00:00",
            f"Материал источника {source_id} " * 20,
            None,
        )
    post_count = conn.execute("SELECT COUNT(*) FROM post").fetchone()[0]
    reconciliation = db.reconcile_active_sources(
        conn,
        ["@one", "@ONE", "@legacycase", "@casedup", "@new-source"],
    )
    assert reconciliation["configured"] == 4
    assert reconciliation["created"] == 1
    assert reconciliation["renamed"] == 1
    active_names = {
        row["username"]
        for row in conn.execute("SELECT username FROM source WHERE active=1")
    }
    assert active_names == {"@one", "@legacycase", "@casedup", "@new-source"}
    assert conn.execute("SELECT username FROM source WHERE id=?", (legacy,)).fetchone()[0] == "@legacycase"
    assert conn.execute("SELECT active FROM source WHERE id=?", (duplicate_case,)).fetchone()[0] == 0
    assert conn.execute("SELECT active FROM source WHERE id=?", (canonical_case,)).fetchone()[0] == 1
    assert conn.execute("SELECT active FROM source WHERE id=?", (chat_source,)).fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM post").fetchone()[0] == post_count
    before_empty_reconcile = [
        tuple(row)
        for row in conn.execute("SELECT id, username, active FROM source ORDER BY id")
    ]
    assert db.reconcile_active_sources(conn, []) == {
        "configured": 0,
        "created": 0,
        "renamed": 0,
        "deactivated": 0,
    }
    assert before_empty_reconcile == [
        tuple(row)
        for row in conn.execute("SELECT id, username, active FROM source ORDER BY id")
    ]
    configured_sources = config.read_sources()
    production_reconciliation = db.reconcile_active_sources(conn, configured_sources)
    assert production_reconciliation["configured"] == len(configured_sources)
    assert (
        conn.execute("SELECT COUNT(*) FROM source WHERE active=1").fetchone()[0]
        == len(configured_sources)
    )
    assert {
        row["username"]
        for row in conn.execute("SELECT username FROM source WHERE active=1")
    } == {username.casefold() for username in configured_sources}
    assert conn.execute("SELECT COUNT(*) FROM post").fetchone()[0] == post_count

    legacy_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    legacy_tmp.close()
    legacy_conn = sqlite3.connect(legacy_tmp.name)
    legacy_conn.executescript(
        """
        CREATE TABLE source(
          id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, title TEXT,
          last_message_id INTEGER NOT NULL DEFAULT 0, last_synced_at TEXT,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE post(
          id INTEGER PRIMARY KEY, source_id INTEGER NOT NULL, tg_message_id INTEGER NOT NULL,
          posted_at TEXT NOT NULL, author TEXT, text TEXT NOT NULL, text_hash TEXT NOT NULL,
          url TEXT, status TEXT NOT NULL DEFAULT 'new',
          created_at TEXT NOT NULL DEFAULT (datetime('now')), UNIQUE(source_id, tg_message_id)
        );
        CREATE TABLE draft(
          id INTEGER PRIMARY KEY, post_id INTEGER NOT NULL, model TEXT, linkedin_text TEXT,
          x_text TEXT, threads_text TEXT, edited_text TEXT, notes TEXT,
          status TEXT NOT NULL DEFAULT 'awaiting_review', tg_message_id INTEGER,
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE publication(
          id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL, platform TEXT NOT NULL,
          status TEXT NOT NULL, external_id TEXT, error TEXT,
          published_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    legacy_conn.execute("INSERT INTO source(id, username) VALUES(1, '@legacy')")
    legacy_conn.execute(
        "INSERT INTO post(source_id, tg_message_id, posted_at, text, text_hash, status) "
        "VALUES(1, 7, '2026-05-01T00:00:00+00:00', '', ?, 'short')",
        (db.text_hash(""),),
    )
    legacy_conn.commit()
    legacy_conn.close()
    migrated = db.connect(legacy_tmp.name)
    assert not db.insert_post(
        migrated,
        1,
        7,
        "2026-05-01T00:00:00+00:00",
        "",
        "https://t.me/legacy/7",
        media_kind="video",
        media_mime="video/mp4",
        media_size=123,
    )
    migrated_post = migrated.execute(
        "SELECT media_kind, media_mime, media_size, status FROM post WHERE source_id=1 AND tg_message_id=7"
    ).fetchone()
    assert tuple(migrated_post) == ("video", "video/mp4", 123, "new")
    migrated.close()
    Path(legacy_tmp.name).unlink(missing_ok=True)

    print("Смоук-тест пройден:", db.stats(conn))
    conn.close()
    Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
