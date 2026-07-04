"""Tests for skills/notify_skill.py.

Telegram and Ollama calls are mocked so these run offline without a real
bot token or Ollama instance.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from skills.notify_skill import (
    _template_message,
    format_suggestion_message,
    notify_suggestion,
    send_telegram_message,
)

BUY_SUGGESTION = {
    "ticker": "AAPL",
    "action": "BUY",
    "entry": 190.0,
    "stop_loss": 182.0,
    "target": 210.0,
    "confidence": 0.75,
    "rationale": "RSI oversold with bullish EMA crossover.",
}

HOLD_SUGGESTION = {
    "ticker": "MSFT",
    "action": "HOLD",
    "entry": None,
    "stop_loss": None,
    "target": None,
    "confidence": 0.4,
    "rationale": "No clear edge either direction.",
}


def test_template_message_includes_price_levels_for_buy():
    message = _template_message(BUY_SUGGESTION)
    assert "AAPL" in message
    assert "Entry: 190.0" in message
    assert "Stop-loss: 182.0" in message
    assert "Target: 210.0" in message


def test_template_message_omits_price_levels_for_hold():
    message = _template_message(HOLD_SUGGESTION)
    assert "MSFT" in message
    assert "Entry:" not in message
    assert "Stop-loss:" not in message


@patch("skills.notify_skill.Settings.load")
def test_format_suggestion_message_without_ollama_returns_template(mock_settings_load):
    mock_settings_load.return_value = MagicMock(ollama_host="")
    message = format_suggestion_message(BUY_SUGGESTION)
    assert message == _template_message(BUY_SUGGESTION)


@patch("skills.notify_skill.requests.post")
@patch("skills.notify_skill.Settings.load")
def test_format_suggestion_message_uses_ollama_when_configured(mock_settings_load, mock_post):
    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.return_value = MagicMock(json=lambda: {"response": "Polished AAPL BUY message"})

    message = format_suggestion_message(BUY_SUGGESTION)

    assert message == "Polished AAPL BUY message"


@patch("skills.notify_skill.requests.post")
@patch("skills.notify_skill.Settings.load")
def test_format_suggestion_message_fails_open_on_ollama_error(mock_settings_load, mock_post):
    import requests

    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.side_effect = requests.RequestException("connection refused")

    message = format_suggestion_message(BUY_SUGGESTION)

    assert message == _template_message(BUY_SUGGESTION)


@patch("skills.notify_skill.Settings.load")
def test_send_telegram_message_raises_without_token(mock_settings_load):
    mock_settings_load.return_value = MagicMock(telegram_bot_token="", telegram_chat_id="123")
    with pytest.raises(ValueError):
        asyncio.run(send_telegram_message("hello"))


@patch("skills.notify_skill.Settings.load")
def test_send_telegram_message_raises_without_chat_id(mock_settings_load):
    mock_settings_load.return_value = MagicMock(telegram_bot_token="fake-token", telegram_chat_id="")
    with pytest.raises(ValueError):
        asyncio.run(send_telegram_message("hello"))


@patch("skills.notify_skill.Bot")
@patch("skills.notify_skill.Settings.load")
def test_send_telegram_message_calls_bot_send_message(mock_settings_load, mock_bot_cls):
    mock_settings_load.return_value = MagicMock(telegram_bot_token="fake-token", telegram_chat_id="12345")
    mock_bot_instance = MagicMock()
    mock_bot_instance.send_message = AsyncMock()
    mock_bot_cls.return_value = mock_bot_instance

    asyncio.run(send_telegram_message("hello"))

    mock_bot_cls.assert_called_once_with(token="fake-token")
    mock_bot_instance.send_message.assert_awaited_once()
    _, kwargs = mock_bot_instance.send_message.call_args
    assert kwargs["chat_id"] == "12345"
    assert kwargs["text"] == "hello"


@patch("skills.notify_skill.Bot")
@patch("skills.notify_skill.Settings.load")
def test_send_telegram_message_uses_explicit_chat_id_override(mock_settings_load, mock_bot_cls):
    mock_settings_load.return_value = MagicMock(telegram_bot_token="fake-token", telegram_chat_id="12345")
    mock_bot_instance = MagicMock()
    mock_bot_instance.send_message = AsyncMock()
    mock_bot_cls.return_value = mock_bot_instance

    asyncio.run(send_telegram_message("hello", chat_id="99999"))

    _, kwargs = mock_bot_instance.send_message.call_args
    assert kwargs["chat_id"] == "99999"


@patch("skills.notify_skill.Bot")
@patch("skills.notify_skill.Settings.load")
def test_notify_suggestion_formats_and_sends(mock_settings_load, mock_bot_cls):
    mock_settings_load.return_value = MagicMock(
        telegram_bot_token="fake-token", telegram_chat_id="12345", ollama_host=""
    )
    mock_bot_instance = MagicMock()
    mock_bot_instance.send_message = AsyncMock()
    mock_bot_cls.return_value = mock_bot_instance

    notify_suggestion(BUY_SUGGESTION)

    mock_bot_instance.send_message.assert_awaited_once()
    _, kwargs = mock_bot_instance.send_message.call_args
    assert "AAPL" in kwargs["text"]
