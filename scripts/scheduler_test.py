"""Offline scheduler test: quarterly window and failed-source-only retries."""
import asyncio
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost import bot, config, db, ingest  # noqa: E402


class FakeMessage:
    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)
        return FakeMessage(len(self.messages))


async def main() -> None:
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    original = {
        "db_path": config.DB_PATH,
        "owner": config.OWNER_CHAT_ID,
        "auto_sync": config.AUTO_SYNC,
        "delay": config.BOT_SEND_DELAY,
        "run_fetch": ingest.run_fetch,
    }
    calls: list[dict] = []

    async def fake_run_fetch(sources, *, window_start, window_end, incremental=False, limit=None):
        calls.append(
            {
                "sources": list(sources),
                "window_start": window_start,
                "window_end": window_end,
            }
        )
        errors = {sources[0]: "temporary"} if len(calls) == 1 else {}
        return {"errors": errors, "added": 3, "stats": {}}

    try:
        config.DB_PATH = tmp.name
        config.OWNER_CHAT_ID = 123
        config.AUTO_SYNC = True
        config.BOT_SEND_DELAY = 0
        ingest.run_fetch = fake_run_fetch
        context = SimpleNamespace(bot=FakeBot())

        await bot.sync_due_job(context)
        conn = db.connect()
        assert len(calls) == 1 and len(calls[0]["sources"]) == len(config.read_sources())
        assert calls[0]["window_start"] == ingest.subtract_months(calls[0]["window_end"], 3)
        failed = json.loads(db.get_meta(conn, "sync_retry_sources"))
        assert failed == [calls[0]["sources"][0]]
        next_full = datetime.fromisoformat(db.get_meta(conn, "next_full_sync_at"))
        assert next_full == ingest.add_months(calls[0]["window_end"], 3)

        db.set_meta(
            conn,
            "next_source_retry_at",
            (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),
        )
        await bot.sync_due_job(context)
        assert len(calls) == 2
        assert calls[1]["sources"] == failed
        assert calls[1]["window_start"] == calls[0]["window_start"]
        assert calls[1]["window_end"] == calls[0]["window_end"]
        assert db.get_meta(conn, "sync_retry_sources") is None
        assert db.get_meta(conn, "next_full_sync_at") is not None
        print("Scheduler-тест пройден: квартальный сбор + retry только ошибочных источников")
    finally:
        config.DB_PATH = original["db_path"]
        config.OWNER_CHAT_ID = original["owner"]
        config.AUTO_SYNC = original["auto_sync"]
        config.BOT_SEND_DELAY = original["delay"]
        ingest.run_fetch = original["run_fetch"]
        Path(tmp.name).unlink(missing_ok=True)


if __name__ == "__main__":
    asyncio.run(main())
