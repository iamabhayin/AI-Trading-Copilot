"""Tests for the Phase 15 Option Trading schema additions in db/schema.sql.

Mirrors test_cleanup_skill.py's real-db pattern: init a fresh SQLite file
in tmp_path (via db.database.init_db) and assert against actual sqlite3
behavior, not mocks — these tables' CHECK/UNIQUE constraints are load-
bearing (e.g. options_engine_state's single-row invariant), so they need
to be exercised against a real connection.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def initialized_db(db_path):
    with patch("config.settings.Settings.load") as mock_load:
        mock_load.return_value = MagicMock(sqlite_db_path=db_path)
        from db.database import init_db

        init_db()
    return db_path


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_all_four_tables_created(initialized_db):
    conn = _connect(initialized_db)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert {"option_chain_snapshots", "options_engine_state", "options_advisories", "options_positions"} <= tables


def test_options_engine_state_seeded_single_row_normal_mode(initialized_db):
    conn = _connect(initialized_db)
    rows = conn.execute("SELECT id, mode FROM options_engine_state").fetchall()
    conn.close()
    assert rows == [(1, "NORMAL")]


def test_options_engine_state_rejects_second_row(initialized_db):
    conn = _connect(initialized_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO options_engine_state (id, mode, updated_ts) VALUES (2, 'NORMAL', datetime('now'))")
    conn.close()


def test_options_engine_state_rejects_invalid_mode(initialized_db):
    conn = _connect(initialized_db)
    conn.execute("DELETE FROM options_engine_state")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO options_engine_state (id, mode, updated_ts) VALUES (1, 'BOGUS', datetime('now'))")
    conn.close()


def test_option_chain_snapshots_rejects_invalid_side(initialized_db):
    conn = _connect(initialized_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, spot) "
            "VALUES ('2026-07-20T09:15:00', '2026-07-20', '2026-07-24', 25000, 'XX', 25050)"
        )
    conn.close()


def test_option_chain_snapshots_enforces_unique_constraint(initialized_db):
    conn = _connect(initialized_db)
    conn.execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, spot) "
        "VALUES ('2026-07-20T09:15:00', '2026-07-20', '2026-07-24', 25000, 'CE', 25050)"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, spot) "
            "VALUES ('2026-07-20T09:15:00', '2026-07-20', '2026-07-24', 25000, 'CE', 25060)"
        )
    conn.close()


def test_options_advisories_rejects_invalid_action(initialized_db):
    conn = _connect(initialized_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO options_advisories (created_ts, action, payload_json) "
            "VALUES ('2026-07-20T09:15:00', 'BOGUS', '{}')"
        )
    conn.close()


def test_options_advisories_defaults_notified_to_zero(initialized_db):
    conn = _connect(initialized_db)
    conn.execute(
        "INSERT INTO options_advisories (created_ts, action, payload_json) "
        "VALUES ('2026-07-20T09:15:00', 'WAIT', '{}')"
    )
    conn.commit()
    row = conn.execute("SELECT notified FROM options_advisories").fetchone()
    conn.close()
    assert row[0] == 0


def test_options_positions_rejects_invalid_status(initialized_db):
    conn = _connect(initialized_db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO options_positions "
            "(status, contract, expiry_date, strike, side, qty_lots, lot_size, entry_premium, thesis_json) "
            "VALUES ('bogus', 'NIFTY 25000 CE 2026-07-24', '2026-07-24', 25000, 'CE', 1, 75, 120.5, '{}')"
        )
    conn.close()


def test_options_positions_defaults_status_active(initialized_db):
    conn = _connect(initialized_db)
    conn.execute(
        "INSERT INTO options_positions "
        "(contract, expiry_date, strike, side, qty_lots, lot_size, entry_premium, thesis_json) "
        "VALUES ('NIFTY 25000 CE 2026-07-24', '2026-07-24', 25000, 'CE', 1, 75, 120.5, '{}')"
    )
    conn.commit()
    row = conn.execute("SELECT status FROM options_positions").fetchone()
    conn.close()
    assert row[0] == "active"


def test_schema_init_is_idempotent(db_path):
    with patch("config.settings.Settings.load") as mock_load:
        mock_load.return_value = MagicMock(sqlite_db_path=db_path)
        from db.database import init_db

        init_db()
        init_db()  # must not raise or duplicate the seeded options_engine_state row

    conn = _connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM options_engine_state").fetchone()[0]
    conn.close()
    assert count == 1
