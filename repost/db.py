import hashlib
import re
import sqlite3

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS source(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  title TEXT,
  last_message_id INTEGER NOT NULL DEFAULT 0,
  last_synced_at TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS post(
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
CREATE INDEX IF NOT EXISTS idx_post_status_date ON post(status, posted_at);
CREATE INDEX IF NOT EXISTS idx_post_hash ON post(text_hash);

CREATE TABLE IF NOT EXISTS draft(
  id INTEGER PRIMARY KEY,
  post_id INTEGER NOT NULL REFERENCES post(id),
  model TEXT,
  linkedin_text TEXT,
  x_text TEXT,
  threads_text TEXT,
  edited_text TEXT,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'awaiting_review',
  tg_message_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_draft_tg ON draft(tg_message_id);

CREATE TABLE IF NOT EXISTS publication(
  id INTEGER PRIMARY KEY,
  draft_id INTEGER NOT NULL REFERENCES draft(id),
  platform TEXT NOT NULL,
  status TEXT NOT NULL,
  external_id TEXT,
  error TEXT,
  published_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# post.status:  new -> drafted -> published | skipped   ('short' — не проходит MIN_POST_CHARS)
# draft.status: awaiting_review -> approved -> published | skipped | failed


def connect(path: str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(draft)")]
    if "edit_msg_id" not in cols:
        conn.execute("ALTER TABLE draft ADD COLUMN edit_msg_id INTEGER")
        conn.commit()
    return conn


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def upsert_source(conn: sqlite3.Connection, username: str, title: str | None = None) -> int:
    conn.execute(
        "INSERT INTO source(username, title) VALUES(?, ?) "
        "ON CONFLICT(username) DO UPDATE SET title=COALESCE(excluded.title, source.title)",
        (username, title),
    )
    conn.commit()
    return conn.execute("SELECT id FROM source WHERE username=?", (username,)).fetchone()["id"]


def insert_post(conn, source_id: int, tg_message_id: int, posted_at: str, text: str, url: str | None,
                author: str | None = None, status: str = "new") -> bool:
    """Returns True if inserted, False if duplicate (by message id or text hash)."""
    h = text_hash(text)
    if conn.execute("SELECT 1 FROM post WHERE text_hash=?", (h,)).fetchone():
        return False
    try:
        conn.execute(
            "INSERT INTO post(source_id, tg_message_id, posted_at, text, text_hash, url, author, status) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (source_id, tg_message_id, posted_at, text, h, url, author, status),
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False


def set_last_message_id(conn, source_id: int, message_id: int) -> None:
    conn.execute(
        "UPDATE source SET last_message_id=MAX(last_message_id, ?), last_synced_at=datetime('now') WHERE id=?",
        (message_id, source_id),
    )
    conn.commit()


def next_new_post(conn) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, s.username, s.title FROM post p JOIN source s ON s.id=p.source_id "
        "WHERE p.status='new' ORDER BY p.posted_at ASC LIMIT 1"
    ).fetchone()


def get_post(conn, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, s.username, s.title FROM post p JOIN source s ON s.id=p.source_id WHERE p.id=?",
        (post_id,),
    ).fetchone()


def set_post_status(conn, post_id: int, status: str) -> None:
    conn.execute("UPDATE post SET status=? WHERE id=?", (status, post_id))
    conn.commit()


def create_draft(conn, post_id: int, model: str, linkedin: str, x: str, threads: str, notes: str) -> int:
    cur = conn.execute(
        "INSERT INTO draft(post_id, model, linkedin_text, x_text, threads_text, notes) VALUES(?,?,?,?,?,?)",
        (post_id, model, linkedin, x, threads, notes),
    )
    conn.execute("UPDATE post SET status='drafted' WHERE id=?", (post_id,))
    conn.commit()
    return cur.lastrowid


def get_draft(conn, draft_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()


def draft_by_message(conn, tg_message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM draft WHERE tg_message_id=? OR edit_msg_id=? ORDER BY id DESC LIMIT 1",
        (tg_message_id, tg_message_id),
    ).fetchone()


def set_edit_msg(conn, draft_id: int, tg_message_id: int) -> None:
    conn.execute("UPDATE draft SET edit_msg_id=? WHERE id=?", (tg_message_id, draft_id))
    conn.commit()


def set_draft_message(conn, draft_id: int, tg_message_id: int) -> None:
    conn.execute("UPDATE draft SET tg_message_id=? WHERE id=?", (tg_message_id, draft_id))
    conn.commit()


def update_draft_texts(conn, draft_id: int, linkedin: str, x: str, threads: str, edited: str | None = None) -> None:
    conn.execute(
        "UPDATE draft SET linkedin_text=?, x_text=?, threads_text=?, edited_text=? WHERE id=?",
        (linkedin, x, threads, edited, draft_id),
    )
    conn.commit()


def set_draft_status(conn, draft_id: int, status: str) -> None:
    conn.execute("UPDATE draft SET status=? WHERE id=?", (status, draft_id))
    conn.commit()


def record_publication(conn, draft_id: int, platform: str, ok: bool, external_id: str | None, error: str | None) -> None:
    conn.execute(
        "INSERT INTO publication(draft_id, platform, status, external_id, error) VALUES(?,?,?,?,?)",
        (draft_id, platform, "ok" if ok else "error", external_id, error),
    )
    conn.commit()


def cleanup(conn, keep_days: int = 120) -> int:
    """Удаляет обработанные посты старше keep_days (и их черновики/публикации).

    Посты в статусе new не трогаем; свежие обработанные держим как защиту от
    повторного добавления тем же синком (окно синка << keep_days).
    """
    ids = [
        r["id"]
        for r in conn.execute(
            "SELECT id FROM post WHERE status != 'new' AND date(posted_at) < date('now', ?)",
            (f"-{keep_days} days",),
        )
    ]
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    conn.execute(
        f"DELETE FROM publication WHERE draft_id IN (SELECT id FROM draft WHERE post_id IN ({marks}))", ids
    )
    conn.execute(f"DELETE FROM draft WHERE post_id IN ({marks})", ids)
    conn.execute(f"DELETE FROM post WHERE id IN ({marks})", ids)
    conn.commit()
    conn.execute("VACUUM")
    return len(ids)


def stats(conn) -> dict:
    out = {}
    for row in conn.execute("SELECT status, COUNT(*) n FROM post GROUP BY status"):
        out[f"post:{row['status']}"] = row["n"]
    for row in conn.execute("SELECT COUNT(*) n, MIN(posted_at) oldest, MAX(posted_at) newest FROM post"):
        out["total"] = row["n"]
        out["oldest"] = row["oldest"]
        out["newest"] = row["newest"]
    for row in conn.execute("SELECT COUNT(*) n FROM source WHERE active=1"):
        out["sources"] = row["n"]
    return out
