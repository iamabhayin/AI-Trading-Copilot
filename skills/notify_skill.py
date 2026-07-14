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

Phase 3 wired the Telegram half; Phase 8 adds Discord as a second,
routed target reusing the same skill.
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord
import requests
from telegram import Bot
from telegram.constants import ParseMode

from config.settings import Settings
from db.database import fetch_one

# Maps a message type to the Config field holding that Discord channel's ID
# — mirrors the proposed #market-news/#signals/#monitoring/#chat/#closed-trades
# split. A type with no entry here (or no channel ID configured) just skips
# Discord and goes to Telegram only.
DISCORD_CHANNEL_FIELD_BY_TYPE = {
    "market_news": "discord_channel_market_news",
    "metals_price": "discord_channel_market_news",
    "signal": "discord_channel_signals",
    "monitoring": "discord_channel_monitoring",
    "chat": "discord_channel_chat",
    "closed_trade": "discord_channel_closed_trades",
}


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
            timeout=30,
        )
        resp.raise_for_status()
        polished = resp.json().get("response", "").strip()
        return polished or base_message
    except requests.RequestException:
        return base_message  # fail open — better a plain message than none at all


async def send_telegram_message(
    text: str, chat_id: str | None = None, parse_mode: str | None = ParseMode.MARKDOWN
) -> None:
    """Send `text` to the configured Telegram chat via the Bot API.

    `parse_mode` defaults to Markdown for hand-formatted messages (bold
    tickers, etc.). Pass `None` for deterministic text that isn't meant to
    contain markdown — e.g. monitor alerts, whose messages embed literal
    field names like "stop_loss": a single underscore reads as an unclosed
    italic entity to Telegram's Markdown parser and raises `BadRequest`,
    silently killing delivery (found live — every stop-loss alert was
    crashing before reaching Telegram).
    """
    settings = Settings.load()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN must be set to send Telegram notifications")

    target_chat_id = chat_id or settings.telegram_chat_id
    if not target_chat_id:
        raise ValueError("TELEGRAM_CHAT_ID must be set to send Telegram notifications")

    bot = Bot(token=settings.telegram_bot_token)
    await bot.send_message(chat_id=target_chat_id, text=text, parse_mode=parse_mode)


class _OneShotDiscordClient(discord.Client):
    """Connects, sends exactly one message to one channel, and disconnects.

    Discord's API is bot-client-based (unlike Telegram's stateless REST
    Bot API) — even a single outbound message needs a client that logs in,
    waits for `on_ready`, then sends. This throwaway client exists purely
    so a single send can be awaited from plain synchronous callers instead
    of running a persistent bot process.
    """

    def __init__(self, message: str, channel_id: int, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.message = message
        self.channel_id = channel_id

    async def on_ready(self) -> None:
        channel = self.get_channel(self.channel_id) or await self.fetch_channel(self.channel_id)
        await channel.send(self.message)
        await self.close()


async def send_discord_message(text: str, channel_id: str) -> None:
    """Send `text` to a specific Discord channel via a one-shot bot client."""
    settings = Settings.load()
    if not settings.discord_bot_token:
        raise ValueError("DISCORD_BOT_TOKEN must be set to send Discord notifications")
    if not channel_id:
        raise ValueError("A Discord channel ID must be configured to send this notification")

    client = _OneShotDiscordClient(text, int(channel_id), intents=discord.Intents.default())
    await client.start(settings.discord_bot_token)


def route_message(
    message_type: str,
    text: str,
    telegram_chat_id: str | None = None,
    send_telegram: bool = True,
    telegram_parse_mode: str | None = ParseMode.MARKDOWN,
) -> None:
    """Send `text` to Telegram (the primary action channel, every message
    type by default) and, if Discord is configured for `message_type`,
    also to the matching Discord channel (the secondary organized reading
    surface).

    The pipeline that produces a message calls this once; deciding where
    it goes lives here, not in the caller (Section 3.4) — a Discord send
    failure never blocks the Telegram send, since Telegram is primary.
    `send_telegram=False` is for passive, non-actionable digests (e.g. the
    metals price digest) that belong on the Discord reading surface only.
    `telegram_parse_mode=None` is for deterministic plain-text messages
    that aren't meant to contain markdown (see `send_telegram_message`).
    """
    if send_telegram:
        asyncio.run(send_telegram_message(text, chat_id=telegram_chat_id, parse_mode=telegram_parse_mode))

    settings = Settings.load()
    channel_field = DISCORD_CHANNEL_FIELD_BY_TYPE.get(message_type)
    channel_id = getattr(settings, channel_field, "") if channel_field else ""
    if not (settings.discord_bot_token and channel_id):
        return

    try:
        asyncio.run(send_discord_message(text, channel_id))
    except Exception as exc:
        print(f"  (not sent to Discord #{message_type}: {exc})")


def notify_suggestion(suggestion: dict) -> None:
    """Format and route a single suggestion — Telegram always, plus the
    #signals Discord channel if configured. The synchronous entry point
    used by plain-Python callers (Signal Skill console script, later the
    OpenClaw scheduler).
    """
    route_message("signal", format_suggestion_message(suggestion))


def build_market_news_digest(watchlist: list[str]) -> str:
    """Build a plain-text roundup of relevant recent headlines per
    watchlist ticker, reusing the same fetch + Ollama relevance filter as
    the main pipeline. Deterministic formatting, no LLM phrasing pass —
    this is a headline roundup, not a trade suggestion.
    """
    from skills.data_fetch_skill import fetch_news, filter_news_relevance

    sections = []
    for ticker in watchlist:
        relevant = [item for item in filter_news_relevance(ticker, fetch_news(ticker)) if item.get("relevant")]
        if not relevant:
            continue
        headlines = "\n".join(f"- {item['headline']}" for item in relevant[:3])
        sections.append(f"*{ticker}*\n{headlines}")

    if not sections:
        return "*Market News Digest*\nNo notable headlines for your watchlist right now."
    return "*Market News Digest*\n\n" + "\n\n".join(sections)


def notify_market_news_digest(watchlist: list[str]) -> None:
    """Build and send the market news digest — Telegram always, plus
    #market-news on Discord if configured. The synchronous entry point
    used by the morning/evening scheduled cron jobs.
    """
    route_message("market_news", build_market_news_digest(watchlist))


def main(argv: list[str] | None = None) -> None:
    """CLI/cron entry point.

    `python notify_skill.py --digest` sends the market news digest for the
    configured watchlist. With no arguments, falls back to notifying the
    latest suggestion in the DB (or a demo message) — unchanged from before.
    """
    from config.startup import StartupService

    args = argv if argv is not None else sys.argv[1:]
    settings = StartupService().start()

    if "--digest" in args:
        notify_market_news_digest(settings.watchlist or ["AAPL"])
        print("Digest sent.")
        return

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


if __name__ == "__main__":
    main()
