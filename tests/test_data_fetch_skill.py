"""Tests for skills/data_fetch_skill.py.

All network calls (yfinance, requests) are mocked so these run offline
without real API keys — matches Phase 1's "validate the data pipeline"
goal without depending on live services.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from skills.data_fetch_skill import fetch_news, fetch_ohlcv, filter_news_relevance


@patch("skills.data_fetch_skill.yf.download")
def test_fetch_ohlcv_normalizes_columns(mock_download):
    index = pd.date_range("2025-01-01", periods=5, freq="D")
    mock_download.return_value = pd.DataFrame(
        {
            "Open": [1, 2, 3, 4, 5],
            "High": [1.5, 2.5, 3.5, 4.5, 5.5],
            "Low": [0.5, 1.5, 2.5, 3.5, 4.5],
            "Close": [1.2, 2.2, 3.2, 4.2, 5.2],
            "Volume": [100, 200, 300, 400, 500],
        },
        index=index,
    )

    df = fetch_ohlcv("AAPL", timeframe="1d", period="5d")

    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert len(df) == 5


@patch("skills.data_fetch_skill.yf.download")
def test_fetch_ohlcv_raises_on_empty_result(mock_download):
    mock_download.return_value = pd.DataFrame()

    with pytest.raises(ValueError):
        fetch_ohlcv("BADTICKER")


@patch("skills.data_fetch_skill.Settings.load")
@patch("skills.data_fetch_skill.requests.get")
def test_fetch_news_without_api_keys_returns_empty(mock_get, mock_settings_load):
    mock_settings_load.return_value = MagicMock(newsapi_key="", finnhub_api_key="")

    result = fetch_news("AAPL")

    assert result == []
    mock_get.assert_not_called()


@patch("skills.data_fetch_skill.Settings.load")
@patch("skills.data_fetch_skill.requests.get")
def test_fetch_news_uses_newsapi_when_key_present(mock_get, mock_settings_load):
    mock_settings_load.return_value = MagicMock(newsapi_key="fake-key", finnhub_api_key="")
    mock_response = MagicMock()
    mock_response.json.return_value = {
        "articles": [
            {
                "title": "Company beats earnings",
                "source": {"name": "Reuters"},
                "publishedAt": "2026-07-01T00:00:00Z",
                "url": "https://example.com/a",
            }
        ]
    }
    mock_get.return_value = mock_response

    result = fetch_news("AAPL")

    assert result == [
        {
            "headline": "Company beats earnings",
            "source": "Reuters",
            "published_at": "2026-07-01T00:00:00Z",
            "url": "https://example.com/a",
        }
    ]


def test_filter_news_relevance_empty_input():
    assert filter_news_relevance("AAPL", []) == []


@patch("skills.data_fetch_skill.Settings.load")
def test_filter_news_relevance_without_ollama_passes_through(mock_settings_load):
    mock_settings_load.return_value = MagicMock(ollama_host="", ollama_model="")
    headlines = [{"headline": "Company beats earnings"}]

    result = filter_news_relevance("AAPL", headlines)

    assert result == [{"headline": "Company beats earnings", "relevant": True}]


@patch("skills.data_fetch_skill.requests.post")
@patch("skills.data_fetch_skill.Settings.load")
def test_filter_news_relevance_uses_ollama_response(mock_settings_load, mock_post):
    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.return_value = MagicMock(json=lambda: {"response": "no"})
    headlines = [{"headline": "Unrelated celebrity gossip"}]

    result = filter_news_relevance("AAPL", headlines)

    assert result == [{"headline": "Unrelated celebrity gossip", "relevant": False}]


@patch("skills.data_fetch_skill.requests.post")
@patch("skills.data_fetch_skill.Settings.load")
def test_filter_news_relevance_fails_open_on_ollama_error(mock_settings_load, mock_post):
    import requests

    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.side_effect = requests.RequestException("connection refused")
    headlines = [{"headline": "Company beats earnings"}]

    result = filter_news_relevance("AAPL", headlines)

    assert result == [{"headline": "Company beats earnings", "relevant": True}]
