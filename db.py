"""SQLite persistence: offers, API-call budget, alert dedupe, run counter."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

# Overridable via the PRICES_DB env var so a targeted dry run can point the
# whole pipeline (tracker, rank, export, report, alerts) at an isolated file.
DEFAULT_DB_PATH = os.environ.get("PRICES_DB", "prices.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    dest_region TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    return_date TEXT NOT NULL,
    price_usd REAL NOT NULL,
    currency TEXT NOT NULL,
    outbound_json TEXT NOT NULL,
    inbound_json TEXT NOT NULL,
    stops_outbound INTEGER NOT NULL,
    stops_inbound INTEGER NOT NULL,
    connection_airports TEXT NOT NULL,
    total_duration_minutes INTEGER NOT NULL,
    fare_brand_names TEXT NOT NULL,
    offer_hash TEXT NOT NULL,
    UNIQUE (run_timestamp, offer_hash) ON CONFLICT IGNORE
);
CREATE INDEX IF NOT EXISTS idx_offers_route ON offers (origin, dest, run_timestamp);
CREATE INDEX IF NOT EXISTS idx_offers_run ON offers (run_timestamp);

CREATE TABLE IF NOT EXISTS api_calls (
    month TEXT PRIMARY KEY,
    calls INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS alerts (
    offer_hash TEXT NOT NULL,
    alerted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_hash ON alerts (offer_hash, alerted_at);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL
);

-- One row per offer rejected by a filter, so per-filter rejection rates,
-- dead routes, and basic-economy misclassification can be analysed later.
-- filter_reason is the first failing filter (evaluation short-circuits).
CREATE TABLE IF NOT EXISTS rejections (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TEXT NOT NULL,
    origin TEXT NOT NULL,
    dest TEXT NOT NULL,
    dest_region TEXT NOT NULL,
    depart_date TEXT NOT NULL,
    price_usd REAL,
    filter_reason TEXT NOT NULL,
    fare_brand_names TEXT NOT NULL,
    carriers TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rejections_run ON rejections (run_timestamp);
CREATE INDEX IF NOT EXISTS idx_rejections_reason ON rejections (filter_reason);
"""

OFFER_COLUMNS = (
    "run_timestamp", "origin", "dest", "dest_region", "depart_date",
    "return_date", "price_usd", "currency", "outbound_json", "inbound_json",
    "stops_outbound", "stops_inbound", "connection_airports",
    "total_duration_minutes", "fare_brand_names", "offer_hash",
)

REJECTION_COLUMNS = (
    "run_timestamp", "origin", "dest", "dest_region", "depart_date",
    "price_usd", "filter_reason", "fare_brand_names", "carriers",
)


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (second precision)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def month_key(now: datetime | None = None) -> str:
    """Key used to bucket API calls per month, e.g. '2026-07'."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m")


def connect(path: str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open (and initialize) the database."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def get_month_calls(conn: sqlite3.Connection, month: str | None = None) -> int:
    """API calls recorded so far this month."""
    row = conn.execute(
        "SELECT calls FROM api_calls WHERE month = ?", (month or month_key(),)
    ).fetchone()
    return row["calls"] if row else 0


def add_api_calls(conn: sqlite3.Connection, count: int = 1, month: str | None = None) -> None:
    """Increment (and immediately persist) this month's API call counter."""
    conn.execute(
        "INSERT INTO api_calls (month, calls) VALUES (?, ?) "
        "ON CONFLICT (month) DO UPDATE SET calls = calls + excluded.calls",
        (month or month_key(), count),
    )
    conn.commit()


def record_run(conn: sqlite3.Connection) -> int:
    """Register a run and return its zero-based index (drives rotation)."""
    cursor = conn.execute("INSERT INTO runs (started_at) VALUES (?)", (utc_now_iso(),))
    conn.commit()
    return cursor.lastrowid - 1


def insert_offer(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one filtered offer (duplicate hash within the same run is ignored)."""
    conn.execute(
        f"INSERT INTO offers ({', '.join(OFFER_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(OFFER_COLUMNS))})",
        tuple(row[col] for col in OFFER_COLUMNS),
    )


def insert_rejection(conn: sqlite3.Connection, row: dict) -> None:
    """Insert one rejected-offer record (why an offer was filtered out)."""
    conn.execute(
        f"INSERT INTO rejections ({', '.join(REJECTION_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(REJECTION_COLUMNS))})",
        tuple(row[col] for col in REJECTION_COLUMNS),
    )


def latest_run_timestamp(conn: sqlite3.Connection) -> str | None:
    """run_timestamp of the most recent run that stored offers."""
    row = conn.execute("SELECT MAX(run_timestamp) AS ts FROM offers").fetchone()
    return row["ts"]


def offers_for_run(
    conn: sqlite3.Connection, run_timestamp: str, region: str | None = None
) -> list[sqlite3.Row]:
    """All offers stored by one run, optionally limited to a region."""
    if region:
        return conn.execute(
            "SELECT * FROM offers WHERE run_timestamp = ? AND dest_region = ?",
            (run_timestamp, region),
        ).fetchall()
    return conn.execute(
        "SELECT * FROM offers WHERE run_timestamp = ?", (run_timestamp,)
    ).fetchall()


def offers_since(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    """All offers from the trailing `days` days, oldest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return conn.execute(
        "SELECT * FROM offers WHERE run_timestamp >= ? ORDER BY run_timestamp",
        (cutoff,),
    ).fetchall()


def rejections_since(conn: sqlite3.Connection, days: int) -> list[sqlite3.Row]:
    """All rejected-offer records from the trailing `days` days, oldest first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    return conn.execute(
        "SELECT * FROM rejections WHERE run_timestamp >= ? ORDER BY run_timestamp",
        (cutoff,),
    ).fetchall()


def route_history_prices(
    conn: sqlite3.Connection, origin: str, dest: str, before_timestamp: str, days: int
) -> list[float]:
    """Prices seen on a route in the `days` days before `before_timestamp`.

    Excludes the current run so a run never scores against itself.
    """
    before = datetime.strptime(before_timestamp, "%Y-%m-%dT%H:%M:%SZ")
    cutoff = (before - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    rows = conn.execute(
        "SELECT price_usd FROM offers WHERE origin = ? AND dest = ? "
        "AND run_timestamp >= ? AND run_timestamp < ?",
        (origin, dest, cutoff, before_timestamp),
    ).fetchall()
    return [row["price_usd"] for row in rows]


def was_alerted_recently(conn: sqlite3.Connection, offer_hash: str, hours: int) -> bool:
    """True if this offer hash was alerted within the trailing `hours` hours."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    row = conn.execute(
        "SELECT 1 FROM alerts WHERE offer_hash = ? AND alerted_at >= ? LIMIT 1",
        (offer_hash, cutoff),
    ).fetchone()
    return row is not None


def record_alert(conn: sqlite3.Connection, offer_hash: str) -> None:
    """Record that an alert was sent for this offer hash."""
    conn.execute(
        "INSERT INTO alerts (offer_hash, alerted_at) VALUES (?, ?)",
        (offer_hash, utc_now_iso()),
    )
    conn.commit()
