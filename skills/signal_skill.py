"""Signal Skill (Strategy Agent) — Section 3.3.

Agent/Model: Claude.

Takes indicator output + news (pre-filtered by Ollama in the Data Fetch
Skill) + optionally a chart image, and sends it to the Claude API for
synthesis. Forces structured JSON output:
`{ticker, action, entry, stop_loss, target, confidence, rationale}`.
Ranks/filters into a shortlist ("prominent stocks"). This is the
highest-stakes judgment in the system — never downgraded to Ollama, even
during cost-saving/validation phases.
"""

# TODO: Phase 2 — signal skill with Claude API call (console-only, sanity-check suggestions)
