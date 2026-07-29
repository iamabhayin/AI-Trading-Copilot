"""Options Position Monitor (Phase 15 Task 6) — re-evaluates every active
options_positions row's ORIGINAL thesis each cycle (rulebook Sections
47-53). Never mutates `thesis_json` (Rule 33: "never silently change the
original trade thesis after entry") — every decision is a fresh read
against the immutable original, not an update to it.

Two-stage design, matching options_rules_engine.py's pattern: granular
check functions -> `build_position_evidence()` composes them into a
Section-48 evidence dict -> `evaluate_position()` (pure) turns that into
one of HOLD / HOLD_TRAIL / PARTIAL_PROFIT / EXIT_TARGET /
EXIT_THESIS_INVALIDATED / EXIT_RISK_CHANGED. Everything up to and
including `evaluate_position()` is pure (no DB/network); the dedup/
notify functions at the bottom touch the DB and Discord/Telegram.

Judgment-call caveat: the rulebook specifies the six output states and
the checklist of questions (Section 48) precisely, but not exact numeric
thresholds for "IV shifted materially" or "R:R deteriorated materially"
— `check_iv_shift`/`check_rr_deterioration`'s default tolerances are this
module's own reasonable starting point, not rulebook-specified numbers.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.options_config import OptionsConfig
from db.database import execute
from skills.notify_skill import route_message

TERMINAL_ACTIONS = ("EXIT_TARGET", "EXIT_THESIS_INVALIDATED", "EXIT_RISK_CHANGED")


# --------------------------------------------------------------------------
# Granular checks (rulebook Section 48's checklist)
# --------------------------------------------------------------------------


def check_thesis_validity(thesis: dict, current_spot: float) -> bool:
    """Is the ORIGINAL underlying invalidation level still not breached?
    Insufficient info (missing invalidation/side) never counts as
    invalid — this only ever fires on a positive, observed breach."""
    invalidation = thesis.get("underlying_invalidation")
    side = thesis.get("side")
    if invalidation is None or side is None:
        return True
    return current_spot > invalidation if side == "CE" else current_spot < invalidation


def check_target_hit(thesis: dict, current_spot: float) -> dict:
    side = thesis.get("side")
    t1, t2 = thesis.get("target_1"), thesis.get("target_2")
    if side == "CE":
        return {"target_1_hit": t1 is not None and current_spot >= t1, "target_2_hit": t2 is not None and current_spot >= t2}
    return {"target_1_hit": t1 is not None and current_spot <= t1, "target_2_hit": t2 is not None and current_spot <= t2}


def check_iv_shift(entry_iv: float | None, current_iv: float | None, threshold_pct: float = 30.0) -> bool:
    """Has IV moved materially since entry (rulebook Section 48 point 5
    — IV crush risk)? 30% relative move is this module's own default,
    not a rulebook-specified number."""
    if entry_iv is None or current_iv is None or entry_iv == 0:
        return False
    return abs(current_iv - entry_iv) / entry_iv * 100 > threshold_pct


def check_theta_danger(days_to_expiry: int, config: OptionsConfig) -> bool:
    return days_to_expiry <= config.theta_danger_days


def check_rr_deterioration(current_rr: float | None, original_rr: float | None, tolerance: float = 0.5) -> bool:
    """Has R:R deteriorated materially from the position's original R:R?"""
    if current_rr is None or original_rr is None:
        return False
    return current_rr < original_rr - tolerance


# --------------------------------------------------------------------------
# Evidence composition + decision
# --------------------------------------------------------------------------


def build_position_evidence(
    thesis: dict,
    current_spot: float,
    current_regime: str,
    opposite_side_oi_classification: str | None,
    entry_iv: float | None,
    current_iv: float | None,
    days_to_expiry: int,
    current_rr: float | None,
    config: OptionsConfig,
) -> dict:
    """Composes the granular checks above into the Section 48 evidence
    dict `evaluate_position()` decides from."""
    thesis_valid = check_thesis_validity(thesis, current_spot)
    targets = check_target_hit(thesis, current_spot)
    iv_shifted = check_iv_shift(entry_iv, current_iv)
    theta_danger = check_theta_danger(days_to_expiry, config)
    rr_deteriorated = check_rr_deterioration(current_rr, thesis.get("risk_reward"))

    side = thesis.get("side")
    # Fresh buying on the OPPOSITE side building against this position
    # (e.g. holding a CE while fresh PE long-buildup appears) — rulebook
    # Section 48 point 4.
    opposite_building = opposite_side_oi_classification == "LONG_BUILDUP"
    structure_agrees = (side == "CE" and current_regime == "BULLISH") or (side == "PE" and current_regime == "BEARISH")

    risk_change_reason = None
    if theta_danger:
        risk_change_reason = f"theta danger: only {days_to_expiry} day(s) to expiry"
    elif iv_shifted:
        risk_change_reason = "IV shifted materially since entry"

    return {
        "thesis_invalidated": not thesis_valid,
        "invalidation_reason": "underlying reclaimed/lost the original breakout/retest structure" if not thesis_valid else None,
        "risk_conditions_changed": theta_danger or iv_shifted,
        "risk_change_reason": risk_change_reason,
        "target_1_hit": targets["target_1_hit"],
        "target_2_hit": targets["target_2_hit"],
        "momentum_favorable": structure_agrees and not opposite_building,
        "thesis_weakening": thesis_valid and (not structure_agrees or opposite_building or rr_deteriorated),
    }


def evaluate_position(evidence: dict) -> dict:
    """Pure decision per rulebook Sections 49-52. Priority order:
    thesis invalidated > risk conditions changed > target 2 (full exit)
    > target 1 (partial) > weakening (trail) > hold. Never both an EXIT
    and a HOLD/TRAIL for the same cycle."""
    if evidence.get("thesis_invalidated"):
        return {"action": "EXIT_THESIS_INVALIDATED", "why": evidence.get("invalidation_reason") or "original thesis structure lost"}

    if evidence.get("risk_conditions_changed"):
        return {"action": "EXIT_RISK_CHANGED", "why": evidence.get("risk_change_reason") or "risk conditions deteriorated materially"}

    if evidence.get("target_2_hit"):
        return {"action": "EXIT_TARGET", "why": "target 2 reached"}

    if evidence.get("target_1_hit"):
        why = "target 1 reached, momentum still favorable" if evidence.get("momentum_favorable") else "target 1 reached"
        return {"action": "PARTIAL_PROFIT", "why": why}

    if evidence.get("thesis_weakening"):
        return {"action": "HOLD_TRAIL", "why": "thesis weakening -- trail stop to protect gains"}

    return {"action": "HOLD", "why": "thesis still valid"}


# --------------------------------------------------------------------------
# Section 53 format, dedup, notification
# --------------------------------------------------------------------------


def format_position_update_message(position: dict, decision: dict, current_premium: float, current_spot: float) -> str:
    """Deterministic Section 53 format — no LLM involved (the numbers
    must never be touched by phrasing)."""
    entry_premium = position["entry_premium"]
    pnl = (current_premium - entry_premium) * position["qty_lots"] * position["lot_size"]
    pnl_pct = (current_premium - entry_premium) / entry_premium * 100 if entry_premium else None

    lines = [
        "MARKET: NIFTY",
        f"CURRENT SPOT: {current_spot:g}",
        f"POSITION: {position['contract']}",
        f"ENTRY: {entry_premium:g}",
        f"CURRENT PREMIUM: {current_premium:g}",
        f"P&L: {pnl:.2f} ({pnl_pct:+.1f}%)" if pnl_pct is not None else f"P&L: {pnl:.2f}",
        f"ACTION: {decision['action']}",
        f"WHY: {decision['why']}",
    ]
    return "\n".join(lines)


def should_notify(position: dict, decision: dict) -> bool:
    """Dedup on state change only — mirrors the equity pipeline's
    `alerts_sent` dedup pattern, but tracked directly on the position row
    (`last_notified_state`) since a position has exactly one current
    state at a time, unlike alerts_sent's multi-alert-type table."""
    return position.get("last_notified_state") != decision["action"]


def record_notified_state(position_id: int, action: str) -> None:
    """Bookkeeping only — never touches thesis_json (Rule 33)."""
    execute("UPDATE options_positions SET last_notified_state = ? WHERE id = ?", (action, position_id))


def notify_position_update(position: dict, decision: dict, current_premium: float, current_spot: float) -> None:
    text = format_position_update_message(position, decision, current_premium, current_spot)
    route_message("option_trading", text, telegram_parse_mode=None)


def close_position_on_exit(position_id: int, exit_premium: float) -> None:
    """For a full-exit decision (EXIT_TARGET/EXIT_THESIS_INVALIDATED/
    EXIT_RISK_CHANGED), the actual close is still a manual action the
    user confirms via options_position_entry.py's reply flow — this
    helper exists for the (optional) case Task 7's orchestrator wants to
    pre-close on a confirmed EXIT_TARGET automatically. Not called by
    default; advisory, not execution, remains the system's default mode."""
    execute(
        "UPDATE options_positions SET status = 'closed', exit_premium = ?, closed_ts = datetime('now') WHERE id = ?",
        (exit_premium, position_id),
    )
