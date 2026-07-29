"""Options Trade Selector (Phase 15 Task 6) — direction -> expiry ->
strike -> entry zone -> underlying invalidation -> option SL -> targets
-> R:R -> position size -> final rejection pass, per docs/options-
rulebook.md's sequence (Section 2, never reversed).

Scope split with options_rules_engine.py (documented in that module's
docstring too): this module owns rulebook Section 42 rules 10-17 — the
rejections that need a computed trade candidate (far-OTM, cheap-option-
only, time-to-expiry, IV/event-risk, R:R, position size, target
proximity, chasing) — as its own "final rejection pass". Rules 1-9/18
(market-level: stale data, mid-range, unconfirmed break, etc.) are
options_rules_engine.evaluate_market_rejections()'s job.

Judgment-call caveat: the rulebook specifies exact formulas for R:R,
position sizing, and the delta-mapped option SL — those are implemented
verbatim below. It does NOT give numeric formulas for "far OTM without
strategic justification" or "entry chasing an extended move" — this
module's heuristics for those (`_is_far_otm`) are a reasonable starting
point, not a rulebook-specified formula; `chasing_extended_move` is left
as a caller-supplied flag rather than invented here, since the rulebook
gives no deterministic trigger for it.

Pure selection functions (expiry/strike/entry/invalidation/SL/targets/
R:R/sizing) take plain data and have no DB/network access, for the same
unit-testability reason as options_analytics.py/options_rules_engine.py.
`write_advisory`/`notify_advisory` at the bottom are the only DB/network-
touching functions, matching signal_skill.py's precedent of mixing pure
computation with persistence/notify in one skill file.
"""

import json
import math
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.options_config import OptionsConfig
from db.database import execute
from skills.angel_client import format_expiry_readable
from skills.notify_skill import route_message


def _parse_expiry(expiry: str) -> date:
    """Angel One scrip-master expiry format, e.g. '24JUL2025'."""
    return datetime.strptime(expiry, "%d%b%Y").date()


# --------------------------------------------------------------------------
# Expiry (rulebook Section 34)
# --------------------------------------------------------------------------


def estimate_holding_days(distance_to_target: float, atr: float | None, min_days: int = 1, max_days: int = 10) -> int:
    """Expected holding period derived from distance-to-target vs ATR —
    rough number of "ATR days" needed to cover the move, clamped to a
    sane range so a huge/zero ATR can't produce a nonsensical holding
    estimate."""
    if not atr or atr <= 0:
        return min_days
    days = math.ceil(abs(distance_to_target) / atr)
    return max(min_days, min(max_days, days))


def select_expiry(available_expiries: list[str], expected_holding_days: float, config: OptionsConfig, today: date) -> str | None:
    """Nearest expiry whose time-to-expiry comfortably fits the expected
    holding period: days_to_expiry >= holding_fit_multiplier *
    expected_holding_days (default 2x). Returns None if no available
    expiry satisfies this."""
    min_days_needed = expected_holding_days * config.holding_fit_multiplier
    for expiry in sorted(available_expiries, key=_parse_expiry):
        days_to_expiry = (_parse_expiry(expiry) - today).days
        if days_to_expiry >= min_days_needed:
            return expiry
    return None


# --------------------------------------------------------------------------
# Strike (rulebook Sections 11, 35-36)
# --------------------------------------------------------------------------


def select_strike(expiry_chain: list[dict], option_side: str, config: OptionsConfig) -> dict | None:
    """Liquid ATM or slightly ITM only — |delta| within
    [delta_band_min, delta_band_max], spread/OI/volume liquidity filter.
    Never far OTM. Among qualifying strikes, picks the one whose |delta|
    is closest to the band's midpoint (the rulebook doesn't specify a
    tiebreak; this is a reasonable "most ATM-like within the acceptable
    range" choice)."""
    candidates = []
    for row in expiry_chain:
        if row.get("side") != option_side:
            continue
        delta = row.get("delta")
        if delta is None:
            continue
        abs_delta = abs(delta)
        if not (config.delta_band_min <= abs_delta <= config.delta_band_max):
            continue

        bid, ask = row.get("bid"), row.get("ask")
        if not bid or not ask or bid <= 0:
            continue
        spread_pct = (ask - bid) / bid * 100
        if spread_pct > config.max_spread_pct:
            continue

        oi = row.get("oi") or 0
        volume = row.get("volume") or 0
        if oi < config.min_oi or volume < config.min_volume:
            continue

        candidates.append({**row, "spread_pct": spread_pct})

    if not candidates:
        return None
    band_mid = (config.delta_band_min + config.delta_band_max) / 2
    return min(candidates, key=lambda row: abs(abs(row["delta"]) - band_mid))


def moneyness(strike: float, spot: float, side: str) -> str:
    if strike == spot:
        return "ATM"
    if side == "CE":
        return "ITM" if strike < spot else "OTM"
    return "ITM" if strike > spot else "OTM"


# --------------------------------------------------------------------------
# Entry zone / invalidation / option SL (rulebook Sections 37-38)
# --------------------------------------------------------------------------


def compute_entry_zone(level: float, direction: str, config: OptionsConfig) -> tuple[float, float]:
    """An entry zone, not a single price (rulebook Section 37): UP ->
    level to level+buffer; DOWN -> level-buffer to level."""
    buffer_pts = level * config.entry_zone_buffer_pct / 100
    return (level, level + buffer_pts) if direction == "UP" else (level - buffer_pts, level)


def compute_invalidation(level: float, direction: str, config: OptionsConfig) -> float:
    """Underlying structural invalidation: loss of the breakout/retest
    structure (rulebook Section 38)."""
    buffer_pts = level * config.invalidation_buffer_pct / 100
    return level - buffer_pts if direction == "UP" else level + buffer_pts


def map_to_option_stop(entry_premium: float, delta: float, entry_spot: float, invalidation_spot: float, config: OptionsConfig) -> float:
    """premium_sl ~= entry_premium - delta * (entry_spot - invalidation_spot),
    floored at config.min_option_premium (rulebook Section 38). Holds for
    both CE (positive delta) and PE (negative delta) since both spot
    displacement and delta are signed consistently."""
    premium_sl = entry_premium - delta * (entry_spot - invalidation_spot)
    return max(config.min_option_premium, premium_sl)


# --------------------------------------------------------------------------
# Targets / R:R (rulebook Sections 39-40)
# --------------------------------------------------------------------------


def next_level(direction: str, level: float, support_resistance: dict) -> float | None:
    """The next S/R level beyond the confirmed breakout/breakdown level."""
    if direction == "UP":
        candidates = [r for r in (support_resistance.get("resistance") or []) if r > level]
        return min(candidates) if candidates else None
    candidates = [s for s in (support_resistance.get("support") or []) if s < level]
    return max(candidates) if candidates else None


def compute_targets(entry: float, level_beyond: float | None, config: OptionsConfig) -> dict:
    """T1 = entry + (next_level - entry) * target_split_ratio; T2 =
    next_level. Both None if no next level is available (never invent a
    target — rulebook Section 39: "never claim an exact target is
    guaranteed")."""
    if level_beyond is None:
        return {"t1": None, "t2": None}
    t1 = entry + (level_beyond - entry) * config.target_split_ratio
    return {"t1": t1, "t2": level_beyond}


def compute_structural_rr(entry: float, invalidation: float, target: float | None) -> float | None:
    if target is None:
        return None
    risk = abs(entry - invalidation)
    if risk <= 0:
        return None
    return abs(target - entry) / risk


def estimate_target_premium(entry_premium: float, delta: float, entry_spot: float, target_spot: float, config: OptionsConfig) -> float:
    """Linear delta-based approximation of the option's premium at the
    target underlying level — informational, not guaranteed (rulebook
    Section 40 explicitly requires this modeling step for the option
    R:R, since underlying R:R alone isn't the real trade's R:R)."""
    modeled = entry_premium + delta * (target_spot - entry_spot)
    return max(config.min_option_premium, modeled)


def compute_option_rr(entry_premium: float, option_sl: float, target_premium: float | None) -> float | None:
    if target_premium is None:
        return None
    risk = entry_premium - option_sl
    if risk <= 0:
        return None
    return (target_premium - entry_premium) / risk


# --------------------------------------------------------------------------
# Position sizing (rulebook Section 41)
# --------------------------------------------------------------------------


def compute_position_size(capital: float, risk_pct: float, entry_premium: float, option_sl: float, lot_size: int) -> int:
    """floor(max_risk / (premium_risk_per_unit * lot_size)); 0 lots means
    NO_TRADE (rulebook Section 41 — "never increase risk merely because
    confidence is high")."""
    max_risk = capital * risk_pct
    premium_risk_per_unit = entry_premium - option_sl
    if premium_risk_per_unit <= 0 or lot_size <= 0:
        return 0
    return math.floor(max_risk / (premium_risk_per_unit * lot_size))


def lot_size_for_expiry(contracts: list[dict], expiry: str) -> int | None:
    for contract in contracts:
        if contract.get("expiry") == expiry and contract.get("lotsize"):
            return int(float(contract["lotsize"]))
    return None


# --------------------------------------------------------------------------
# Event risk (rulebook Section 42 rule 13) — best-effort, never blocks on
# a missing/malformed calendar file
# --------------------------------------------------------------------------


def check_event_risk(expiry: str, config: OptionsConfig, today: date) -> bool:
    """Reads a JSON array of ISO event dates from
    config.iv_event_calendar_path. Returns False (no known event risk)
    if the file is missing or malformed — never blocks the pipeline on a
    calendar file that doesn't exist."""
    path = Path(config.iv_event_calendar_path)
    if not path.exists():
        return False
    try:
        event_dates = {date.fromisoformat(entry) for entry in json.loads(path.read_text())}
    except (json.JSONDecodeError, OSError, ValueError):
        return False
    expiry_date = _parse_expiry(expiry)
    return any(today <= event_date <= expiry_date for event_date in event_dates)


def _is_far_otm(abs_delta: float, threshold: float = 0.35) -> bool:
    """Judgment-call heuristic (see module docstring): a strike whose
    |delta| is far below the delta band's midpoint reads as "far OTM"
    even if it technically cleared the band filter at its edge."""
    return abs_delta < threshold


# --------------------------------------------------------------------------
# Scoring sub-scores this module owns (rulebook Section 43's "rr" and
# "liquidity" categories — deferred from options_rules_engine.py's Task 5
# scorer since both need a computed trade candidate; see that module's
# docstring for the split).
# --------------------------------------------------------------------------


def score_rr(risk_reward: float | None, config: OptionsConfig) -> float:
    """0-100, scaled relative to min_rr (min_rr itself scores 50; 2x
    min_rr or better scores 100) — the rulebook doesn't specify an exact
    scaling formula, this is a reasonable default."""
    if risk_reward is None or risk_reward <= 0 or config.min_rr <= 0:
        return 0.0
    return min(100.0, (risk_reward / config.min_rr) * 50)


def score_liquidity(spread_pct: float | None, config: OptionsConfig) -> float:
    """0-100, scaled linearly from 0% spread (100) to max_spread_pct (0)."""
    if spread_pct is None:
        return 0.0
    if config.max_spread_pct <= 0:
        return 0.0
    return max(0.0, 100.0 * (1 - spread_pct / config.max_spread_pct))


# --------------------------------------------------------------------------
# Final rejection pass (rulebook Section 42, rules 10-17)
# --------------------------------------------------------------------------


def evaluate_trade_rejections(trade: dict, config: OptionsConfig) -> list[str]:
    """This module's own final rejection pass. See module docstring for
    the split with options_rules_engine.evaluate_market_rejections()
    (rules 1-9/18)."""
    reasons = []
    if trade.get("far_otm"):
        reasons.append("far OTM without strategic justification")
    if trade.get("cheap_only"):
        reasons.append("option selected only because cheap")
    if trade.get("event_risk"):
        reasons.append("abnormal IV / event risk not accounted for")

    rr = trade.get("risk_reward")
    if rr is not None and rr < config.min_rr:
        reasons.append("risk:reward below threshold")

    if trade.get("position_size_lots") == 0:
        reasons.append("position exceeds risk limit")

    if trade.get("price_too_close_to_target"):
        reasons.append("price already too close to target")

    if trade.get("chasing_extended_move"):
        reasons.append("entry requires chasing an extended move")

    return reasons


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def select_trade(
    direction: str,
    confirmation_level: float,
    option_side: str,
    expiry_chain: list[dict],
    contracts: list[dict],
    entry_spot: float,
    support_resistance: dict,
    atr: float | None,
    capital: float,
    config: OptionsConfig,
    today: date,
    chasing_extended_move: bool = False,
    cheap_only: bool = False,
) -> dict:
    """Full sequence per rulebook Section 2. `expiry_chain` may span
    multiple expiries (each row must carry an "expiry" key) or be
    pre-filtered to one — either works, this filters internally.

    Returns {"action": "BUY_CE_CANDIDATE"|"BUY_PE_CANDIDATE", "trade": {...}}
    or {"action": "NO_TRADE", "reasons": [...], "trade": {...} | None}.
    """
    available_expiries = sorted({row["expiry"] for row in expiry_chain if row.get("expiry")}, key=_parse_expiry) or sorted(
        {c["expiry"] for c in contracts if c.get("expiry")}, key=_parse_expiry
    )
    if not available_expiries:
        return {"action": "NO_TRADE", "reasons": ["no listed expiries available"], "trade": None}

    target_level = next_level(direction, confirmation_level, support_resistance)
    holding_days = estimate_holding_days(abs((target_level or confirmation_level) - entry_spot), atr)
    expiry = select_expiry(available_expiries, holding_days, config, today)
    if expiry is None:
        return {"action": "NO_TRADE", "reasons": ["insufficient time to expiry"], "trade": None}

    candidates_for_expiry = [row for row in expiry_chain if row.get("expiry", expiry) == expiry]
    strike_row = select_strike(candidates_for_expiry, option_side, config)
    if strike_row is None:
        return {"action": "NO_TRADE", "reasons": ["no liquid ATM/slightly-ITM strike available"], "trade": None}

    entry_zone = compute_entry_zone(confirmation_level, direction, config)
    entry_underlying = entry_zone[1] if direction == "UP" else entry_zone[0]
    invalidation_spot = compute_invalidation(confirmation_level, direction, config)
    entry_premium = strike_row["ltp"]
    delta = strike_row["delta"]
    option_sl = map_to_option_stop(entry_premium, delta, entry_underlying, invalidation_spot, config)

    targets = compute_targets(entry_underlying, target_level, config)
    target_premium = (
        estimate_target_premium(entry_premium, delta, entry_underlying, targets["t2"], config)
        if targets["t2"] is not None
        else None
    )
    risk_reward = compute_option_rr(entry_premium, option_sl, target_premium)
    if risk_reward is None:
        risk_reward = compute_structural_rr(entry_underlying, invalidation_spot, targets["t2"])

    lot_size = lot_size_for_expiry(contracts, expiry) or 0
    position_size_lots = compute_position_size(capital, config.risk_pct, entry_premium, option_sl, lot_size)

    price_too_close_to_target = targets["t2"] is not None and abs(targets["t2"] - entry_underlying) <= (
        entry_underlying * config.proximity_threshold_pct / 100
    )

    trade = {
        "expiry": expiry,
        "strike": strike_row["strike"],
        "side": option_side,
        "moneyness": moneyness(strike_row["strike"], entry_spot, option_side),
        "entry_zone": entry_zone,
        "entry_premium": entry_premium,
        "underlying_invalidation": invalidation_spot,
        "option_stop": option_sl,
        "target_1": targets["t1"],
        "target_2": targets["t2"],
        "risk_reward": risk_reward,
        "position_size_lots": position_size_lots,
        "lot_size": lot_size,
        "far_otm": _is_far_otm(abs(delta)),
        "cheap_only": cheap_only,
        "event_risk": check_event_risk(expiry, config, today),
        "price_too_close_to_target": price_too_close_to_target,
        "chasing_extended_move": chasing_extended_move,
    }

    reasons = evaluate_trade_rejections(trade, config)
    if reasons:
        return {"action": "NO_TRADE", "reasons": reasons, "trade": trade}

    return {"action": "BUY_CE_CANDIDATE" if option_side == "CE" else "BUY_PE_CANDIDATE", "trade": trade}


# --------------------------------------------------------------------------
# Section 54 payload, persistence, notification
# --------------------------------------------------------------------------


def build_advisory_payload(
    result: dict,
    data_timestamp: str,
    spot: float,
    regime: str,
    support_resistance: dict,
    pcr: float | None,
    score: int | None,
    why: list[str] | None = None,
) -> dict:
    """Full Section 54 structured payload. `why` (from
    options_rules_engine.summarize_confirmation_evidence) is the plain-
    language evidence list shown in the Discord/Telegram message —
    optional since a NO_TRADE result has `reasons` instead."""
    payload = {
        "market": "NIFTY",
        "data_timestamp": data_timestamp,
        "spot": spot,
        "market_regime": regime,
        "support": support_resistance.get("support"),
        "resistance": support_resistance.get("resistance"),
        "pcr": pcr,
        "action": result["action"],
        "confidence": score,
    }
    trade = result.get("trade")
    if trade:
        payload.update(
            {
                "expiry": trade["expiry"],
                "strike": trade["strike"],
                "side": trade["side"],
                "moneyness": trade["moneyness"],
                "entry_zone": trade["entry_zone"],
                "underlying_invalidation": trade["underlying_invalidation"],
                "option_stop": trade["option_stop"],
                "target_1": trade["target_1"],
                "target_2": trade["target_2"],
                "risk_reward": trade["risk_reward"],
                "position_size_lots": trade["position_size_lots"],
                "lot_size": trade["lot_size"],
                "entry_premium": trade["entry_premium"],
            }
        )
    if why:
        payload["why"] = why
    if result.get("reasons"):
        payload["reasons"] = result["reasons"]
    return payload


def write_advisory(payload: dict, score: int | None, rules_log: dict) -> int:
    """Persists the advisory to options_advisories — the only DB-touching
    function in this module."""
    return execute(
        "INSERT INTO options_advisories (created_ts, action, payload_json, score, rules_log_json) VALUES (?, ?, ?, ?, ?)",
        (payload["data_timestamp"], payload["action"], json.dumps(payload), score, json.dumps(rules_log)),
    )


def _template_advisory_message(payload: dict) -> str:
    """Deterministic fallback formatting — no LLM involved. Exact wording
    still open for feedback (see docs discussion) — the WHY section is
    the important part, phrasing can be refined later."""
    action = payload["action"]
    lines = [f"NIFTY Options - {action}"]
    if action in ("BUY_CE_CANDIDATE", "BUY_PE_CANDIDATE"):
        readable_expiry = format_expiry_readable(payload["expiry"])
        lines.append(f"Suggest: BUY NIFTY {payload['strike']:g} {payload['side']}, expiry {readable_expiry}")
        lines.append(f"({payload['moneyness']}, full expiry {payload['expiry']})")
        if payload.get("why"):
            lines.append("")
            lines.append("Why this breakout:")
            lines.extend(f"- {reason}" for reason in payload["why"])
        lines.append("")
        lines.append(f"Entry zone: {payload['entry_zone']}")
        lines.append(f"Underlying invalidation: {payload['underlying_invalidation']:g}")
        lines.append(f"Option stop: {payload['option_stop']:g}")
        lines.append(f"Target 1: {payload['target_1']}")
        lines.append(f"Target 2: {payload['target_2']}")
        lines.append(f"Risk:Reward: {payload['risk_reward']}")
        lines.append(f"Position size: {payload['position_size_lots']} lot(s)")
    else:
        lines.append(f"Reasons: {', '.join(payload.get('reasons', []))}")
    lines.append(f"Confidence: {payload.get('confidence')}/100")
    lines.append(f"Data timestamp: {payload['data_timestamp']}")
    return "\n".join(lines)


def format_advisory_message(payload: dict) -> str:
    """Always the deterministic template — no Ollama phrasing pass.

    Verified live (2026-07-20): running this payload through Ollama's
    phrasing prompt (same pattern as notify_skill.format_suggestion_message)
    silently compressed away the option stop, target 2, position size,
    confidence score, and data timestamp, keeping only a short summary
    paragraph — despite the prompt explicitly saying "keep every number".
    A dropped field in a financial advisory isn't a cosmetic issue, so
    this message type never goes through an LLM rewrite. The template
    itself already reads as plain English (the WHY section is built from
    summarize_confirmation_evidence's human-readable bullets), so there's
    no real phrasing benefit being given up.
    """
    return _template_advisory_message(payload)


def notify_advisory(payload: dict) -> None:
    """Sends a BUY_CE/PE advisory to Telegram + Discord #option-trading.
    Callers decide whether to call this at all — routine WAIT/NO_TRADE
    cycles stay SQLite-only per the Phase 15 notification spec."""
    route_message("option_trading", format_advisory_message(payload), telegram_parse_mode=None)
