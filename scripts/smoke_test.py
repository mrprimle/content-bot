"""Смоук-тест без внешних сервисов: схема БД, дедупликация, очередь, статусы.

Запуск: python scripts/smoke_test.py
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import db, prompts, publisher  # noqa: E402


def main() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    conn = db.connect(tmp.name)

    sid = db.upsert_source(conn, "@test_channel", "Тестовый канал")
    assert db.upsert_source(conn, "@test_channel", None) == sid, "upsert должен вернуть тот же id"

    ok1 = db.insert_post(conn, sid, 1, "2026-07-20T10:00:00+00:00", "Первый пост " * 20, "https://t.me/test_channel/1")
    dup_id = db.insert_post(conn, sid, 1, "2026-07-20T10:00:00+00:00", "другое", None)
    dup_hash = db.insert_post(conn, sid, 2, "2026-07-21T10:00:00+00:00", "  первый   пост " * 20, None)
    ok2 = db.insert_post(conn, sid, 3, "2026-07-19T09:00:00+00:00", "Более ранний пост " * 20, None)
    assert ok1 and ok2, "новые посты должны вставляться"
    assert not dup_id, "дубликат по message id должен отсекаться"
    assert not dup_hash, "дубликат по хэшу текста должен отсекаться"

    post = db.next_new_post(conn)
    assert post["tg_message_id"] == 3, "очередь должна отдавать самый старый пост"

    draft_id = db.create_draft(conn, post["id"], "test-model", "EN text", "x", "threads", "")
    assert db.get_post(conn, post["id"])["status"] == "drafted"
    db.set_draft_message(conn, draft_id, 777)
    assert db.draft_by_message(conn, 777)["id"] == draft_id

    db.update_draft_texts(conn, draft_id, "edited full", "x2", "t2", "edited full")
    db.record_publication(conn, draft_id, "linkedin", True, "post_123", None)
    db.record_publication(conn, draft_id, "twitter", False, None, "boom")
    db.set_draft_status(conn, draft_id, "approved")

    nxt = db.next_new_post(conn)
    assert nxt["tg_message_id"] == 1, "после drafted следующим идёт пост #1"

    db.set_last_message_id(conn, sid, 3)
    db.set_last_message_id(conn, sid, 2)
    row = conn.execute("SELECT last_message_id FROM source WHERE id=?", (sid,)).fetchone()
    assert row["last_message_id"] == 3, "last_message_id не должен откатываться назад"

    sysp = prompts.system_prompt()
    userp = prompts.user_message("@test_channel", "2026-07-20", "текст")
    assert "linkedin_text" in sysp and "текст" in userp
    assert "max 250 characters" in sysp

    captured: dict[str, str] = {}
    original_channels = publisher.config.buffer_channels
    original_create_post = publisher.create_post
    try:
        publisher.config.buffer_channels = lambda: {"twitter": "channel-id"}

        def fake_create_post(channel_id: str, text: str) -> str:
            captured["channel_id"] = channel_id
            captured["text"] = text
            return "post-id"

        publisher.create_post = fake_create_post
        result = publisher.publish_all({"twitter": "x" * 300})
    finally:
        publisher.config.buffer_channels = original_channels
        publisher.create_post = original_create_post

    assert result["twitter"] == (True, "post-id")
    assert len(captured["text"]) == 250 and captured["text"].endswith("…")

    print("Смоук-тест пройден:", db.stats(conn))
    Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
