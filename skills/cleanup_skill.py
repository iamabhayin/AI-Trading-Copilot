"""Cleanup Skill (Manual Only) — Section 3.8.

Agent/Model: Code (no LLM — deterministic wipe, no judgment involved).

No scheduling — triggered only by an explicit command from you (exact
command name still TBD, e.g. `/cleanup`). Performs a full wipe of
`suggestions`, `conversations`, `alerts_sent`, and closed `positions`.
Exception: active/open positions are never deleted — protects the
monitoring loop from losing track of a live trade. Confirms back to you
what was wiped and what (if anything) was kept.
"""

# TODO: Phase 7 — cleanup skill (manual command), final piece before Discord rollout
