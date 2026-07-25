"""Выгрузка постов публичного канала через веб-превью t.me/s/<username>.

Работает сразу, без авторизации, api_id и ботов — только для публичных
каналов с включённым веб-превью. Для приватных групп и как основной
надёжный путь остаётся repost.ingest (Telethon).

  python -m repost.webingest @dumik --days 3
  python -m repost.webingest @a @b @c --days 90
"""
import argparse
import html as htmllib
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

from . import config, db

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
MAX_PAGES = 60


def _extract_text(chunk: str) -> str:
    m = re.search(r'<div class="tgme_widget_message_text[^>]*>(.*?)</div>', chunk, re.S)
    if not m:
        return ""
    t = re.sub(r"<br\s*/?>", "\n", m.group(1))
    t = re.sub(r"<[^>]+>", "", t)
    return htmllib.unescape(t).strip()


def fetch_channel(username: str, days: int) -> tuple[str | None, list[tuple[int, datetime, str]]]:
    """-> (channel_title, [(message_id, dt, text), ...]) за последние `days` дней."""
    uname = username.lstrip("@")
    base = f"https://t.me/s/{uname}"
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    title: str | None = None
    posts: list[tuple[int, datetime, str]] = []
    before: int | None = None

    with httpx.Client(headers={"User-Agent": UA}, timeout=30, follow_redirects=True) as client:
        for _ in range(MAX_PAGES):
            url = base + (f"?before={before}" if before else "")
            r = client.get(url)
            r.raise_for_status()
            if "/s/" not in str(r.url):
                raise RuntimeError(f"у @{uname} выключено веб-превью (t.me/s недоступен) — нужен Telethon")
            if title is None:
                m = re.search(r'<meta property="og:title" content="([^"]*)"', r.text)
                title = htmllib.unescape(m.group(1)) if m else None

            page: list[tuple[int, datetime, str]] = []
            for chunk in r.text.split('class="tgme_widget_message_wrap')[1:]:
                m_id = re.search(r'data-post="[^"/]+/(\d+)"', chunk)
                m_dt = re.search(r'<time datetime="([^"]+)"', chunk)
                if not m_id or not m_dt:
                    continue
                dt = datetime.fromisoformat(m_dt.group(1))
                page.append((int(m_id.group(1)), dt, _extract_text(chunk)))
            if not page:
                break
            posts.extend(page)
            if min(p[1] for p in page) < cutoff:
                break
            before = min(p[0] for p in page)
            time.sleep(0.7)

    return title, [p for p in posts if p[1] >= cutoff]


def run(channels: list[str], days: int) -> None:
    conn = db.connect()
    for username in channels:
        username = username if username.startswith("@") else "@" + username
        try:
            title, posts = fetch_channel(username, days)
        except Exception as e:  # noqa: BLE001 — один канал не должен ронять остальные
            print(f"{username:<25} ОШИБКА: {e}")
            continue
        source_id = db.upsert_source(conn, username, title)
        added = 0
        for mid, dt, text in posts:
            if not text:
                continue
            status = "new" if len(text) >= config.MIN_POST_CHARS else "short"
            url = f"https://t.me/{username.lstrip('@')}/{mid}"
            if db.insert_post(conn, source_id, mid, dt.astimezone(timezone.utc).isoformat(),
                              text, url, author=title, status=status):
                added += 1
        if posts:
            db.set_last_message_id(conn, source_id, max(p[0] for p in posts))
        print(f"{username:<25} найдено {len(posts):>4} за {days} дн., добавлено в базу {added}")
    print(f"\nИтого в базе: {db.stats(conn)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Выгрузка публичных каналов через t.me/s")
    ap.add_argument("channels", nargs="+", help="@username каналов")
    ap.add_argument("--days", type=int, default=3)
    args = ap.parse_args()
    run(args.channels, args.days)


if __name__ == "__main__":
    sys.exit(main())
