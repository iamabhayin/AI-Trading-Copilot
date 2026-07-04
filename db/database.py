"""Thin sqlite3 helper — no ORM.

Provides schema initialization plus small connection/query helpers used
by every skill that touches the `suggestions` / `positions` /
`conversations` / `alerts_sent` tables (see schema.sql).
"""

import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import Settings

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(db_path: str | None = None) -> None:
    """Create the database file and apply schema.sql (idempotent)."""
    settings = Settings.load()
    path = db_path or settings.sqlite_db_path
    with get_connection(path) as conn:
        conn.executescript(SCHEMA_PATH.read_text())
        conn.commit()


@contextmanager
def get_connection(db_path: str | None = None):
    """Context manager yielding a sqlite3 connection with row access by column name."""
    settings = Settings.load()
    path = db_path or settings.sqlite_db_path
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")  # per-connection setting — schema.sql's PRAGMA only covers init_db()'s own connection
    try:
        yield conn
    finally:
        conn.close()


def fetch_all(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(query, params).fetchall()


def fetch_one(query: str, params: tuple = ()) -> sqlite3.Row | None:
    with get_connection() as conn:
        return conn.execute(query, params).fetchone()


def execute(query: str, params: tuple = ()) -> int:
    """Run an INSERT/UPDATE/DELETE, commit, and return the last row id."""
    with get_connection() as conn:
        cursor = conn.execute(query, params)
        conn.commit()
        return cursor.lastrowid


if __name__ == "__main__":
    init_db()
    print("Database initialized.")
