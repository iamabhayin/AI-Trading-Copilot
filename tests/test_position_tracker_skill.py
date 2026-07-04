"""Tests for skills/position_tracker_skill.py.

DB and Ollama calls are mocked so these run offline without a real
database file or Ollama instance.
"""

from unittest.mock import MagicMock, patch

import pytest

from skills.position_tracker_skill import (
    close_position,
    handle_reply,
    open_position,
    parse_trade,
    parse_trade_reply,
    parse_trade_reply_with_ollama,
)


@pytest.mark.parametrize(
    "message,expected",
    [
        (
            "bought 10 RELIANCE @ 2950",
            {"action": "buy", "ticker": "RELIANCE", "qty": 10.0, "price": 2950.0},
        ),
        (
            "sold 10 RELIANCE @ 3050",
            {"action": "sell", "ticker": "RELIANCE", "qty": 10.0, "price": 3050.0},
        ),
        (
            "buy 5 shares of AAPL at 190.5",
            {"action": "buy", "ticker": "AAPL", "qty": 5.0, "price": 190.5},
        ),
        (
            "closed 2 tsla at 250",
            {"action": "sell", "ticker": "TSLA", "qty": 2.0, "price": 250.0},
        ),
    ],
)
def test_parse_trade_reply_regex_matches(message, expected):
    assert parse_trade_reply(message) == expected


def test_parse_trade_reply_returns_none_for_unmatched_text():
    assert parse_trade_reply("what's the weather like today?") is None


@patch("skills.position_tracker_skill.Settings.load")
def test_parse_trade_reply_with_ollama_returns_none_without_host(mock_settings_load):
    mock_settings_load.return_value = MagicMock(ollama_host="")
    assert parse_trade_reply_with_ollama("picked up some AAPL this morning") is None


@patch("skills.position_tracker_skill.requests.post")
@patch("skills.position_tracker_skill.Settings.load")
def test_parse_trade_reply_with_ollama_parses_json_response(mock_settings_load, mock_post):
    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.return_value = MagicMock(
        json=lambda: {"response": '{"action": "buy", "ticker": "aapl", "qty": 3, "price": 190.5}'}
    )

    result = parse_trade_reply_with_ollama("picked up a few AAPL shares this morning, paid around 190.5 each")

    assert result == {"action": "buy", "ticker": "AAPL", "qty": 3.0, "price": 190.5}


@patch("skills.position_tracker_skill.requests.post")
@patch("skills.position_tracker_skill.Settings.load")
def test_parse_trade_reply_with_ollama_returns_none_on_non_trade(mock_settings_load, mock_post):
    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.return_value = MagicMock(json=lambda: {"response": "null"})

    assert parse_trade_reply_with_ollama("how's the market looking today?") is None


@patch("skills.position_tracker_skill.requests.post")
@patch("skills.position_tracker_skill.Settings.load")
def test_parse_trade_reply_with_ollama_fails_open_on_error(mock_settings_load, mock_post):
    import requests

    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.side_effect = requests.RequestException("connection refused")

    assert parse_trade_reply_with_ollama("bought some stock today") is None


def test_parse_trade_prefers_regex_over_ollama():
    with patch("skills.position_tracker_skill.parse_trade_reply_with_ollama") as mock_ollama:
        result = parse_trade("bought 10 RELIANCE @ 2950")
        assert result == {"action": "buy", "ticker": "RELIANCE", "qty": 10.0, "price": 2950.0}
        mock_ollama.assert_not_called()


@patch("skills.position_tracker_skill.execute")
@patch("skills.position_tracker_skill.fetch_one")
def test_open_position_links_to_matching_suggestion(mock_fetch_one, mock_execute):
    mock_fetch_one.return_value = {"id": 7, "stop_loss": 2800.0, "target": 3200.0}
    mock_execute.return_value = 101

    position_id = open_position({"ticker": "RELIANCE", "qty": 10.0, "price": 2950.0})

    assert position_id == 101
    args, _ = mock_execute.call_args
    _, params = args
    assert params == (7, "RELIANCE", 10.0, 2950.0, 2800.0, 3200.0)


@patch("skills.position_tracker_skill.execute")
@patch("skills.position_tracker_skill.fetch_one")
def test_open_position_without_matching_suggestion(mock_fetch_one, mock_execute):
    mock_fetch_one.return_value = None
    mock_execute.return_value = 102

    open_position({"ticker": "XYZ", "qty": 1.0, "price": 50.0})

    args, _ = mock_execute.call_args
    _, params = args
    assert params == (None, "XYZ", 1.0, 50.0, None, None)


@patch("skills.position_tracker_skill.execute")
@patch("skills.position_tracker_skill.fetch_one")
def test_close_position_updates_matching_active_position(mock_fetch_one, mock_execute):
    mock_fetch_one.return_value = {"id": 55}

    closed_id = close_position("RELIANCE", 3050.0)

    assert closed_id == 55
    mock_execute.assert_called_once()


@patch("skills.position_tracker_skill.fetch_one")
def test_close_position_returns_none_when_no_active_position(mock_fetch_one):
    mock_fetch_one.return_value = None

    assert close_position("RELIANCE", 3050.0) is None


def test_handle_reply_returns_unparsed_for_unmatched_text():
    result = handle_reply("what's the weather like today?")
    assert result == {"status": "unparsed", "message": "what's the weather like today?"}


@patch("skills.position_tracker_skill.open_position")
def test_handle_reply_opens_position_for_buy(mock_open_position):
    mock_open_position.return_value = 200

    result = handle_reply("bought 10 RELIANCE @ 2950")

    assert result["status"] == "opened"
    assert result["position_id"] == 200
    assert result["ticker"] == "RELIANCE"


@patch("skills.position_tracker_skill.close_position")
def test_handle_reply_closes_position_for_sell(mock_close_position):
    mock_close_position.return_value = 200

    result = handle_reply("sold 10 RELIANCE @ 3050")

    assert result["status"] == "closed"
    assert result["position_id"] == 200


@patch("skills.position_tracker_skill.close_position")
def test_handle_reply_reports_no_active_position(mock_close_position):
    mock_close_position.return_value = None

    result = handle_reply("sold 10 RELIANCE @ 3050")

    assert result["status"] == "no_active_position"
