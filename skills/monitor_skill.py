"""Monitor Skill (Risk/Guard Agent + Broker-Native Tripwire) — Section 3.6.

Agent/Model: Claude (risk judgment). Hard stop-loss/target trigger itself
is handled by a broker-native GTT/OCO order placed at trade entry, not by
this skill or any polling loop.

Claude evaluates news/fundamentals/context for early-exit or
stop-loss-adjustment judgment calls. Since positions are swing/positional
(days to months), this runs on a light cadence — once or twice a day,
aligned with the pre-market/post-market runs — rather than continuous
intraday polling, or triggered ad hoc by a significant news event. Tracks
sent alerts (`alerts_sent` table) to avoid duplicate spam.
"""

# TODO: Phase 5 — monitor skill (code tripwire + agent risk judgment), light daily/twice-daily check
