"""
db.py — Database layer for watches, price history, and push subscriptions.

Backend selection
-----------------
If DATABASE_URL is set in the environment (PostgreSQL in production), all
queries run against PostgreSQL via psycopg2.  Otherwise the module falls back
to SQLite for local development.

Tables
------
watches            — one row per active or completed watch request
price_history      — every price observation, timestamped
push_subscriptions — browser push subscription objects linked to watches
waitlist           — early-access email signups
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

_USE_POSTGRES = bool(config.DATABASE_URL)

if _USE_POSTGRES:
    import psycopg2
    import psycopg2.extras  # RealDictCursor
    log.info("db: using PostgreSQL backend (%s)", config.DATABASE_URL[:30] + "…")
else:
    import sqlite3
    log.info("db: using SQLite backend (%s)", config.DB_PATH)


# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect():
    """Return an open database connection for the configured backend."""
    if _USE_POSTGRES:
        conn = psycopg2.connect(config.DATABASE_URL)
        return conn
    else:
        conn = sqlite3.connect(config.DB_PATH)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


def _row_to_dict(row) -> dict:
    """Normalise a database row to a plain dict regardless of backend."""
    if row is None:
        return None
    if isinstance(row, dict):
        return row
    # sqlite3.Row
    return dict(row)


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't already exist."""
    if _USE_POSTGRES:
        _init_postgres()
    else:
        _init_sqlite()


def _init_postgres() -> None:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS watches (
                    id              SERIAL PRIMARY KEY,
                    origin          TEXT    NOT NULL,
                    destination     TEXT    NOT NULL,
                    date            TEXT    NOT NULL,
                    train_numbers   TEXT,
                    time_start      TEXT,
                    time_end        TEXT,
                    price_min       REAL,
                    price_max       REAL,
                    fare_class      TEXT    DEFAULT 'coach',
                    active          INTEGER DEFAULT 1,
                    created_at      TEXT    NOT NULL
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS price_history (
                    id             SERIAL PRIMARY KEY,
                    watch_id       INTEGER,
                    route          TEXT    NOT NULL,
                    train_number   TEXT    NOT NULL,
                    departure_time TEXT,
                    price          REAL    NOT NULL,
                    fare_class     TEXT,
                    timestamp      TEXT    NOT NULL,
                    FOREIGN KEY (watch_id) REFERENCES watches(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS push_subscriptions (
                    id                SERIAL PRIMARY KEY,
                    endpoint          TEXT    NOT NULL UNIQUE,
                    subscription_json TEXT    NOT NULL,
                    watch_id          INTEGER,
                    created_at        TEXT    NOT NULL,
                    FOREIGN KEY (watch_id) REFERENCES watches(id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS waitlist (
                    id         SERIAL PRIMARY KEY,
                    email      TEXT UNIQUE NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
        conn.commit()
        log.info("PostgreSQL database initialized")
    finally:
        conn.close()


def _init_sqlite() -> None:
    conn = _connect()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS watches (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            origin          TEXT    NOT NULL,
            destination     TEXT    NOT NULL,
            date            TEXT    NOT NULL,
            train_numbers   TEXT,
            time_start      TEXT,
            time_end        TEXT,
            price_min       REAL,
            price_max       REAL,
            fare_class      TEXT    DEFAULT 'coach',
            active          INTEGER DEFAULT 1,
            created_at      TEXT    NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            watch_id       INTEGER,
            route          TEXT    NOT NULL,
            train_number   TEXT    NOT NULL,
            departure_time TEXT,
            price          REAL    NOT NULL,
            fare_class     TEXT,
            timestamp      TEXT    NOT NULL,
            FOREIGN KEY (watch_id) REFERENCES watches(id)
        );

        CREATE TABLE IF NOT EXISTS push_subscriptions (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint          TEXT    NOT NULL UNIQUE,
            subscription_json TEXT    NOT NULL,
            watch_id          INTEGER,
            created_at        TEXT    NOT NULL,
            FOREIGN KEY (watch_id) REFERENCES watches(id)
        );

        CREATE TABLE IF NOT EXISTS waitlist (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT    UNIQUE NOT NULL,
            created_at TEXT    NOT NULL
        );
    """)
    conn.commit()
    conn.close()
    log.info("SQLite database initialized at %s", config.DB_PATH)


# ---------------------------------------------------------------------------
# Watch CRUD
# ---------------------------------------------------------------------------

def create_watch(
    origin: str,
    destination: str,
    date: str,
    train_numbers: list[str] | None = None,
    time_start: str | None = None,
    time_end: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    fare_class: str = "coach",
) -> int:
    """Insert a new watch and return its row id."""
    trains_json = json.dumps(train_numbers) if train_numbers else None
    now = datetime.now(timezone.utc).isoformat()

    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO watches
                       (origin, destination, date, train_numbers, time_start, time_end,
                        price_min, price_max, fare_class, active, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 1, %s)
                       RETURNING id""",
                    (
                        origin, destination, date, trains_json,
                        time_start, time_end, price_min, price_max, fare_class,
                        now,
                    ),
                )
                watch_id = cur.fetchone()[0]
            conn.commit()
        else:
            cur = conn.execute(
                """INSERT INTO watches
                   (origin, destination, date, train_numbers, time_start, time_end,
                    price_min, price_max, fare_class, active, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    origin, destination, date, trains_json,
                    time_start, time_end, price_min, price_max, fare_class,
                    now,
                ),
            )
            conn.commit()
            watch_id = cur.lastrowid
    finally:
        conn.close()

    log.info("Created watch #%d: %s→%s on %s", watch_id, origin, destination, date)
    return watch_id


def find_active_watch(origin: str, destination: str, date: str) -> dict | None:
    """Find an existing active watch for the same route+date."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM watches WHERE active = 1 AND origin = %s AND destination = %s AND date = %s",
                    (origin, destination, date),
                )
                row = cur.fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM watches WHERE active = 1 AND origin = ? AND destination = ? AND date = ?",
                (origin, destination, date),
            ).fetchone()
    finally:
        conn.close()
    return _row_to_dict(row) if row else None


def update_watch_trains(watch_id: int, train_numbers: list[str]) -> None:
    """Update the watched train numbers for an existing watch."""
    trains_json = json.dumps(train_numbers) if train_numbers else None
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE watches SET train_numbers = %s WHERE id = %s",
                    (trains_json, watch_id),
                )
        else:
            conn.execute(
                "UPDATE watches SET train_numbers = ? WHERE id = ?",
                (trains_json, watch_id),
            )
        conn.commit()
    finally:
        conn.close()
    log.info("Updated trains for watch #%d: %s", watch_id, train_numbers)


def get_active_watches() -> list[dict]:
    """Return all active watches as a list of dicts."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM watches WHERE active = 1")
                rows = cur.fetchall()
        else:
            rows = conn.execute("SELECT * FROM watches WHERE active = 1").fetchall()
    finally:
        conn.close()

    result = []
    for row in rows:
        d = _row_to_dict(row)
        if d.get("train_numbers"):
            try:
                d["train_numbers"] = json.loads(d["train_numbers"])
            except json.JSONDecodeError:
                d["train_numbers"] = []
        else:
            d["train_numbers"] = []
        result.append(d)
    return result


def deactivate_watch(watch_id: int) -> None:
    """Deactivate a single watch by id."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute("UPDATE watches SET active = 0 WHERE id = %s", (watch_id,))
        else:
            conn.execute("UPDATE watches SET active = 0 WHERE id = ?", (watch_id,))
        conn.commit()
    finally:
        conn.close()
    log.info("Deactivated watch #%d", watch_id)


# ---------------------------------------------------------------------------
# Push subscriptions
# ---------------------------------------------------------------------------

def store_subscription(
    endpoint: str,
    subscription_json: str,
    watch_id: int | None = None,
) -> int:
    """
    Store or update a push subscription.

    Uses UPSERT on endpoint (unique) so re-subscribing updates the existing row.
    """
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO push_subscriptions
                           (endpoint, subscription_json, watch_id, created_at)
                       VALUES (%s, %s, %s, %s)
                       ON CONFLICT (endpoint) DO UPDATE SET
                           subscription_json = EXCLUDED.subscription_json,
                           watch_id = COALESCE(EXCLUDED.watch_id, push_subscriptions.watch_id)
                       RETURNING id""",
                    (endpoint, subscription_json, watch_id, now),
                )
                sub_id = cur.fetchone()[0]
            conn.commit()
        else:
            cur = conn.execute(
                """INSERT INTO push_subscriptions
                       (endpoint, subscription_json, watch_id, created_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(endpoint) DO UPDATE SET
                       subscription_json = excluded.subscription_json,
                       watch_id = COALESCE(excluded.watch_id, push_subscriptions.watch_id)""",
                (endpoint, subscription_json, watch_id, now),
            )
            conn.commit()
            sub_id = cur.lastrowid
    finally:
        conn.close()
    return sub_id


def get_subscriptions_for_watch(watch_id: int) -> list[dict]:
    """Return all push subscriptions linked to a watch."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    "SELECT * FROM push_subscriptions WHERE watch_id = %s",
                    (watch_id,),
                )
                rows = cur.fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM push_subscriptions WHERE watch_id = ?",
                (watch_id,),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def get_all_subscriptions() -> list[dict]:
    """Return all push subscriptions."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT * FROM push_subscriptions")
                rows = cur.fetchall()
        else:
            rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


def remove_subscription_by_endpoint(endpoint: str) -> None:
    """Remove a subscription by its endpoint URL (e.g. when expired)."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint = %s",
                    (endpoint,),
                )
        else:
            conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint = ?",
                (endpoint,),
            )
        conn.commit()
    finally:
        conn.close()
    log.info("Removed subscription for endpoint: %s", endpoint[:60])


# ---------------------------------------------------------------------------
# Price history
# ---------------------------------------------------------------------------

def record_price(
    watch_id: int,
    route: str,
    train_number: str,
    departure_time: str | None,
    price: float,
    fare_class: str | None = None,
) -> None:
    """Record a single price observation."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO price_history
                       (watch_id, route, train_number, departure_time, price, fare_class, timestamp)
                       VALUES (%s, %s, %s, %s, %s, %s, %s)""",
                    (watch_id, route, train_number, departure_time, price, fare_class, now),
                )
        else:
            conn.execute(
                """INSERT INTO price_history
                   (watch_id, route, train_number, departure_time, price, fare_class, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (watch_id, route, train_number, departure_time, price, fare_class, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_price_history(watch_id: int, limit: int = 50) -> list[dict]:
    """Return recent price observations for a watch."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT * FROM price_history
                       WHERE watch_id = %s
                       ORDER BY timestamp DESC
                       LIMIT %s""",
                    (watch_id, limit),
                )
                rows = cur.fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM price_history
                   WHERE watch_id = ?
                   ORDER BY timestamp DESC
                   LIMIT ?""",
                (watch_id, limit),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------

def add_to_waitlist(email: str) -> None:
    """Add an email to the waitlist (idempotent — ignores duplicates)."""
    now = datetime.now(timezone.utc).isoformat()
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO waitlist (email, created_at)
                       VALUES (%s, %s)
                       ON CONFLICT (email) DO NOTHING""",
                    (email, now),
                )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO waitlist (email, created_at) VALUES (?, ?)",
                (email, now),
            )
        conn.commit()
    finally:
        conn.close()


def get_latest_prices_for_watch(watch_id: int) -> list[dict]:
    """Return the most recent price for each train in a watch."""
    conn = _connect()
    try:
        if _USE_POSTGRES:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """SELECT ph.*
                       FROM price_history ph
                       INNER JOIN (
                           SELECT train_number, MAX(timestamp) AS max_ts
                           FROM price_history
                           WHERE watch_id = %s
                           GROUP BY train_number
                       ) latest ON ph.train_number = latest.train_number
                                AND ph.timestamp = latest.max_ts
                       WHERE ph.watch_id = %s
                       ORDER BY ph.price ASC""",
                    (watch_id, watch_id),
                )
                rows = cur.fetchall()
        else:
            rows = conn.execute(
                """SELECT ph.*
                   FROM price_history ph
                   INNER JOIN (
                       SELECT train_number, MAX(timestamp) AS max_ts
                       FROM price_history
                       WHERE watch_id = ?
                       GROUP BY train_number
                   ) latest ON ph.train_number = latest.train_number
                            AND ph.timestamp = latest.max_ts
                   WHERE ph.watch_id = ?
                   ORDER BY ph.price ASC""",
                (watch_id, watch_id),
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_dict(r) for r in rows]
