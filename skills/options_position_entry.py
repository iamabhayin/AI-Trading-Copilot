"""Options Position Entry (Phase 15 Task 6) — Discord reply parsing for
NIFTY options trades, mirroring skills/position_tracker_skill.py's
regex-first + Ollama-fallback pattern for the equity pipeline.

Design choice: opening a position REQUIRES a matching BUY_CE_CANDIDATE/
BUY_PE_CANDIDATE advisory already in `options_advisories` (same expiry +
strike) — that's where the original thesis (Section 47) and the live
lot size come from. If no matching advisory exists, the reply is
rejected rather than guessing a lot size or fabricating a thesis (rule:
"never invent data"/never invent a thesis). In practice this is never a
real constraint — the whole point of the engine is that you only enter
a position because it just told you to.
"""

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

# e.g. "bought NIFTY 24JUL2025 25200 CE at 145", "sold 2 lots NIFTY
# 24JUL2025 25200 CE @ 210"
OPTIONS_TRADE_PATTERN = re.compile(
    r"(?P<action>bought|buy|sold|sell|closed?|long)\s+"
    r"(?:(?P<qty>\d+)\s+lots?\s+)?"
    r"nifty\s+"
    r"(?P<expiry>\d{1,2}[a-z]{3}\d{2,4})\s+"
    r"(?P<strike>\d+(?:\.\d+)?)\s*"
    r"(?P<side>ce|pe)\s+"
    r"(?:@|at)\s*"
    r"(?:₹|rs\.?)?\s*"
    r"(?P<price>\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def _normalize_expiry(raw: str) -> str:
    """User-typed expiry like '24jul25' or '24Jul2025' -> Angel One's
    scrip-master DDMMMYYYY format (e.g. '24JUL2025')."""
    raw = raw.strip().upper()
    day, month, year = raw[:2], raw[2:5], raw[5:]
    if len(year) == 2:
        year = "20" + year
    return f"{day}{month}{year}"


def parse_options_trade_reply(message: str) -> dict | None:
    """Extract {action, expiry, strike, side, qty_lots, price} via regex.
    Returns None if the pattern doesn't match — caller falls back to
    `parse_options_trade_reply_with_ollama`."""
    match = OPTIONS_TRADE_PATTERN.search(message)
    if not match:
        return None
    side = "buy" if match.group("action").lower() in BUY_WORDS else "sell"
    return {
        "action": side,
        "expiry": _normalize_expiry(match.group("expiry")),
        "strike": float(match.group("strike")),
        "side": match.group("side").upper(),
        "qty_lots": int(match.group("qty")) if match.group("qty") else 1,
        "price": float(match.group("price")),
    }


def parse_options_trade_reply_with_ollama(message: str) -> dict | None:
    """Fallback extraction for phrasing the regex can't handle. Returns
    None if OLLAMA_HOST isn't configured, unreachable, or the response
    isn't a well-formed trade."""
    settings = Settings.load()
    if not settings.ollama_host:
        return None

    prompt = (
        "Extract the NIFTY options trade action from this message. Respond "
        'with ONLY a JSON object of the shape {"action": "buy"|"sell", '
        '"expiry": str (DDMMMYYYY, e.g. "24JUL2025"), "strike": number, '
        '"side": "CE"|"PE", "qty_lots": number, "price": number}. If the '
        "message does not describe a completed NIFTY options trade, "
        "respond with exactly: null\n\n"
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

    required = {"action", "expiry", "strike", "side", "price"}
    if not isinstance(parsed, dict) or not required.issubset(parsed):
        return None
    return {
        "action": str(parsed["action"]).lower(),
        "expiry": _normalize_expiry(str(parsed["expiry"])),
        "strike": float(parsed["strike"]),
        "side": str(parsed["side"]).upper(),
        "qty_lots": int(parsed.get("qty_lots") or 1),
        "price": float(parsed["price"]),
    }


def parse_options_trade(message: str) -> dict | None:
    """Parse a free-form NIFTY options trade reply: regex first, Ollama
    fallback."""
    return parse_options_trade_reply(message) or parse_options_trade_reply_with_ollama(message)


def open_position(trade: dict) -> int | None:
    """Opens a new active options_positions row, sourcing its thesis and
    live lot size from the most recent matching BUY_CE/PE advisory.
    Returns None (never opens) if no matching advisory exists — see
    module docstring."""
    action = "BUY_CE_CANDIDATE" if trade["side"] == "CE" else "BUY_PE_CANDIDATE"
    advisory = fetch_one(
        "SELECT payload_json FROM options_advisories WHERE action = ? ORDER BY created_ts DESC LIMIT 1",
        (action,),
    )
    if not advisory:
        return None

    payload = json.loads(advisory["payload_json"])
    if payload.get("expiry") != trade["expiry"] or float(payload.get("strike", -1)) != trade["strike"]:
        return None

    lot_size = payload.get("lot_size")
    if not lot_size:
        return None

    contract = f"NIFTY {trade['strike']:g} {trade['side']} {trade['expiry']}"
    return execute(
        """
        INSERT INTO options_positions
            (status, contract, expiry_date, strike, side, qty_lots, lot_size,
             entry_premium, entry_spot, thesis_json)
        VALUES ('active', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            contract,
            trade["expiry"],
            trade["strike"],
            trade["side"],
            trade["qty_lots"],
            lot_size,
            trade["price"],
            payload.get("spot"),
            json.dumps(payload),
        ),
    )


def close_position(trade: dict) -> int | None:
    """Closes the most recently opened active position matching this
    trade's (expiry, strike, side). Returns None if no such active
    position exists."""
    position = fetch_one(
        """
        SELECT id FROM options_positions
        WHERE status = 'active' AND expiry_date = ? AND strike = ? AND side = ?
        ORDER BY opened_ts DESC LIMIT 1
        """,
        (trade["expiry"], trade["strike"], trade["side"]),
    )
    if not position:
        return None

    execute(
        "UPDATE options_positions SET status = 'closed', exit_premium = ?, closed_ts = datetime('now') WHERE id = ?",
        (trade["price"], position["id"]),
    )
    return position["id"]


def handle_options_reply(message: str) -> dict:
    """Top-level entry point: parse a reply and apply it to
    options_positions. Returns a small result dict for the caller
    (OpenClaw agent dispatch) to relay back to the user."""
    trade = parse_options_trade(message)
    if trade is None:
        return {"status": "unparsed", "message": message}

    if trade["action"] == "buy":
        position_id = open_position(trade)
        if position_id is None:
            return {"status": "no_matching_advisory", **trade}
        return {"status": "opened", "position_id": position_id, **trade}

    position_id = close_position(trade)
    if position_id is None:
        return {"status": "no_active_position", **trade}
    return {"status": "closed", "position_id": position_id, **trade}


def format_options_reply_result(result: dict) -> str:
    """Render a handle_options_reply() result as a clean, human-readable
    confirmation — callers relay this text back to the user."""
    status = result["status"]
    if status == "unparsed":
        return f"Could not recognize a NIFTY options trade in: {result['message']!r}"
    if status == "no_matching_advisory":
        return f"No matching BUY advisory found for {result['expiry']} {result['strike']:g} {result['side']} -- not recorded."
    if status == "no_active_position":
        return f"No active {result['expiry']} {result['strike']:g} {result['side']} position found to close."

    action_word = "Bought" if result["action"] == "buy" else "Sold"
    verb = "Opened" if status == "opened" else "Closed"
    return (
        f"{verb} position #{result['position_id']}: {action_word} {result['qty_lots']} lot(s) "
        f"NIFTY {result['expiry']} {result['strike']:g} {result['side']} @ {result['price']:g}"
    )


def main(argv: list[str] | None = None) -> None:
    """CLI entry point.

    `python options_position_entry.py "bought NIFTY 24JUL2025 25200 CE at 145"`
    parses and records a real trade reply. Run with no arguments to fall
    back to the hardcoded demo, for manual testing without a real message.
    """
    parser = argparse.ArgumentParser(description="Parse a NIFTY options trade reply and record it in options_positions.")
    parser.add_argument("message", nargs="*", help='e.g. "bought NIFTY 24JUL2025 25200 CE at 145"')
    args = parser.parse_args(argv)

    from config.startup import StartupService

    StartupService().start()

    if args.message:
        print(format_options_reply_result(handle_options_reply(" ".join(args.message))))
        return

    demo_replies = [
        "bought NIFTY 24JUL2025 25200 CE at 145",
        "sold NIFTY 24JUL2025 25200 CE at 210",
    ]
    for reply in demo_replies:
        result = handle_options_reply(reply)
        print(f"{reply!r}\n  -> {result}")


if __name__ == "__main__":
    main()
