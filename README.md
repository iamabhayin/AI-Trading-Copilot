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

| Phase | What gets built | Notes |
|---|---|---|
| 0 | Validation phase — 5-stock watchlist, Ollama for filtering/extraction + Claude Haiku for signal synthesis, paper trading only | Cheap go/no-go gate before real spend or real trading |
| 1 | Data fetch + indicator engine (plain Python, console output) | No OpenClaw yet — validate data pipeline first |
| 2 | Signal skill with Claude API call | Still console-only, sanity-check suggestions |
| 3 | Install OpenClaw on VPS, wire Telegram notify skill | First real message delivery |
| 4 | Position tracker (reply parsing → SQLite) | "Bought X" flow working end-to-end |
| 5 | Monitor skill (code tripwire + agent risk judgment) | Light daily/twice-daily check, not tight polling |
| 6 | Chat skill (conversational Q&A) | Free-form follow-up questions |
| 7 | Cleanup skill (manual command) | Final piece |
| 8+ | Discord dual-channel rollout, refinements | Adds Discord as organized reading/logging surface once Telegram flow is trusted |

## Notes

- No auto-execution of trades — the bot only advises and monitors; you always buy/sell manually.
- Secrets (API keys, tokens) live only in `.env`, never in the repo.
