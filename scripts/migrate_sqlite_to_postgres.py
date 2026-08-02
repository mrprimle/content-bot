"""Copy the durable local queue/state into a freshly provisioned PostgreSQL DB."""

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import psycopg
from psycopg import sql

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from repost.db import POSTGRES_SCHEMA

TABLES = (
    "source",
    "post",
    "draft",
    "publication",
    "delivery_batch",
    "delivery_item",
    "app_meta",
)
SEQUENCED_TABLES = TABLES[:-1]


def sqlite_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]


def postgres_columns(conn, table: str) -> set[str]:
    rows = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=%s",
        (table,),
    ).fetchall()
    return {row[0] for row in rows}


def initialize_schema(conn) -> None:
    for statement in POSTGRES_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.commit()


def migrate(sqlite_path: Path, database_url: str) -> dict[str, int]:
    source = sqlite3.connect(sqlite_path)
    source.row_factory = sqlite3.Row
    target = psycopg.connect(database_url)
    initialize_schema(target)
    counts: dict[str, int] = {}
    try:
        for table in TABLES:
            source_cols = sqlite_columns(source, table)
            target_cols = postgres_columns(target, table)
            columns = [column for column in source_cols if column in target_cols]
            rows = source.execute(
                f"SELECT {', '.join(columns)} FROM {table} ORDER BY rowid"
            ).fetchall()
            if not rows:
                counts[table] = 0
                continue
            identifiers = sql.SQL(", ").join(map(sql.Identifier, columns))
            placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in columns)
            key = "key" if table == "app_meta" else "id"
            updates = [column for column in columns if column != key]
            update_sql = sql.SQL(", ").join(
                sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(column), sql.Identifier(column))
                for column in updates
            )
            statement = sql.SQL(
                "INSERT INTO {table} ({columns}) VALUES ({values}) "
                "ON CONFLICT ({key}) DO UPDATE SET {updates}"
            ).format(
                table=sql.Identifier(table),
                columns=identifiers,
                values=placeholders,
                key=sql.Identifier(key),
                updates=update_sql,
            )
            with target.cursor() as cursor:
                cursor.executemany(statement, [tuple(row[column] for column in columns) for row in rows])
            target.commit()
            counts[table] = len(rows)

        for table in SEQUENCED_TABLES:
            target.execute(
                sql.SQL(
                    "SELECT setval(pg_get_serial_sequence({literal}, 'id'), "
                    "COALESCE((SELECT MAX(id) FROM {table}), 1), "
                    "EXISTS(SELECT 1 FROM {table}))"
                ).format(
                    literal=sql.Literal(table),
                    table=sql.Identifier(table),
                )
            )
        target.commit()

        for table, expected in counts.items():
            actual = target.execute(
                sql.SQL("SELECT COUNT(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()[0]
            if actual != expected:
                raise RuntimeError(
                    f"Migration verification failed for {table}: expected {expected}, got {actual}"
                )
        return counts
    finally:
        source.close()
        target.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="repost.db", type=Path)
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL"))
    args = parser.parse_args()
    if not args.sqlite.is_file():
        raise SystemExit(f"SQLite database not found: {args.sqlite}")
    if not args.database_url:
        raise SystemExit("Pass --database-url or set DATABASE_URL")
    counts = migrate(args.sqlite.resolve(), args.database_url)
    print("Migration verified: " + ", ".join(f"{table}={count}" for table, count in counts.items()))


if __name__ == "__main__":
    main()
