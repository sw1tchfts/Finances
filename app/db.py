"""SQLite connection, initialization, and small helpers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "finances.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    sql = SCHEMA_PATH.read_text()
    conn.executescript(sql)
    _seed_default_settings(conn)


DEFAULT_SETTINGS = {
    "partnership_start": "2025-06-16",
    "partnership_end": "",
    "partner_user_name": "You",
    "partner_other_name": "Partner",
    "default_split_user_pct": "50",
    "default_split_partner_pct": "50",
}


def _seed_default_settings(conn: sqlite3.Connection) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value, updated_at) VALUES (?, ?, datetime('now')) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = datetime('now')",
        (key, value),
    )
    log(conn, "setting_update", "setting", None, {"key": key, "value": value})


def log(
    conn: sqlite3.Connection,
    event_type: str,
    entity_type: str,
    entity_id: int | None,
    details: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(event_type, entity_type, entity_id, details) VALUES (?, ?, ?, ?)",
        (event_type, entity_type, entity_id, json.dumps(details or {})),
    )


@contextmanager
def transaction(conn: sqlite3.Connection):
    """Wrap a block in BEGIN/COMMIT with automatic rollback on error."""
    conn.execute("BEGIN")
    try:
        yield
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
