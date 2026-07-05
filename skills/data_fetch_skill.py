"""Data Fetch Skill (Data Agent) — Section 3.1.

Agent/Model: Ollama (news coarse relevance filter only).

Pulls OHLCV candles from the broker API (Zerodha Kite / Upstox / Fyers), or
Alpha Vantage / yfinance for testing. Pulls latest news headlines per ticker
(NewsAPI / Finnhub). Runs a coarse Ollama relevance filter on raw headlines
before they reach the Signal Skill — high volume, low stakes, cheap. Outputs
clean structured data; final news synthesis judgment happens in Claude via
the Signal Skill, not here.
"""

# TODO: Phase 1 — data fetch + indicator engine (plain Python, console output)
# TODO: Phase 3+ — swap fetch_ohlcv's data source to the live broker API

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import requests
import yfinance as yf

from config.settings import Settings

# yfinance has no native 4h bucket; use 60m and let downstream resampling
# handle it (TODO: Phase 1 follow-up if 4h is actually needed for entry timing).
TIMEFRAME_TO_YF_INTERVAL = {
    "1h": "60m",
    "4h": "60m",
    "1d": "1d",
    "1wk": "1wk",
}

OHLCV_COLUMNS = ["open", "high", "low", "close", "volume"]


def fetch_ohlcv(ticker: str, timeframe: str = "1d", period: str = "6mo") -> pd.DataFrame:
    """Pull OHLCV candles for `ticker` at the given timeframe.

    Uses yfinance for local/testing (Phase 0-1, free, no key required).
    Broker API integration is a later-phase swap-in behind this same
    function signature.
    """
    interval = TIMEFRAME_TO_YF_INTERVAL.get(timeframe, timeframe)
    data = yf.download(ticker, period=period, interval=interval, progress=False, auto_adjust=True)

    if isinstance(data.columns, pd.MultiIndex):
        data.columns = [str(col[0]).lower() for col in data.columns]
    else:
        data.columns = [str(col).lower() for col in data.columns]

    if data.empty:
        raise ValueError(f"No OHLCV data returned for {ticker} ({timeframe})")

    return data[OHLCV_COLUMNS]


def fetch_news(ticker: str, limit: int = 10) -> list[dict]:
    """Pull latest headlines for `ticker` via NewsAPI, falling back to
    Finnhub. Returns an empty list if neither API key is configured.
    """
    settings = Settings.load()

    if settings.newsapi_key:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": ticker,
                "sortBy": "publishedAt",
                "pageSize": limit,
                "apiKey": settings.newsapi_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {
                "headline": article["title"],
                "source": article.get("source", {}).get("name", "newsapi"),
                "published_at": article.get("publishedAt"),
                "url": article.get("url"),
            }
            for article in resp.json().get("articles", [])
        ]

    if settings.finnhub_api_key:
        today = datetime.now(UTC).date()
        resp = requests.get(
            "https://finnhub.io/api/v1/company-news",
            params={
                "symbol": ticker,
                "from": (today - timedelta(days=7)).isoformat(),
                "to": today.isoformat(),
                "token": settings.finnhub_api_key,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return [
            {
                "headline": article["headline"],
                "source": article.get("source", "finnhub"),
                "published_at": article.get("datetime"),
                "url": article.get("url"),
            }
            for article in resp.json()[:limit]
        ]

    return []


def filter_news_relevance(ticker: str, headlines: list[dict]) -> list[dict]:
    """Coarse relevance filter via Ollama — high-volume, low-stakes first
    pass ahead of the Claude synthesis step in the Signal Skill.

    Falls back to passing headlines through unfiltered (tagged
    `relevant=True`) if OLLAMA_HOST isn't configured or isn't reachable, so
    the pipeline still runs end-to-end without a local Ollama instance
    during early testing.
    """
    settings = Settings.load()
    if not headlines:
        return []
    if not settings.ollama_host:
        return [{**headline, "relevant": True} for headline in headlines]

    filtered = []
    for headline in headlines:
        prompt = (
            f'Headline: "{headline["headline"]}"\n'
            f"Is this headline materially relevant to the stock {ticker}? "
            f"Reply with only 'yes' or 'no'."
        )
        try:
            resp = requests.post(
                f"{settings.ollama_host}/api/generate",
                json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
                timeout=15,
            )
            resp.raise_for_status()
            answer = resp.json().get("response", "").strip().lower()
            relevant = answer.startswith("yes")
        except requests.RequestException:
            relevant = True  # fail open — don't silently drop news if Ollama is unreachable
        filtered.append({**headline, "relevant": relevant})
    return filtered


def fetch_ticker_snapshot(ticker: str, timeframe: str = "1d") -> dict:
    """Bundle OHLCV + relevance-filtered news into the structured payload
    consumed by the Indicator Engine and Signal Skill.
    """
    return {
        "ticker": ticker,
        "timeframe": timeframe,
        "ohlcv": fetch_ohlcv(ticker, timeframe),
        "news": filter_news_relevance(ticker, fetch_news(ticker)),
    }


if __name__ == "__main__":
    from config.startup import StartupService

    settings = StartupService().start()
    watchlist = settings.watchlist or ["AAPL"]
    default_timeframe = settings.default_timeframe or "1d"

    for symbol in watchlist:
        print(f"--- {symbol} ({default_timeframe}) ---")
        snapshot = fetch_ticker_snapshot(symbol, default_timeframe)
        print(snapshot["ohlcv"].tail())
        relevant_count = sum(1 for item in snapshot["news"] if item["relevant"])
        print(f"News: {len(snapshot['news'])} fetched, {relevant_count} flagged relevant")
