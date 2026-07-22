"""Options Rules Engine (Phase 15 Task 5) — dual-cadence state machine,
breakout/breakdown confirmation, market-level hard rejections, and
scoring. Pure functions over plain data (dict/DataFrame in, dict out) —
no network or DB access, no wall-clock reads (every function that needs
"now" takes it as an explicit parameter). Task 7's orchestrator owns
reading/writing `options_engine_state`, gating cron cadence, and sending
notifications; this module only decides what should happen.

Scope split with Trade Selector (Task 6), matching the original spec's
own wording ("Trade Selector Sequence ends with a final rejection
pass"): `evaluate_market_rejections()` here covers rulebook Section 42
rules 1-9 and 18 — the market-level checks answerable before a trade
candidate exists. Rules 10-17 (far-OTM, cheap-option-only, time-to-
expiry, IV/event-risk, R:R, position size, target proximity, chasing an
extended move) all require a computed entry/SL/target/size and are Task
6's job, not this module's.

Scoring caveat: rulebook Section 43 specifies the nine category WEIGHTS
precisely (they sum to 100 and are config-overridable) — those are
load-bearing. The sub-score formulas that turn raw evidence into each
category's 0-100 value are this module's own reasonable implementation,
not something the rulebook specifies numerically; treat them as a
starting point to tune against real trading data, not gospel.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from config.options_config import OptionsConfig
from skills.options_analytics import classify_premium_oi

# --------------------------------------------------------------------------
# Regime classification (rulebook Section 31)
# --------------------------------------------------------------------------


def detect_price_trend(candles: pd.DataFrame, lookback: int = 10) -> str:
    """'UP' on a higher-high/higher-low pattern, 'DOWN' on lower-high/
    lower-low, else 'FLAT' — comparing the first vs second half of the
    trailing `lookback` candles' high/low extremes."""
    if candles is None or len(candles) < 4:
        return "FLAT"
    recent = candles.tail(lookback)
    mid = len(recent) // 2
    first_half, second_half = recent.iloc[:mid], recent.iloc[mid:]

    higher_high = second_half["high"].max() > first_half["high"].max()
    higher_low = second_half["low"].min() > first_half["low"].min()
    lower_high = second_half["high"].max() < first_half["high"].max()
    lower_low = second_half["low"].min() < first_half["low"].min()

    if higher_high and higher_low:
        return "UP"
    if lower_high and lower_low:
        return "DOWN"
    return "FLAT"


def classify_regime(spot: float, support: float | None, resistance: float | None, price_trend: str) -> str:
    """BULLISH/BEARISH/RANGE/UNCERTAIN. UNCERTAIN covers both missing/
    invalid levels and conflicting evidence (spot broke a level but trend
    disagrees) — rulebook Rule 28: "if signals conflict, WAIT"."""
    if support is None or resistance is None or support >= resistance:
        return "UNCERTAIN"
    if spot > resistance and price_trend == "UP":
        return "BULLISH"
    if spot < support and price_trend == "DOWN":
        return "BEARISH"
    if support <= spot <= resistance:
        return "RANGE"
    return "UNCERTAIN"


# --------------------------------------------------------------------------
# Proximity check (config.proximity_mode: 'pct' | 'atr')
# --------------------------------------------------------------------------


def has_crossed_level(spot: float, level: float, direction: str) -> bool:
    """True if spot is already on the far side of `level` in `direction`
    -- distinct from check_proximity ("close to"). Used at alert-send
    time to catch a level that's already been breached by the time a
    fresh spot re-check runs, since the cycle's own spot (fetched
    earlier) can be stale by several seconds to minutes (alert-staleness
    fix, Part A2). A strict comparison, matching detect_price_cross's
    convention -- merely touching the level doesn't count as crossed."""
    return spot > level if direction == "UP" else spot < level


def check_proximity(spot: float, level: float, config: OptionsConfig, atr: float | None = None) -> bool:
    if config.proximity_mode == "atr":
        if not atr:
            return False
        return abs(spot - level) <= config.atr_multiplier * atr
    return abs(spot - level) <= level * config.proximity_threshold_pct / 100


def find_nearest_level(
    spot: float, support_resistance: dict, config: OptionsConfig, atr: float | None = None
) -> dict | None:
    """Checks proximity to the nearest support and nearest resistance
    candidate by actual distance to spot (options_analytics.
    merge_support_resistance sorts each list ascending, so with more than
    one candidate the "nearest" one isn't reliably at a fixed index).
    Returns the closer in-proximity level, or None if neither is close
    enough to escalate to WATCH mode."""
    candidates = []
    resistance_levels = support_resistance.get("resistance") or []
    support_levels = support_resistance.get("support") or []

    if resistance_levels:
        nearest_resistance = min(resistance_levels, key=lambda level: abs(spot - level))
        if check_proximity(spot, nearest_resistance, config, atr):
            candidates.append({"level": nearest_resistance, "direction": "UP"})
    if support_levels:
        nearest_support = min(support_levels, key=lambda level: abs(spot - level))
        if check_proximity(spot, nearest_support, config, atr):
            candidates.append({"level": nearest_support, "direction": "DOWN"})

    if not candidates:
        return None
    return min(candidates, key=lambda c: abs(spot - c["level"]))


# --------------------------------------------------------------------------
# Dual-cadence state machine (options_engine_state row shape in/out)
# --------------------------------------------------------------------------


def decide_transition(
    engine_state: dict,
    spot: float,
    support_resistance: dict,
    now: datetime,
    config: OptionsConfig,
    atr: float | None = None,
    confirmation: dict | None = None,
) -> dict:
    """One state-machine step. NORMAL mode only escalates or stays; WATCH
    mode checks de-escalation first (distance, timeout), then, if
    `confirmation` evidence was supplied, checks false-breakout / confirmed.

    Returns {"next_state": {...}, "action": "NONE"|"ESCALATE"|"DEESCALATE"|
    "CONFIRMED"|"FALSE_BREAKOUT", "reason": str}. Never touches the DB or
    sends notifications — Task 7's orchestrator acts on `action`.
    """
    mode = engine_state.get("mode", "NORMAL")

    if mode == "NORMAL":
        nearest = find_nearest_level(spot, support_resistance, config, atr)
        if nearest is None:
            return {"next_state": {**engine_state, "mode": "NORMAL"}, "action": "NONE", "reason": "no level in proximity"}
        return {
            "next_state": {
                "mode": "WATCH",
                "watch_level": nearest["level"],
                "watch_direction": nearest["direction"],
                "watch_started_ts": now.isoformat(),
            },
            "action": "ESCALATE",
            "reason": f"spot {spot} within proximity of {nearest['direction']} level {nearest['level']}",
        }

    # mode == WATCH
    watch_level = engine_state.get("watch_level")
    watch_started_ts = engine_state.get("watch_started_ts")

    if watch_level and not check_proximity(spot, watch_level, config, atr):
        distance_pct = abs(spot - watch_level) / watch_level * 100
        if distance_pct > config.deescalate_threshold_pct:
            return _deescalate(now, f"spot moved {distance_pct:.2f}% away from watched level {watch_level} (beyond de-escalation threshold)")

    if watch_started_ts:
        age_minutes = (now - datetime.fromisoformat(watch_started_ts)).total_seconds() / 60
        if age_minutes > config.watch_timeout_minutes:
            return _deescalate(now, f"watch timed out after {age_minutes:.0f} minutes without confirmation")

    if confirmation is None:
        return {"next_state": engine_state, "action": "NONE", "reason": "watch continuing, no confirmation evidence yet"}

    if confirmation.get("false_breakout"):
        return _deescalate(now, "false breakout/breakdown confirmed", action="FALSE_BREAKOUT")

    if confirmation.get("confirmed"):
        return {
            "next_state": {"mode": "NORMAL", "watch_level": None, "watch_direction": None, "watch_started_ts": None},
            "action": "CONFIRMED",
            "reason": "breakout/breakdown confirmed -- handing off to trade selector",
        }

    return {"next_state": engine_state, "action": "NONE", "reason": "watch continuing, confirmation not yet met"}


def _deescalate(now: datetime, reason: str, action: str = "DEESCALATE") -> dict:
    return {
        "next_state": {"mode": "NORMAL", "watch_level": None, "watch_direction": None, "watch_started_ts": None},
        "action": action,
        "reason": reason,
    }


# --------------------------------------------------------------------------
# Breakout/breakdown confirmation (rulebook Sections 32-33) + false-breakout
# signature (Sections 24, 27)
# --------------------------------------------------------------------------


def detect_price_cross(candles: pd.DataFrame, level: float, direction: str, lookback: int = 10) -> bool:
    """True if ANY close within the trailing `lookback` candles moved
    beyond `level` in `direction` — not just the latest candle, so a
    breakout that later reversed still registers as "crossed" (needed to
    detect a false breakout, which by definition crossed and failed)."""
    if candles is None or candles.empty:
        return False
    closes = candles["close"].tail(lookback)
    return bool((closes > level).any()) if direction == "UP" else bool((closes < level).any())


def check_sustain(candles: pd.DataFrame, level: float, direction: str, sustain_minutes: int, candle_minutes: int = 1) -> bool:
    """Has price stayed beyond `level` for the trailing `sustain_minutes`
    (i.e. every one of those candles' closes stayed on the breakout side)?"""
    if candles is None or candles.empty:
        return False
    needed_candles = max(1, sustain_minutes // candle_minutes)
    closes = candles["close"].tail(needed_candles)
    return bool((closes > level).all()) if direction == "UP" else bool((closes < level).all())


def check_retest_hold(candles: pd.DataFrame, level: float, direction: str, config: OptionsConfig) -> bool | None:
    """Optional per rulebook Rule 12. Looks for a candle touching within
    `config.proximity_threshold_pct` of `level` (low, for an UP
    breakout; high, for DOWN), then checks whether the following candle
    closed back in the breakout direction. Returns None if no retest is
    detectable in the given candles — absence is not a rejection signal,
    just "not applicable"."""
    if candles is None or len(candles) < 2:
        return None
    tolerance = level * config.proximity_threshold_pct / 100
    for i in range(len(candles) - 1):
        row = candles.iloc[i]
        touch = row["low"] if direction == "UP" else row["high"]
        if abs(touch - level) <= tolerance:
            next_close = candles.iloc[i + 1]["close"]
            return bool(next_close > level) if direction == "UP" else bool(next_close < level)
    return None


def check_level_strike_oi_behavior(
    chain: list[dict], previous_snapshot: list[dict] | None, level_strike: float, option_side: str, config: OptionsConfig
) -> str | None:
    """classify_premium_oi() at the specific level strike/side —
    SHORT_COVERING supports the move (shorts closing = level weakening);
    SHORT_BUILDUP is the false-breakout/false-breakdown signature (fresh
    writing defending the level)."""
    row = next((r for r in chain if r["strike"] == level_strike and r["side"] == option_side), None)
    prev = next((r for r in (previous_snapshot or []) if r["strike"] == level_strike and r["side"] == option_side), None)
    if not row or not prev:
        return None
    return classify_premium_oi(row.get("ltp"), prev.get("ltp"), row.get("oi"), prev.get("oi"), config.classification_min_move_pct)


def evaluate_breakout_confirmation(
    candles: pd.DataFrame,
    level: float,
    direction: str,
    level_strike: float,
    option_side: str,
    chain: list[dict],
    previous_snapshot: list[dict] | None,
    volume_result: dict,
    regime: str,
    config: OptionsConfig,
) -> dict:
    """All of rulebook Sections 32 (breakout) / 33 (breakdown)'s
    confirmation checklist, combined. `direction` is 'UP' (breakout,
    option_side should be 'CE') or 'DOWN' (breakdown, option_side 'PE').
    `regime` is classify_regime()'s output for the current cycle.

    Returns a structured evidence dict; `confirmed` and `false_breakout`
    are never both True.
    """
    crossed = detect_price_cross(candles, level, direction)
    sustained = check_sustain(candles, level, direction, config.sustain_minutes)
    volume_confirmed = volume_result.get("confirmed", False)
    oi_classification = check_level_strike_oi_behavior(chain, previous_snapshot, level_strike, option_side, config)
    oi_supports = oi_classification == "SHORT_COVERING"
    oi_contradicts = oi_classification == "SHORT_BUILDUP"
    retest = check_retest_hold(candles, level, direction, config)
    retest_ok = retest is not False  # None (no retest occurred) or True both acceptable per Rule 12

    structure_agrees = (direction == "UP" and regime == "BULLISH") or (direction == "DOWN" and regime == "BEARISH")

    false_breakout = crossed and oi_contradicts and (not sustained or retest is False)

    confirmed = (
        crossed
        and sustained
        and volume_confirmed
        and oi_supports
        and structure_agrees
        and retest_ok
        and not false_breakout
    )

    return {
        "crossed": crossed,
        "sustained": sustained,
        "volume_confirmed": volume_confirmed,
        "oi_classification": oi_classification,
        "oi_supports": oi_supports,
        "structure_agrees": structure_agrees,
        "retest": retest,
        "confirmed": confirmed,
        "false_breakout": false_breakout,
    }


def summarize_confirmation_evidence(confirmation: dict) -> list[str]:
    """Human-readable bullet points explaining a confirmation decision —
    the "why", not just the "what", for Discord/Telegram advisory
    messages (rulebook Section 54's REASONS field)."""
    reasons = []
    if confirmation.get("crossed"):
        reasons.append("Price crossed the level")
    if confirmation.get("sustained"):
        reasons.append("Sustained beyond the level for the required window")
    if confirmation.get("volume_confirmed"):
        reasons.append("Volume confirmed the move")

    oi_classification = confirmation.get("oi_classification")
    if oi_classification == "SHORT_COVERING":
        reasons.append("Level-strike OI shows short covering (shorts closing, level weakening)")
    elif oi_classification == "SHORT_BUILDUP":
        reasons.append("Level-strike OI shows fresh short build-up (against the move)")

    if confirmation.get("retest") is True:
        reasons.append("Retest of the level held")
    elif confirmation.get("retest") is False:
        reasons.append("Retest of the level failed")

    if confirmation.get("structure_agrees"):
        reasons.append("Broader trend structure agrees")

    return reasons


# --------------------------------------------------------------------------
# Hard rejection rules — market-level only (rulebook Section 42, rules 1-9, 18)
# --------------------------------------------------------------------------


def evaluate_market_rejections(evidence: dict, config: OptionsConfig) -> list[str]:
    """Rules evaluable before a trade candidate exists. See module
    docstring for the Task 5/Task 6 scope split. Every `evidence` key is
    optional — a missing/None key means "not evaluated at this stage",
    never triggers a rejection by omission.
    """
    reasons = []
    if evidence.get("data_stale"):
        reasons.append("data stale")
    if evidence.get("critical_data_missing"):
        reasons.append("critical data missing")
    if evidence.get("no_clear_setup"):
        reasons.append("no clear setup")
    if evidence.get("mid_range"):
        reasons.append("market in middle of range")
    if evidence.get("breakout_unconfirmed"):
        reasons.append("breakout unconfirmed")
    if evidence.get("breakdown_unconfirmed"):
        reasons.append("breakdown unconfirmed")
    if evidence.get("conflicting_signals"):
        reasons.append("conflicting signals")

    spread_pct = evidence.get("spread_pct")
    if spread_pct is not None and spread_pct > config.max_spread_pct:
        reasons.append("excessive bid-ask spread")

    oi = evidence.get("oi")
    if oi is not None and oi < config.min_oi:
        reasons.append("poor liquidity (OI below minimum)")

    volume = evidence.get("volume")
    if volume is not None and volume < config.min_volume:
        reasons.append("poor liquidity (volume below minimum)")

    if evidence.get("market_structure_changed"):
        reasons.append("market structure changed before entry")

    return reasons


# --------------------------------------------------------------------------
# Scoring (rulebook Section 43 — weights are load-bearing, sub-scores are not)
# --------------------------------------------------------------------------

SCORE_WEIGHT_FIELDS = {
    "trend_pa": "score_weight_trend_pa",
    "confirmation": "score_weight_confirmation",
    "oi_structure": "score_weight_oi_structure",
    "delta_oi": "score_weight_delta_oi",
    "volume": "score_weight_volume",
    "rr": "score_weight_rr",
    "iv_greeks": "score_weight_iv_greeks",
    "pcr": "score_weight_pcr",
    "liquidity": "score_weight_liquidity",
}


def score_setup(sub_scores: dict, config: OptionsConfig) -> dict:
    """Weighted sum per rulebook Section 43. Each `sub_scores` value is
    0-100 (percentage of that category's max); a missing category scores
    0 (e.g. "rr"/"liquidity" before Task 6's Trade Selector has run —
    this function only computes the number, whatever calls it applies
    the min_score/hard-rejection gate)."""
    total = 0.0
    components = {}
    for key, weight_field in SCORE_WEIGHT_FIELDS.items():
        weight = getattr(config, weight_field)
        sub = sub_scores.get(key) or 0
        contribution = weight * (sub / 100)
        components[key] = contribution
        total += contribution
    return {"score": round(total), "components": components}


def score_trend_pa(regime: str) -> float:
    return 100.0 if regime in ("BULLISH", "BEARISH") else 0.0


def score_confirmation(confirmation: dict | None) -> float:
    if not confirmation:
        return 0.0
    checks = ("crossed", "sustained", "volume_confirmed", "oi_supports")
    passed = sum(1 for check in checks if confirmation.get(check))
    retest_bonus = 1 if confirmation.get("retest") else 0
    return (passed + retest_bonus) / (len(checks) + 1) * 100


def score_oi_structure(oi_classification: str | None) -> float:
    if oi_classification == "SHORT_COVERING":
        return 100.0
    if oi_classification == "SHORT_BUILDUP":
        return 0.0
    return 50.0


def score_delta_oi(oi_change: float | None, previous_oi: float | None) -> float:
    if not oi_change or not previous_oi:
        return 0.0
    pct_move = abs(oi_change) / previous_oi * 100
    return min(100.0, pct_move * 5)


def score_volume(volume_result: dict) -> float:
    return 100.0 if volume_result.get("confirmed") else 0.0


def score_iv_greeks(greeks_crosscheck: list[dict]) -> float:
    if not greeks_crosscheck:
        return 50.0  # no data -- neutral, not a penalty (cross-check is informational only)
    return 0.0 if any(row.get("large_divergence") for row in greeks_crosscheck) else 100.0


def score_pcr(pcr: float | None) -> float:
    if pcr is None:
        return 50.0
    return 100.0 if 0.7 <= pcr <= 1.5 else 50.0
