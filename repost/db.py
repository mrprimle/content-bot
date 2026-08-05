import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone

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
  media_kind TEXT NOT NULL DEFAULT 'text',
  media_mime TEXT,
  media_size INTEGER,
  media_name TEXT,
  bot_media_file_id TEXT,
  media_access_token TEXT,
  no_forwards INTEGER NOT NULL DEFAULT 0,
  transcript TEXT,
  summary TEXT,
  offered_at TEXT,
  raw_message_id INTEGER,
  manual_prompt_id INTEGER,
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
  ai_prompt_id INTEGER,
  include_media INTEGER NOT NULL DEFAULT 0,
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

CREATE TABLE IF NOT EXISTS delivery_batch(
  id INTEGER PRIMARY KEY,
  slot_key TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS delivery_item(
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES delivery_batch(id),
  source_id INTEGER NOT NULL REFERENCES source(id),
  post_id INTEGER UNIQUE NOT NULL REFERENCES post(id),
  status TEXT NOT NULL DEFAULT 'claimed',
  claim_token TEXT,
  claimed_at TEXT,
  bot_message_id INTEGER,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS planning_session(
  id INTEGER PRIMARY KEY,
  planning_date TEXT UNIQUE NOT NULL,
  target_date TEXT NOT NULL,
  target_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS planning_slot(
  id INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES planning_session(id),
  position INTEGER NOT NULL,
  publish_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'selecting',
  post_id INTEGER UNIQUE REFERENCES post(id),
  draft_id INTEGER UNIQUE REFERENCES draft(id),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  publish_claimed_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(session_id, position)
);
CREATE INDEX IF NOT EXISTS idx_planning_slot_due ON planning_slot(status, publish_at);

CREATE TABLE IF NOT EXISTS app_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

POSTGRES_SCHEMA = """
CREATE TABLE IF NOT EXISTS source(
  id BIGSERIAL PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  title TEXT,
  last_message_id BIGINT NOT NULL DEFAULT 0,
  last_synced_at TEXT,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS post(
  id BIGSERIAL PRIMARY KEY,
  source_id BIGINT NOT NULL REFERENCES source(id),
  tg_message_id BIGINT NOT NULL,
  posted_at TEXT NOT NULL,
  author TEXT,
  text TEXT NOT NULL,
  text_hash TEXT NOT NULL,
  url TEXT,
  media_kind TEXT NOT NULL DEFAULT 'text',
  media_mime TEXT,
  media_size BIGINT,
  media_name TEXT,
  bot_media_file_id TEXT,
  media_access_token TEXT,
  no_forwards INTEGER NOT NULL DEFAULT 0,
  transcript TEXT,
  summary TEXT,
  offered_at TEXT,
  raw_message_id BIGINT,
  manual_prompt_id BIGINT,
  status TEXT NOT NULL DEFAULT 'new',
  created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text),
  UNIQUE(source_id, tg_message_id)
);
CREATE INDEX IF NOT EXISTS idx_post_status_date ON post(status, posted_at);
CREATE INDEX IF NOT EXISTS idx_post_hash ON post(text_hash);
CREATE INDEX IF NOT EXISTS idx_post_manual_prompt ON post(manual_prompt_id);
ALTER TABLE post ADD COLUMN IF NOT EXISTS bot_media_file_id TEXT;
ALTER TABLE post ADD COLUMN IF NOT EXISTS media_access_token TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_post_media_token ON post(media_access_token);

CREATE TABLE IF NOT EXISTS draft(
  id BIGSERIAL PRIMARY KEY,
  post_id BIGINT NOT NULL REFERENCES post(id),
  model TEXT,
  linkedin_text TEXT,
  x_text TEXT,
  threads_text TEXT,
  edited_text TEXT,
  notes TEXT,
  status TEXT NOT NULL DEFAULT 'awaiting_review',
  tg_message_id BIGINT,
  edit_msg_id BIGINT,
  ai_prompt_id BIGINT,
  include_media INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text)
);
CREATE INDEX IF NOT EXISTS idx_draft_tg ON draft(tg_message_id);
ALTER TABLE draft ADD COLUMN IF NOT EXISTS ai_prompt_id BIGINT;
ALTER TABLE draft ADD COLUMN IF NOT EXISTS include_media INTEGER NOT NULL DEFAULT 0;

CREATE TABLE IF NOT EXISTS publication(
  id BIGSERIAL PRIMARY KEY,
  draft_id BIGINT NOT NULL REFERENCES draft(id),
  platform TEXT NOT NULL,
  status TEXT NOT NULL,
  external_id TEXT,
  error TEXT,
  published_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text)
);

CREATE TABLE IF NOT EXISTS delivery_batch(
  id BIGSERIAL PRIMARY KEY,
  slot_key TEXT UNIQUE NOT NULL,
  created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text)
);

CREATE TABLE IF NOT EXISTS delivery_item(
  id BIGSERIAL PRIMARY KEY,
  batch_id BIGINT NOT NULL REFERENCES delivery_batch(id),
  source_id BIGINT NOT NULL REFERENCES source(id),
  post_id BIGINT UNIQUE NOT NULL REFERENCES post(id),
  status TEXT NOT NULL DEFAULT 'claimed',
  claim_token TEXT,
  claimed_at TEXT,
  bot_message_id BIGINT,
  sent_at TEXT
);

CREATE TABLE IF NOT EXISTS planning_session(
  id BIGSERIAL PRIMARY KEY,
  planning_date TEXT UNIQUE NOT NULL,
  target_date TEXT NOT NULL,
  target_count INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text),
  completed_at TEXT
);

CREATE TABLE IF NOT EXISTS planning_slot(
  id BIGSERIAL PRIMARY KEY,
  session_id BIGINT NOT NULL REFERENCES planning_session(id),
  position INTEGER NOT NULL,
  publish_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'selecting',
  post_id BIGINT UNIQUE REFERENCES post(id),
  draft_id BIGINT UNIQUE REFERENCES draft(id),
  attempt_count INTEGER NOT NULL DEFAULT 0,
  publish_claimed_at TEXT,
  last_error TEXT,
  created_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text),
  updated_at TEXT NOT NULL DEFAULT ((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text),
  UNIQUE(session_id, position)
);
CREATE INDEX IF NOT EXISTS idx_planning_slot_due ON planning_slot(status, publish_at);

CREATE TABLE IF NOT EXISTS app_meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

_PG_SCHEMA_READY = False


class PostgresConnection:
    """Small DB-API compatibility layer for the existing SQLite-oriented queries."""

    backend = "postgres"

    def __init__(self, raw):
        self.raw = raw

    @staticmethod
    def _sql(sql: str) -> str:
        normalized = sql.replace("?", "%s")
        normalized = normalized.replace(
            "datetime('now')",
            "((CURRENT_TIMESTAMP AT TIME ZONE 'UTC')::text)",
        )
        normalized = normalized.replace(
            "MAX(last_message_id, %s)",
            "GREATEST(last_message_id, %s)",
        )
        normalized = normalized.replace(
            "MAX(no_forwards, %s)",
            "GREATEST(no_forwards, %s)",
        )
        if "INSERT OR IGNORE INTO" in normalized:
            normalized = normalized.replace("INSERT OR IGNORE INTO", "INSERT INTO")
            normalized = normalized.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        return normalized

    def execute(self, sql: str, params=()):
        return self.raw.execute(self._sql(sql), params)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()


def is_postgres(conn) -> bool:
    return getattr(conn, "backend", None) == "postgres"


def acquire_telegram_session_lock(conn) -> None:
    if is_postgres(conn):
        conn.execute("SELECT pg_advisory_lock(781342992)")


def release_telegram_session_lock(conn) -> None:
    if is_postgres(conn):
        conn.execute("SELECT pg_advisory_unlock(781342992)")
        conn.commit()


def acquire_queue_sync_lock(conn) -> None:
    """Block queue claims while a PostgreSQL refetch is forming the next pool."""
    if is_postgres(conn):
        conn.execute("SELECT pg_advisory_lock(781342991)")


def release_queue_sync_lock(conn) -> None:
    if is_postgres(conn):
        conn.execute("SELECT pg_advisory_unlock(781342991)")
        conn.commit()


def _begin_write(conn) -> None:
    if is_postgres(conn):
        # Serialize queue/source reconciliation exactly like SQLite BEGIN IMMEDIATE.
        conn.execute("SELECT pg_advisory_xact_lock(781342991)")
    else:
        conn.execute("BEGIN IMMEDIATE")


def _is_integrity_error(exc: BaseException) -> bool:
    if isinstance(exc, sqlite3.IntegrityError):
        return True
    try:
        import psycopg

        return isinstance(exc, psycopg.IntegrityError)
    except ImportError:
        return False

# post.status:  new -> queued -> offered -> generating/awaiting_manual -> drafted -> published | skipped
#               ('short' — не проходит MIN_POST_CHARS)
# draft.status: awaiting_review -> approved -> published | skipped | failed


def connect(path: str | None = None):
    global _PG_SCHEMA_READY
    if config.DATABASE_URL and path is None:
        import psycopg
        from psycopg.rows import dict_row

        raw = psycopg.connect(config.DATABASE_URL, row_factory=dict_row)
        conn = PostgresConnection(raw)
        if not _PG_SCHEMA_READY:
            for statement in POSTGRES_SCHEMA.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.commit()
            _PG_SCHEMA_READY = True
        return conn
    conn = sqlite3.connect(path or config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _ensure_column(conn, "draft", "edit_msg_id", "INTEGER")
    _ensure_column(conn, "draft", "ai_prompt_id", "INTEGER")
    _ensure_column(conn, "post", "media_kind", "TEXT NOT NULL DEFAULT 'text'")
    _ensure_column(conn, "post", "media_mime", "TEXT")
    _ensure_column(conn, "post", "media_size", "INTEGER")
    _ensure_column(conn, "post", "media_name", "TEXT")
    _ensure_column(conn, "post", "bot_media_file_id", "TEXT")
    _ensure_column(conn, "post", "media_access_token", "TEXT")
    _ensure_column(conn, "post", "no_forwards", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "post", "transcript", "TEXT")
    _ensure_column(conn, "post", "summary", "TEXT")
    _ensure_column(conn, "post", "offered_at", "TEXT")
    _ensure_column(conn, "post", "raw_message_id", "INTEGER")
    _ensure_column(conn, "post", "manual_prompt_id", "INTEGER")
    _ensure_column(conn, "draft", "include_media", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "delivery_item", "claim_token", "TEXT")
    _ensure_column(conn, "delivery_item", "claimed_at", "TEXT")
    _remove_delivery_source_uniqueness(conn)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_post_manual_prompt ON post(manual_prompt_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_post_media_token ON post(media_access_token)")
    conn.commit()
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _remove_delivery_source_uniqueness(conn: sqlite3.Connection) -> None:
    """Allow a slot crossing a pool boundary to contain the same source twice.

    The old queue delivered one row per source *per slot* and therefore had a
    UNIQUE(batch_id, source_id) constraint. The new queue keeps that guarantee
    per candidate-pool round instead. A two-item slot may consume the last item
    of one round and the first item of the next, which can legitimately be the
    same source. Rebuild the small bookkeeping table once for existing DBs.
    """
    leftovers = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('delivery_item_legacy','delivery_item_new')"
    ).fetchall()
    if leftovers:
        names = ", ".join(row["name"] for row in leftovers)
        raise RuntimeError(
            f"Найдена незавершённая миграция delivery_item ({names}); "
            "восстанови БД из backup перед запуском"
        )
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='delivery_item'"
    ).fetchone()
    if not row or not row["sql"]:
        return
    normalized = re.sub(r"\s+", "", row["sql"]).casefold()
    if "unique(batch_id,source_id)" not in normalized:
        return
    _begin_write(conn)
    try:
        conn.execute(
            """
            CREATE TABLE delivery_item_new(
              id INTEGER PRIMARY KEY,
              batch_id INTEGER NOT NULL REFERENCES delivery_batch(id),
              source_id INTEGER NOT NULL REFERENCES source(id),
              post_id INTEGER UNIQUE NOT NULL REFERENCES post(id),
              status TEXT NOT NULL DEFAULT 'claimed',
              claim_token TEXT,
              claimed_at TEXT,
              bot_message_id INTEGER,
              sent_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO delivery_item_new("
            "id, batch_id, source_id, post_id, status, claim_token, claimed_at, bot_message_id, sent_at"
            ") SELECT "
            "id, batch_id, source_id, post_id, status, claim_token, claimed_at, bot_message_id, sent_at "
            "FROM delivery_item"
        )
        conn.execute("DROP TABLE delivery_item")
        conn.execute("ALTER TABLE delivery_item_new RENAME TO delivery_item")
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def text_hash(text: str) -> str:
    norm = re.sub(r"\s+", " ", text).strip().lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def upsert_source(conn: sqlite3.Connection, username: str, title: str | None = None) -> int:
    if username.startswith("@"):
        username = username.casefold()
    conn.execute(
        "INSERT INTO source(username, title) VALUES(?, ?) "
        "ON CONFLICT(username) DO UPDATE SET title=COALESCE(excluded.title, source.title)",
        (username, title),
    )
    conn.commit()
    return conn.execute("SELECT id FROM source WHERE username=?", (username,)).fetchone()["id"]


def reconcile_active_sources(conn: sqlite3.Connection, usernames: list[str]) -> dict[str, int]:
    """Make the configured source list authoritative without deleting stored data.

    An empty list is treated as a missing/unreadable configuration and is a safe
    no-op. Legacy case variants are collapsed to one active row, while any
    duplicate rows and all non-configured sources are retained but deactivated.
    """
    configured: list[str] = []
    seen: set[str] = set()
    for raw in usernames:
        username = raw.strip()
        if not username:
            continue
        if username.startswith("@"):
            username = username.casefold()
        key = username.casefold()
        if key in seen:
            continue
        seen.add(key)
        configured.append(username)
    if not configured:
        return {"configured": 0, "created": 0, "renamed": 0, "deactivated": 0}

    created = renamed = 0
    active_ids: list[int] = []
    _begin_write(conn)
    try:
        for username in configured:
            matches = conn.execute(
                "SELECT id, username FROM source WHERE lower(username)=lower(?) "
                "ORDER BY CASE WHEN username=? THEN 0 ELSE 1 END, id",
                (username, username),
            ).fetchall()
            if matches:
                chosen = matches[0]
                source_id = chosen["id"]
                if chosen["username"] != username:
                    conn.execute("UPDATE source SET username=? WHERE id=?", (username, source_id))
                    renamed += 1
            else:
                cur = conn.execute(
                    "INSERT INTO source(username, active) VALUES(?, 1) RETURNING id",
                    (username,),
                )
                source_id = cur.fetchone()["id"]
                created += 1
            active_ids.append(source_id)

        marks = ",".join("?" for _ in active_ids)
        previously_active = conn.execute(
            f"SELECT COUNT(*) n FROM source WHERE active=1 AND id NOT IN ({marks})",
            active_ids,
        ).fetchone()["n"]
        conn.execute(
            f"UPDATE source SET active=CASE WHEN id IN ({marks}) THEN 1 ELSE 0 END",
            active_ids,
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "configured": len(active_ids),
        "created": created,
        "renamed": renamed,
        "deactivated": previously_active,
    }


def insert_post(
    conn,
    source_id: int,
    tg_message_id: int,
    posted_at: str,
    text: str,
    url: str | None,
    author: str | None = None,
    status: str = "new",
    *,
    media_kind: str = "text",
    media_mime: str | None = None,
    media_size: int | None = None,
    media_name: str | None = None,
    no_forwards: bool = False,
) -> bool:
    """Insert one source item, suppressing message and cross-source text duplicates.

    Empty-text media use a source/message-specific hash, so unrelated voice or
    video posts are never collapsed merely because they have no caption.
    """
    text = text or ""
    h = text_hash(text) if text.strip() else text_hash(f"media:{source_id}:{tg_message_id}")
    exact_message = conn.execute(
        "SELECT id FROM post WHERE source_id=? AND tg_message_id=?",
        (source_id, tg_message_id),
    ).fetchone()
    if exact_message is None and text.strip():
        duplicate_text = conn.execute(
            "SELECT id FROM post WHERE text_hash=? AND trim(text)<>'' LIMIT 1",
            (h,),
        ).fetchone()
        if duplicate_text is not None:
            return False
    try:
        conn.execute(
            "INSERT INTO post(source_id, tg_message_id, posted_at, text, text_hash, url, author, status, "
            "media_kind, media_mime, media_size, media_name, no_forwards) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                source_id,
                tg_message_id,
                posted_at,
                text,
                h,
                url,
                author,
                status,
                media_kind,
                media_mime,
                media_size,
                media_name,
                int(no_forwards),
            ),
        )
        conn.commit()
        return True
    except Exception as exc:
        if not _is_integrity_error(exc):
            raise
        conn.rollback()
        conn.execute(
            "UPDATE post SET "
            "text=CASE WHEN text='' AND ?!='' THEN ? ELSE text END, "
            "text_hash=CASE WHEN text='' AND ?!='' THEN ? ELSE text_hash END, "
            "url=COALESCE(url, ?), author=COALESCE(author, ?), "
            "media_kind=CASE WHEN media_kind='text' AND ?!='text' THEN ? ELSE media_kind END, "
            "media_mime=COALESCE(media_mime, ?), media_size=COALESCE(media_size, ?), "
            "media_name=COALESCE(media_name, ?), no_forwards=MAX(no_forwards, ?), "
            "status=CASE WHEN status='short' AND ?!='text' THEN 'new' ELSE status END "
            "WHERE source_id=? AND tg_message_id=?",
            (
                text,
                text,
                text,
                text_hash(text) if text.strip() else h,
                url,
                author,
                media_kind,
                media_kind,
                media_mime,
                media_size,
                media_name,
                int(no_forwards),
                media_kind,
                source_id,
                tg_message_id,
            ),
        )
        conn.commit()
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


def _fill_candidate_pool(
    conn: sqlite3.Connection,
    *,
    source_username: str | None = None,
) -> int:
    """Queue one oldest pending material from every active source."""
    params: list[object] = []
    source_filter = ""
    if source_username:
        source_filter = " AND lower(s.username)=lower(?)"
        params.append(source_username)
    rows = conn.execute(
        "WITH ranked AS ("
        " SELECT p.id post_id,"
        " ROW_NUMBER() OVER (PARTITION BY p.source_id ORDER BY p.posted_at, p.tg_message_id, p.id) rn"
        " FROM post p JOIN source s ON s.id=p.source_id"
        " WHERE p.status='new' AND s.active=1" + source_filter +
        ") SELECT post_id FROM ranked WHERE rn=1",
        params,
    ).fetchall()
    if not rows:
        return 0
    ids = [row["post_id"] for row in rows]
    marks = ",".join("?" for _ in ids)
    conn.execute(
        f"UPDATE post SET status='queued' WHERE id IN ({marks}) AND status='new'",
        ids,
    )
    return len(ids)


def claim_oldest_posts(
    conn: sqlite3.Connection,
    slot_key: str,
    *,
    source_username: str | None = None,
    max_items: int = 2,
) -> list[sqlite3.Row]:
    """Atomically reserve the oldest materials for one delivery slot.

    A persistent candidate pool contains one oldest pending item of every
    active source. The next round is filled only after the current pool is
    exhausted. From the pool we take ``max_items`` globally oldest rows.
    """
    if max_items < 1:
        return []
    _begin_write(conn)
    try:
        existing = conn.execute("SELECT id FROM delivery_batch WHERE slot_key=?", (slot_key,)).fetchone()
        if existing is None:
            cur = conn.execute(
                "INSERT INTO delivery_batch(slot_key) VALUES(?) RETURNING id",
                (slot_key,),
            )
            batch_id = cur.fetchone()["id"]
        else:
            batch_id = existing["id"]
        existing_count = conn.execute(
            "SELECT COUNT(*) n FROM delivery_item WHERE batch_id=?",
            (batch_id,),
        ).fetchone()["n"]
        if existing_count == 0:
            params: list[object] = []
            source_filter = ""
            if source_username:
                source_filter = " AND lower(s.username)=lower(?)"
                params.append(source_username)
            now = datetime.now(timezone.utc).isoformat()
            reserved = 0
            while reserved < max_items:
                select_params = [*params, max_items - reserved]
                chosen = conn.execute(
                    "SELECT p.id post_id, p.source_id FROM post p "
                    "JOIN source s ON s.id=p.source_id "
                    "WHERE p.status='queued' AND s.active=1" + source_filter +
                    " ORDER BY p.posted_at, p.tg_message_id, p.id LIMIT ?",
                    select_params,
                ).fetchall()
                if not chosen:
                    if _fill_candidate_pool(conn, source_username=source_username) == 0:
                        break
                    continue
                reserved_before = reserved
                for row in chosen:
                    updated = conn.execute(
                        "UPDATE post SET status='offered', offered_at=? "
                        "WHERE id=? AND status='queued'",
                        (now, row["post_id"]),
                    )
                    if updated.rowcount != 1:
                        continue
                    inserted = conn.execute(
                        "INSERT OR IGNORE INTO delivery_item(batch_id, source_id, post_id) "
                        "VALUES(?,?,?)",
                        (batch_id, row["source_id"], row["post_id"]),
                    )
                    if inserted.rowcount == 1:
                        reserved += 1
                    else:
                        conn.execute(
                            "UPDATE post SET status='queued', offered_at=NULL "
                            "WHERE id=? AND status='offered'",
                            (row["post_id"],),
                        )
                if reserved == reserved_before:
                    break
        token = uuid.uuid4().hex
        conn.execute(
            "UPDATE delivery_item SET status='sending', claim_token=?, claimed_at=datetime('now') "
            "WHERE batch_id=? AND status='claimed'",
            (token, batch_id),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return conn.execute(
        "SELECT p.*, s.username, s.title, di.id delivery_item_id, di.claim_token "
        "FROM delivery_item di JOIN post p ON p.id=di.post_id JOIN source s ON s.id=p.source_id "
        "WHERE di.batch_id=? AND di.status='sending' AND di.claim_token=? "
        "ORDER BY p.posted_at, p.tg_message_id, p.id",
        (batch_id, token),
    ).fetchall()


def mark_delivery_sent(
    conn: sqlite3.Connection,
    post_id: int,
    bot_message_id: int,
    claim_token: str,
) -> bool:
    """Finish a delivery only when the caller still owns its sending lease."""
    try:
        cur = conn.execute(
            "UPDATE delivery_item SET status='sent', bot_message_id=?, sent_at=datetime('now') "
            "WHERE post_id=? AND status='sending' AND claim_token=?",
            (bot_message_id, post_id, claim_token),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        post_cur = conn.execute("UPDATE post SET raw_message_id=? WHERE id=?", (bot_message_id, post_id))
        if post_cur.rowcount != 1:
            raise RuntimeError(f"delivery post {post_id} disappeared")
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def release_delivery(conn: sqlite3.Connection, post_id: int, claim_token: str) -> bool:
    """Return a material to the queue when its raw Telegram delivery failed."""
    try:
        cur = conn.execute(
            "DELETE FROM delivery_item WHERE post_id=? AND status='sending' AND claim_token=?",
            (post_id, claim_token),
        )
        if cur.rowcount != 1:
            conn.rollback()
            return False
        post_cur = conn.execute(
            "UPDATE post SET status='queued', offered_at=NULL WHERE id=? AND status='offered'",
            (post_id,),
        )
        if post_cur.rowcount != 1:
            conn.rollback()
            return False
        conn.commit()
        return True
    except Exception:
        conn.rollback()
        raise


def recover_incomplete_deliveries(conn: sqlite3.Connection) -> int:
    """Requeue items reserved by a process that stopped before sending them."""
    _begin_write(conn)
    try:
        ids = [
            row["post_id"]
            for row in conn.execute(
                "SELECT post_id FROM delivery_item WHERE status IN ('claimed','sending')"
            )
        ]
        if ids:
            marks = ",".join("?" for _ in ids)
            conn.execute(f"DELETE FROM delivery_item WHERE post_id IN ({marks})", ids)
            conn.execute(
                f"UPDATE post SET status='queued', offered_at=NULL "
                f"WHERE id IN ({marks}) AND status='offered'",
                ids,
            )
        conn.commit()
        return len(ids)
    except Exception:
        conn.rollback()
        raise


def recover_stranded_work(conn: sqlite3.Connection) -> dict[str, int]:
    """Reconcile work interrupted between a durable state change and its result.

    ``generating`` is a local LLM/adaptation lease. It is safe to make the
    original action available again when no delivered draft exists. A
    ``publishing`` draft is different: Buffer may already have accepted it, so
    startup must mark the result unknown and must never retry automatically.
    """
    result = {
        "generating_reoffered": 0,
        "generating_manual": 0,
        "generating_reconciled": 0,
        "undelivered_drafts_reopened": 0,
        "manual_without_prompt_reoffered": 0,
        "publishing_unknown": 0,
        "ai_edits_reopened": 0,
    }
    _begin_write(conn)
    try:
        publishing = conn.execute(
            "UPDATE draft SET status='publish_unknown' WHERE status='publishing'"
        )
        result["publishing_unknown"] = publishing.rowcount
        ai_editing = conn.execute(
            "UPDATE draft SET status='awaiting_review' WHERE status='ai_editing'"
        )
        result["ai_edits_reopened"] = ai_editing.rowcount

        rows = conn.execute(
            "SELECT p.id post_id, p.manual_prompt_id, "
            "d.id draft_id, d.status draft_status, d.tg_message_id draft_message_id "
            "FROM post p "
            "LEFT JOIN draft d ON d.id=("
            " SELECT latest.id FROM draft latest "
            " WHERE latest.post_id=p.id ORDER BY latest.id DESC LIMIT 1"
            ") "
            "WHERE p.status='generating' ORDER BY p.id"
        ).fetchall()
        for row in rows:
            draft_status = row["draft_status"]
            draft_was_delivered = row["draft_message_id"] is not None
            if draft_status == "published":
                target = "published"
                result["generating_reconciled"] += 1
            elif draft_status in {"skipped", "expired"}:
                target = "skipped"
                result["generating_reconciled"] += 1
            elif draft_status in {"approved", "publish_unknown"} or (
                draft_status == "awaiting_review" and draft_was_delivered
            ):
                target = "drafted"
                result["generating_reconciled"] += 1
            elif row["manual_prompt_id"] is not None:
                # The raw-media keyboard was replaced by a ForceReply prompt.
                # Reopen that prompt instead of leaving the post unreachable.
                target = "awaiting_manual"
                result["generating_manual"] += 1
            else:
                # No durable/delivered draft: the original raw keyboard can be
                # used again and no external side effect needs to be repeated.
                target = "offered"
                result["generating_reoffered"] += 1
            conn.execute("UPDATE post SET status=? WHERE id=?", (target, row["post_id"]))

        undelivered_drafts = conn.execute(
            "SELECT p.id post_id, p.manual_prompt_id, "
            "d.id draft_id, d.status draft_status "
            "FROM post p "
            "JOIN draft d ON d.id=("
            " SELECT latest.id FROM draft latest "
            " WHERE latest.post_id=p.id ORDER BY latest.id DESC LIMIT 1"
            ") "
            "WHERE p.status='drafted' "
            "AND d.status IN ('awaiting_review','delivery_failed') "
            "AND d.tg_message_id IS NULL "
            "ORDER BY p.id"
        ).fetchall()
        for row in undelivered_drafts:
            # create_draft commits before Telegram delivery. Preserve the draft
            # itself, mark its delivery incomplete, and reopen the action that
            # can resend this exact draft without another LLM or publish call.
            if row["draft_status"] == "awaiting_review":
                conn.execute(
                    "UPDATE draft SET status='delivery_failed' "
                    "WHERE id=? AND status='awaiting_review' AND tg_message_id IS NULL",
                    (row["draft_id"],),
                )
            target = (
                "awaiting_manual"
                if row["manual_prompt_id"] is not None
                else "offered"
            )
            conn.execute("UPDATE post SET status=? WHERE id=?", (target, row["post_id"]))
            result["undelivered_drafts_reopened"] += 1

        missing_manual_prompts = conn.execute(
            "UPDATE post SET status='offered' "
            "WHERE status='awaiting_manual' AND manual_prompt_id IS NULL"
        )
        result["manual_without_prompt_reoffered"] = missing_manual_prompts.rowcount

        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def claim_next_post(conn) -> sqlite3.Row | None:
    """Самый старый пост из очереди, сразу помеченный 'drafting'.

    Пометка ставится до обращения к LLM: иначе второй вызов (слот расписания
    и /next одновременно) успевает взять тот же пост, и черновик создаётся дважды.
    """
    row = next_new_post(conn)
    if row is not None:
        set_post_status(conn, row["id"], "drafting")
    return row


def pending_drafts(conn) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM draft WHERE status='awaiting_review' ORDER BY id").fetchall()


def get_post(conn, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, s.username, s.title FROM post p JOIN source s ON s.id=p.source_id WHERE p.id=?",
        (post_id,),
    ).fetchone()


def transition_post(conn: sqlite3.Connection, post_id: int, from_statuses: tuple[str, ...], to_status: str) -> bool:
    marks = ",".join("?" for _ in from_statuses)
    cur = conn.execute(
        f"UPDATE post SET status=? WHERE id=? AND status IN ({marks})",
        (to_status, post_id, *from_statuses),
    )
    conn.commit()
    return cur.rowcount == 1


def transition_draft(
    conn: sqlite3.Connection,
    draft_id: int,
    from_statuses: tuple[str, ...],
    to_status: str,
) -> bool:
    """Compare-and-set a draft so only one concurrent button action wins."""
    marks = ",".join("?" for _ in from_statuses)
    cur = conn.execute(
        f"UPDATE draft SET status=? WHERE id=? AND status IN ({marks})",
        (to_status, draft_id, *from_statuses),
    )
    conn.commit()
    return cur.rowcount == 1


def set_post_status(conn, post_id: int, status: str) -> None:
    conn.execute("UPDATE post SET status=? WHERE id=?", (status, post_id))
    conn.commit()


def set_post_bot_media(conn, post_id: int, file_id: str, access_token: str) -> None:
    """Persist a reusable Bot API file id and an unguessable public media token."""
    conn.execute(
        "UPDATE post SET bot_media_file_id=?, media_access_token=? WHERE id=?",
        (file_id, access_token, post_id),
    )
    conn.commit()


def post_by_media_token(conn, access_token: str):
    return conn.execute(
        "SELECT p.*, s.username, s.title FROM post p JOIN source s ON s.id=p.source_id "
        "WHERE p.media_access_token=? AND p.bot_media_file_id IS NOT NULL LIMIT 1",
        (access_token,),
    ).fetchone()


def create_draft(conn, post_id: int, model: str, linkedin: str, x: str, threads: str, notes: str) -> int:
    existing = conn.execute(
        "SELECT id FROM draft WHERE post_id=? AND status NOT IN ('expired','skipped') ORDER BY id DESC LIMIT 1",
        (post_id,),
    ).fetchone()
    if existing:
        return existing["id"]
    cur = conn.execute(
        "INSERT INTO draft(post_id, model, linkedin_text, x_text, threads_text, notes) "
        "VALUES(?,?,?,?,?,?) RETURNING id",
        (post_id, model, linkedin, x, threads, notes),
    )
    draft_id = cur.fetchone()["id"]
    conn.execute("UPDATE post SET status='drafted' WHERE id=?", (post_id,))
    conn.commit()
    return draft_id


def active_draft_for_post(conn: sqlite3.Connection, post_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM draft WHERE post_id=? AND status IN ('awaiting_review','approved','delivery_failed') "
        "ORDER BY id DESC LIMIT 1",
        (post_id,),
    ).fetchone()


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


def set_ai_prompt(conn, draft_id: int, tg_message_id: int | None) -> None:
    conn.execute("UPDATE draft SET ai_prompt_id=? WHERE id=?", (tg_message_id, draft_id))
    conn.commit()


def draft_by_ai_prompt(conn, tg_message_id: int):
    return conn.execute(
        "SELECT * FROM draft WHERE ai_prompt_id=? ORDER BY id DESC LIMIT 1",
        (tg_message_id,),
    ).fetchone()


def set_manual_prompt(conn: sqlite3.Connection, post_id: int, tg_message_id: int) -> None:
    conn.execute(
        "UPDATE post SET manual_prompt_id=?, status='awaiting_manual' WHERE id=?",
        (tg_message_id, post_id),
    )
    conn.commit()


def post_by_manual_prompt(conn: sqlite3.Connection, tg_message_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT p.*, s.username, s.title FROM post p JOIN source s ON s.id=p.source_id "
        "WHERE p.manual_prompt_id=? ORDER BY p.id DESC LIMIT 1",
        (tg_message_id,),
    ).fetchone()


def pending_owner_post(conn, chat_id: int) -> sqlite3.Row | None:
    """Return the owner's unfinished anytime/manual post input, if one exists."""
    return conn.execute(
        "SELECT p.*, s.username, s.title FROM post p JOIN source s ON s.id=p.source_id "
        "WHERE s.username=? AND p.status='awaiting_manual' ORDER BY p.id DESC LIMIT 1",
        (f"manual:{chat_id}",),
    ).fetchone()


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


def set_draft_include_media(conn, draft_id: int, include_media: bool) -> None:
    conn.execute(
        "UPDATE draft SET include_media=? WHERE id=?",
        (1 if include_media else 0, draft_id),
    )
    conn.commit()


def claim_draft_publish(conn: sqlite3.Connection, draft_id: int) -> bool:
    cur = conn.execute(
        "UPDATE draft SET status='publishing' WHERE id=? AND status IN ('awaiting_review','approved')",
        (draft_id,),
    )
    conn.commit()
    return cur.rowcount == 1


def set_transcript(conn: sqlite3.Connection, post_id: int, transcript: str, summary: str) -> None:
    conn.execute("UPDATE post SET transcript=?, summary=? WHERE id=?", (transcript, summary, post_id))
    conn.commit()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_meta(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()


def delete_meta(conn: sqlite3.Connection, *keys: str) -> None:
    if not keys:
        return
    marks = ",".join("?" for _ in keys)
    conn.execute(f"DELETE FROM app_meta WHERE key IN ({marks})", keys)
    conn.commit()


def claim_incremental_refetch(conn, lease_seconds: int = 600) -> bool:
    """Acquire a cross-process lease so queue exhaustion triggers one Telegram sync."""
    now = datetime.now(timezone.utc)
    _begin_write(conn)
    try:
        raw = get_meta(conn, "incremental_refetch_lease_until")
        if raw:
            try:
                if datetime.fromisoformat(raw) > now:
                    conn.rollback()
                    return False
            except ValueError:
                pass
        lease_until = now.timestamp() + lease_seconds
        set_meta(
            conn,
            "incremental_refetch_lease_until",
            datetime.fromtimestamp(lease_until, timezone.utc).isoformat(),
        )
        return True
    except Exception:
        conn.rollback()
        raise


def finish_incremental_refetch(conn, result: dict | None = None) -> None:
    delete_meta(conn, "incremental_refetch_lease_until")
    if result is not None:
        set_meta(conn, "last_incremental_refetch_at", datetime.now(timezone.utc).isoformat())
        set_meta(conn, "last_incremental_refetch_added", str(result.get("added", 0)))


def record_publication(conn, draft_id: int, platform: str, ok: bool, external_id: str | None, error: str | None) -> None:
    conn.execute(
        "INSERT INTO publication(draft_id, platform, status, external_id, error) VALUES(?,?,?,?,?)",
        (draft_id, platform, "ok" if ok else "error", external_id, error),
    )
    conn.commit()


def create_planning_session(
    conn,
    planning_date: str,
    target_date: str,
    publish_at: list[str],
) -> tuple[object, bool]:
    """Create one durable evening session and its ordered publication slots."""
    if not publish_at:
        raise ValueError("planning session requires at least one publication slot")
    _begin_write(conn)
    try:
        existing = conn.execute(
            "SELECT * FROM planning_session WHERE planning_date=?",
            (planning_date,),
        ).fetchone()
        created = existing is None
        if created:
            cur = conn.execute(
                "INSERT INTO planning_session(planning_date, target_date, target_count) "
                "VALUES(?,?,?) RETURNING id",
                (planning_date, target_date, len(publish_at)),
            )
            session_id = cur.fetchone()["id"]
            for position, planned_at in enumerate(publish_at, start=1):
                conn.execute(
                    "INSERT INTO planning_slot(session_id, position, publish_at) VALUES(?,?,?)",
                    (session_id, position, planned_at),
                )
        else:
            session_id = existing["id"]
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return get_planning_session(conn, session_id), created


def get_planning_session(conn, session_id: int):
    return conn.execute(
        "SELECT * FROM planning_session WHERE id=?",
        (session_id,),
    ).fetchone()


def planning_session_for_date(conn, planning_date: str):
    return conn.execute(
        "SELECT * FROM planning_session WHERE planning_date=?",
        (planning_date,),
    ).fetchone()


def next_planning_slot(conn, session_id: int):
    return conn.execute(
        "SELECT ps.*, s.planning_date, s.target_date, s.target_count, "
        "s.status session_status FROM planning_slot ps "
        "JOIN planning_session s ON s.id=ps.session_id "
        "WHERE ps.session_id=? AND ps.status IN ('selecting','reviewing') "
        "ORDER BY ps.position LIMIT 1",
        (session_id,),
    ).fetchone()


def planning_slot_for_post(conn, post_id: int):
    return conn.execute(
        "SELECT ps.*, s.planning_date, s.target_date, s.target_count, "
        "s.status session_status FROM planning_slot ps "
        "JOIN planning_session s ON s.id=ps.session_id WHERE ps.post_id=?",
        (post_id,),
    ).fetchone()


def planning_slot_for_draft(conn, draft_id: int):
    return conn.execute(
        "SELECT ps.*, s.planning_date, s.target_date, s.target_count, "
        "s.status session_status FROM planning_slot ps "
        "JOIN planning_session s ON s.id=ps.session_id WHERE ps.draft_id=?",
        (draft_id,),
    ).fetchone()


def reserve_planning_attempt(conn, slot_id: int):
    cur = conn.execute(
        "UPDATE planning_slot SET attempt_count=attempt_count+1, updated_at=datetime('now') "
        "WHERE id=? AND status='selecting' AND post_id IS NULL",
        (slot_id,),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    return conn.execute("SELECT * FROM planning_slot WHERE id=?", (slot_id,)).fetchone()


def assign_planning_post(conn, slot_id: int, post_id: int) -> bool:
    cur = conn.execute(
        "UPDATE planning_slot SET post_id=?, status='reviewing', updated_at=datetime('now') "
        "WHERE id=? AND status='selecting' AND post_id IS NULL",
        (post_id, slot_id),
    )
    conn.commit()
    return cur.rowcount == 1


def clear_planning_post(conn, slot_id: int, post_id: int) -> bool:
    cur = conn.execute(
        "UPDATE planning_slot SET post_id=NULL, draft_id=NULL, status='selecting', "
        "updated_at=datetime('now') WHERE id=? AND post_id=? AND status='reviewing'",
        (slot_id, post_id),
    )
    conn.commit()
    return cur.rowcount == 1


def attach_planning_draft(conn, post_id: int, draft_id: int) -> bool:
    cur = conn.execute(
        "UPDATE planning_slot SET draft_id=?, updated_at=datetime('now') "
        "WHERE post_id=? AND status='reviewing' AND (draft_id IS NULL OR draft_id=?)",
        (draft_id, post_id, draft_id),
    )
    conn.commit()
    return cur.rowcount == 1


def mark_planning_draft_ready(conn, draft_id: int):
    """Atomically save one reviewed draft and advance the session counter."""
    _begin_write(conn)
    try:
        slot = planning_slot_for_draft(conn, draft_id)
        if slot is None or slot["session_status"] != "active" or slot["status"] != "reviewing":
            conn.rollback()
            return None
        draft = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        if draft is None or draft["status"] != "awaiting_review":
            conn.rollback()
            return None
        conn.execute("UPDATE draft SET status='approved' WHERE id=?", (draft_id,))
        conn.execute("UPDATE post SET status='scheduled' WHERE id=?", (draft["post_id"],))
        conn.execute(
            "UPDATE planning_slot SET status='ready', updated_at=datetime('now') WHERE id=?",
            (slot["id"],),
        )
        ready = conn.execute(
            "SELECT COUNT(*) n FROM planning_slot WHERE session_id=? AND status='ready'",
            (slot["session_id"],),
        ).fetchone()["n"]
        if ready == slot["target_count"]:
            conn.execute(
                "UPDATE planning_session SET status='scheduled', completed_at=datetime('now') "
                "WHERE id=? AND status='active'",
                (slot["session_id"],),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "session_id": slot["session_id"],
        "position": slot["position"],
        "ready": ready,
        "target": slot["target_count"],
        "target_date": slot["target_date"],
    }


def discard_planning_draft(conn, draft_id: int):
    """Discard a generated draft and reopen the same planning slot for candidates."""
    _begin_write(conn)
    try:
        slot = planning_slot_for_draft(conn, draft_id)
        if slot is None or slot["session_status"] != "active" or slot["status"] != "reviewing":
            conn.rollback()
            return None
        draft = conn.execute("SELECT * FROM draft WHERE id=?", (draft_id,)).fetchone()
        if draft is None or draft["status"] not in ("awaiting_review", "approved"):
            conn.rollback()
            return None
        conn.execute("UPDATE draft SET status='skipped' WHERE id=?", (draft_id,))
        conn.execute("UPDATE post SET status='skipped' WHERE id=?", (draft["post_id"],))
        conn.execute(
            "UPDATE planning_slot SET post_id=NULL, draft_id=NULL, status='selecting', "
            "updated_at=datetime('now') WHERE id=?",
            (slot["id"],),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "session_id": slot["session_id"],
        "position": slot["position"],
        "target": slot["target_count"],
    }


def close_stale_planning_sessions(conn, current_planning_date: str) -> int:
    """Close unfinished older sessions while preserving already-ready slots."""
    _begin_write(conn)
    try:
        sessions = conn.execute(
            "SELECT id FROM planning_session WHERE status='active' AND planning_date<?",
            (current_planning_date,),
        ).fetchall()
        for session in sessions:
            pending = conn.execute(
                "SELECT post_id, draft_id FROM planning_slot "
                "WHERE session_id=? AND status IN ('selecting','reviewing')",
                (session["id"],),
            ).fetchall()
            for row in pending:
                if row["draft_id"] is not None:
                    conn.execute(
                        "UPDATE draft SET status='skipped' WHERE id=? "
                        "AND status IN ('awaiting_review','delivery_failed')",
                        (row["draft_id"],),
                    )
                if row["post_id"] is not None:
                    conn.execute(
                        "UPDATE post SET status='skipped' WHERE id=? "
                        "AND status IN ('offered','generating','drafted','awaiting_manual')",
                        (row["post_id"],),
                    )
            conn.execute(
                "UPDATE planning_slot SET status='cancelled', updated_at=datetime('now') "
                "WHERE session_id=? AND status IN ('selecting','reviewing')",
                (session["id"],),
            )
            conn.execute(
                "UPDATE planning_session SET status='partial', completed_at=datetime('now') WHERE id=?",
                (session["id"],),
            )
        conn.commit()
        return len(sessions)
    except Exception:
        conn.rollback()
        raise


def cancel_planning_session(conn, session_id: int) -> dict[str, int] | None:
    """Stop the remaining evening flow; already-ready posts stay scheduled."""
    _begin_write(conn)
    try:
        session = get_planning_session(conn, session_id)
        if session is None or session["status"] != "active":
            conn.rollback()
            return None
        pending = conn.execute(
            "SELECT post_id, draft_id FROM planning_slot "
            "WHERE session_id=? AND status IN ('selecting','reviewing')",
            (session_id,),
        ).fetchall()
        for row in pending:
            if row["draft_id"] is not None:
                conn.execute(
                    "UPDATE draft SET status='skipped' WHERE id=? "
                    "AND status IN ('awaiting_review','delivery_failed')",
                    (row["draft_id"],),
                )
            if row["post_id"] is not None:
                conn.execute(
                    "UPDATE post SET status='skipped' WHERE id=? "
                    "AND status IN ('offered','generating','drafted','awaiting_manual')",
                    (row["post_id"],),
                )
        conn.execute(
            "UPDATE planning_slot SET status='cancelled', updated_at=datetime('now') "
            "WHERE session_id=? AND status IN ('selecting','reviewing')",
            (session_id,),
        )
        ready = conn.execute(
            "SELECT COUNT(*) n FROM planning_slot WHERE session_id=? AND status='ready'",
            (session_id,),
        ).fetchone()["n"]
        conn.execute(
            "UPDATE planning_session SET status=?, completed_at=datetime('now') WHERE id=?",
            ("partial" if ready else "cancelled", session_id),
        )
        conn.commit()
        return {"ready": ready, "target": session["target_count"]}
    except Exception:
        conn.rollback()
        raise


def claim_due_planning_slots(conn, now_iso: str) -> list[object]:
    """Lease every ready slot due by now; each draft remains idempotent per platform."""
    _begin_write(conn)
    try:
        rows = conn.execute(
            "SELECT ps.*, s.target_date FROM planning_slot ps "
            "JOIN planning_session s ON s.id=ps.session_id "
            "WHERE ps.status='ready' AND ps.publish_at<=? ORDER BY ps.publish_at, ps.position",
            (now_iso,),
        ).fetchall()
        claimed = []
        for row in rows:
            cur = conn.execute(
                "UPDATE planning_slot SET status='publishing', publish_claimed_at=?, "
                "updated_at=datetime('now') "
                "WHERE id=? AND status='ready'",
                (now_iso, row["id"]),
            )
            if cur.rowcount == 1:
                claimed.append(row)
        conn.commit()
        return claimed
    except Exception:
        conn.rollback()
        raise


def finish_planning_publication(conn, slot_id: int, draft_status: str, error: str | None = None) -> str:
    if draft_status == "published":
        slot_status = "published"
    elif draft_status == "approved":
        slot_status = "ready"
    elif draft_status == "publish_unknown":
        slot_status = "publish_unknown"
    else:
        slot_status = "failed"
    conn.execute(
        "UPDATE planning_slot SET status=?, last_error=?, publish_claimed_at=NULL, "
        "updated_at=datetime('now') WHERE id=?",
        (slot_status, error, slot_id),
    )
    row = conn.execute("SELECT session_id FROM planning_slot WHERE id=?", (slot_id,)).fetchone()
    if row is not None:
        remaining = conn.execute(
            "SELECT COUNT(*) n FROM planning_slot WHERE session_id=? "
            "AND status NOT IN ('published','cancelled')",
            (row["session_id"],),
        ).fetchone()["n"]
        if remaining == 0:
            conn.execute(
                "UPDATE planning_session SET status='published' WHERE id=?",
                (row["session_id"],),
            )
    conn.commit()
    return slot_status


def recover_planning_publications(conn, cutoff_iso: str | None = None) -> dict[str, int]:
    """Reconcile planning leases after a function/process interruption."""
    result = {"ready": 0, "published": 0, "unknown": 0, "failed": 0}
    where = "WHERE ps.status='publishing'"
    params: tuple[object, ...] = ()
    if cutoff_iso is not None:
        where += " AND ps.publish_claimed_at<=?"
        params = (cutoff_iso,)
    rows = conn.execute(
        "SELECT ps.id, d.status draft_status FROM planning_slot ps "
        "LEFT JOIN draft d ON d.id=ps.draft_id " + where,
        params,
    ).fetchall()
    for row in rows:
        status = row["draft_status"] or "missing"
        if status == "publishing":
            conn.execute(
                "UPDATE draft SET status='publish_unknown' WHERE id=("
                "SELECT draft_id FROM planning_slot WHERE id=?)",
                (row["id"],),
            )
            status = "publish_unknown"
        slot_status = finish_planning_publication(
            conn,
            row["id"],
            status,
            "recovered after interrupted scheduled publication",
        )
        key = {
            "ready": "ready",
            "published": "published",
            "publish_unknown": "unknown",
        }.get(slot_status, "failed")
        result[key] += 1
    return result


def cleanup(conn, keep_days: int = 120) -> int:
    """Refuse legacy physical cleanup.

    Telegram callback payloads and replacement-slot idempotency use persistent
    numeric post/draft IDs. Deleting terminal rows lets SQLite reuse those IDs,
    so an old inline button could target a different future record. Retention
    must compact content while preserving tombstones; that policy is not yet
    implemented.
    """
    del conn, keep_days
    raise RuntimeError(
        "Физическое удаление отключено: post/draft/publication/delivery "
        "хранятся как постоянные tombstone-записи"
    )


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
    for row in conn.execute("SELECT status, COUNT(*) n FROM planning_slot GROUP BY status"):
        out[f"planning:{row['status']}"] = row["n"]
    return out
