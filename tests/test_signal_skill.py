"""Tests for skills/signal_skill.py.

The Anthropic client is mocked so these run offline without a real API key.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from skills.signal_skill import SYSTEM_PROMPT, _build_user_prompt, generate_suggestion, main, save_suggestion, shortlist


def _mock_response(payload: dict, stop_reason: str = "end_turn"):
    text_block = MagicMock(type="text", text=json.dumps(payload))
    return MagicMock(content=[text_block], stop_reason=stop_reason, stop_details=None)


def test_system_prompt_names_expected_chart_patterns():
    prompt_lower = SYSTEM_PROMPT.lower()
    assert "pullback" in prompt_lower
    assert "golden/death cross" in prompt_lower
    assert "squeeze" in prompt_lower
    assert "recent history" in prompt_lower or "history window" in prompt_lower


def test_build_user_prompt_includes_recent_history():
    recent = [{"close": 100.0, "ema_14d": 99.0}, {"close": 101.0, "ema_14d": 99.5}]

    prompt = _build_user_prompt("AAPL", {"rsi_14": 50.0}, [], recent)

    assert json.dumps(recent) in prompt
    assert "Recent history" in prompt


@patch("skills.signal_skill.anthropic.Anthropic")
@patch("skills.signal_skill.Settings.load")
def test_generate_suggestion_returns_structured_dict(mock_settings_load, mock_anthropic_cls):
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        {
            "action": "BUY",
            "entry": 100.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "confidence": 0.8,
            "rationale": "RSI oversold with bullish EMA crossover.",
        }
    )
    mock_anthropic_cls.return_value = mock_client

    suggestion = generate_suggestion("AAPL", {"rsi_14": 28.0}, [], [])

    assert suggestion["ticker"] == "AAPL"
    assert suggestion["action"] == "BUY"
    assert suggestion["entry"] == 100.0
    mock_client.messages.create.assert_called_once()
    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["output_config"]["format"]["type"] == "json_schema"


@patch("skills.signal_skill.anthropic.Anthropic")
@patch("skills.signal_skill.Settings.load")
def test_generate_suggestion_raises_on_refusal(mock_settings_load, mock_anthropic_cls):
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_response = MagicMock(content=[], stop_reason="refusal", stop_details=MagicMock())
    mock_client.messages.create.return_value = mock_response
    mock_anthropic_cls.return_value = mock_client

    with pytest.raises(RuntimeError):
        generate_suggestion("AAPL", {}, [], [])


@patch("skills.signal_skill.execute")
def test_save_suggestion_passes_expected_params(mock_execute):
    mock_execute.return_value = 42
    suggestion = {
        "ticker": "AAPL",
        "action": "BUY",
        "entry": 100.0,
        "stop_loss": 95.0,
        "target": 110.0,
        "confidence": 0.8,
        "rationale": "test",
    }

    row_id = save_suggestion(suggestion, timeframe="1d")

    assert row_id == 42
    args, _ = mock_execute.call_args
    query, params = args
    assert "INSERT INTO suggestions" in query
    assert params == ("AAPL", "BUY", 100.0, 95.0, 110.0, 0.8, "test", "1d")


def test_shortlist_excludes_hold_and_sorts_by_confidence():
    suggestions = [
        {"ticker": "A", "action": "HOLD", "confidence": 0.9},
        {"ticker": "B", "action": "BUY", "confidence": 0.6},
        {"ticker": "C", "action": "SELL", "confidence": 0.95},
    ]

    result = shortlist(suggestions, limit=5)

    assert [s["ticker"] for s in result] == ["C", "B"]


def test_shortlist_respects_limit():
    suggestions = [{"ticker": str(i), "action": "BUY", "confidence": i} for i in range(10)]

    result = shortlist(suggestions, limit=3)

    assert len(result) == 3
    assert result[0]["ticker"] == "9"


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_notifies_only_shortlisted_suggestions(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    mock_startup_cls.return_value.start.return_value = MagicMock(watchlist=["AAPL", "MSFT"], default_timeframe="1h")
    mock_fetch_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    buy_suggestion = {"ticker": "AAPL", "action": "BUY", "confidence": 0.8}
    hold_suggestion = {"ticker": "MSFT", "action": "HOLD", "confidence": 0.5}
    mock_generate_suggestion.side_effect = [buy_suggestion, hold_suggestion]

    main()

    assert mock_save_suggestion.call_count == 2
    mock_notify_suggestion.assert_called_once_with(buy_suggestion)


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_skips_ticker_whose_fetch_fails_and_continues_watchlist(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    mock_startup_cls.return_value.start.return_value = MagicMock(watchlist=["DELISTED", "AAPL"], default_timeframe="1h")
    mock_fetch_snapshot.side_effect = [ValueError("No OHLCV data returned for DELISTED (1h)"), {"ohlcv": MagicMock(), "news": []}]
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    buy_suggestion = {"ticker": "AAPL", "action": "BUY", "confidence": 0.8}
    mock_generate_suggestion.return_value = buy_suggestion

    main()

    mock_generate_suggestion.assert_called_once()
    mock_save_suggestion.assert_called_once()
    mock_notify_suggestion.assert_called_once_with(buy_suggestion)
