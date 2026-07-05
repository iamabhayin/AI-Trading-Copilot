"""Signal Skill (Strategy Agent) — Section 3.3.

Agent/Model: Claude.

Takes indicator output + news (pre-filtered by Ollama in the Data Fetch
Skill) + optionally a chart image, and sends it to the Claude API for
synthesis. Forces structured JSON output:
`{ticker, action, entry, stop_loss, target, confidence, rationale}`.
Ranks/filters into a shortlist ("prominent stocks"). This is the
highest-stakes judgment in the system — never downgraded to Ollama, even
during cost-saving/validation phases.
"""

# TODO: Phase 2 — signal skill with Claude API call (console-only, sanity-check suggestions)

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic

from config.settings import Settings
from db.database import execute

MODEL = "claude-opus-4-8"

VALID_ACTIONS = {"BUY", "SELL", "HOLD"}

SYSTEM_PROMPT = (
    "You are a swing-trading signal analyst. Given technical indicators and "
    "recent relevant news headlines for a stock, decide whether to suggest "
    "BUY, SELL, or HOLD. For BUY/SELL, give a concrete entry price, "
    "stop-loss, and target based on the supplied indicators (RSI, the "
    "14-day/50-day EMA pair, and Bollinger Bands). For HOLD, entry/"
    "stop_loss/target may be null. Give a confidence score between 0 and 1, "
    "and a short rationale grounded in the specific indicator values and "
    "headlines you were given."
)

SUGGESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["BUY", "SELL", "HOLD"]},
        "entry": {"type": ["number", "null"]},
        "stop_loss": {"type": ["number", "null"]},
        "target": {"type": ["number", "null"]},
        "confidence": {"type": "number"},
        "rationale": {"type": "string"},
    },
    "required": ["action", "entry", "stop_loss", "target", "confidence", "rationale"],
    "additionalProperties": False,
}


def _build_user_prompt(ticker: str, indicators: dict, news: list[dict]) -> str:
    relevant_headlines = [item["headline"] for item in news if item.get("relevant")]
    return (
        f"Ticker: {ticker}\n"
        f"Indicators: {json.dumps(indicators, sort_keys=True)}\n"
        f"Relevant recent headlines: {json.dumps(relevant_headlines)}"
    )


def generate_suggestion(ticker: str, indicators: dict, news: list[dict]) -> dict:
    """Call Claude with indicator + news context and return a validated,
    structured suggestion: {ticker, action, entry, stop_loss, target,
    confidence, rationale}.
    """
    settings = Settings.load()
    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

    response = client.messages.create(
        model=MODEL,
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        output_config={"format": {"type": "json_schema", "schema": SUGGESTION_SCHEMA}},
        messages=[{"role": "user", "content": _build_user_prompt(ticker, indicators, news)}],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined to analyze {ticker}: {response.stop_details}")

    text = next(block.text for block in response.content if block.type == "text")
    suggestion = json.loads(text)
    suggestion["ticker"] = ticker
    return suggestion


def save_suggestion(suggestion: dict, timeframe: str) -> int:
    """Persist a suggestion to the `suggestions` table. Returns the new row id."""
    return execute(
        """
        INSERT INTO suggestions (ticker, action, entry_price, stop_loss, target, confidence, rationale, timeframe)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            suggestion["ticker"],
            suggestion["action"],
            suggestion.get("entry"),
            suggestion.get("stop_loss"),
            suggestion.get("target"),
            suggestion.get("confidence"),
            suggestion.get("rationale"),
            timeframe,
        ),
    )


def shortlist(suggestions: list[dict], limit: int = 5) -> list[dict]:
    """Rank non-HOLD suggestions by confidence and return the top `limit` —
    the "prominent stocks" shortlist referenced in Section 3.3.
    """
    actionable = [s for s in suggestions if s["action"] in ("BUY", "SELL")]
    return sorted(actionable, key=lambda s: s.get("confidence") or 0, reverse=True)[:limit]


def main() -> None:
    """CLI/cron entry point: the full pipeline for the whole watchlist —
    data fetch -> indicators -> signal generation -> save -> notify the
    shortlisted (non-HOLD) suggestions. This is what the scheduler invokes.
    """
    from config.startup import StartupService
    from skills.data_fetch_skill import fetch_ticker_snapshot
    from skills.indicator_engine import compute_indicators, summarize_latest
    from skills.notify_skill import notify_suggestion

    settings = StartupService().start()
    watchlist = settings.watchlist or ["AAPL"]
    timeframe = settings.default_timeframe or "1h"

    suggestions = []
    for ticker in watchlist:
        print(f"--- {ticker} ---")
        snapshot = fetch_ticker_snapshot(ticker, timeframe)
        indicators = summarize_latest(compute_indicators(snapshot["ohlcv"], timeframe))
        suggestion = generate_suggestion(ticker, indicators, snapshot["news"])
        print(json.dumps(suggestion, indent=2))
        save_suggestion(suggestion, timeframe)
        suggestions.append(suggestion)

    print("\n--- Shortlist ---")
    for suggestion in shortlist(suggestions):
        print(f"{suggestion['ticker']}: {suggestion['action']} (confidence={suggestion['confidence']})")
        notify_suggestion(suggestion)


if __name__ == "__main__":
    main()
