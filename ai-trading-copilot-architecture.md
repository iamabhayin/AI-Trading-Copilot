# AI Trading Copilot — Architecture Plan (v1)

**Project Name: AI Trading Copilot**

## 1. Goal

A personal trading assistant that:
- Researches a given stock watchlist before market open and after market close
- Suggests BUY/SELL candidates with entry price, stop-loss, and target
- **Trading style: swing/positional** — holding periods of days to months, not intraday/same-day trading
- **Configurable chart timeframe for analysis** (e.g., 1-hour, 1-day, etc. — see Section 3.2) — not fixed to one interval
- Sends suggestions via Telegram and Discord (dual-channel — see Section 3.4 for split)
- Lets you reply with what you actually bought/sold (qty + price) to start monitoring
- Monitors active positions for stop-loss/target/news-driven exits, at a cadence appropriate for swing holding periods (not high-frequency polling)
- Supports free-form conversation ("why this stop-loss?") grounded in live data
- Runs entirely for a single user — no dashboard, no multi-user needs

---

## 2. Core Principle: Code vs. Agent Split

| Task | Who does it | Model (if Agent) | Why |
|---|---|---|---|
| Technical indicator math (RSI, MACD, EMA, Bollinger, support/resistance) | **Code** (pandas-ta) | — | Must be exact and reproducible — no hallucination risk |
| Hard stop-loss / target trigger check | **Code** | — | Needs to be instant and 100% reliable — no LLM latency/failure in the critical path |
| Chart pattern analysis | **Agent** | **Claude** | Judgment call — numeric pattern data or chart image reasoning; directly informs a trade decision |
| Market news reading & relevance (coarse filter) | **Agent** | **Ollama** | High-volume, lower-stakes first pass — filters headlines before the real synthesis step |
| Market news synthesis (final call feeding into signal) | **Agent** | **Claude** | Genuine LLM strength — sentiment, materiality, context — feeds directly into a trade decision |
| Risk judgment (beyond hard SL/target, on live positions) | **Agent** | **Claude** | "Should I exit early given this news?" directly affects real, already-committed capital |
| Notifications (phrasing/formatting) | **Agent** | **Ollama** | Wording an already-decided alert is stylistic, not a trading decision |
| Position reply parsing ("bought 10 @ 2950") | **Agent** | **Ollama** | Pure extraction task, high frequency, no real judgment involved |
| Conversational Q&A | **Agent** | **Claude** | User-facing trust — answers must be reliably grounded, not a place to cut corners |

**Rule of thumb:** Code computes the numbers. Between agents: **Ollama handles filtering, extraction, and formatting** (high-volume, reversible-if-wrong); **Claude handles synthesis, judgment, and risk calls** (low-volume, directly drives money or trust). See Section 11 for the full cost rationale behind this split.

---

## 3. System Components

### 3.1 Data Fetch (Data Agent) — News pre-filter Model: **Ollama**
- Pulls OHLCV candles from broker API (Zerodha Kite / Upstox / Fyers) or Alpha Vantage/yfinance for testing
- Pulls latest news headlines per ticker (NewsAPI / Finnhub)
- **Ollama does a coarse relevance filter** on raw headlines before they reach the Signal Skill — high volume, low stakes, cheap
- Outputs clean structured data — final news synthesis judgment still happens in Claude (Section 3.3), not here

### 3.2 Indicator Engine (Code)
- Uses `pandas-ta` to compute RSI, MACD, EMA crossovers, Bollinger Bands, ADX, volume anomalies, support/resistance
- Deterministic, auditable — no LLM involved
- **Configurable chart timeframe** — user-selectable candle interval per analysis run (e.g., `1h`, `4h`, `1d`, `1wk`), not hardcoded to one resolution
  - Swing/positional trades typically favor **daily (`1d`)** or **weekly (`1wk`)** candles for the primary trend read, with an optional **hourly (`1h`)** timeframe as a secondary check for entry timing precision
  - Timeframe is a parameter passed into the Data Fetch and Indicator Engine calls — same skill code, different interval, no separate skill needed
  - Default recommendation: `1d` as the main analysis timeframe for swing trading, with `1h` available as an opt-in secondary view
- Outputs structured indicator values per ticker, tagged with the timeframe used

### 3.3 Signal Skill (Strategy Agent) — Model: **Claude**
- Takes indicator output + news (pre-filtered by Ollama, see 3.1) + (optionally) chart image
- Sends to Claude API for synthesis
- **Forces structured JSON output**: `{ticker, action, entry, stop_loss, target, confidence, rationale}`
- Ranks/filters into a shortlist ("prominent stocks")
- Highest-stakes judgment in the system — never downgraded to Ollama, even during cost-saving/validation phases

### 3.4 Notify Skill (Messaging Agent) — Model: **Ollama** (phrasing/formatting only; underlying decision already made by Claude in 3.3/3.6) — Dual Channel: Telegram + Discord

WhatsApp is **rolled out** — not used (approval friction, per-message cost, wrong shape for a solo bot).

**Division of labor between the two platforms:**

| Platform | Role | Why |
|---|---|---|
| **Telegram** | Primary **action** channel — structured buy/sell confirmations, quick chat on the go | Lighter on mobile, native inline buttons for one-tap "bought/sold" confirmation, best fit for fast in-the-moment interaction |
| **Discord** | Secondary **organized reading/logging** surface, split across channels | Multi-channel structure lets updates be separated by type instead of one continuous stream — better for reviewing at a desk |

**Proposed Discord channel split:**
```
#market-news      → raw news feed / sentiment flags
#signals          → pre-market/post-market shortlist
#monitoring        → real-time SL/target/risk alerts
#chat             → free-form Q&A with the bot
#closed-trades     → log of exits (optional)
```

- Agent decides phrasing/urgency, not just fixed templates
- The core signal/indicator/monitor pipeline runs **once**; output is **routed** to the right destination (Telegram chat, or a specific Discord channel) based on message type — no duplicate logic, just a routing layer
- Two-way replies (buy/sell confirmation, follow-up chat) need a listener on **both** platforms, both writing to the same SQLite tables — slightly more integration surface to build/test than a single-channel setup
- **Build order**: Telegram flow built and proven first (Phases 1–4); Discord added afterward as a second notification target (Phase 8+) reusing the same skills

### 3.5 Position Tracker (Reply Parser) — Model: **Ollama**
- Listens for your reply (e.g., "bought 10 RELIANCE @ 2950")
- Parses ticker/qty/price (regex first, Ollama fallback for messy phrasing — pure extraction, no trading judgment)
- Writes to `positions` table, status = `active`

### 3.6 Monitor Skill (Risk/Guard Agent + Broker-Native Tripwire) — Agent Model: **Claude**

**Revised for swing/positional holding periods (days to months) — no longer tight-interval polling:**

- **Hard stop-loss / target**: place as a **broker-native GTT (Good Till Triggered) or OCO (One-Cancels-Other) order** at trade entry, instead of a custom polling loop. The broker's own system handles instant, guaranteed triggering — this removes the need for the bot to poll every 1–5 min just to catch a price cross, and is more reliable than a custom check that depends on your bot staying up
- **Claude**: evaluates news/fundamentals/context for early-exit or stop-loss-adjustment judgment calls — since positions are held for days to months, this runs on a much lighter cadence: e.g., **once or twice a day** (aligned with the pre-market/post-market runs) rather than every 15–30 minutes, or triggered ad hoc by a significant news event
- Tracks sent alerts to avoid duplicate spam
- **Net effect**: far fewer Claude calls than an intraday design, and less dependence on the bot's uptime for the safety-critical stop-loss trigger itself (see Section 11 for the updated cost impact)

### 3.7 Chat Skill (Conversational Agent) — Model: **Claude**
- Listens for any free-form message, not just structured replies
- Identifies which ticker you're asking about (implicit last-discussed ticker, or explicit mention)
- Re-fetches fresh data snapshot for that ticker before answering (never answers from stale memory alone)
- Builds prompt: your question + original suggestion + fresh snapshot + recent conversation history
- Replies within 1–3 seconds (acceptable latency, confirmed)
- Stays on Claude — this is user-facing trust, not a place to downgrade for cost

### 3.8 Cleanup Skill (Manual Only)
- **No scheduling** — triggered only by an explicit command from you (e.g., `/cleanup` — exact command name still TBD)
- Full wipe: `suggestions`, `conversations`, `alerts_sent`, and closed `positions`
- **Exception**: active/open positions are never deleted — protects the monitoring loop from losing track of a live trade
- Confirms back to you what was wiped and what (if anything) was kept

---

## 4. Orchestration Layer — OpenClaw

OpenClaw provides the "glue" so you don't hand-build scheduling, messaging plumbing, and cross-session memory:

- **Scheduler**: pre-market run, post-market run, and a light daily/twice-daily monitor check (no tight intraday polling loop needed — see Section 3.6 for the GTT/OCO shift)
- **Multi-channel messaging**: Telegram/Discord/WhatsApp connectors built-in
- **Persistent memory**: retains context across separate runs/messages (needed for conversation continuity and position tracking)
- **Skill architecture**: each component above is a "skill" — modular Python you write and plug in

⚠️ **Known risk (flagged earlier, still relevant):** OpenClaw's skill marketplace has had documented incidents of malicious third-party skills and framework vulnerabilities. Mitigation: only use skills you've written or personally reviewed, run on an isolated VPS, and keep the system **read-only/advisory** — no auto order execution — until trust is established.

---

## 5. Storage — SQLite (single file, no server)

```sql
suggestions      -- every recommendation ever made (ticker, action, entry, SL, target, confidence, rationale, created_at)
positions        -- what you actually did (qty, entry, SL, target, status: active/closed, entry/exit time & price)
conversations    -- chat history per ticker (role, message, timestamp) — powers follow-up Q&A
alerts_sent      -- dedup tracking so monitoring doesn't spam repeat alerts
```

- No archiving — full wipe on manual cleanup, active positions excluded
- No dashboard requirement — SQLite is sufficient at this scale

---

## 6. Open Decisions (not yet finalized)

1. **Manual cleanup command name** — e.g. `/cleanup`, `/reset`, `/newslate` (your choice)
2. **Session scope for conversations** — was discussed as daily-reset vs. persistent; superseded by "manual full wipe only," so likely a non-issue now, but worth confirming nothing else needs a shorter reset cycle
3. **Discord channel naming/structure** — proposed split in Section 3.4, open to adjustment
4. **WhatsApp** — rolled out of scope; not part of this build

---

## 7. Build Order (Recommended)

| Phase | What gets built | Notes |
|---|---|---|
| 0 | **Validation phase** — 5-stock watchlist, Ollama for filtering/extraction + Claude Haiku or Sonnet for signal synthesis, paper trading only, no real capital | Cheap go/no-go gate before any real spend or real trading — see Section 11 |
| 1 | Data fetch + indicator engine (plain Python, console output) | No OpenClaw yet — validate data pipeline first |
| 2 | Signal skill with Claude API call | Still console-only, sanity-check suggestions |
| 3 | Install OpenClaw on VPS, wire Telegram notify skill | First real message delivery |
| 4 | Position tracker (reply parsing → SQLite) | "Bought X" flow working end-to-end |
| 5 | Monitor skill (code tripwire + agent risk judgment) | Real-time loop during market hours |
| 6 | Chat skill (conversational Q&A) | Free-form follow-up questions |
| 7 | Cleanup skill (manual command) | Final piece |
| 8+ | Discord dual-channel rollout, refinements | Add Discord as organized reading/logging surface, once Telegram flow is trusted |

---

## 8. Explicit Non-Goals (for this version)

- No auto-execution of trades — you always buy/sell manually, bot only advises and monitors
- No dashboard or analytics UI
- No historical performance archive (full wipe means no long-term profitability tracking unless you explicitly ask to preserve data before a cleanup)
- No multi-user support — built for one person (you)

---

## 9. Version Control & Repo Structure (Production-Ready)

**Repo**: private Git repository.

### Branching strategy — mapped to build phases

```
main                          → always stable, deployable state
  └── develop                 → integration branch, merges from phase branches
        ├── phase-1-data-indicators
        ├── phase-2-signal-agent
        ├── phase-3-telegram-notify
        ├── phase-4-position-tracker
        ├── phase-5-monitor-risk
        ├── phase-6-chat-skill
        ├── phase-7-cleanup-skill
        └── phase-8-discord-dualchannel
```

- Each phase branch created off `develop`, merged back via PR after testing — gives a review checkpoint and clean history even for solo work
- `main` only receives merges from `develop` once a phase is fully tested and stable
- `develop` is where cross-phase integration happens (e.g., phase-5 monitoring depends on phase-4 position data)
- Tag `main` after each phase merge (`v0.1-data-pipeline`, `v0.2-signals`, etc.) for rollback points

### Repo structure

```
ai-trading-copilot/
├── .env.example              # template — real .env is gitignored, never committed
├── .gitignore                # .env, *.db, __pycache__, .venv, logs/
├── README.md                 # setup instructions, architecture summary
├── requirements.txt          # pinned dependency versions
├── config/
│   └── settings.py           # loads from .env, central config object
├── skills/
│   ├── data_fetch_skill.py
│   ├── indicator_engine.py
│   ├── signal_skill.py
│   ├── notify_skill.py
│   ├── position_tracker_skill.py
│   ├── monitor_skill.py
│   ├── chat_skill.py
│   └── cleanup_skill.py
├── db/
│   ├── schema.sql             # 4-table SQLite schema
│   └── database.py            # connection + query helpers
├── tests/
│   └── test_<skill_name>.py   # one test file per skill, matching phases
├── logs/                      # gitignored, runtime logs land here
└── openclaw.config.yaml       # scheduler/skill registration
```

### Production-ready conventions

- **Secrets never touch the repo** — API keys (broker, Claude, Telegram/Discord tokens) live in `.env`, loaded via `python-dotenv`; `.env.example` committed as a placeholder template
- **Commit convention** — `feat:`, `fix:`, `test:`, `docs:` prefixes for scannable history
- **One test file per skill** — basic shape/output tests per skill to catch regressions as later phases touch earlier code
- **README kept current** — setup steps, required environment variables, how to run each phase locally
- **`.gitignore` must catch the SQLite `.db` file** — trade history and credentials should never be committed, even accidentally

## 10. OpenClaw Setup Guide (Windows / PowerShell)

**Target environment confirmed: Windows, using PowerShell as the command line.**

> ⚠️ Note: OpenClaw's official docs recommend WSL2 for the most stable experience on Windows — native PowerShell works but is a newer, less battle-tested path. Flagging this now since it may matter once we reach Phase 5 (always-on monitoring loop), where background-service reliability counts. Steps below use native PowerShell as requested; switch to WSL2 later if stability issues come up.

### Prerequisites
- **Node.js 22+** (24 recommended) — check with:
  ```powershell
  node --version
  ```
- **An Anthropic API key** ready (onboarding will prompt for it)
- **Git for Windows** — if missing, the installer bootstraps a local copy automatically

### Step 1 — Install OpenClaw
Open PowerShell and run:
```powershell
iwr -useb https://openclaw.ai/install.ps1 | iex
```
This detects Node, installs it if missing, installs OpenClaw, and launches onboarding automatically.

To install without immediately starting onboarding:
```powershell
& ([scriptblock]::Create((iwr -useb https://openclaw.ai/install.ps1))) -NoOnboard
```

### Step 2 — Verify the install
```powershell
openclaw --version
openclaw doctor
openclaw gateway status --json
```
`doctor` surfaces risky or misconfigured settings — run this after install and after any upgrade.

**If `openclaw` isn't recognized after install:** close and reopen PowerShell (PATH updates need a fresh session), or check:
```powershell
npm config get prefix
```
and confirm that directory is in your user PATH.

### Step 3 — Run onboarding
```powershell
openclaw onboard --install-daemon
```
- `--install-daemon` sets up a **Scheduled Task** on native Windows (with a Startup-folder login item as fallback) so OpenClaw keeps running without a terminal open — required for our always-on monitoring design.
- During onboarding: select **Anthropic (Claude)** as model provider, paste your API key, and bind the Gateway to **loopback/localhost** (not public network) unless you have a specific reason not to.
- **Skip channel setup during onboarding** — connect Telegram separately afterward (Step 5) to isolate any issues.

### Step 4 — Confirm Gateway health before anything else
```powershell
openclaw status
openclaw gateway status
openclaw logs --follow
```
A healthy result shows `Runtime: running` and `RPC probe: ok`. Test in the local Web UI (typically `http://127.0.0.1:18789`) with a plain chat message before touching any channel — isolates whether a future issue is the core assistant or a channel-specific problem.

### Step 5 — Connect Telegram (primary channel, per Section 3.4)
Get a bot token from **@BotFather** on Telegram, then:
```powershell
openclaw channels add telegram
openclaw channels status --probe
```
Discord is intentionally **not** connected yet — per our phased plan, it's added in Phase 8, after the full Telegram flow (Phases 1–7) is built and trusted. This keeps the debugging surface small while the core skills are still unproven.

### Step 6 — Security hardening (before installing any skills)
- Review any skill's listing/VirusTotal scan before installing — the ClawHub marketplace has had documented malicious-skill incidents (ClawHavoc supply-chain attack)
- Use the `allowBundled` whitelist in config to control which bundled skills auto-load — some activate automatically if the matching CLI tool is present on your system
- Never commit `~/.openclaw/openclaw.json` (or its Windows equivalent path) to the Git repo — this holds your API keys; must be covered by `.gitignore`

### Quick reference — troubleshooting commands
| Command | Purpose |
|---|---|
| `openclaw status` | Overall health |
| `openclaw gateway status --json` | Gateway process check |
| `openclaw doctor` | Diagnose misconfiguration |
| `openclaw logs --follow` | Live log tail |
| `openclaw channels status --probe` | Confirm Telegram/Discord are actually connected |

## 11. Cost Estimation & Model Assignment (Ollama vs. Claude)

### Guiding principle

The more a task directly drives a real buy/sell/hold decision or touches capital already committed, the more it belongs on **Claude**. The more a task is high-frequency, mechanical, or purely extraction/formatting, the more it's a fit for **Ollama** (free, locally-run via OpenClaw).

```
Ollama = filtering, extraction, formatting  → high-volume, reversible-if-wrong, $0 cost
Claude = synthesis, judgment, risk calls    → low-volume, directly drives money/trust
```

### Task assignment (see also Section 2 and 3.x annotations)

| Model | Tasks |
|---|---|
| **Ollama** (local, free) | News coarse-filtering, position reply parsing, notification phrasing/formatting |
| **Claude** (API, paid) | Signal synthesis, chart pattern reasoning, news synthesis feeding the final call, risk judgment on live positions, conversational Q&A |

**Never downgraded to Ollama, even for cost savings:** signal synthesis, live-position risk judgment, conversational Q&A — these are exactly where a wrong or lower-quality call costs real money or erodes trust in the system.

### Phase 0 — Validation (cheap, disposable, no real capital)

Before spending meaningfully on either trading capital or bot infrastructure, run a scaled-down validation phase:

- **Watchlist**: 5 stocks (not the full list)
- **Monitoring cadence**: hard stop-loss/target via broker GTT/OCO order (free); Claude risk-judgment check once daily during validation
- **Model**: Ollama for all filtering/extraction; Claude Haiku 4.5 (cheapest Claude tier) for signal synthesis
- **Mode**: paper trading only — bot suggests, you track hypothetical outcomes, no real money moves
- **Estimated cost**: under **$5/month**
- **Duration**: 4–6 weeks
- **Decision gate**: if suggestion quality holds up under honest review → proceed to full build (Phases 1–8) with Claude Sonnet 5 and real capital. If not → iterate on strategy logic or shelve, having spent single-digit dollars finding out.

### Production-scale estimate (post-validation, full build) — Revised for Swing/Positional Trading

Assumptions updated per swing-trade holding periods (days to months): 20-stock watchlist, 2x daily signal runs on daily (`1d`) candles, ~4 active positions on an average day with **broker-native GTT/OCO handling the hard stop-loss/target trigger** (free, no bot involvement), Claude risk-judgment checks **once or twice daily** instead of every ~20 min, ~20 chat messages/day.

| Component | Est. daily tokens (in+out) | Model |
|---|---|---|
| Signal generation (20 stocks × 2 runs, daily timeframe) | ~64,000 | Claude Sonnet 5 |
| Risk/monitoring judgment (1–2x/day, not continuous polling) | ~10,000–15,000 | Claude Sonnet 5 |
| Chat Q&A | ~30,000 | Claude Sonnet 5 |
| News filtering, position parsing, notification formatting | — | Ollama (free) |
| Hard stop-loss / target trigger | — | Broker GTT/OCO (free, no LLM/code polling needed) |

**Estimated cost: ~$8–15/month** — meaningfully lower than the original intraday estimate (~$15–25/month), since the monitoring component drops from ~95,000 tokens/day to roughly ~10,000–15,000 tokens/day by moving off tight polling and onto broker-native order triggers plus daily-cadence agent judgment. Pricing basis: Claude Sonnet 5 introductory rate ($2/$10 per million input/output tokens, in effect through August 31, 2026; standard $3/$15 thereafter), likely lower still with prompt caching.

### Development-phase cost (building the code, separate from the running bot)

This is Claude.ai/Claude Code usage while writing the skills — a separate cost from the API usage above.

- **Claude Pro** ($20/month) — recommended starting point; includes Claude Code, sufficient for building this project's ~300–450 lines across 8 phases in focused sessions
- **Claude Max 5x** ($100/month) — only worth it if you're hitting Pro's session limits during heavy back-to-back coding sessions
- Anthropic doesn't publish exact token/prompt counts per plan, only usage multipliers — start on Pro, upgrade only if you actually hit limits

---

*This document reflects everything planned in our conversation so far. Nothing here has been coded yet — this is the blueprint for review before implementation begins.*
