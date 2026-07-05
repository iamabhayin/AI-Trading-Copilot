# OpenClaw Agent — Inbound Message Routing

> **Where this actually lives:** OpenClaw's `boot-md` hook only runs `BOOT.md` once, at gateway
> startup, as a one-off internal check (`deliver: false, suppressPromptPersistence: true` in the
> hook's own handler) — it is **not** re-read for regular inbound messages. The file that *is*
> loaded on every session, and therefore the one that actually sees live Discord/Telegram
> messages, is `AGENTS.md` ("operating instructions... loaded at the start of every session",
> per OpenClaw's docs). So:
>
> - The **routing rules below** are what got appended to the real
>   `AGENTS.md` at `~/.openclaw/workspace/AGENTS.md` (a separate, machine-local, per-user file —
>   not something this repo can own directly, same as `openclaw.json` vs this repo's
>   `openclaw.config.yaml` scaffold). This file is the versioned, reviewable source of truth for
>   that section.
> - A minimal, genuine `BOOT.md` (startup-only sanity check — confirms the two scripts below are
>   reachable) was deployed to `~/.openclaw/workspace/BOOT.md` and the `boot-md` hook was enabled,
>   satisfying the letter of "create a BOOT.md" without relying on it for the actual routing.

## Routing rules (deployed into `AGENTS.md`)

This machine also runs the **AI Trading Copilot** project
(`D:\Projects\AI-Trading-Copilot`). When an inbound message arrives on the Discord channels bound
to this project, classify it before responding conversationally:

**Working directory matters.** Both exec commands below read a relative-path SQLite DB
(`db/trading_copilot.db`), so they only work correctly when run with working directory
`D:\Projects\AI-Trading-Copilot` — set that explicitly on the exec call itself (not by `cd`-ing
first). Running from any other directory fails with a "DB path" or "unable to open database file"
error even though the exec itself succeeds. (Found live: the agent's first real attempt hit this,
self-diagnosed by reading `db/database.py`, and retried correctly — but that's wasted tool calls
that won't always self-resolve, hence spelling it out here.)

**1. Trade confirmation** — the message reports a completed buy/sell (a quantity, a ticker, and
usually a price), e.g. "bought 10 RELIANCE @ 2950" or "just bought some RELIANCE, 10 shares around
2950":
- Run via `exec` (cwd=`D:\Projects\AI-Trading-Copilot`):
  `D:\Projects\AI-Trading-Copilot\.venv\Scripts\python.exe D:\Projects\AI-Trading-Copilot\skills\position_tracker_skill.py "<the exact message text>"`
- Reply with exactly what the script printed to stdout — don't paraphrase or invent details it
  didn't report.

**2. A question about a stock, a past suggestion, or a position** — anything asking why/what/how
about a ticker, a stop-loss, a target, or "what's the latest on X", e.g. "why this stop-loss on
AAPL?" or "what's going on with RELIANCE":
- Run via `exec` (cwd=`D:\Projects\AI-Trading-Copilot`):
  `D:\Projects\AI-Trading-Copilot\.venv\Scripts\python.exe D:\Projects\AI-Trading-Copilot\skills\chat_skill.py "<the exact message text>"`
- Relay the script's answer back — it's already grounded in fresh data, don't add your own market
  commentary on top of it.

**3. Anything else** (small talk, unrelated questions, ambiguous messages with no
ticker/quantity/price at all) — respond conversationally as normal, no exec call.

**Examples:**

| Message | Action |
|---|---|
| "bought 10 RELIANCE @ 2950" | Trade confirmation → `position_tracker_skill.py` |
| "just bought some RELIANCE, 10 shares around 2950" | Still a trade confirmation (messy phrasing is fine — the script's own regex/Ollama fallback handles that) → `position_tracker_skill.py` |
| "why this stop-loss on AAPL?" | Question → `chat_skill.py` |
| "what's the latest on AAPL?" | Question → `chat_skill.py` |
| "how's your day going?" | Small talk → respond conversationally, no exec |

Always pass the **exact original message text** as the single, quoted argument — don't summarize
or reformat it before handing it to the script; the scripts do their own parsing.

## Startup sanity check (the actual `BOOT.md`, runs once per gateway restart)

Note: exec is scoped to only two exact invocations (see below) — you cannot use exec to check
file existence here, and attempting to will be denied. Skip that; just proceed silently unless
you already know from prior context that one of these paths is missing:
- `D:\Projects\AI-Trading-Copilot\.venv\Scripts\python.exe`
- `D:\Projects\AI-Trading-Copilot\skills\position_tracker_skill.py`
- `D:\Projects\AI-Trading-Copilot\skills\chat_skill.py`

If you do know one is missing, send a message (via the message tool) to the owner noting which
one and that inbound trade/question routing won't work until it's fixed. Otherwise stay silent.

## Exec tool scoping (deployed into `~/.openclaw/exec-approvals.json`)

The routing rules above only run these two scripts safely if the `exec` tool can't run anything
else. Before enabling the routing rules live, the `main` agent's exec policy was locked down from
the OpenClaw default (`security=full` — fully unrestricted) to an explicit allowlist:

```json
{
  "version": 1,
  "socket": {},
  "defaults": {},
  "agents": {
    "main": {
      "security": "allowlist",
      "ask": "off",
      "askFallback": "deny",
      "allowlist": [
        {
          "id": "ai-trading-copilot-position-tracker",
          "pattern": "D:\\Projects\\AI-Trading-Copilot\\.venv\\Scripts\\python.exe",
          "argPattern": "position_tracker_skill\\.py",
          "source": "manual"
        },
        {
          "id": "ai-trading-copilot-chat-skill",
          "pattern": "D:\\Projects\\AI-Trading-Copilot\\.venv\\Scripts\\python.exe",
          "argPattern": "chat_skill\\.py",
          "source": "manual"
        }
      ]
    }
  }
}
```

Applied via `openclaw approvals set --file <path>`. Allowlist entries match the resolved binary
path (the venv's `python.exe`, not a bare `python` — bare names only match commands resolved via
`PATH`) intersected with an `argPattern` regex that must match the invocation's arguments. This
means: the `main` agent's exec tool can run the venv's `python.exe` only when its arguments mention
`position_tracker_skill.py` or `chat_skill.py` — nothing else, and no other binary at all
(`security=allowlist` denies everything not explicitly listed; `askFallback=deny` means an
unmatched command is silently denied rather than prompting, since there's no interactive approver
during a live Discord conversation).

Verify anytime with `openclaw exec-policy show` (should read `security=allowlist, ask=off`) or
`openclaw approvals get --json`.
