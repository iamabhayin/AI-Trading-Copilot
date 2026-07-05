"""Tests for skills/cleanup_skill.py.

Most tests mock the DB connection to verify call structure/ordering; one
integration test runs against a real temp SQLite file with the actual
schema (including foreign key enforcement) applied, since this skill's
whole job is a destructive multi-table wipe where a wrong statement order
would only be caught by a real FK-constrained database, not a mock.
"""

from unittest.mock import MagicMock, patch

from skills.cleanup_skill import cleanup, format_cleanup_summary


def _mock_conn(active_positions):
    conn = MagicMock()
    conn.execute.return_value.fetchall.return_value = active_positions
    return conn


@patch("skills.cleanup_skill.get_connection")
def test_cleanup_returns_wiped_list_and_kept_positions(mock_get_connection):
    conn = _mock_conn([{"id": 1, "ticker": "RELIANCE"}])
    mock_get_connection.return_value.__enter__.return_value = conn

    result = cleanup()

    assert result["wiped"] == ["suggestions", "conversations", "alerts_sent", "closed_positions"]
    assert result["kept_active_positions"] == [{"id": 1, "ticker": "RELIANCE"}]


@patch("skills.cleanup_skill.get_connection")
def test_cleanup_executes_statements_in_fk_safe_order(mock_get_connection):
    conn = _mock_conn([])
    mock_get_connection.return_value.__enter__.return_value = conn

    cleanup()

    executed = [call.args[0] for call in conn.execute.call_args_list]
    assert "SELECT" in executed[0]
    assert "DELETE FROM alerts_sent" in executed[1]
    assert "DELETE FROM positions WHERE status = 'closed'" in executed[2]
    assert "DELETE FROM conversations" in executed[3]
    assert "UPDATE positions SET suggestion_id = NULL" in executed[4]
    assert "DELETE FROM suggestions" in executed[5]
    conn.commit.assert_called_once()


def test_format_cleanup_summary_lists_kept_positions():
    result = {
        "wiped": ["suggestions", "conversations"],
        "kept_active_positions": [{"id": 1, "ticker": "AAPL"}],
    }

    summary = format_cleanup_summary(result)

    assert "suggestions" in summary
    assert "AAPL" in summary
    assert "Kept 1 active position" in summary


def test_format_cleanup_summary_no_kept_positions():
    result = {"wiped": ["suggestions"], "kept_active_positions": []}

    summary = format_cleanup_summary(result)

    assert "No active positions were kept" in summary


@patch("db.database.Settings.load")
def test_cleanup_real_db_wipes_correctly_and_respects_foreign_keys(mock_settings_load, tmp_path):
    from db.database import execute, fetch_all, init_db

    mock_settings_load.return_value = MagicMock(sqlite_db_path=str(tmp_path / "test.db"))

    init_db()
    suggestion_id = execute("INSERT INTO suggestions (ticker, action) VALUES (?, ?)", ("AAPL", "BUY"))
    execute(
        "INSERT INTO positions (suggestion_id, ticker, qty, entry_price, status) VALUES (?, ?, ?, ?, 'active')",
        (suggestion_id, "AAPL", 10, 190.0),
    )
    closed_position_id = execute(
        "INSERT INTO positions (ticker, qty, entry_price, status) VALUES (?, ?, ?, 'closed')",
        ("MSFT", 5, 300.0),
    )
    execute("INSERT INTO alerts_sent (position_id, alert_type) VALUES (?, ?)", (closed_position_id, "target"))
    execute("INSERT INTO conversations (ticker, role, message) VALUES (?, ?, ?)", ("AAPL", "user", "hi"))

    result = cleanup()

    assert result["kept_active_positions"] == [{"id": 1, "ticker": "AAPL"}]
    assert fetch_all("SELECT * FROM suggestions") == []
    assert fetch_all("SELECT * FROM conversations") == []
    assert fetch_all("SELECT * FROM alerts_sent") == []

    remaining_positions = [dict(row) for row in fetch_all("SELECT * FROM positions")]
    assert len(remaining_positions) == 1
    assert remaining_positions[0]["ticker"] == "AAPL"
    assert remaining_positions[0]["status"] == "active"
    assert remaining_positions[0]["suggestion_id"] is None
