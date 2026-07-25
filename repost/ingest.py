"""Сбор постов из Telegram-каналов через MTProto (Telethon).

Команды:
  python -m repost.ingest login                 # одноразовая интерактивная авторизация
  python -m repost.ingest backfill --days 90    # выгрузка истории по sources.txt
  python -m repost.ingest backfill --days 3 --sources @a,@b   # тестовый прогон
  python -m repost.ingest sync                  # догрузка только новых сообщений
  python -m repost.ingest status                # статистика базы
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient

from . import config, db


def make_client() -> TelegramClient:
    if not config.API_ID or not config.API_HASH:
        sys.exit("Заполни TELEGRAM_API_ID и TELEGRAM_API_HASH в .env (my.telegram.org → API development tools)")
    client = TelegramClient(config.SESSION, int(config.API_ID), config.API_HASH)
    client.flood_sleep_threshold = 120
    return client


async def _fetch_source(client: TelegramClient, conn, username: str, *, cutoff=None, min_id=0, limit=None) -> tuple[int, int]:
    entity = await client.get_entity(username)
    title = getattr(entity, "title", None) or username
    source_id = db.upsert_source(conn, username, title)
    if min_id == 0:
        row = conn.execute("SELECT last_message_id FROM source WHERE id=?", (source_id,)).fetchone()
        stored = row["last_message_id"]
    else:
        stored = min_id

    added = seen = 0
    max_id = stored
    kwargs = {"reverse": True}
    if cutoff is not None:
        kwargs["offset_date"] = cutoff
    if stored:
        kwargs["min_id"] = stored
    async for msg in client.iter_messages(entity, **kwargs):
        seen += 1
        max_id = max(max_id, msg.id)
        text = (msg.message or "").strip()
        if len(text) < config.MIN_POST_CHARS:
            continue
        url = f"https://t.me/{username.lstrip('@')}/{msg.id}"
        posted_at = msg.date.astimezone(timezone.utc).isoformat()
        author = getattr(msg, "post_author", None)
        if db.insert_post(conn, source_id, msg.id, posted_at, text, url, author=author):
            added += 1
        if limit and added >= limit:
            break
    if max_id:
        db.set_last_message_id(conn, source_id, max_id)
    return added, seen


async def run_fetch(sources: list[str], days: int | None, limit: int | None) -> None:
    conn = db.connect()
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    client = make_client()
    async with client:
        for username in sources:
            try:
                added, seen = await _fetch_source(client, conn, username, cutoff=cutoff, limit=limit)
                print(f"{username:<30} просмотрено {seen:>5}, добавлено {added}")
            except Exception as e:  # noqa: BLE001 — один битый источник не должен ронять остальные
                print(f"{username:<30} ОШИБКА: {e}")
            await asyncio.sleep(1.5)
    s = db.stats(conn)
    print(f"\nВ базе: {s.get('total', 0)} постов из {s.get('sources', 0)} источников "
          f"({s.get('oldest', '—')} … {s.get('newest', '—')})")


async def run_login() -> None:
    client = make_client()
    async with client:
        me = await client.get_me()
        print(f"Авторизован как: {me.first_name or ''} {me.last_name or ''} (@{me.username or '—'})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="интерактивная авторизация Telethon")
    p_back = sub.add_parser("backfill", help="выгрузка истории")
    p_back.add_argument("--days", type=int, default=90)
    p_back.add_argument("--sources", help="переопределить sources.txt: @a,@b")
    p_back.add_argument("--limit", type=int, help="максимум постов с одного источника")
    sub.add_parser("sync", help="догрузить только новые сообщения")
    sub.add_parser("status", help="статистика базы")
    args = ap.parse_args()

    if args.cmd == "login":
        asyncio.run(run_login())
        return
    if args.cmd == "status":
        print(db.stats(db.connect()))
        return

    if args.cmd == "backfill" and args.sources:
        sources = [s.strip() for s in args.sources.split(",") if s.strip()]
    else:
        sources = config.read_sources()
        if args.cmd == "sync":
            conn = db.connect()
            known = [r["username"] for r in conn.execute("SELECT username FROM source WHERE active=1")]
            sources = sorted(set(sources) | set(known))
    if not sources:
        sys.exit("Нет источников: заполни sources.txt или передай --sources @a,@b")

    days = args.days if args.cmd == "backfill" else None
    limit = getattr(args, "limit", None)
    asyncio.run(run_fetch(sources, days, limit))


if __name__ == "__main__":
    main()
