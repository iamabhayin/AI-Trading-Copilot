# AI Trading Copilot

A personal swing/positional trading assistant: researches a watchlist pre/post market, suggests BUY/SELL candidates with entry/stop-loss/target, tracks positions you actually take, monitors them for risk, and answers free-form questions — all grounded in live data. See the architecture doc for full design details.

## Prerequisites

- **Python 3.11+**
- **Node.js 22+** (24 recommended) — required for [OpenClaw](https://openclaw.ai), the orchestration layer used from Phase 3 onward
- An Anthropic API key (Claude)
- Optionally: broker API credentials, Alpha Vantage / NewsAPI / Finnhub keys, Telegram bot token, Discord bot token — see `.env.example`

## Setup

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Copy the env template and fill in real values (never commit `.env`):
   ```bash
   cp .env.example .env
   ```
3. Initialize the SQLite database (creates the `suggestions`, `positions`, `conversations`, `alerts_sent` tables):
   ```bash
   python db/database.py
   ```

## Project layout

```
config/     Pydantic-validated Config/ConfigLoader (settings.py) + StartupService (startup.py)
skills/     one module per skill (data fetch, indicators, signal, notify, position tracker, monitor, chat, cleanup)
db/         schema.sql + sqlite3 connection/query helpers
tests/      one pytest file per skill (see Phase Status below for what's covered)
logs/       runtime logs (gitignored)
pyproject.toml         ruff + pytest config
openclaw.config.yaml   scheduler + skill registration for OpenClaw
```

## Build order

Each phase is its own branch off `develop` (see repo's branching strategy), merged back via PR once tested.

| Phase | What gets built | Notes | Status |
|---|---|---|---|
| 0 | Validation phase — 5-stock watchlist, Ollama for filtering/extraction + Claude Haiku for signal synthesis, paper trading only | Cheap go/no-go gate before real spend or real trading | Skipped |
| 1 | Data fetch + indicator engine (plain Python, console output) | No OpenClaw yet — validate data pipeline first | ✅ Done — merged into `develop` (PR #1) |
| 2 | Signal skill with Claude API call | Still console-only, sanity-check suggestions | ✅ Done — merged into `develop` (PR #3) |
| 3 | Install OpenClaw on VPS, wire Telegram notify skill | First real message delivery | ✅ Skill code done and merged into `develop` (PR #5). OpenClaw VPS install and live Telegram credentials are still pending manual setup — see below |
| 4 | Position tracker (reply parsing → SQLite) | "Bought X" flow working end-to-end | ✅ Done — merged into `develop` (PR #7) |
| 5 | Monitor skill (code tripwire + agent risk judgment) | Light daily/twice-daily check, not tight polling | ✅ Done — merged into `develop` (PR #10) |
| 6 | Chat skill (conversational Q&A) | Free-form follow-up questions | ✅ Done — merged into `develop` (PR #12) |
| 7 | Cleanup skill (manual command) | Final piece | ✅ Done — merged into `develop` (PR #14) |
| 8+ | Discord dual-channel rollout, refinements | Adds Discord as organized reading/logging surface once Telegram flow is trusted | ✅ Done — merged into `main` (PR #16) |

## Phase Status

Detail behind the Build order table above — what each completed skill does, which files it touches, and what's tested. See `ai-trading-copilot-architecture.md` Section 3 for the full design rationale behind each skill.

### Phase 1 — Data Fetch + Indicator Engine

- **`skills/data_fetch_skill.py`** — pulls OHLCV candles (yfinance for now; a later phase swaps in the live broker API behind the same `fetch_ohlcv` signature), pulls news headlines (NewsAPI, falling back to Finnhub), and runs a coarse Ollama relevance filter on those headlines. Returns `[]` for news if no provider key is configured, and fails open (passes headlines through unfiltered) if `OLLAMA_HOST` is unset or unreachable — the pipeline still runs end-to-end without either.
- **`skills/indicator_engine.py`** — computes RSI, MACD, EMA-20/50 crossover, Bollinger Bands, ADX, a volume-anomaly flag, and rolling support/resistance via `pandas-ta`, tagged with the configurable timeframe (`1h`/`4h`/`1d`/`1wk`).
- **Tested:** `tests/test_data_fetch_skill.py` (8 tests — OHLCV column normalization, empty-result handling, NewsAPI parsing, Ollama relevance filtering including its fail-open path) and `tests/test_indicator_engine.py` (8 tests — expected columns present, RSI bounds, support ≤ resistance, missing-column validation, timeframe tagging). All network calls (yfinance, requests) are mocked.

### Phase 2 — Signal Skill

- **`skills/signal_skill.py`** — sends indicator + relevance-filtered news context to Claude (`claude-opus-4-8`), forcing structured JSON output via `output_config.format` (a schema, not a prefill) so the response always matches `{ticker, action, entry, stop_loss, target, confidence, rationale}` exactly. Persists suggestions to the `suggestions` table and ranks non-HOLD suggestions into a confidence-sorted shortlist.
- **Tested:** `tests/test_signal_skill.py` (5 tests — structured response parsing, refusal handling, DB insert parameters, shortlist ranking/filtering/limit). The Anthropic client is mocked.

### Phase 3 — Telegram Notify Skill

- **`skills/notify_skill.py`** — formats a suggestion into a Telegram message (deterministic template by default, with an optional Ollama phrasing pass that fails open to the template if Ollama isn't configured/reachable), then sends it via `python-telegram-bot`'s async `Bot.send_message` (wrapped in `asyncio.run` for a synchronous call site). Discord routing was added later, in Phase 8 (see below).
- **Tested:** `tests/test_notify_skill.py` (10 tests — template formatting for BUY vs. HOLD, Ollama phrasing path and its fail-open behavior, missing-token/chat-id validation, the actual Telegram send call). Telegram and Ollama are both mocked — no real message has been sent from this environment (no bot token configured); see Pending manual setup below.

### Phase 4 — Position Tracker

- **`skills/position_tracker_skill.py`** — parses free-form trade replies ("bought 10 RELIANCE @ 2950") via regex first, falling back to Ollama for phrasing regex can't match (fails open to `"unparsed"` rather than guessing). Opens a new `active` position linked to the most recent matching suggestion (for its stop-loss/target), and closes the most recent active position for a ticker on a "sold" reply, setting `exit_price`/`exit_time`.
- **Tested:** `tests/test_position_tracker_skill.py` (18 tests — regex matching across phrasing variants, the Ollama fallback and its fail-open path, position open/close DB writes, and the top-level reply-handling dispatch). Verified once against a real local SQLite DB (not just mocks): an open followed by a close correctly updated the same row.

### Phase 5 — Monitor Skill

- **`skills/monitor_skill.py`** — `check_price_trigger()` is a deterministic, LLM-free stop-loss/target check against each active position's latest price, deliberately decoupled from `evaluate_risk_judgment()` (Claude, structured JSON) so the safety-critical price check still fires even if the Claude call fails. This codebase has no real broker GTT/OCO order integration yet, so this is a backup/visibility check, not the primary safety net the architecture doc describes. Both alert types dedup via the `alerts_sent` table.
- **Tested:** `tests/test_monitor_skill.py` (14 tests — price-trigger boundaries, risk-judgment parsing/refusal, dedup, and the price check surviving a Claude failure). Verified end-to-end against a real local DB: seeded a real position, forced a target breach, confirmed the alert fired and persisted even with the Claude call failing openly, and a second run correctly deduped.

### Phase 6 — Chat Skill

- **`skills/chat_skill.py`** — `known_tickers()` pools the watchlist with every ticker ever suggested/positioned/discussed; `extract_ticker()` matches an explicit mention against that set, falling back to the implicit last-discussed ticker. `answer_question()` re-fetches a fresh snapshot, builds a prompt from question + original suggestion + snapshot + recent history, and calls Claude for a plain-text answer (not structured JSON, unlike Signal/Monitor — this is free-form conversation). Persists both turns to `conversations`.
- **Tested:** `tests/test_chat_skill.py` (16 tests — ticker resolution, prompt construction, the no-ticker-found path, refusal handling). Verified end-to-end against a real local DB and live watchlist data.

### Phase 7 — Cleanup Skill

- **`skills/cleanup_skill.py`** — `cleanup()` wipes `alerts_sent` → closed `positions` → `conversations` → nulls `suggestion_id` on kept active positions → wipes `suggestions`, in one transaction (order matters for foreign key integrity). Active positions are never deleted. Manual-only, never scheduled.
- **Also fixed a real bug found while building this**: `db/database.py`'s `get_connection()` never actually set `PRAGMA foreign_keys = ON` — it's a per-connection SQLite setting, so `schema.sql`'s PRAGMA only ever applied inside `init_db()`'s own connection, meaning FK constraints had been silently unenforced since Phase 1 across every skill.
- **Tested:** `tests/test_cleanup_skill.py` (5 tests, including a real-DB integration test with actual FK enforcement, not just mocks — a wrong deletion order would raise `sqlite3.IntegrityError` there). Verified end-to-end against a real local DB with all 4 tables seeded.

### Phase 8 — Discord Dual-Channel Routing

- **`skills/notify_skill.py`** — `route_message(message_type, text)` is the shared routing layer: every message always goes to Telegram (primary action channel), and if Discord is configured for that message type, it also goes to the matching Discord channel (`market_news` / `signal` / `monitoring` / `chat` / `closed_trade`, mapped to the `DISCORD_CHANNEL_*` env vars — see the proposed channel split in the architecture doc's Section 3.4). `send_discord_message()` uses a one-shot `discord.Client` subclass (Discord's API is bot-client-based, unlike Telegram's stateless REST API) that connects, sends one message, and disconnects. A Discord failure is caught and logged but never blocks the Telegram send, since Telegram is primary. `monitor_skill.py` and `cleanup_skill.py` were updated to call `route_message()` instead of `send_telegram_message()` directly, so alerts route to `#monitoring` and cleanup confirmations still reach Telegram (no Discord channel is mapped for cleanup — an unmapped type just skips Discord).
- **Tested:** `tests/test_notify_skill.py` grew by 7 tests (Discord send validation, routing to the right channel, skipping unmapped types, Discord failures not blocking Telegram). Verified end-to-end with real network calls: a fake Discord token produced Discord's actual "Improper token has been passed" rejection (proving a real connection attempt, not a mock), which was caught cleanly while the Telegram send still completed.
- Two-way listening (a live bot process routing incoming Discord/Telegram messages to Position Tracker / Chat Skill) is *not* built — the architecture doc frames Phase 8 as adding Discord as a "reading/logging surface," and no persistent listener exists for Telegram either yet (that's OpenClaw's job once actually deployed — see Pending manual setup).

### Test summary

96 tests total across all 8 phases. Run with `pytest`.

## Pending manual setup

Left for later, deliberately not automated by the assistant:

- **Telegram credentials.** `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env` are still empty. Create a bot via [@BotFather](https://t.me/BotFather), get your chat ID, fill in `.env`, then run `python skills/notify_skill.py` to confirm a real message actually arrives.
- **Discord credentials.** `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, and the per-channel `DISCORD_CHANNEL_*` IDs in `.env` are still empty. Create a Discord application/bot, invite it to your server with `Send Messages` permission on the channels in the proposed split (`#market-news`, `#signals`, `#monitoring`, `#chat`, `#closed-trades`), and fill in the channel IDs.
- **Install OpenClaw on a VPS.** Section 10 of the architecture doc covers the setup guide (Node.js 22+ on the VPS, OpenClaw install, scheduler wiring). Once a VPS is available, flip `scheduler.pre_market_run` / `post_market_run` / `monitor_check` to `enabled: true` in `openclaw.config.yaml` and set their `cron` values.

## Notes

- No auto-execution of trades — the bot only advises and monitors; you always buy/sell manually.
- Secrets (API keys, tokens) live only in `.env`, never in the repo.
