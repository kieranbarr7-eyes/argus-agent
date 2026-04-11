#!/usr/bin/env python3
"""
migrate_sqlite_to_postgres.py — One-time migration from SQLite to PostgreSQL.

Reads all rows from the local SQLite database (argus_agent.db by default) and
inserts them into the PostgreSQL database pointed to by DATABASE_URL.

Usage
-----
    DATABASE_URL=postgresql://... python migrate_sqlite_to_postgres.py

    # Override the SQLite path:
    DB_PATH=/path/to/argus_agent.db DATABASE_URL=postgresql://... python migrate_sqlite_to_postgres.py

Safety
------
- Idempotent: rows that already exist in PostgreSQL (by primary key) are
  skipped, so the script is safe to run multiple times.
- The SQLite database is never modified.
- Each table is migrated in a separate transaction; a failure in one table
  does not roll back the others.
- After migration the PostgreSQL sequences are updated so that new rows
  receive IDs that don't collide with the migrated ones.
"""

import logging
import os
import sqlite3
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-5s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("migrate")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("DB_PATH", "argus_agent.db")

if not DATABASE_URL:
    log.error("DATABASE_URL environment variable is not set. Aborting.")
    sys.exit(1)

if not os.path.exists(DB_PATH):
    log.error("SQLite database not found at '%s'. Nothing to migrate.", DB_PATH)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sqlite_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def pg_connect():
    import psycopg2
    return psycopg2.connect(DATABASE_URL)


def migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table: str,
    columns: list[str],
    conflict_column: str = "id",
) -> tuple[int, int]:
    """
    Copy all rows from *table* in SQLite into PostgreSQL.

    Returns (inserted, skipped) counts.
    """
    import psycopg2.extras

    rows = sqlite_conn.execute(
        f"SELECT {', '.join(columns)} FROM {table}"
    ).fetchall()

    if not rows:
        log.info("  [%s] No rows in SQLite — nothing to migrate.", table)
        return 0, 0

    col_list = ", ".join(columns)
    placeholders = ", ".join(["%s"] * len(columns))
    sql = (
        f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
        f"ON CONFLICT ({conflict_column}) DO NOTHING"
    )

    inserted = 0
    skipped = 0

    with pg_conn.cursor() as cur:
        for row in rows:
            values = tuple(row[c] for c in columns)
            cur.execute(sql, values)
            if cur.rowcount == 1:
                inserted += 1
            else:
                skipped += 1

    pg_conn.commit()
    return inserted, skipped


def reset_sequence(pg_conn, table: str, id_column: str = "id") -> None:
    """
    Advance the PostgreSQL SERIAL sequence so the next auto-generated ID
    is greater than the current maximum, preventing primary-key collisions
    after the migration.
    """
    with pg_conn.cursor() as cur:
        cur.execute(f"SELECT MAX({id_column}) FROM {table}")
        max_id = cur.fetchone()[0]
        if max_id is not None:
            # setval(seq, value, is_called=true) means next value = max_id + 1
            cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', '{id_column}'), %s, true)",
                (max_id,),
            )
            log.info("  [%s] Sequence reset to %d (next id = %d).", table, max_id, max_id + 1)
    pg_conn.commit()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    log.info("=" * 60)
    log.info("Argus SQLite → PostgreSQL migration")
    log.info("  Source : %s", DB_PATH)
    log.info("  Target : %s…", DATABASE_URL[:40])
    log.info("=" * 60)

    sqlite_conn = sqlite_connect()
    pg_conn = pg_connect()

    try:
        # ------------------------------------------------------------------ #
        # 1. watches
        # ------------------------------------------------------------------ #
        log.info("Migrating: watches")
        ins, skp = migrate_table(
            sqlite_conn,
            pg_conn,
            table="watches",
            columns=[
                "id", "origin", "destination", "date", "train_numbers",
                "time_start", "time_end", "price_min", "price_max",
                "fare_class", "active", "created_at",
            ],
        )
        log.info("  watches: %d inserted, %d skipped (already existed)", ins, skp)
        reset_sequence(pg_conn, "watches")

        # ------------------------------------------------------------------ #
        # 2. price_history
        # ------------------------------------------------------------------ #
        log.info("Migrating: price_history")
        ins, skp = migrate_table(
            sqlite_conn,
            pg_conn,
            table="price_history",
            columns=[
                "id", "watch_id", "route", "train_number",
                "departure_time", "price", "fare_class", "timestamp",
            ],
        )
        log.info("  price_history: %d inserted, %d skipped", ins, skp)
        reset_sequence(pg_conn, "price_history")

        # ------------------------------------------------------------------ #
        # 3. push_subscriptions
        # ------------------------------------------------------------------ #
        log.info("Migrating: push_subscriptions")
        ins, skp = migrate_table(
            sqlite_conn,
            pg_conn,
            table="push_subscriptions",
            columns=[
                "id", "endpoint", "subscription_json", "watch_id", "created_at",
            ],
            conflict_column="endpoint",  # unique on endpoint, not id
        )
        log.info("  push_subscriptions: %d inserted, %d skipped", ins, skp)
        reset_sequence(pg_conn, "push_subscriptions")

        # ------------------------------------------------------------------ #
        # 4. waitlist
        # ------------------------------------------------------------------ #
        log.info("Migrating: waitlist")
        ins, skp = migrate_table(
            sqlite_conn,
            pg_conn,
            table="waitlist",
            columns=["id", "email", "created_at"],
            conflict_column="email",  # unique on email
        )
        log.info("  waitlist: %d inserted, %d skipped", ins, skp)
        reset_sequence(pg_conn, "waitlist")

    except Exception as exc:
        log.error("Migration failed: %s", exc, exc_info=True)
        pg_conn.rollback()
        sys.exit(1)
    finally:
        sqlite_conn.close()
        pg_conn.close()

    log.info("=" * 60)
    log.info("Migration complete.")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
