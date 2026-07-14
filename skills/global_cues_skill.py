"""Global Cues Skill — overnight US market context.

Agent/Model: Ollama — phrasing/formatting, and (per explicit user
instruction, 2026-07-14) the market-sentiment narrative below too. Note
this deviates from the Code-vs-Agent split in the architecture doc's
Section 2/11, which assigns genuine interpretive judgment ("is this
bullish or bearish") to Claude — Signal Skill and Chat Skill never get
downgraded to Ollama for exactly that reason. Kept on Ollama here
anyway, so treat the sentiment narrative as best-effort/lower-confidence
commentary, not a synthesis-grade call.

Not part of the NSE signal pipeline itself, but feeds into it (Phase 14
Task 3): fetches S&P 500 / Nasdaq / USD-INR / crude oil % change and the
VIX level via yfinance — the same library already used for every OHLCV
fetch in this codebase, zero new dependency — plus broad market-wide
headlines (NewsAPI/Finnhub, mirroring data_fetch_skill.fetch_news()'s
pattern but with a market-wide query instead of a per-ticker one), and
posts a digest combining both the raw numbers and an Ollama-generated
sentiment read to Discord #market-news before the NSE pre-market run.

Error handling is stricter than metals_price_skill.py's: if the price
fetch comes back with nothing usable, this skill skips silently (log
only, no placeholder alert) rather than sending a "data unavailable"
message — an explicit choice for this phase, not a fail-open default.
The news fetch and sentiment narrative are best-effort on top of that:
either can come back empty/None without blocking the numbers-only digest.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from config.settings import Settings
from skills.data_fetch_skill import fetch_ohlcv
from skills.notify_skill import route_message

# Broad market-wide terms, not tied to any one ticker -- mirrors
# data_fetch_skill.fetch_news()'s query param, just not ticker-shaped.
MARKET_NEWS_QUERY = "stock market OR Nasdaq OR S&P 500 OR Federal Reserve OR crude oil OR Wall Street"

# ^GSPC/^IXIC/^VIX/INR=X/CL=F are all standard Yahoo Finance tickers,
# fetched via the same fetch_ohlcv() every other skill in this repo uses.
CUE_TICKERS = {
    "sp500": "^GSPC",
    "nasdaq": "^IXIC",
    "vix": "^VIX",
    "usd_inr": "INR=X",
    "crude_oil": "CL=F",
}

CUE_LABELS = {
    "sp500": "S&P 500",
    "nasdaq": "Nasdaq",
    "vix": "VIX",
    "usd_inr": "USD/INR",
    "crude_oil": "Crude Oil (WTI)",
}


def fetch_global_cues() -> dict:
    """Fetch overnight global market cues: S&P 500 / Nasdaq / USD-INR /
    crude oil % change (previous close vs. prior close) and the VIX
    level. Each ticker is fetched independently so one bad ticker
    doesn't block the rest — same isolation standard as the Signal
    Skill's per-ticker loop and Monitor Skill's per-position loop. Pure
    code, no LLM. A cue is omitted from the returned dict if its own
    fetch failed.
    """
    cues = {}
    for key, ticker in CUE_TICKERS.items():
        try:
            ohlcv = fetch_ohlcv(ticker, timeframe="1d", period="5d")
            closes = ohlcv["close"].dropna()
            if key == "vix":
                if len(closes) < 1:
                    raise ValueError(f"no close price data for {ticker}")
                cues[key] = {"level": float(closes.iloc[-1])}
            else:
                if len(closes) < 2:
                    raise ValueError(f"not enough close history for {ticker}")
                latest_close = float(closes.iloc[-1])
                prior_close = float(closes.iloc[-2])
                cues[key] = {
                    "close": latest_close,
                    "change_pct": (latest_close - prior_close) / prior_close * 100,
                }
        except Exception as exc:  # one bad ticker must not block the rest of the digest
            print(f"  skipping {key} ({ticker}): {exc}")

    return cues


def fetch_market_news(limit: int = 8) -> list[dict]:
    """Pull broad market-wide headlines (not tied to one ticker) via
    NewsAPI, falling back to Finnhub's general market-news endpoint.
    Mirrors data_fetch_skill.fetch_news()'s NewsAPI/Finnhub structure,
    just with a market-wide query instead of a per-ticker one. Returns
    an empty list if neither API key is configured — the sentiment
    narrative below reasons from price action alone in that case.
    """
    settings = Settings.load()

    if settings.newsapi_key:
        resp = requests.get(
            "https://newsapi.org/v2/everything",
            params={
                "q": MARKET_NEWS_QUERY,
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
        resp = requests.get(
            "https://finnhub.io/api/v1/news",
            params={"category": "general", "token": settings.finnhub_api_key},
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


def generate_market_sentiment(cues: dict, headlines: list[dict]) -> str | None:
    """Ollama-generated market-sentiment narrative: overnight mood,
    bullish/bearish/mixed lean for Indian markets, and which headline(s)
    are most likely driving it. Best-effort — returns `None` if
    OLLAMA_HOST isn't configured or the call fails, so the digest still
    sends numbers-only rather than blocking on this.

    This is genuine interpretive judgment, which this codebase's own
    model-assignment split says belongs to Claude, not Ollama — kept on
    Ollama here per explicit user instruction. Treat the result as
    best-effort commentary, not a grounded synthesis call.
    """
    settings = Settings.load()
    if not settings.ollama_host:
        return None

    headline_texts = [item["headline"] for item in headlines]
    prompt = (
        "Based on the following overnight global market data and recent "
        "broad market headlines, write a short 3-4 sentence summary: how "
        "did the overnight session go, is the backdrop bullish, bearish, "
        "or mixed for Indian markets (Nifty/Sensex) today, and which "
        "specific headline (if any) is most likely driving that "
        "direction. Ground this only in the data/headlines given below — "
        "if the headline list is empty, say so and reason from the price "
        "data alone. Do not invent anything not present here.\n\n"
        f"Data: {json.dumps(cues, sort_keys=True)}\n"
        f"Headlines: {json.dumps(headline_texts)}"
    )
    try:
        resp = requests.post(
            f"{settings.ollama_host}/api/generate",
            json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
            timeout=30,
        )
        resp.raise_for_status()
        text = resp.json().get("response", "").strip()
        return text or None
    except requests.RequestException:
        return None


def _template_global_cues_digest(cues: dict) -> str:
    """Deterministic fallback formatting — no LLM involved."""
    lines = ["*Overnight Global Cues*"]
    for key in CUE_TICKERS:
        if key not in cues:
            continue
        label = CUE_LABELS[key]
        cue = cues[key]
        if key == "vix":
            lines.append(f"{label}: {cue['level']:.2f}")
        else:
            lines.append(f"{label}: {cue['close']:.2f} ({cue['change_pct']:+.2f}%)")
    return "\n".join(lines)


def format_global_cues_digest(cues: dict, sentiment: str | None) -> str:
    """Combine the deterministic numbers section with an optional
    Ollama-generated market-sentiment narrative into the final digest —
    both forms in one message, per explicit user instruction. Numbers
    are never touched by an LLM regardless of whether `sentiment` is
    present.
    """
    digest = _template_global_cues_digest(cues)
    if sentiment:
        digest += "\n\n*Market Sentiment*\n" + sentiment
    return digest


def build_global_cues_digest() -> str | None:
    """Fetch + format the digest. Returns `None` if the price fetch
    produced no usable data at all — the caller skips sending entirely
    (log only, no alert) rather than posting a placeholder message.
    The market-news fetch and sentiment narrative are best-effort on top
    of that: either can come back empty/None without blocking the
    numbers-only digest.
    """
    cues = fetch_global_cues()
    if not cues:
        print("  global cues unavailable; skipping digest")
        return None

    try:
        headlines = fetch_market_news()
    except Exception as exc:  # news is best-effort -- numbers-only digest must still go out
        print(f"  market news fetch failed: {exc}")
        headlines = []

    sentiment = generate_market_sentiment(cues, headlines)
    return format_global_cues_digest(cues, sentiment)


def notify_global_cues_digest() -> bool:
    """Build and send the global cues digest to Discord #market-news
    only — the synchronous entry point used by the morning cron job.
    Sends nothing if no cue data was fetchable. Returns whether a digest
    was actually sent.
    """
    digest = build_global_cues_digest()
    if digest is None:
        return False
    route_message("global_cues", digest, send_telegram=False)
    return True


def main(argv: list[str] | None = None) -> None:
    """CLI/cron entry point: fetch, format, and send today's global cues digest."""
    from config.startup import StartupService

    StartupService().start()
    sent = notify_global_cues_digest()
    print("Global cues digest sent." if sent else "Global cues digest skipped (no data available).")


if __name__ == "__main__":
    main()
