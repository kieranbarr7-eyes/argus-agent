"""
db.py — SQLite database for watches, price history, and push subscriptions.

Tables
------
watches           — one row per active or completed watch request
price_history     — every price observation, timestamped
push_subscriptions — browser push subscription objects linked to watches
"""

import json
import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection helper
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


# ---------------------------------------------------------------------------
# Schema bootstrap
# ---------------------------------------------------------------------------

def init_db() -> None:
    """Create tables if they don't already exist."""
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
    log.info("Database initialized at %s", config.DB_PATH)


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
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO watches
           (origin, destination, date, train_numbers, time_start, time_end,
            price_min, price_max, fare_class, active, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (
            origin, destination, date, trains_json,
            time_start, time_end, price_min, price_max, fare_class,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    watch_id = cur.lastrowid
    conn.close()
    log.info("Created watch #%d: %s→%s on %s", watch_id, origin, destination, date)
    return watch_id


def find_active_watch(origin: str, destination: str, date: str) -> dict | None:
    """Find an existing active watch for the same route+date."""
    conn = _connect()
    row = conn.execute(
        "SELECT * FROM watches WHERE active = 1 AND origin = ? AND destination = ? AND date = ?",
        (origin, destination, date),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def update_watch_trains(watch_id: int, train_numbers: list[str]) -> None:
    """Update the watched train numbers for an existing watch."""
    trains_json = json.dumps(train_numbers) if train_numbers else None
    conn = _connect()
    conn.execute(
        "UPDATE watches SET train_numbers = ? WHERE id = ?",
        (trains_json, watch_id),
    )
    conn.commit()
    conn.close()
    log.info("Updated trains for watch #%d: %s", watch_id, train_numbers)


def get_active_watches() -> list[dict]:
    """Return all active watches as a list of dicts."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM watches WHERE active = 1"
    ).fetchall()
    conn.close()
    result = []
    for row in rows:
        d = dict(row)
        # Parse train_numbers JSON back to list
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
    conn.execute("UPDATE watches SET active = 0 WHERE id = ?", (watch_id,))
    conn.commit()
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
    conn = _connect()
    cur = conn.execute(
        """INSERT INTO push_subscriptions (endpoint, subscription_json, watch_id, created_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(endpoint) DO UPDATE SET
               subscription_json = excluded.subscription_json,
               watch_id = COALESCE(excluded.watch_id, push_subscriptions.watch_id)""",
        (
            endpoint, subscription_json, watch_id,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    sub_id = cur.lastrowid
    conn.close()
    return sub_id


def get_subscriptions_for_watch(watch_id: int) -> list[dict]:
    """Return all push subscriptions linked to a watch."""
    conn = _connect()
    rows = conn.execute(
        "SELECT * FROM push_subscriptions WHERE watch_id = ?",
        (watch_id,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_all_subscriptions() -> list[dict]:
    """Return all push subscriptions."""
    conn = _connect()
    rows = conn.execute("SELECT * FROM push_subscriptions").fetchall()
    conn.close()
    return [dict(row) for row in rows]


def remove_subscription_by_endpoint(endpoint: str) -> None:
    """Remove a subscription by its endpoint URL (e.g. when expired)."""
    conn = _connect()
    conn.execute("DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,))
    conn.commit()
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
    conn = _connect()
    conn.execute(
        """INSERT INTO price_history
           (watch_id, route, train_number, departure_time, price, fare_class, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            watch_id, route, train_number, departure_time,
            price, fare_class,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()
    conn.close()


def get_price_history(watch_id: int, limit: int = 50) -> list[dict]:
    """Return recent price observations for a watch."""
    conn = _connect()
    rows = conn.execute(
        """SELECT * FROM price_history
           WHERE watch_id = ?
           ORDER BY timestamp DESC
           LIMIT ?""",
        (watch_id, limit),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Waitlist
# ---------------------------------------------------------------------------

def add_to_waitlist(email: str) -> None:
    """Add an email to the waitlist (idempotent — ignores duplicates)."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO waitlist (email, created_at) VALUES (?, datetime('now'))",
            (email,),
        )
        conn.commit()
    finally:
        conn.close()


def get_latest_prices_for_watch(watch_id: int) -> list[dict]:
    """Return the most recent price for each train in a watch."""
    conn = _connect()
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
    conn.close()
    return [dict(row) for row in rows]
