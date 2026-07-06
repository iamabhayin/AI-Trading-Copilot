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
| 3 | Install OpenClaw, wire Telegram notify skill | First real message delivery | ✅ Done — merged into `develop` (PR #5). Live Telegram delivery confirmed working |
| 4 | Position tracker (reply parsing → SQLite) | "Bought X" flow working end-to-end | ✅ Done — merged into `develop` (PR #7) |
| 5 | Monitor skill (code tripwire + agent risk judgment) | Light daily/twice-daily check, not tight polling | ✅ Done — merged into `develop` (PR #10) |
| 6 | Chat skill (conversational Q&A) | Free-form follow-up questions | ✅ Done — merged into `develop` (PR #12) |
| 7 | Cleanup skill (manual command) | Final piece | ✅ Done — merged into `develop` (PR #14) |
| 8 | Discord dual-channel rollout, refinements | Adds Discord as organized reading/logging surface once Telegram flow is trusted | ✅ Done — merged into `main` (PR #16) |
| 9 | Inbound message routing (CLI entry points, OpenClaw agent dispatch) | Turns Discord/Telegram replies into real Position Tracker / Chat Skill calls | ✅ Done — merged into `develop` (PR #18) |
| 10 | 1h timeframe migration + live 3x/day scheduler | Real cron jobs on the OpenClaw Gateway, not just config | ✅ Done — merged into `develop` (PR #19) |

## Phase Status

Detail behind the Build order table above — what each completed skill does, which files it touches, and what's tested. See `ai-trading-copilot-architecture.md` Section 3 for the full design rationale behind each skill.

### Phase 1 — Data Fetch + Indicator Engine

- **`skills/data_fetch_skill.py`** — pulls OHLCV candles (yfinance for now; a later phase swaps in the live broker API behind the same `fetch_ohlcv` signature), pulls news headlines (NewsAPI, falling back to Finnhub), and runs a coarse Ollama relevance filter on those headlines. Returns `[]` for news if no provider key is configured, and fails open (passes headlines through unfiltered) if `OLLAMA_HOST` is unset or unreachable — the pipeline still runs end-to-end without either. Defaults to `1h` candles (see Phase 10).
- **`skills/indicator_engine.py`** — computes exactly 3 indicators via `pandas-ta`: RSI-14, an EMA(14-day)/EMA(50-day) pair, and Bollinger Bands (20-period). "14-day"/"50-day" are calendar trading days, not candle counts — a `TRADING_HOURS_PER_DAY = 6.25` constant converts them to the right candle lookback for whatever `timeframe` is passed (e.g. ~88/~312 candles on `1h`). See Phase 10 for why this replaced an earlier, broader indicator set.
- **Tested:** `tests/test_data_fetch_skill.py` (8 tests — OHLCV column normalization, empty-result handling, NewsAPI parsing, Ollama relevance filtering including its fail-open path) and `tests/test_indicator_engine.py` (16 tests — expected columns present, RSI/Bollinger periods staying fixed across timeframes, the calendar-day→candle conversion, missing-column validation, timeframe tagging/default). All network calls (yfinance, requests) are mocked.

### Phase 2 — Signal Skill

- **`skills/signal_skill.py`** — sends indicator + relevance-filtered news context to Claude (`claude-opus-4-8`), forcing structured JSON output via `output_config.format` (a schema, not a prefill) so the response always matches `{ticker, action, entry, stop_loss, target, confidence, rationale}` exactly. Persists suggestions to the `suggestions` table and ranks non-HOLD suggestions into a confidence-sorted shortlist. `main()` (added in Phase 10) is the actual scheduled pipeline entry point: fetch → indicators → signal → save → notify the shortlist, with per-ticker error isolation so one bad/delisted ticker can't take down the whole watchlist run.
- **Tested:** `tests/test_signal_skill.py` (7 tests — structured response parsing, refusal handling, DB insert parameters, shortlist ranking/filtering/limit, the pipeline notifying only shortlisted suggestions, per-ticker failure isolation). The Anthropic client is mocked.

### Phase 3 — Telegram Notify Skill

- **`skills/notify_skill.py`** — formats a suggestion into a Telegram message (deterministic template by default, with an optional Ollama phrasing pass that fails open to the template if Ollama isn't configured/reachable), then sends it via `python-telegram-bot`'s async `Bot.send_message` (wrapped in `asyncio.run` for a synchronous call site). Discord routing was added later, in Phase 8 (see below).
- **Tested:** `tests/test_notify_skill.py` (10 of its 17 tests — template formatting for BUY vs. HOLD, Ollama phrasing path and its fail-open behavior, missing-token/chat-id validation, the actual Telegram send call; the other 7 are Phase 8's Discord routing tests). Telegram and Discord are both mocked in tests, but real credentials are configured and live delivery is confirmed working (see Phase 10's scheduled-run verification).

### Phase 4 — Position Tracker

- **`skills/position_tracker_skill.py`** — parses free-form trade replies ("bought 10 RELIANCE @ 2950") via regex first, falling back to Ollama for phrasing regex can't match (fails open to `"unparsed"` rather than guessing). Opens a new `active` position linked to the most recent matching suggestion (for its stop-loss/target), and closes the most recent active position for a ticker on a "sold" reply, setting `exit_price`/`exit_time`. `main(argv)` (added in Phase 9) is a real CLI entry point — `python skills/position_tracker_skill.py "bought 10 RELIANCE @ 2950"` — used by the OpenClaw agent to dispatch real inbound messages.
- **Tested:** `tests/test_position_tracker_skill.py` (24 tests — regex matching across phrasing variants, the Ollama fallback and its fail-open path, position open/close DB writes, the top-level reply-handling dispatch, and the CLI entry point). Verified once against a real local SQLite DB (not just mocks): an open followed by a close correctly updated the same row.

### Phase 5 — Monitor Skill

- **`skills/monitor_skill.py`** — `check_price_trigger()` is a deterministic, LLM-free stop-loss/target check against each active position's latest price, deliberately decoupled from `evaluate_risk_judgment()` (Claude, structured JSON) so the safety-critical price check still fires even if the Claude call fails. This codebase has no real broker GTT/OCO order integration yet, so this is a backup/visibility check, not the primary safety net the architecture doc describes. Both alert types dedup via the `alerts_sent` table.
- **Tested:** `tests/test_monitor_skill.py` (14 tests — price-trigger boundaries, risk-judgment parsing/refusal, dedup, and the price check surviving a Claude failure). Verified end-to-end against a real local DB: seeded a real position, forced a target breach, confirmed the alert fired and persisted even with the Claude call failing openly, and a second run correctly deduped.

### Phase 6 — Chat Skill

- **`skills/chat_skill.py`** — `known_tickers()` pools the watchlist with every ticker ever suggested/positioned/discussed; `extract_ticker()` matches an explicit mention against that set, falling back to the implicit last-discussed ticker. `answer_question()` re-fetches a fresh snapshot, builds a prompt from question + original suggestion + snapshot + recent history, and calls Claude for a plain-text answer (not structured JSON, unlike Signal/Monitor — this is free-form conversation). Persists both turns to `conversations`. `main(argv)` (added in Phase 9) is a real CLI entry point — `python skills/chat_skill.py "why this stop-loss?"` — used by the OpenClaw agent to dispatch real inbound questions.
- **Tested:** `tests/test_chat_skill.py` (19 tests — ticker resolution, prompt construction, the no-ticker-found path, refusal handling, the CLI entry point). Verified end-to-end against a real local DB and live watchlist data.

### Phase 7 — Cleanup Skill

- **`skills/cleanup_skill.py`** — `cleanup()` wipes `alerts_sent` → closed `positions` → `conversations` → nulls `suggestion_id` on kept active positions → wipes `suggestions`, in one transaction (order matters for foreign key integrity). Active positions are never deleted. Manual-only, never scheduled.
- **Also fixed a real bug found while building this**: `db/database.py`'s `get_connection()` never actually set `PRAGMA foreign_keys = ON` — it's a per-connection SQLite setting, so `schema.sql`'s PRAGMA only ever applied inside `init_db()`'s own connection, meaning FK constraints had been silently unenforced since Phase 1 across every skill.
- **Tested:** `tests/test_cleanup_skill.py` (5 tests, including a real-DB integration test with actual FK enforcement, not just mocks — a wrong deletion order would raise `sqlite3.IntegrityError` there). Verified end-to-end against a real local DB with all 4 tables seeded.

### Phase 8 — Discord Dual-Channel Routing

- **`skills/notify_skill.py`** — `route_message(message_type, text)` is the shared routing layer: every message always goes to Telegram (primary action channel), and if Discord is configured for that message type, it also goes to the matching Discord channel (`market_news` / `signal` / `monitoring` / `chat` / `closed_trade`, mapped to the `DISCORD_CHANNEL_*` env vars — see the proposed channel split in the architecture doc's Section 3.4). `send_discord_message()` uses a one-shot `discord.Client` subclass (Discord's API is bot-client-based, unlike Telegram's stateless REST API) that connects, sends one message, and disconnects. A Discord failure is caught and logged but never blocks the Telegram send, since Telegram is primary. `monitor_skill.py` and `cleanup_skill.py` were updated to call `route_message()` instead of `send_telegram_message()` directly, so alerts route to `#monitoring` and cleanup confirmations still reach Telegram (no Discord channel is mapped for cleanup — an unmapped type just skips Discord).
- **Tested:** `tests/test_notify_skill.py` grew by 7 tests (Discord send validation, routing to the right channel, skipping unmapped types, Discord failures not blocking Telegram). Verified end-to-end with real network calls: a fake Discord token produced Discord's actual "Improper token has been passed" rejection (proving a real connection attempt, not a mock), which was caught cleanly while the Telegram send still completed.
- Two-way listening (a live bot routing incoming Discord/Telegram messages to Position Tracker / Chat Skill) was *not yet* built as of Phase 8 — that gap was closed in Phase 9, below.

### Phase 9 — Inbound Message Routing

- **CLI entry points**: `position_tracker_skill.py` and `chat_skill.py` gained real `main(argv)` functions (see Phase 4/6 above), replacing their old hardcoded demo `__main__` blocks, so they can be invoked with an arbitrary real message from the command line — the piece that makes them callable by an external agent at all.
- **`BOOT.md` + `AGENTS.md`** (in the OpenClaw workspace, not this repo) give the OpenClaw Discord agent plain-language routing rules: if a message looks like a trade confirmation, run `position_tracker_skill.py`; if it's a question, run `chat_skill.py`; otherwise respond conversationally. `AGENTS.md` carries the *standing* rule since `boot-md` only fires once at gateway startup, not per message.
- **Scoped the agent's `exec` tool** to only these two scripts (with a message argument), not arbitrary shell commands — since Discord input is technically untrusted even from a single known user. This scoping mattered again in Phase 10 (see below).
- Also fixed an Ollama request timeout that was too tight (15s → 30s) for slower local inference.
- Verified live end-to-end: a real `@mention` in Discord correctly triggered `position_tracker_skill.py`/`chat_skill.py` and replied with the real result.

### Phase 10 — 1h Timeframe Migration + Live Scheduler

- **Indicator engine migrated from `1d` to `1h` candles**, and trimmed from 7 computed indicators down to exactly the 3 actually used for trading (see Phase 1's updated description above) — dropped MACD, ADX, the old EMA-20/50 crossover flag, volume anomaly, and rolling support/resistance.
- **`signal_skill.py` now notifies its own shortlist** — previously neither it nor `notify_skill.py` actually sent a notification for a completed pipeline run; now `main()` calls `notify_suggestion()` for each shortlisted (non-HOLD) suggestion, making it the real, complete scheduled pipeline entry point.
- **The scheduler is genuinely live**: 4 real jobs on the OpenClaw Gateway (via `openclaw cron add`, not `openclaw.config.yaml` — that file is a documentation-only placeholder OpenClaw never reads) — `pre_market_run` (08:30 IST), `midday_run` (12:00 IST), `pre_close_run` (14:45 IST), and `monitor_check` (15:35 IST), all Mon–Fri. Jobs are plain command payloads (direct `argv`, not a shell string) rather than agent-turn jobs, so they run deterministically without depending on a shell binary or routing through the agent's (deliberately narrow, see Phase 9) exec-approvals allowlist.
- Verified live end-to-end: a real scheduled run produced a real BUY suggestion that was delivered to the configured Discord signals channel, and the position-monitor job ran clean with no open positions.

### Test summary

115 tests total (includes 5 for `config/startup.py`'s `StartupService`, added during the structure refactor between Phases 4 and 5). Run with `pytest`.

## Notes

- No auto-execution of trades — the bot only advises and monitors; you always buy/sell manually.
- Secrets (API keys, tokens) live only in `.env`, never in the repo.
