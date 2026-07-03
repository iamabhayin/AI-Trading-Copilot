"""Chat Skill (Conversational Agent) — Section 3.7.

Agent/Model: Claude.

Listens for any free-form message, not just structured replies. Identifies
which ticker you're asking about (implicit last-discussed ticker, or
explicit mention). Re-fetches a fresh data snapshot for that ticker before
answering — never answers from stale memory alone. Builds a prompt from
your question + the original suggestion + the fresh snapshot + recent
conversation history. Replies within 1-3 seconds. Stays on Claude — this is
user-facing trust, not a place to downgrade for cost.
"""

# TODO: Phase 6 — chat skill (conversational Q&A), free-form follow-up questions
