# Server Migration Plan

Not yet executed — this documents what moving off the local Windows machine
(currently running the OpenClaw Gateway as a Scheduled Task) to a dedicated
server/VPS would involve, so a future session can pick this up without
re-deriving it.

**Why**: the Gateway, scheduler, DB, and session state all currently live on
one Windows machine. If it's off, asleep, or rebooting, all 6 cron jobs and
inbound Discord/Telegram routing stop with nothing else picking up the
slack. This was actually the original plan (architecture doc Phase 3:
"Install OpenClaw on a VPS") but got shelved in favor of running locally —
see `PROJECT_STATUS_AND_NEXT_STEPS.md` / project memory for that history.

## 1. Pre-migration cleanup (do this regardless of server choice)

- **Fix the portability problem first**: cron jobs, `AGENTS.md`, and the
  exec-allowlist all hardcode `D:\Projects\AI-Trading-Copilot\...` Windows
  paths. Decide on a canonical path convention (e.g. a `PROJECT_ROOT` env
  var referenced everywhere) before moving, so the same config works on
  Windows or Linux.
- **Migrate secrets off plaintext**: `openclaw doctor` flags
  `gateway.auth.token` and `channels.discord.token` sitting in plaintext in
  `openclaw.json`. Fix this via `openclaw secrets configure` *before* the
  move — no reason to copy an insecure setup onto new infrastructure.
- **Decide Ollama's fate**: either it runs on the new server too (extra
  RAM/CPU), or `data_fetch_skill.py`/`position_tracker_skill.py`'s Ollama
  fallback points at a remote host. This changes the sizing requirement for
  the server, so decide before provisioning.

## 2. What actually needs to move

| Item | Source | Notes |
|---|---|---|
| This repo | `D:\Projects\AI-Trading-Copilot` | `git clone` is enough — no local state lives only in the repo. |
| SQLite DB | `db/trading_copilot.db` | Contains real open positions — copy the *file*, don't recreate schema-only. |
| OpenClaw config/state | `~/.openclaw/` (`openclaw.json`, agent workspace, `AGENTS.md`, session store) | Doesn't cleanly migrate — see step 3. |
| Secrets | Anthropic/Discord/Telegram tokens | Re-enter via `openclaw secrets configure` on the new machine rather than copying plaintext files across. |

## 3. OpenClaw-specific migration steps

1. Install OpenClaw fresh on the new server, run `openclaw setup`/`openclaw onboard`.
2. Expect to hit the same device-pairing scope wall hit before during cron
   setup (see the `feedback-openclaw-cron-scope` memory) — a brand-new
   Gateway means a brand-new device identity needing
   `operator.admin`/`write` approval via the Gateway token. Budget time for
   this.
3. Re-create the 6 cron jobs (`openclaw cron add`) with Linux-style
   `--command-argv` (e.g. `["/path/venv/bin/python", "skills/signal_skill.py"]`)
   — the Windows `sh ENOENT` bug won't apply on Linux, but argv paths still
   need rewriting.
4. Re-deploy the routing rules into the new `~/.openclaw/workspace/AGENTS.md`,
   updated with Linux paths.
5. Re-apply the exec-approvals allowlist (`openclaw approvals set --file ...`)
   with the new binary path patterns — a fresh security boundary, not
   something that copies over automatically.
6. Re-link Discord/Telegram/WhatsApp channels (`openclaw channels add`) —
   typically tied to the machine's paired session, not just config.

## 4. Cutover strategy

Since this system holds real open positions, don't do a "kill old, hope new
works" cutover:

- Stand up the new server fully, verify cron jobs run clean and inbound
  Discord routing works (a real `@mention` round-trip, same bar as the
  Phase 9 verification) **while the old machine is still running**.
- Only then disable the old machine's Scheduled Task / cron jobs, to avoid
  both instances double-firing signals or double-processing a Discord trade
  confirmation.
- Copy the DB file over as the very last step, right before disabling the
  old instance, so a position opened during the transition window isn't
  lost.

## 5. Post-migration validation checklist

- [ ] `openclaw status` shows Gateway healthy on new host
- [ ] All 6 cron jobs present via `openclaw cron list`, next-run times correct
- [ ] A live inbound test message ("bought 1 TESTTICKER @ 1") round-trips correctly
- [ ] `openclaw doctor` clean (or at least no new findings vs. the pre-migration baseline)
- [ ] Old machine's scheduled task/cron disabled (closing the app isn't enough — Windows Scheduled Tasks will restart it)
