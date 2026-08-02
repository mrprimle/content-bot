"""Сбор истории Telegram-каналов через пользовательскую Telethon-сессию.

Команды:
  python -m repost.ingest login
  python -m repost.ingest backfill --months 3
  python -m repost.ingest backfill --days 3 --sources @a,@b --limit 1
  python -m repost.ingest sync
  python -m repost.ingest status
"""
import argparse
import asyncio
import calendar
import fcntl
import mimetypes
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from telethon import TelegramClient
from telethon.sessions import StringSession

from . import config, db

_CLIENT_LOCK = asyncio.Lock()


def make_client() -> TelegramClient:
    if not config.API_ID or not config.API_HASH:
        sys.exit("Заполни TELEGRAM_API_ID и TELEGRAM_API_HASH в .env (my.telegram.org → API development tools)")
    session = (
        StringSession(config.TELEGRAM_SESSION_STRING)
        if config.TELEGRAM_SESSION_STRING
        else config.SESSION
    )
    client = TelegramClient(session, int(config.API_ID), config.API_HASH)
    client.flood_sleep_threshold = 120
    return client


@asynccontextmanager
async def client_session():
    """Serialize Telethon session use across async tasks and local processes."""
    async with _CLIENT_LOCK:
        lock_conn = None
        lock_file = None
        if config.DATABASE_URL:
            lock_conn = db.connect()
            await asyncio.to_thread(db.acquire_telegram_session_lock, lock_conn)
        else:
            lock_path = Path(f"{config.SESSION}.lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = lock_path.open("a+")
            await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_EX)
        try:
            client = make_client()
            async with client:
                yield client
        finally:
            if lock_conn is not None:
                await asyncio.to_thread(db.release_telegram_session_lock, lock_conn)
                lock_conn.close()
            if lock_file is not None:
                await asyncio.to_thread(fcntl.flock, lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()


def subtract_months(value: datetime, months: int) -> datetime:
    """Calendar subtraction: 31 July - 3 months = 30 April."""
    year = value.year
    month = value.month - months
    while month <= 0:
        year -= 1
        month += 12
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def add_months(value: datetime, months: int) -> datetime:
    year = value.year
    month = value.month + months
    while month > 12:
        year += 1
        month -= 12
    day = min(value.day, calendar.monthrange(year, month)[1])
    return value.replace(year=year, month=month, day=day)


def _media_info(msg) -> tuple[str, str | None, int | None, str | None]:
    if msg.voice:
        kind = "voice"
    elif msg.video_note:
        kind = "video_note"
    elif msg.video:
        kind = "video"
    elif msg.audio:
        kind = "audio"
    elif msg.photo:
        kind = "photo"
    elif msg.document:
        kind = "document"
    else:
        return "text", None, None, None
    file = getattr(msg, "file", None)
    return (
        kind,
        getattr(file, "mime_type", None),
        getattr(file, "size", None),
        getattr(file, "name", None),
    )


async def _fetch_source(
    client: TelegramClient,
    conn,
    username: str,
    *,
    window_start: datetime | None,
    window_end: datetime,
    incremental: bool,
    limit: int | None,
) -> tuple[int, int]:
    entity = await client.get_entity(username)
    title = getattr(entity, "title", None) or username
    source_id = db.upsert_source(conn, username, title)
    stored = 0
    if incremental:
        row = conn.execute("SELECT last_message_id FROM source WHERE id=?", (source_id,)).fetchone()
        stored = row["last_message_id"]

    kwargs = {}
    if stored:
        kwargs["min_id"] = stored

    added = seen = 0
    max_id = stored
    async for msg in client.iter_messages(entity, **kwargs):
        msg_date = msg.date.astimezone(timezone.utc)
        if msg_date > window_end:
            continue
        if window_start is not None and msg_date < window_start:
            break
        seen += 1
        max_id = max(max_id, msg.id)
        text = (msg.message or "").strip()
        media_kind, media_mime, media_size, media_name = _media_info(msg)
        if media_kind == "text" and len(text) < config.MIN_POST_CHARS:
            status = "short"
        else:
            status = "new"
        url = f"https://t.me/{username.lstrip('@')}/{msg.id}"
        author = getattr(msg, "post_author", None)
        if db.insert_post(
            conn,
            source_id,
            msg.id,
            msg_date.isoformat(),
            text,
            url,
            author=author,
            status=status,
            media_kind=media_kind,
            media_mime=media_mime,
            media_size=media_size,
            media_name=media_name,
            no_forwards=bool(getattr(msg, "noforwards", False)),
        ):
            added += 1
        if limit and added >= limit:
            break
    if max_id and (incremental or limit is None):
        db.set_last_message_id(conn, source_id, max_id)
    return added, seen


async def run_fetch(
    sources: list[str],
    *,
    window_start: datetime | None,
    window_end: datetime | None = None,
    incremental: bool = False,
    limit: int | None = None,
) -> dict:
    """Fetch sources and return a machine-readable summary for the scheduler."""
    sources = list(
        dict.fromkeys(
            source.casefold() if source.startswith("@") else source
            for source in sources
        )
    )
    conn = db.connect()
    window_end = window_end or datetime.now(timezone.utc)
    errors: dict[str, str] = {}
    total_added = total_seen = 0
    async with client_session() as client:
        for username in sources:
            try:
                added, seen = await _fetch_source(
                    client,
                    conn,
                    username.casefold() if username.startswith("@") else username,
                    window_start=window_start,
                    window_end=window_end,
                    incremental=incremental,
                    limit=limit,
                )
                total_added += added
                total_seen += seen
                print(f"{username:<30} просмотрено {seen:>5}, добавлено {added}")
            except Exception as exc:  # noqa: BLE001 — один источник не роняет остальные
                errors[username] = str(exc)
                print(f"{username:<30} ОШИБКА: {exc}")
            await asyncio.sleep(1.5)
    stats = db.stats(conn)
    print(
        f"\nВ базе: {stats.get('total', 0)} постов из {stats.get('sources', 0)} источников "
        f"({stats.get('oldest', '—')} … {stats.get('newest', '—')})"
    )
    return {
        "sources": len(sources),
        "added": total_added,
        "seen": total_seen,
        "errors": errors,
        "window_end": window_end.isoformat(),
        "stats": stats,
    }


async def run_backfill(
    sources: list[str],
    *,
    months: int = 3,
    days: int | None = None,
    limit: int | None = None,
) -> dict:
    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(days=days) if days is not None else subtract_months(window_end, months)
    return await run_fetch(
        sources,
        window_start=window_start,
        window_end=window_end,
        incremental=False,
        limit=limit,
    )


def _extension_for(post, downloaded_name: str | None = None) -> str:
    name = downloaded_name or post["media_name"] or ""
    suffix = Path(name).suffix
    if suffix:
        return suffix
    if post["media_kind"] == "voice":
        return ".ogg"
    if post["media_kind"] in {"video", "video_note"}:
        return ".mp4"
    if post["media_kind"] == "photo":
        return ".jpg"
    return mimetypes.guess_extension(post["media_mime"] or "") or ".bin"


async def download_post_media(post, destination: str | Path) -> Path:
    """Download one stored Telegram attachment just in time for bot delivery."""
    if post["media_kind"] == "text":
        raise RuntimeError("У поста нет медиа")
    if post["no_forwards"]:
        raise RuntimeError("Автор канала запретил пересылку этого материала")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    async with client_session() as client:
        entity = await client.get_entity(post["username"])
        msg = await client.get_messages(entity, ids=post["tg_message_id"])
        if msg is None or msg.media is None:
            raise RuntimeError("Оригинальное медиа удалено или недоступно")
        if getattr(msg, "noforwards", False):
            raise RuntimeError("Автор канала запретил пересылку этого материала")
        ext = _extension_for(post)
        target = destination / f"{post['source_id']}_{post['tg_message_id']}{ext}"
        result = await client.download_media(msg.media, file=str(target))
    if not result:
        raise RuntimeError("Telegram не вернул файл")
    path = Path(result)
    if path != target and target.suffix == ".bin":
        target = target.with_suffix(_extension_for(post, path.name))
        path.replace(target)
        path = target
    return path


async def stage_post_for_bot(
    post,
    bot_username: str,
    correlation_token: str,
) -> tuple[int, int, int]:
    """Send media server-side to the bot, replying to a correlation marker.

    The returned message IDs belong to the Telethon user's view of the chat.
    The bot must use the inbound Bot API update ID for any copy operation.
    """
    if post["media_kind"] == "text":
        raise RuntimeError("У поста нет медиа")
    if post["no_forwards"]:
        raise RuntimeError("Автор канала запретил пересылку этого материала")
    target = bot_username if bot_username.startswith("@") else "@" + bot_username
    async with client_session() as client:
        me = await client.get_me()
        entity = await client.get_entity(post["username"])
        msg = await client.get_messages(entity, ids=post["tg_message_id"])
        if msg is None or msg.media is None:
            raise RuntimeError("Оригинальное медиа удалено или недоступно")
        if getattr(msg, "noforwards", False):
            raise RuntimeError("Автор канала запретил пересылку этого материала")
        marker = await client.send_message(target, f"repost-staging:{correlation_token}", parse_mode=None)
        try:
            staged = await client.send_file(
                target,
                msg.media,
                caption=(msg.message or "")[:1024],
                reply_to=marker.id,
                voice_note=post["media_kind"] == "voice",
                video_note=post["media_kind"] == "video_note",
                supports_streaming=post["media_kind"] == "video",
                parse_mode=None,
            )
        except BaseException:
            try:
                await asyncio.shield(client.delete_messages(target, [marker.id], revoke=True))
            except Exception:
                pass
            raise
    if staged is None:
        raise RuntimeError("Telegram не подтвердил передачу медиа")
    return me.id, marker.id, staged.id


async def delete_bot_staging_messages(bot_username: str, message_ids: list[int]) -> None:
    target = bot_username if bot_username.startswith("@") else "@" + bot_username
    async with client_session() as client:
        await client.delete_messages(target, message_ids, revoke=True)


async def run_login() -> None:
    async with client_session() as client:
        me = await client.get_me()
        print(f"Авторизован как: {me.first_name or ''} {me.last_name or ''} (@{me.username or '—'})")


def main() -> None:
    ap = argparse.ArgumentParser(description="Telegram ingest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("login", help="интерактивная авторизация Telethon")
    p_back = sub.add_parser("backfill", help="полная выгрузка окна истории")
    window = p_back.add_mutually_exclusive_group()
    window.add_argument("--months", type=int, default=3, help="календарных месяцев истории (по умолчанию 3)")
    window.add_argument("--days", type=int, help="дней истории; удобно для теста")
    p_back.add_argument("--sources", help="переопределить sources.txt: @a,@b")
    p_back.add_argument("--limit", type=int, help="максимум новых материалов с одного источника")
    sub.add_parser("sync", help="догрузить только сообщения новее last_message_id")
    sub.add_parser("status", help="статистика базы")
    p_clean = sub.add_parser("cleanup", help="отключено: физическое удаление данных запрещено")
    p_clean.add_argument("--keep-days", type=int, default=120)
    args = ap.parse_args()

    if args.cmd == "login":
        asyncio.run(run_login())
        return
    if args.cmd == "status":
        print(db.stats(db.connect()))
        return
    if args.cmd == "cleanup":
        raise SystemExit(
            "Команда cleanup отключена: постоянные post/draft/publication/delivery "
            "нужны для безопасных Telegram-кнопок и защиты от дублей."
        )

    if args.cmd == "backfill" and args.sources:
        sources = [s.strip().casefold() for s in args.sources.split(",") if s.strip()]
    else:
        sources = config.read_sources()
        if args.cmd == "sync":
            conn = db.connect()
            known = [r["username"] for r in conn.execute("SELECT username FROM source WHERE active=1")]
            sources = sorted(set(sources) | set(known), key=str.casefold)
    if not sources:
        sys.exit("Нет источников: заполни sources.txt или передай --sources @a,@b")

    if args.cmd == "backfill":
        result = asyncio.run(run_backfill(sources, months=args.months, days=args.days, limit=args.limit))
        if result["errors"]:
            print(
                f"Сбор завершён с ошибками: {len(result['errors'])} из {result['sources']} источников.",
                file=sys.stderr,
            )
            raise SystemExit(1)
        if args.days is None and not args.sources and args.limit is None:
            completed_at = datetime.fromisoformat(result["window_end"])
            conn = db.connect()
            db.set_meta(conn, "last_full_sync_at", completed_at.isoformat())
            db.set_meta(conn, "next_full_sync_at", add_months(completed_at, args.months).isoformat())
    else:
        asyncio.run(
            run_fetch(
                sources,
                window_start=subtract_months(datetime.now(timezone.utc), config.SYNC_MONTHS),
                window_end=datetime.now(timezone.utc),
                incremental=True,
            )
        )


if __name__ == "__main__":
    main()
