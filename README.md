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
config/     central Settings object, loads from .env
skills/     one module per skill (data fetch, indicators, signal, notify, position tracker, monitor, chat, cleanup)
db/         schema.sql + sqlite3 connection/query helpers
tests/      one pytest stub per skill
logs/       runtime logs (gitignored)
openclaw.config.yaml   scheduler + skill registration for OpenClaw
```

## Build order

Each phase is its own branch off `develop` (see repo's branching strategy), merged back via PR once tested.

| Phase | What gets built | Notes | Status |
|---|---|---|---|
| 0 | Validation phase — 5-stock watchlist, Ollama for filtering/extraction + Claude Haiku for signal synthesis, paper trading only | Cheap go/no-go gate before real spend or real trading | Skipped |
| 1 | Data fetch + indicator engine (plain Python, console output) | No OpenClaw yet — validate data pipeline first | ✅ Done — merged into `develop` via PR #1 |
| 2 | Signal skill with Claude API call | Still console-only, sanity-check suggestions | ✅ Done — merged into `develop` via PR #3 |
| 3 | Install OpenClaw on VPS, wire Telegram notify skill | First real message delivery | ⚠️ Code done on `phase-3-telegram-notify`, not yet merged — see [Pending manual setup](#pending-manual-setup) below (Telegram credentials + VPS install still needed) |
| 4 | Position tracker (reply parsing → SQLite) | "Bought X" flow working end-to-end | Not started |
| 5 | Monitor skill (code tripwire + agent risk judgment) | Light daily/twice-daily check, not tight polling | Not started |
| 6 | Chat skill (conversational Q&A) | Free-form follow-up questions | Not started |
| 7 | Cleanup skill (manual command) | Final piece | Not started |
| 8+ | Discord dual-channel rollout, refinements | Adds Discord as organized reading/logging surface once Telegram flow is trusted | Not started |

## Pending manual setup

Left for later, deliberately not automated by the assistant:

- **Telegram credentials.** `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` in `.env` are still empty. Create a bot via [@BotFather](https://t.me/BotFather), get your chat ID, fill in `.env`, then run `python skills/notify_skill.py` to confirm a real message actually arrives — this has only been verified up to the missing-credentials check, not with a live send.
- **Install OpenClaw on a VPS.** Section 10 of the architecture doc covers the setup guide (Node.js 22+ on the VPS, OpenClaw install, scheduler wiring). This is real infrastructure provisioning outside what can be done from a local dev session — do this once a VPS is available, then flip `scheduler.pre_market_run` / `post_market_run` to `enabled: true` in `openclaw.config.yaml` and set their `cron` values.

## Notes

- No auto-execution of trades — the bot only advises and monitors; you always buy/sell manually.
- Secrets (API keys, tokens) live only in `.env`, never in the repo.
