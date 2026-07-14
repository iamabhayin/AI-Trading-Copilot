"""Tests for skills/global_cues_skill.py.

fetch_ohlcv, requests, and Ollama calls are mocked so these run offline.
"""

from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from skills.global_cues_skill import (
    build_global_cues_digest,
    fetch_global_cues,
    fetch_market_news,
    format_global_cues_digest,
    generate_market_sentiment,
    notify_global_cues_digest,
)


def _ohlcv(*closes: float) -> pd.DataFrame:
    return pd.DataFrame({"close": list(closes)})


# Matches CUE_TICKERS order: sp500, nasdaq, vix, usd_inr, crude_oil.
GOOD_CLOSES = [
    _ohlcv(7575.39, 7515.34),  # sp500
    _ohlcv(26281.61, 25873.18),  # nasdaq
    _ohlcv(15.03, 16.75),  # vix
    _ohlcv(95.32, 96.19),  # usd_inr
    _ohlcv(78.14, 80.56),  # crude_oil
]


@patch("skills.global_cues_skill.fetch_ohlcv")
def test_fetch_global_cues_returns_structured_dict_for_all_tickers(mock_fetch_ohlcv):
    mock_fetch_ohlcv.side_effect = GOOD_CLOSES

    cues = fetch_global_cues()

    assert set(cues) == {"sp500", "nasdaq", "vix", "usd_inr", "crude_oil"}
    assert cues["sp500"]["close"] == pytest.approx(7515.34)
    assert cues["sp500"]["change_pct"] == pytest.approx((7515.34 - 7575.39) / 7575.39 * 100)
    assert cues["vix"] == {"level": pytest.approx(16.75)}
    assert "change_pct" not in cues["vix"]


@patch("skills.global_cues_skill.fetch_ohlcv")
def test_fetch_global_cues_isolates_one_bad_ticker(mock_fetch_ohlcv):
    mock_fetch_ohlcv.side_effect = [
        GOOD_CLOSES[0],
        GOOD_CLOSES[1],
        GOOD_CLOSES[2],
        ValueError("No OHLCV data returned for INR=X (1d)"),
        GOOD_CLOSES[4],
    ]

    cues = fetch_global_cues()

    assert set(cues) == {"sp500", "nasdaq", "vix", "crude_oil"}
    assert "usd_inr" not in cues


@patch("skills.global_cues_skill.fetch_ohlcv")
def test_fetch_global_cues_skips_ticker_with_insufficient_history(mock_fetch_ohlcv):
    mock_fetch_ohlcv.side_effect = [_ohlcv(7515.34), GOOD_CLOSES[1], GOOD_CLOSES[2], GOOD_CLOSES[3], GOOD_CLOSES[4]]

    cues = fetch_global_cues()

    assert "sp500" not in cues
    assert set(cues) == {"nasdaq", "vix", "usd_inr", "crude_oil"}


@patch("skills.global_cues_skill.Settings.load")
def test_fetch_market_news_returns_empty_without_any_key_configured(mock_settings_load):
    mock_settings_load.return_value = MagicMock(newsapi_key="", finnhub_api_key="")

    assert fetch_market_news() == []


@patch("skills.global_cues_skill.requests.get")
@patch("skills.global_cues_skill.Settings.load")
def test_fetch_market_news_uses_newsapi_when_configured(mock_settings_load, mock_get):
    mock_settings_load.return_value = MagicMock(newsapi_key="fake-newsapi-key", finnhub_api_key="")
    mock_get.return_value = MagicMock(
        json=lambda: {
            "articles": [
                {"title": "Fed signals rate pause", "source": {"name": "Reuters"}, "publishedAt": "t", "url": "u"}
            ]
        }
    )

    headlines = fetch_market_news()

    assert headlines == [{"headline": "Fed signals rate pause", "source": "Reuters", "published_at": "t", "url": "u"}]
    args, kwargs = mock_get.call_args
    assert "newsapi.org" in args[0]
    assert kwargs["params"]["q"] == "stock market OR Nasdaq OR S&P 500 OR Federal Reserve OR crude oil OR Wall Street"


@patch("skills.global_cues_skill.requests.get")
@patch("skills.global_cues_skill.Settings.load")
def test_fetch_market_news_falls_back_to_finnhub(mock_settings_load, mock_get):
    mock_settings_load.return_value = MagicMock(newsapi_key="", finnhub_api_key="fake-finnhub-key")
    mock_get.return_value = MagicMock(
        json=lambda: [{"headline": "Crude spikes on supply fears", "source": "finnhub", "datetime": 1, "url": "u"}]
    )

    headlines = fetch_market_news()

    assert headlines == [{"headline": "Crude spikes on supply fears", "source": "finnhub", "published_at": 1, "url": "u"}]
    args, kwargs = mock_get.call_args
    assert "finnhub.io" in args[0]
    assert kwargs["params"]["category"] == "general"


@patch("skills.global_cues_skill.Settings.load")
def test_generate_market_sentiment_returns_none_without_ollama(mock_settings_load):
    mock_settings_load.return_value = MagicMock(ollama_host="")

    assert generate_market_sentiment({"vix": {"level": 16.75}}, []) is None


@patch("skills.global_cues_skill.requests.post")
@patch("skills.global_cues_skill.Settings.load")
def test_generate_market_sentiment_uses_ollama_when_configured(mock_settings_load, mock_post):
    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.return_value = MagicMock(json=lambda: {"response": "Mixed overnight session, mildly bearish lean."})

    sentiment = generate_market_sentiment({"vix": {"level": 16.75}}, [{"headline": "Fed holds rates"}])

    assert sentiment == "Mixed overnight session, mildly bearish lean."
    _, kwargs = mock_post.call_args
    assert "Fed holds rates" in kwargs["json"]["prompt"]


@patch("skills.global_cues_skill.requests.post")
@patch("skills.global_cues_skill.Settings.load")
def test_generate_market_sentiment_returns_none_on_ollama_error(mock_settings_load, mock_post):
    import requests

    mock_settings_load.return_value = MagicMock(ollama_host="http://localhost:11434", ollama_model="llama3")
    mock_post.side_effect = requests.RequestException("connection refused")

    assert generate_market_sentiment({"vix": {"level": 16.75}}, []) is None


def test_format_global_cues_digest_numbers_only_without_sentiment():
    cues = {"sp500": {"close": 7515.34, "change_pct": -0.79}, "vix": {"level": 16.75}}

    digest = format_global_cues_digest(cues, sentiment=None)

    assert "S&P 500" in digest
    assert "7515.34" in digest
    assert "-0.79%" in digest
    assert "VIX: 16.75" in digest
    assert "Market Sentiment" not in digest


def test_format_global_cues_digest_includes_both_forms_when_sentiment_present():
    cues = {"vix": {"level": 16.75}}

    digest = format_global_cues_digest(cues, sentiment="Bearish overnight tone on rate fears.")

    assert "VIX: 16.75" in digest
    assert "Market Sentiment" in digest
    assert "Bearish overnight tone on rate fears." in digest


@patch("skills.global_cues_skill.fetch_global_cues")
def test_build_global_cues_digest_returns_none_when_no_price_data(mock_fetch_cues):
    mock_fetch_cues.return_value = {}

    assert build_global_cues_digest() is None


@patch("skills.global_cues_skill.generate_market_sentiment")
@patch("skills.global_cues_skill.fetch_market_news")
@patch("skills.global_cues_skill.fetch_global_cues")
def test_build_global_cues_digest_combines_numbers_and_sentiment(mock_fetch_cues, mock_fetch_news, mock_sentiment):
    mock_fetch_cues.return_value = {"vix": {"level": 16.75}}
    mock_fetch_news.return_value = [{"headline": "Fed holds rates steady"}]
    mock_sentiment.return_value = "Mixed session, mildly cautious for Indian markets."

    digest = build_global_cues_digest()

    assert "VIX: 16.75" in digest
    assert "Mixed session, mildly cautious for Indian markets." in digest
    mock_sentiment.assert_called_once_with({"vix": {"level": 16.75}}, [{"headline": "Fed holds rates steady"}])


@patch("skills.global_cues_skill.generate_market_sentiment")
@patch("skills.global_cues_skill.fetch_market_news")
@patch("skills.global_cues_skill.fetch_global_cues")
def test_build_global_cues_digest_survives_market_news_fetch_failure(mock_fetch_cues, mock_fetch_news, mock_sentiment):
    # News is best-effort -- a failed fetch must still produce a
    # numbers-only (or numbers + sentiment-from-price-alone) digest,
    # never block the whole thing.
    mock_fetch_cues.return_value = {"vix": {"level": 16.75}}
    mock_fetch_news.side_effect = RuntimeError("NewsAPI unreachable")
    mock_sentiment.return_value = None

    digest = build_global_cues_digest()

    assert digest is not None
    assert "VIX: 16.75" in digest
    mock_sentiment.assert_called_once_with({"vix": {"level": 16.75}}, [])


@patch("skills.global_cues_skill.route_message")
@patch("skills.global_cues_skill.build_global_cues_digest")
def test_notify_global_cues_digest_skips_when_no_digest(mock_build_digest, mock_route_message):
    mock_build_digest.return_value = None

    sent = notify_global_cues_digest()

    assert sent is False
    mock_route_message.assert_not_called()


@patch("skills.global_cues_skill.route_message")
@patch("skills.global_cues_skill.build_global_cues_digest")
def test_notify_global_cues_digest_routes_to_discord_only(mock_build_digest, mock_route_message):
    mock_build_digest.return_value = "digest text"

    sent = notify_global_cues_digest()

    assert sent is True
    mock_route_message.assert_called_once_with("global_cues", "digest text", send_telegram=False)
