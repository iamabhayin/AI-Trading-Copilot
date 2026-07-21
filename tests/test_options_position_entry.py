"""Tests for skills/options_position_entry.py — Phase 15 Task 6."""

import json
from unittest.mock import MagicMock, patch

import pytest

from skills.options_position_entry import (
    _normalize_expiry,
    close_position,
    format_options_reply_result,
    handle_options_reply,
    open_position,
    parse_options_trade,
    parse_options_trade_reply,
    parse_options_trade_reply_with_ollama,
)


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    mock_settings = MagicMock(sqlite_db_path=db_path)
    monkeypatch.setattr("config.settings.Settings.load", lambda: mock_settings)

    from db.database import init_db

    init_db()
    return db_path


def _insert_advisory(action="BUY_CE_CANDIDATE", expiry="24JUL2025", strike=25200.0, lot_size=75, spot=25230.0):
    from db.database import execute

    payload = {
        "action": action, "expiry": expiry, "strike": strike, "side": "CE" if "CE" in action else "PE",
        "lot_size": lot_size, "spot": spot, "target_1": 25320.0, "target_2": 25400.0,
        "underlying_invalidation": 25124.4, "risk_reward": 2.5,
    }
    execute(
        "INSERT INTO options_advisories (created_ts, action, payload_json) VALUES (datetime('now'), ?, ?)",
        (action, json.dumps(payload)),
    )
    return payload


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_normalize_expiry_two_digit_year():
    assert _normalize_expiry("24jul25") == "24JUL2025"


def test_normalize_expiry_four_digit_year():
    assert _normalize_expiry("24Jul2025") == "24JUL2025"


def test_parse_options_trade_reply_buy():
    result = parse_options_trade_reply("bought NIFTY 24JUL2025 25200 CE at 145")
    assert result == {"action": "buy", "expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "qty_lots": 1, "price": 145.0}


def test_parse_options_trade_reply_sell_with_lots():
    result = parse_options_trade_reply("sold 2 lots NIFTY 24JUL2025 25200 CE @ 210")
    assert result["action"] == "sell"
    assert result["qty_lots"] == 2
    assert result["price"] == 210.0


def test_parse_options_trade_reply_pe():
    result = parse_options_trade_reply("bought NIFTY 24JUL2025 24800 PE at 90")
    assert result["side"] == "PE"
    assert result["strike"] == 24800.0


def test_parse_options_trade_reply_no_match_returns_none():
    assert parse_options_trade_reply("what's the market doing today") is None


def test_parse_options_trade_reply_with_ollama_no_host_returns_none():
    with patch("skills.options_position_entry.Settings.load", return_value=MagicMock(ollama_host="")):
        assert parse_options_trade_reply_with_ollama("bought some nifty calls") is None


def test_parse_options_trade_reply_with_ollama_success():
    mock_settings = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3.2")
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "response": json.dumps({"action": "buy", "expiry": "24JUL2025", "strike": 25200, "side": "CE", "qty_lots": 1, "price": 145})
    }
    with (
        patch("skills.options_position_entry.Settings.load", return_value=mock_settings),
        patch("skills.options_position_entry.requests.post", return_value=mock_response),
    ):
        result = parse_options_trade_reply_with_ollama("grabbed some nifty 25200 calls this morning around 145")
    assert result["strike"] == 25200.0
    assert result["expiry"] == "24JUL2025"


def test_parse_options_trade_falls_back_to_ollama():
    with patch("skills.options_position_entry.parse_options_trade_reply_with_ollama", return_value={"action": "buy"}) as mock_ollama:
        result = parse_options_trade("nonsense that regex can't parse")
    mock_ollama.assert_called_once()
    assert result == {"action": "buy"}


# --------------------------------------------------------------------------
# Open / close (real DB)
# --------------------------------------------------------------------------


def test_open_position_with_matching_advisory(real_db):
    _insert_advisory()
    trade = {"action": "buy", "expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "qty_lots": 1, "price": 145.0}

    position_id = open_position(trade)

    assert position_id is not None
    import sqlite3

    conn = sqlite3.connect(real_db)
    row = conn.execute("SELECT status, contract, lot_size, entry_premium FROM options_positions WHERE id = ?", (position_id,)).fetchone()
    conn.close()
    assert row == ("active", "NIFTY 25200 CE 24 July", 75, 145.0)


def test_open_position_no_matching_advisory_returns_none(real_db):
    trade = {"action": "buy", "expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "qty_lots": 1, "price": 145.0}
    assert open_position(trade) is None


def test_open_position_advisory_wrong_strike_returns_none(real_db):
    _insert_advisory(strike=25200.0)
    trade = {"action": "buy", "expiry": "24JUL2025", "strike": 25300.0, "side": "CE", "qty_lots": 1, "price": 145.0}
    assert open_position(trade) is None


def test_close_position_matching(real_db):
    _insert_advisory()
    open_trade = {"action": "buy", "expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "qty_lots": 1, "price": 145.0}
    position_id = open_position(open_trade)

    close_trade = {"expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "price": 210.0}
    closed_id = close_position(close_trade)

    assert closed_id == position_id
    import sqlite3

    conn = sqlite3.connect(real_db)
    row = conn.execute("SELECT status, exit_premium FROM options_positions WHERE id = ?", (position_id,)).fetchone()
    conn.close()
    assert row == ("closed", 210.0)


def test_close_position_no_active_position_returns_none(real_db):
    close_trade = {"expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "price": 210.0}
    assert close_position(close_trade) is None


# --------------------------------------------------------------------------
# handle_options_reply / formatting
# --------------------------------------------------------------------------


def test_handle_options_reply_unparsed():
    # "hello there" falls through to the Ollama fallback -- mock Settings
    # so this doesn't make a real network call using .env's real OLLAMA_HOST.
    with patch("skills.options_position_entry.Settings.load", return_value=MagicMock(ollama_host="")):
        result = handle_options_reply("hello there")
    assert result["status"] == "unparsed"


def test_handle_options_reply_opened(real_db):
    _insert_advisory()
    result = handle_options_reply("bought NIFTY 24JUL2025 25200 CE at 145")
    assert result["status"] == "opened"


def test_handle_options_reply_no_matching_advisory(real_db):
    result = handle_options_reply("bought NIFTY 24JUL2025 25200 CE at 145")
    assert result["status"] == "no_matching_advisory"


def test_handle_options_reply_no_active_position(real_db):
    result = handle_options_reply("sold NIFTY 24JUL2025 25200 CE at 210")
    assert result["status"] == "no_active_position"


def test_format_options_reply_result_opened():
    text = format_options_reply_result(
        {"status": "opened", "position_id": 1, "action": "buy", "qty_lots": 1, "expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "price": 145.0}
    )
    assert "Opened" in text
    assert "25200" in text


def test_format_options_reply_result_unparsed():
    assert "Could not recognize" in format_options_reply_result({"status": "unparsed", "message": "hi"})


def test_format_options_reply_result_no_matching_advisory():
    text = format_options_reply_result({"status": "no_matching_advisory", "expiry": "24JUL2025", "strike": 25200.0, "side": "CE"})
    assert "No matching BUY advisory" in text
