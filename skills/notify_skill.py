"""Notify Skill (Messaging Agent) — Section 3.4.

Agent/Model: Ollama (phrasing/formatting only — the underlying decision is
already made upstream by Claude in the Signal Skill / Monitor Skill).

Dual channel: Telegram (primary action channel — structured buy/sell
confirmations, quick chat on the go) + Discord (secondary organized
reading/logging surface, split across #market-news, #signals, #monitoring,
#chat, #closed-trades). The core signal/indicator/monitor pipeline runs
once; output is routed to the right destination based on message type — no
duplicate logic, just a routing layer. Two-way replies need a listener on
both platforms, both writing to the same SQLite tables.

Phase 3 wires the Telegram half only; Discord routing lands in Phase 8.
"""

# TODO: Phase 8 — add Discord as a second notification target, reusing the same skill

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests
from telegram import Bot
from telegram.constants import ParseMode

from config.settings import Settings
from db.database import fetch_one


def _template_message(suggestion: dict) -> str:
    """Deterministic fallback formatting — no LLM involved."""
    ticker = suggestion["ticker"]
    action = suggestion["action"]
    lines = [f"*{ticker}* — {action}"]
    if action in ("BUY", "SELL"):
        lines.append(f"Entry: {suggestion.get('entry')}")
        lines.append(f"Stop-loss: {suggestion.get('stop_loss')}")
        lines.append(f"Target: {suggestion.get('target')}")
    lines.append(f"Confidence: {suggestion.get('confidence')}")
    lines.append(f"Rationale: {suggestion.get('rationale')}")
    return "\n".join(lines)


def format_suggestion_message(suggestion: dict) -> str:
    """Turn a structured suggestion into a Telegram-ready message.

    Uses Ollama for light phrasing polish only — the underlying BUY/SELL/HOLD
    decision was already made by Claude in the Signal Skill. Falls back to a
    deterministic template if OLLAMA_HOST isn't configured or unreachable, so
    notifications still go out without a local Ollama instance.
    """
    settings = Settings.load()
    base_message = _template_message(suggestion)
    if not settings.ollama_host:
        return base_message

    prompt = (
        "Rewrite the following trade suggestion as a short, clear Telegram "
        "message. Keep every number exactly as given, keep the Markdown "
        "bold ticker, and do not add any information that isn't already "
        "present.\n\n" + base_message
    )
    try:
        resp = requests.post(
            f"{settings.ollama_host}/api/generate",
            json={"model": settings.ollama_model, "prompt": prompt, "stream": False},
            timeout=15,
        )
        resp.raise_for_status()
        polished = resp.json().get("response", "").strip()
        return polished or base_message
    except requests.RequestException:
        return base_message  # fail open — better a plain message than none at all


async def send_telegram_message(text: str, chat_id: str | None = None) -> None:
    """Send `text` to the configured Telegram chat via the Bot API."""
    settings = Settings.load()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN must be set to send Telegram notifications")

    target_chat_id = chat_id or settings.telegram_chat_id
    if not target_chat_id:
        raise ValueError("TELEGRAM_CHAT_ID must be set to send Telegram notifications")

    bot = Bot(token=settings.telegram_bot_token)
    await bot.send_message(chat_id=target_chat_id, text=text, parse_mode=ParseMode.MARKDOWN)


def notify_suggestion(suggestion: dict) -> None:
    """Format and send a single suggestion to Telegram — the synchronous
    entry point used by plain-Python callers (Signal Skill console script,
    later the OpenClaw scheduler).
    """
    message = format_suggestion_message(suggestion)
    asyncio.run(send_telegram_message(message))


if __name__ == "__main__":
    row = fetch_one("SELECT * FROM suggestions ORDER BY created_at DESC LIMIT 1")
    if row:
        suggestion = dict(row)
        suggestion["entry"] = suggestion.pop("entry_price")
        print(f"Notifying latest suggestion from DB: {suggestion['ticker']} {suggestion['action']}")
    else:
        print("No suggestions in the database yet — sending a demo notification instead.")
        suggestion = {
            "ticker": "AAPL",
            "action": "BUY",
            "entry": 190.0,
            "stop_loss": 182.0,
            "target": 210.0,
            "confidence": 0.75,
            "rationale": "Demo notification — Phase 3 Telegram wiring smoke test.",
        }

    print(format_suggestion_message(suggestion))
    notify_suggestion(suggestion)
    print("Sent.")
