# AI Trading Copilot — Project Status & Handoff Prompt

> **Purpose of this file:** Context for a Claude Code agent (or any new session) picking up this project.
> Repo: `github.com/iamabhayin/AI-Trading-Copilot` (private, Windows/PowerShell dev environment).
> Full original architecture: `ai-trading-copilot-architecture.md` in project knowledge base.

---

## 1. What has been completed

### Code (all merged to `main`)
- All 8 phases built and merged: data fetch, indicator engine, signal skill, Telegram notify,
  position tracker, monitor/risk skill, chat skill, cleanup skill, Discord dual-channel.
- 96/96 tests passing, clean `ruff` lint.
- `notify_skill.py` has a working `route_message(message_type, text)` fan-out: Telegram always
  sends (primary), Discord sends too if that message type has a configured channel
  (secondary/logging). Discord failure does not block Telegram (tested).
- README's Build Order table incorrectly still shows Phase 8 as "not merged" — **stale, needs a
  one-line fix** (it's actually merged, verified via `git log`).

### Infrastructure (OpenClaw, local Windows machine — no VPS yet, by design)
- OpenClaw installed, onboarded, running as a background service (`--install-daemon` /
  Scheduled Task).
- Hooks enabled: `session-memory`, `command-logger`. Skipped: `boot-md`, `bootstrap-extra-files`,
  `compaction-notifier` (boot-md will be needed for the next step — see §4).
- Model: OpenClaw's own internal agent (`agent:main:main`) is pinned to **Anthropic
  Claude Opus/Sonnet** (was accidentally pinned to `openai/gpt-5.4-mini` via a stray Codex plugin
  auto-load at one point — fixed via `/model anthropic/claude-opus-4-8` + config default).
- **Discord channel: fully working end-to-end.** Bot token configured via guided
  `openclaw channels add` wizard, DM policy = pairing, channel access = allowlist
  (`guildId/channelId` pairs for `#market-news`, `#signals`, `#monitoring`, `#chat`,
  `#closed-trades`). `intents:content=limited` is expected/normal for bots under 100 servers —
  **not a bug**, do not chase this further. `@mention` in a guild channel correctly triggers the
  agent, which replies in Discord.
- **Anthropic API billing resolved** — credit balance funded, confirmed working (no more
  "credit balance too low" errors on Discord replies).
- **Ollama installed and verified working** locally (`llama3.2` model pulled,
  `OLLAMA_HOST`/`OLLAMA_MODEL` set in `.env`). Confirmed `parse_trade_reply_with_ollama()` in
  `position_tracker_skill.py` returns correct parsed output when called directly.

### Decisions made (see also memory notes)
- **Broker order-placement (Kite/Zerodha) is explicitly deferred**, by user decision, not a gap
  to close right now. User will place buy/sell AND set their own stop-loss/target manually in
  Kite. Bot's job for now: advise, track, monitor — **no broker API calls, no order placement**.
  Revisit only after the manual-suggestion phase proves the signals are actually useful.
- Discord is temporarily standing in as the **primary two-way (listen + reply) channel** while
  Telegram's OpenClaw connector is broken (see §2). This is a temporary role-swap, not a
  permanent architecture change — original design still has Telegram as primary/action,
  Discord as secondary/logging, and should be reverted once Telegram's connector is fixed.
- `python-telegram-bot`-based outbound sending (`notify_skill.py`) is independent of OpenClaw
  entirely and has never been broken — confirmed via direct `curl.exe` test to
  `api.telegram.org/bot<token>/getMe` returning valid JSON, and via `notify_skill.py` test sends.

---

## 2. What's working vs. what's not

| Piece | Status | Notes |
|---|---|---|
| Outbound Telegram alerts (`notify_skill.py`) | ✅ Working | Independent of OpenClaw |
| Outbound Discord alerts (`notify_skill.py`) | ✅ Working | Independent of OpenClaw |
| OpenClaw Discord listener (receive + reply) | ✅ Working | Confirmed via `@mention` round-trip |
| OpenClaw Anthropic model + billing | ✅ Working | Opus/Sonnet, credits funded |
| Ollama (local, free tier) | ✅ Working | Verified from actual skill code, not just CLI |
| **OpenClaw Telegram listener** | ❌ Broken | `channel stop timed out after 5000ms` on
  restart; stuck in `disconnected`/degraded state. Root cause not fully isolated — possibly
  related to a stale duplicate Node process or event-loop CPU pegging
  (`eventLoopUtilization` was observed near/at 1.0 and climbing). Direct API connectivity from
  the same machine is confirmed fine (`curl` test), so it is **not** an ISP/network block on
  Telegram — it's specific to OpenClaw's own Telegram plugin/process state. **Parked** —
  Discord is the working substitute for now. |
| **Scheduler (cron)** | ❌ Not started | `openclaw.config.yaml`'s `pre_market_run`,
  `post_market_run`, `monitor_check` are all `enabled: false` with empty cron values. Nothing
  runs automatically yet — every skill only runs when manually invoked with `python`. |
| **Inbound message → skill routing** | ❌ Does not exist yet | This is the current focus —
  see §3/§4 below. |
| Broker GTT/OCO integration | ⏸️ Deferred by user decision | Not a gap — intentionally
  out of scope for this phase. `monitor_skill.py`'s `check_price_trigger()` remains a
  database-only visibility check, not a real broker order. |

---

## 3. The gap that still exists (current focus)

**Nothing currently turns an inbound Discord/Telegram message into a call to
`position_tracker_skill.py` or `chat_skill.py`.**

Confirmed by direct code inspection:
- `position_tracker_skill.py` has a working `handle_reply(message: str) -> dict` function, but
  its `__main__` block only runs a **hardcoded demo list** of sample replies — there is no CLI
  argument parsing, so it cannot currently be invoked with an arbitrary real message from the
  command line.
- `chat_skill.py` has the equivalent problem — logic exists, no real invocation path.
- `openclaw.config.yaml`'s `skills:` list (e.g. `- name: position_tracker_skill`) is **not real
  OpenClaw config syntax** — the file's own header comment calls it a "placeholder scaffold."
  OpenClaw does not read this list at all. Real OpenClaw skills are Node/TypeScript
  `SKILL.md` + `handler.ts` packages discovered from specific directories — this project's
  skills are plain Python files with no such wrapper.
- OpenClaw's own Discord agent currently has **no instructions** telling it what to do with an
  inbound trade-confirmation-shaped message — it just chats conversationally.

So: OpenClaw's Discord channel is a working **transport**, but there is no **dispatcher** on top
of it yet.

---

## 4. The plan to close the gap

**Mechanism: give OpenClaw's agent an `exec` tool (already available to OpenClaw agents) plus
explicit instructions (via the `boot-md` hook, currently enabled but empty) telling it when to
invoke your Python skills.**

Three concrete pieces of work:

1. **Add real CLI entry points to the skill files.**
   Replace the hardcoded demo `__main__` blocks in `position_tracker_skill.py` and
   `chat_skill.py` with proper `sys.argv`/`argparse`-based entry points, e.g.:
   ```powershell
   python skills/position_tracker_skill.py "bought 10 RELIANCE @ 2950"
   python skills/chat_skill.py "why this stop-loss?"
   ```
   Each should print a clean, single structured result (success/failure + what was recorded/answered)
   to stdout, since the agent will read that output back to relay it in Discord.

2. **Write a `BOOT.md` (or equivalent) instructions file for the OpenClaw agent.**
   Plain-language rules: *"If a message looks like a trade confirmation (bought/sold + qty +
   ticker + price), run `position_tracker_skill.py` with the exact message text via `exec` and
   reply with the result. If it's a question about a stock, a suggestion, or a position, run
   `chat_skill.py` instead and relay the answer. Otherwise, respond conversationally as normal."*
   This is what makes the previously-enabled-but-inert `boot-md` hook actually do something.

3. **Scope the `exec` tool narrowly**, so the agent can only invoke these two specific scripts
   with a message argument — not arbitrary shell commands. This matters because Discord input is
   technically untrusted, even from a single known user, and this keeps the earlier
   security reasoning (why Opus/Sonnet was chosen for this agent over a weaker model) meaningful
   in practice, not just in theory.

**What this does NOT cover (explicitly out of scope for this step):**
- No broker order placement (per user decision, §1).
- No scheduler/cron work (separate, still-open item — §2).
- Telegram's OpenClaw connector stays broken; this bridge only works through Discord until that's
  separately fixed.

**Known risk to test for, not assumed to be solved on day one:** this relies on the LLM
correctly classifying an inbound message as "trade confirmation" vs. "question" vs. "small talk."
Should be tested against a few ambiguous real-world phrasings once built, not just the clean
example above.

---

## 5. Immediate next step

Build the three pieces in §4, in this order:
1. CLI entry points for `position_tracker_skill.py` and `chat_skill.py` (testable standalone
   from PowerShell before touching OpenClaw at all).
2. `BOOT.md` instructions file.
3. Scoped `exec` tool permission for the OpenClaw Discord agent.

Then test end-to-end: send `bought 10 RELIANCE @ 2950` in the Discord `#chat` channel (as a
resolved `@mention`) and confirm a row appears in the `positions` SQLite table, with the agent
replying back confirming what was tracked.

**After this bridge is confirmed working**, the next priorities (in order) are:
- Turn on the scheduler (`openclaw.config.yaml` cron entries) for `pre_market_run` /
  `post_market_run` / `monitor_check`.
- Run the Phase 0 paper-trading validation (5-stock watchlist, <$5/month, 4–6 weeks) — this is
  the actual gate the user wants to pass before considering broker integration (§1) at all.
- Revisit the stuck Telegram OpenClaw connector, ideally by trying OpenClaw under WSL2
  (the officially-recommended path for Windows) instead of native PowerShell, since the current
  fault (event-loop degradation, stuck channel shutdown) looks like exactly the kind of
  native-Windows rough edge that path is meant to avoid.
- Fix the stale README Phase 8 status line.
- Security hardening: lock `plugins.allow` explicitly (a dummy placeholder value, not an empty
  array — empty array is a known OpenClaw bug that silently allows everything) once the Codex
  plugin auto-load issue is confirmed fully resolved.
