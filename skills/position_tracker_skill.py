"""Position Tracker (Reply Parser) — Section 3.5.

Agent/Model: Ollama.

Listens for your reply (e.g. "bought 10 RELIANCE @ 2950"). Parses
ticker/qty/price using regex first, falling back to Ollama for messy
phrasing — pure extraction, no trading judgment involved. Writes the
result to the `positions` table with status = 'active'.
"""

# TODO: Phase 4 — position tracker (reply parsing -> SQLite), "bought X" flow working end-to-end
