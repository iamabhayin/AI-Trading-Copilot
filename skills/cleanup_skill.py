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

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.database import get_connection

WIPED_TABLES = ["suggestions", "conversations", "alerts_sent", "closed_positions"]


def cleanup() -> dict:
    """Full wipe of suggestions, conversations, alerts_sent, and closed
    positions, in one transaction. Active positions are never deleted —
    their `suggestion_id` backlink is cleared first (a position's own
    ticker/qty/entry_price/stop_loss/target were already copied onto its
    row at open time, so it doesn't depend on the suggestion surviving)
    so wiping every suggestion doesn't violate the foreign key.

    Returns {"wiped": [...], "kept_active_positions": [...]}.
    """
    with get_connection() as conn:
        kept_active_positions = [
            dict(row) for row in conn.execute("SELECT id, ticker FROM positions WHERE status = 'active'").fetchall()
        ]

        conn.execute("DELETE FROM alerts_sent")
        conn.execute("DELETE FROM positions WHERE status = 'closed'")
        conn.execute("DELETE FROM conversations")
        conn.execute("UPDATE positions SET suggestion_id = NULL WHERE status = 'active'")
        conn.execute("DELETE FROM suggestions")

        conn.commit()

    return {"wiped": WIPED_TABLES, "kept_active_positions": kept_active_positions}


def format_cleanup_summary(result: dict) -> str:
    """Human-readable confirmation of what was wiped and what was kept."""
    lines = [f"Cleanup complete. Wiped: {', '.join(result['wiped'])}."]

    kept = result["kept_active_positions"]
    if kept:
        tickers = ", ".join(position["ticker"] for position in kept)
        lines.append(f"Kept {len(kept)} active position(s): {tickers}.")
    else:
        lines.append("No active positions were kept (none existed).")

    return " ".join(lines)


if __name__ == "__main__":
    from config.startup import StartupService
    from skills.notify_skill import route_message

    StartupService().start()

    summary = format_cleanup_summary(cleanup())
    print(summary)
    try:
        route_message("cleanup", summary)
    except ValueError as exc:
        print(f"  (not sent to Telegram: {exc})")
