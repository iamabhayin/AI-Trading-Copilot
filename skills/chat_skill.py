"""Chat Skill (Conversational Agent) — Section 3.7.

Agent/Model: Claude.

Listens for any free-form message, not just structured replies. Identifies
which ticker you're asking about (implicit last-discussed ticker, or
explicit mention). Re-fetches a fresh data snapshot for that ticker before
answering — never answers from stale memory alone. Builds a prompt from
your question + the original suggestion + the fresh snapshot + recent
conversation history. Replies within 1-3 seconds. Stays on Claude — this is
user-facing trust, not a place to downgrade for cost.
"""

# TODO: Phase 6 — chat skill (conversational Q&A), free-form follow-up questions

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import anthropic

from config.settings import Settings
from db.database import execute, fetch_all, fetch_one

MODEL = "claude-opus-4-8"

SYSTEM_PROMPT = (
    "You are a trading assistant answering a follow-up question about a "
    "specific stock. Ground your answer in the indicator data, news, and "
    "original suggestion provided below — never answer from stale memory "
    "or general knowledge alone. Be concise; this is a quick follow-up, "
    "not a full report."
)

TOKEN_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9.]*")


def known_tickers(watchlist: list[str] | None = None) -> set[str]:
    """Every ticker the system actively tracks or has discussed — the
    watchlist, plus anything ever suggested, positioned, or chatted about.
    Used to recognize an explicit mention in a free-form message without
    false-positiving on ordinary capitalized words. Chat Skill is for
    follow-ups on stocks the system already has context for (Section 3.7
    assumes an "original suggestion" exists), not cold-starting on an
    arbitrary new ticker.
    """
    rows = fetch_all(
        "SELECT ticker FROM suggestions "
        "UNION SELECT ticker FROM positions "
        "UNION SELECT ticker FROM conversations WHERE ticker IS NOT NULL"
    )
    tickers = {row["ticker"].upper() for row in rows}
    tickers.update(ticker.upper() for ticker in (watchlist or []))
    return tickers


def extract_ticker(message: str, known: set[str], ticker_hint: str | None = None) -> str | None:
    """Identify which ticker a free-form message is about: an explicit
    mention of a known ticker wins; otherwise fall back to `ticker_hint`
    (the implicit "last discussed" ticker).
    """
    message_tokens = {token.upper() for token in TOKEN_PATTERN.findall(message)}
    for ticker in known:
        if ticker in message_tokens:
            return ticker
    return ticker_hint


def last_discussed_ticker() -> str | None:
    """The implicit ticker: whichever one was most recently discussed."""
    row = fetch_one("SELECT ticker FROM conversations ORDER BY timestamp DESC LIMIT 1")
    return row["ticker"] if row else None


def recent_history(ticker: str, limit: int = 10) -> list[dict]:
    """Recent conversation turns for `ticker`, oldest first, for prompt context."""
    rows = fetch_all(
        "SELECT role, message FROM conversations WHERE ticker = ? ORDER BY timestamp DESC LIMIT ?",
        (ticker, limit),
    )
    return [dict(row) for row in reversed(rows)]


def latest_suggestion(ticker: str) -> dict | None:
    """The most recent Signal Skill suggestion for `ticker`, for prompt context."""
    row = fetch_one(
        "SELECT * FROM suggestions WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
        (ticker,),
    )
    return dict(row) if row else None


def record_turn(ticker: str, role: str, message: str) -> int:
    """Persist one turn of the conversation."""
    return execute(
        "INSERT INTO conversations (ticker, role, message) VALUES (?, ?, ?)",
        (ticker, role, message),
    )


def build_user_prompt(
    question: str, ticker: str, suggestion: dict | None, indicators: dict, news: list[dict], history: list[dict]
) -> str:
    parts = [f"Ticker: {ticker}"]

    if suggestion:
        parts.append(
            "Original suggestion: "
            f"{suggestion['action']} @ entry {suggestion['entry_price']}, "
            f"stop_loss {suggestion['stop_loss']}, target {suggestion['target']}, "
            f"confidence {suggestion['confidence']} — {suggestion['rationale']}"
        )

    parts.append(f"Fresh indicators: {json.dumps(indicators, sort_keys=True)}")

    relevant_headlines = [item["headline"] for item in news if item.get("relevant")]
    parts.append(f"Relevant recent headlines: {json.dumps(relevant_headlines)}")

    if history:
        history_lines = "\n".join(f"{turn['role']}: {turn['message']}" for turn in history)
        parts.append(f"Recent conversation:\n{history_lines}")

    parts.append(f"Question: {question}")
    return "\n\n".join(parts)


def answer_question(message: str) -> dict:
    """Top-level entry point: identify the ticker, gather fresh context,
    ask Claude, persist both turns, and return {"ticker", "answer"}.
    """
    from skills.data_fetch_skill import fetch_ticker_snapshot
    from skills.indicator_engine import compute_indicators, summarize_latest

    settings = Settings.load()
    ticker = extract_ticker(message, known_tickers(settings.watchlist), ticker_hint=last_discussed_ticker())
    if ticker is None:
        return {"ticker": None, "answer": "I'm not sure which stock you mean — mention a ticker to get started."}

    timeframe = settings.default_timeframe or "1d"

    snapshot = fetch_ticker_snapshot(ticker, timeframe)
    indicators = summarize_latest(compute_indicators(snapshot["ohlcv"], timeframe))
    suggestion = latest_suggestion(ticker)
    history = recent_history(ticker)

    record_turn(ticker, "user", message)

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": build_user_prompt(message, ticker, suggestion, indicators, snapshot["news"], history)}],
    )

    if response.stop_reason == "refusal":
        raise RuntimeError(f"Claude declined to answer about {ticker}: {response.stop_details}")

    answer = next(block.text for block in response.content if block.type == "text")
    record_turn(ticker, "assistant", answer)

    return {"ticker": ticker, "answer": answer}


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    `python chat_skill.py "why this stop-loss?"` answers a real question
    via answer_question(). Run with no arguments to fall back to a
    hardcoded demo question, for manual testing without a real message.
    """
    parser = argparse.ArgumentParser(description="Answer a free-form question about a tracked stock.")
    parser.add_argument("question", nargs="*", help='Question text, e.g. "why this stop-loss?"')
    args = parser.parse_args(argv)

    from config.startup import StartupService

    StartupService().start()

    question = " ".join(args.question) if args.question else "What's the latest on AAPL?"
    print(f"Q: {question}")
    result = answer_question(question)
    if result["ticker"] is None:
        print(result["answer"])
    else:
        print(f"A ({result['ticker']}): {result['answer']}")


if __name__ == "__main__":
    main()
