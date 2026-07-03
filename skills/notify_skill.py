"""Notify Skill (Messaging Agent) — Section 3.4.

Agent/Model: Ollama (phrasing/formatting only — the underlying decision is
already made upstream by Claude in the Signal Skill / Monitor Skill).

Dual channel: Telegram (primary action channel — structured buy/sell
confirmations, quick chat on the go) + Discord (secondary organized
reading/logging surface, split across #market-news, #signals, #monitoring,
#chat, #closed-trades). The core signal/indicator/monitor pipeline runs
once; output is routed to the right destination based on message type — no
duplicate logic, just a routing layer. Two-way replies need a listener on
both platforms, both writing to the same SQLite tables.
"""

# TODO: Phase 3 — install OpenClaw on VPS, wire Telegram notify skill (first real message delivery)
# TODO: Phase 8 — add Discord as a second notification target, reusing the same skill
