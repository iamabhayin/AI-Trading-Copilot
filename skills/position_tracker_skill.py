"""Position Tracker (Reply Parser) — Section 3.5.

Agent/Model: Ollama.

Listens for your reply (e.g. "bought 10 RELIANCE @ 2950"). Parses
ticker/qty/price using regex first, falling back to Ollama for messy
phrasing — pure extraction, no trading judgment involved. Writes the
result to the `positions` table with status = 'active'.

Closing a position ("sold 10 RELIANCE @ 3050") is the same reply-parsing
problem in reverse, so it lives in this skill too — the schema's
`status`/`exit_price`/`exit_time` columns exist for exactly this.
"""

# TODO: Phase 4 — position tracker (reply parsing -> SQLite), "bought X" flow working end-to-end

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from config.settings import Settings
from db.database import execute, fetch_one

BUY_WORDS = {"bought", "buy", "long"}
SELL_WORDS = {"sold", "sell", "closed", "close"}

# e.g. "bought 10 RELIANCE @ 2950", "buy 5 shares of AAPL at 190.5"
TRADE_PATTERN = re.compile(
    r"(?P<action>bought|buy|sold|sell|closed?|long)\s+"
    r"(?P<qty>\d+(?:\.\d+)?)\s+"
    r"(?:shares?\s+of\s+)?"
    r"(?P<ticker>[A-Za-z0-9.]{1,10})\s+"
    r"(?:@|at)\s*"
    r"(?P<price>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_trade_reply(message: str) -> dict | None:
    """Extract {action, ticker, qty, price} from a free-form trade reply
    using regex. Returns None if the pattern doesn't match — the caller
    should fall back to `parse_trade_reply_with_ollama`.
    """
    match = TRADE_PATTERN.search(message)
    if not match:
        return None
    side = "buy" if match.group("action").lower() in BUY_WORDS else "sell"
    return {
        "action": side,
        "ticker": match.group("ticker").upper(),
        "qty": float(match.group("qty")),
        "price": float(match.group("price")),
    }


def parse_trade_reply_with_ollama(message: str) -> dict | None:
    """Fallback extraction for phrasing the regex can't handle. Returns
    None if OLLAMA_HOST isn't configured, unreachable, or the response
    isn't a well-formed trade — the caller should treat the message as
    unparsed rather than guess.
    """
    settings = Settings.load()
    if not settings.ollama_host:
        return None

    prompt = (
        "Extract the trade action from this message. Respond with ONLY a "
        'JSON object of the shape {"action": "buy"|"sell", "ticker": str, '
        '"qty": number, "price": number}. If the message does not describe '
        "a completed trade, respond with exactly: null\n\n"
        f"Message: {message}"
    )
    try:
        resp = requests.post(
            f"{settings.ollama_host}/api/generate",
            json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
            timeout=30,
        )
        resp.raise_for_status()
        parsed = json.loads(resp.json().get("response", "").strip())
    except (requests.RequestException, json.JSONDecodeError):
        return None

    if not isinstance(parsed, dict) or not {"action", "ticker", "qty", "price"}.issubset(parsed):
        return None
    return {
        "action": str(parsed["action"]).lower(),
        "ticker": str(parsed["ticker"]).upper(),
        "qty": float(parsed["qty"]),
        "price": float(parsed["price"]),
    }


def parse_trade(message: str) -> dict | None:
    """Parse a free-form trade reply: regex first, Ollama fallback."""
    return parse_trade_reply(message) or parse_trade_reply_with_ollama(message)


def open_position(trade: dict) -> int:
    """Record a new active position, linking it to the most recent
    suggestion for that ticker (for stop_loss/target) if one exists.
    Returns the new position id.
    """
    suggestion = fetch_one(
        "SELECT id, stop_loss, target FROM suggestions WHERE ticker = ? ORDER BY created_at DESC LIMIT 1",
        (trade["ticker"],),
    )
    suggestion_id = suggestion["id"] if suggestion else None
    stop_loss = suggestion["stop_loss"] if suggestion else None
    target = suggestion["target"] if suggestion else None

    return execute(
        """
        INSERT INTO positions (suggestion_id, ticker, qty, entry_price, stop_loss, target, status)
        VALUES (?, ?, ?, ?, ?, ?, 'active')
        """,
        (suggestion_id, trade["ticker"], trade["qty"], trade["price"], stop_loss, target),
    )


def close_position(ticker: str, exit_price: float) -> int | None:
    """Close the most recently opened active position for `ticker`.
    Returns the closed position's id, or None if there is no active
    position to close.
    """
    position = fetch_one(
        "SELECT id FROM positions WHERE ticker = ? AND status = 'active' ORDER BY entry_time DESC LIMIT 1",
        (ticker,),
    )
    if not position:
        return None

    execute(
        "UPDATE positions SET status = 'closed', exit_price = ?, exit_time = datetime('now') WHERE id = ?",
        (exit_price, position["id"]),
    )
    return position["id"]


def handle_reply(message: str) -> dict:
    """Top-level entry point: parse a reply and apply it to the positions
    table. Returns a small result dict describing what happened, for the
    caller (Notify Skill / OpenClaw) to relay back to the user.
    """
    trade = parse_trade(message)
    if trade is None:
        return {"status": "unparsed", "message": message}

    if trade["action"] == "buy":
        position_id = open_position(trade)
        return {"status": "opened", "position_id": position_id, **trade}

    position_id = close_position(trade["ticker"], trade["price"])
    if position_id is None:
        return {"status": "no_active_position", **trade}
    return {"status": "closed", "position_id": position_id, **trade}


def format_reply_result(result: dict) -> str:
    """Render a handle_reply() result as a clean, human-readable
    confirmation — callers relay this text back to the user (e.g.
    OpenClaw replying in Discord), not the raw dict.
    """
    status = result["status"]
    if status == "unparsed":
        return f"Could not recognize a trade in: {result['message']!r}"
    if status == "no_active_position":
        return f"No active {result['ticker']} position found to close."

    action_word = "Bought" if result["action"] == "buy" else "Sold"
    verb = "Opened" if status == "opened" else "Closed"
    return (
        f"{verb} position #{result['position_id']}: {action_word} {result['qty']:g} "
        f"{result['ticker']} @ {result['price']:g}"
    )


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    `python position_tracker_skill.py "bought 10 RELIANCE @ 2950"` parses
    and records a real trade reply via handle_reply(). Run with no
    arguments to fall back to the hardcoded demo, for manual testing
    without a real message.
    """
    parser = argparse.ArgumentParser(description="Parse a trade reply and record it in the positions table.")
    parser.add_argument("message", nargs="*", help='Trade reply text, e.g. "bought 10 RELIANCE @ 2950"')
    args = parser.parse_args(argv)

    from config.startup import StartupService

    StartupService().start()

    if args.message:
        print(format_reply_result(handle_reply(" ".join(args.message))))
        return

    demo_replies = [
        "bought 10 RELIANCE @ 2950",
        "sold 10 RELIANCE @ 3050",
        "picked up a few AAPL shares this morning, paid around 190 each",
    ]
    for reply in demo_replies:
        result = handle_reply(reply)
        print(f"{reply!r}\n  -> {result}")


if __name__ == "__main__":
    main()
