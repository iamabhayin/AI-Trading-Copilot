"""Tests for skills/chat_skill.py.

DB and Anthropic calls are mocked so these run offline without a real
database file or API key.
"""

from unittest.mock import MagicMock, patch

import pytest

from skills.chat_skill import (
    answer_question,
    build_user_prompt,
    extract_ticker,
    known_tickers,
    last_discussed_ticker,
    latest_suggestion,
    recent_history,
    record_turn,
)


def _mock_response(text: str, stop_reason: str = "end_turn"):
    text_block = MagicMock(type="text", text=text)
    return MagicMock(content=[text_block], stop_reason=stop_reason, stop_details=None)


@patch("skills.chat_skill.fetch_all")
def test_known_tickers_merges_db_and_watchlist(mock_fetch_all):
    mock_fetch_all.return_value = [{"ticker": "reliance"}, {"ticker": "TSLA"}]

    result = known_tickers(watchlist=["aapl", "MSFT"])

    assert result == {"RELIANCE", "TSLA", "AAPL", "MSFT"}


def test_extract_ticker_finds_explicit_mention():
    assert extract_ticker("what's up with AAPL today?", known={"AAPL", "MSFT"}) == "AAPL"


def test_extract_ticker_is_case_insensitive():
    assert extract_ticker("what's up with aapl today?", known={"AAPL"}) == "AAPL"


def test_extract_ticker_falls_back_to_hint_when_no_mention():
    result = extract_ticker("what do you think about it?", known={"AAPL"}, ticker_hint="AAPL")
    assert result == "AAPL"


def test_extract_ticker_returns_none_when_nothing_matches():
    assert extract_ticker("how's the weather?", known={"AAPL"}, ticker_hint=None) is None


@patch("skills.chat_skill.fetch_one")
def test_last_discussed_ticker_returns_most_recent(mock_fetch_one):
    mock_fetch_one.return_value = {"ticker": "AAPL"}
    assert last_discussed_ticker() == "AAPL"


@patch("skills.chat_skill.fetch_one")
def test_last_discussed_ticker_returns_none_when_empty(mock_fetch_one):
    mock_fetch_one.return_value = None
    assert last_discussed_ticker() is None


@patch("skills.chat_skill.fetch_all")
def test_recent_history_returns_oldest_first(mock_fetch_all):
    mock_fetch_all.return_value = [
        {"role": "assistant", "message": "second"},
        {"role": "user", "message": "first"},
    ]

    result = recent_history("AAPL")

    assert [turn["message"] for turn in result] == ["first", "second"]


@patch("skills.chat_skill.fetch_one")
def test_latest_suggestion_returns_dict(mock_fetch_one):
    mock_fetch_one.return_value = {"ticker": "AAPL", "action": "BUY"}
    assert latest_suggestion("AAPL") == {"ticker": "AAPL", "action": "BUY"}


@patch("skills.chat_skill.fetch_one")
def test_latest_suggestion_returns_none_when_missing(mock_fetch_one):
    mock_fetch_one.return_value = None
    assert latest_suggestion("AAPL") is None


@patch("skills.chat_skill.execute")
def test_record_turn_inserts_expected_params(mock_execute):
    mock_execute.return_value = 3
    result = record_turn("AAPL", "user", "how's it doing?")

    assert result == 3
    args, _ = mock_execute.call_args
    _, params = args
    assert params == ("AAPL", "user", "how's it doing?")


def test_build_user_prompt_includes_all_context():
    suggestion = {
        "action": "BUY",
        "entry_price": 190.0,
        "stop_loss": 182.0,
        "target": 210.0,
        "confidence": 0.75,
        "rationale": "Bullish crossover",
    }
    indicators = {"rsi_14": 55.0}
    news = [{"headline": "Good news", "relevant": True}, {"headline": "Irrelevant", "relevant": False}]
    history = [{"role": "user", "message": "earlier question"}]

    prompt = build_user_prompt("What's the outlook?", "AAPL", suggestion, indicators, news, history)

    assert "AAPL" in prompt
    assert "BUY" in prompt
    assert "Bullish crossover" in prompt
    assert "Good news" in prompt
    assert "Irrelevant" not in prompt
    assert "earlier question" in prompt
    assert "What's the outlook?" in prompt


def test_build_user_prompt_handles_no_suggestion_or_history():
    prompt = build_user_prompt("What's the outlook?", "AAPL", None, {"rsi_14": 55.0}, [], [])
    assert "AAPL" in prompt
    assert "Original suggestion" not in prompt


@patch("skills.chat_skill.known_tickers", return_value=set())
@patch("skills.chat_skill.extract_ticker", return_value=None)
@patch("skills.chat_skill.last_discussed_ticker", return_value=None)
@patch("skills.chat_skill.Settings.load")
def test_answer_question_returns_clarification_when_no_ticker(
    mock_settings_load, mock_last_discussed, mock_extract, mock_known_tickers
):
    mock_settings_load.return_value = MagicMock(watchlist=[])

    result = answer_question("how's the market today?")

    assert result["ticker"] is None
    assert "not sure" in result["answer"].lower()


@patch("skills.chat_skill.anthropic.Anthropic")
@patch("skills.chat_skill.record_turn")
@patch("skills.chat_skill.recent_history", return_value=[])
@patch("skills.chat_skill.latest_suggestion", return_value=None)
@patch("skills.chat_skill.known_tickers", return_value=set())
@patch("skills.chat_skill.extract_ticker", return_value="AAPL")
@patch("skills.chat_skill.last_discussed_ticker", return_value=None)
@patch("skills.chat_skill.Settings.load")
def test_answer_question_happy_path(
    mock_settings_load,
    mock_last_discussed,
    mock_extract,
    mock_known_tickers,
    mock_latest_suggestion,
    mock_recent_history,
    mock_record_turn,
    mock_anthropic_cls,
):
    mock_settings_load.return_value = MagicMock(watchlist=["AAPL"], default_timeframe="1d", anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response("AAPL looks strong today.")
    mock_anthropic_cls.return_value = mock_client

    with (
        patch("skills.data_fetch_skill.fetch_ticker_snapshot") as mock_snapshot,
        patch("skills.indicator_engine.compute_indicators"),
        patch("skills.indicator_engine.summarize_latest", return_value={"rsi_14": 55.0}),
    ):
        mock_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
        result = answer_question("what's up with AAPL?")

    assert result == {"ticker": "AAPL", "answer": "AAPL looks strong today."}
    assert mock_record_turn.call_count == 2


@patch("skills.chat_skill.anthropic.Anthropic")
@patch("skills.chat_skill.record_turn")
@patch("skills.chat_skill.recent_history", return_value=[])
@patch("skills.chat_skill.latest_suggestion", return_value=None)
@patch("skills.chat_skill.known_tickers", return_value=set())
@patch("skills.chat_skill.extract_ticker", return_value="AAPL")
@patch("skills.chat_skill.last_discussed_ticker", return_value=None)
@patch("skills.chat_skill.Settings.load")
def test_answer_question_raises_on_refusal(
    mock_settings_load,
    mock_last_discussed,
    mock_extract,
    mock_known_tickers,
    mock_latest_suggestion,
    mock_recent_history,
    mock_record_turn,
    mock_anthropic_cls,
):
    mock_settings_load.return_value = MagicMock(watchlist=["AAPL"], default_timeframe="1d", anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[], stop_reason="refusal", stop_details=MagicMock())
    mock_anthropic_cls.return_value = mock_client

    with (
        patch("skills.data_fetch_skill.fetch_ticker_snapshot") as mock_snapshot,
        patch("skills.indicator_engine.compute_indicators"),
        patch("skills.indicator_engine.summarize_latest", return_value={"rsi_14": 55.0}),
    ):
        mock_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
        with pytest.raises(RuntimeError):
            answer_question("what's up with AAPL?")
