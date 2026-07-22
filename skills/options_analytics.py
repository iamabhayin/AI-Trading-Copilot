"""Options Analytics (Phase 15 Task 4) — deterministic, pure-function
analysis over an already-fetched option chain. No network or DB access
anywhere in this module: every function takes plain data (list[dict] /
pandas DataFrame) in and returns plain data out, so it's unit-testable
against fixture chains reproducing docs/options-rulebook.md's worked
examples.

Chain row shape (matches skills/angel_client.py's joined output /
option_chain_snapshots columns): {"strike": float, "side": "CE"|"PE",
"ltp": float, "oi": float, "volume": float, "bid": float, "ask": float,
"iv": float, "delta": float, "gamma": float, "theta": float, "vega": float}.

Golden rule this module encodes (rulebook Section 17): OPTION CHAIN
PRODUCES A HYPOTHESIS. Nothing here decides BUY/WAIT/NO_TRADE — that's
options_rules_engine.py (Task 5), which consumes this module's output.
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from py_vollib.black_scholes.greeks.analytical import delta as bs_delta
from py_vollib.black_scholes.implied_volatility import implied_volatility as bs_iv

from config.options_config import OptionsConfig

CLASSIFICATIONS = ("LONG_BUILDUP", "SHORT_BUILDUP", "SHORT_COVERING", "LONG_UNWINDING", "NEUTRAL")


# --------------------------------------------------------------------------
# ATM / strike window (rulebook Section 6, Section 2's "boundaries unclear" rule)
# --------------------------------------------------------------------------


def compute_atm(strikes: list[float], spot: float) -> float:
    """Nearest listed strike to spot — never assume fixed 50-point
    spacing (rulebook Section 6). `strikes` must be actually-listed
    strikes, not derived from a fixed increment."""
    return min(strikes, key=lambda strike: abs(strike - spot))


def select_strike_window(chain: list[dict], atm: float, config: OptionsConfig) -> list[dict]:
    """ATM ± strike_window_primary listed strikes, expanded to ± extended
    when boundaries are unclear (config.boundary_min_share) or a major OI
    concentration sits outside the primary window. Re-run every cycle —
    never cache a window as spot moves (rulebook Section 28)."""
    strikes = sorted({row["strike"] for row in chain})
    if atm not in strikes:
        return []
    atm_index = strikes.index(atm)

    def _window_strikes(radius: int) -> set[float]:
        lo = max(0, atm_index - radius)
        hi = min(len(strikes), atm_index + radius + 1)
        return set(strikes[lo:hi])

    primary_strikes = _window_strikes(config.strike_window_primary)
    primary_rows = [row for row in chain if row["strike"] in primary_strikes]

    if _boundaries_unclear(chain, primary_strikes, primary_rows, config.boundary_min_share):
        extended_strikes = _window_strikes(config.strike_window_extended)
        return [row for row in chain if row["strike"] in extended_strikes]

    return primary_rows


def _boundaries_unclear(
    chain: list[dict], primary_strikes: set[float], primary_rows: list[dict], boundary_min_share: float
) -> bool:
    for side in ("CE", "PE"):
        side_rows = [row for row in primary_rows if row["side"] == side]
        total_oi = sum(row.get("oi") or 0 for row in side_rows)
        max_in_window_oi = max((row.get("oi") or 0 for row in side_rows), default=0)
        if total_oi <= 0 or max_in_window_oi < total_oi * boundary_min_share:
            return True  # no single strike dominates this window/side

        outside_max_oi = max(
            (row.get("oi") or 0 for row in chain if row["side"] == side and row["strike"] not in primary_strikes),
            default=0,
        )
        if outside_max_oi > max_in_window_oi:
            return True  # a bigger OI concentration sits outside the primary window

    return False


# --------------------------------------------------------------------------
# Change in OI — vs previous snapshot, first-of-day, and prior trading day
# (rulebook Section 4/15: never trust a single static OI number)
# --------------------------------------------------------------------------


def _index_by_strike_side(rows: list[dict]) -> dict[tuple[float, str], dict]:
    return {(row["strike"], row["side"]): row for row in rows}


def _delta(current, previous):
    return (current - previous) if current is not None and previous is not None else None


def annotate_change_in_oi(
    chain: list[dict],
    previous_snapshot: list[dict] | None = None,
    first_of_day_snapshot: list[dict] | None = None,
    prior_day_last_snapshot: list[dict] | None = None,
) -> list[dict]:
    """Augment each chain row with oi_change_vs_previous/_first_of_day/
    _prior_day. Any historical snapshot that isn't supplied (e.g. first
    cycle of the day) yields None for that field rather than guessing."""
    prev_idx = _index_by_strike_side(previous_snapshot or [])
    first_idx = _index_by_strike_side(first_of_day_snapshot or [])
    prior_day_idx = _index_by_strike_side(prior_day_last_snapshot or [])

    annotated = []
    for row in chain:
        key = (row["strike"], row["side"])
        prev = prev_idx.get(key)
        first = first_idx.get(key)
        prior_day = prior_day_idx.get(key)
        annotated.append(
            {
                **row,
                "oi_change_vs_previous": _delta(row.get("oi"), prev.get("oi") if prev else None),
                "oi_change_vs_first_of_day": _delta(row.get("oi"), first.get("oi") if first else None),
                "oi_change_vs_prior_day": _delta(row.get("oi"), prior_day.get("oi") if prior_day else None),
            }
        )
    return annotated


# --------------------------------------------------------------------------
# PCR (rulebook Section 29 — contextual evidence only, never a signal alone)
# --------------------------------------------------------------------------


def compute_pcr(chain: list[dict]) -> float | None:
    """Total PE OI / total CE OI over whatever chain/window is passed in."""
    total_pe_oi = sum(row.get("oi") or 0 for row in chain if row["side"] == "PE")
    total_ce_oi = sum(row.get("oi") or 0 for row in chain if row["side"] == "CE")
    if total_ce_oi == 0:
        return None
    return total_pe_oi / total_ce_oi


# --------------------------------------------------------------------------
# Max Pain (rulebook Section 30 — lowest-priority context, never a target)
# --------------------------------------------------------------------------


def compute_max_pain(chain: list[dict]) -> float | None:
    """Strike minimizing aggregate intrinsic-value payout to option
    holders across all listed strikes in `chain`, based on current OI."""
    strikes = sorted({row["strike"] for row in chain})
    if not strikes:
        return None

    best_strike = None
    best_payout = None
    for candidate in strikes:
        payout = 0.0
        for row in chain:
            oi = row.get("oi") or 0
            if row["side"] == "CE":
                payout += max(0.0, candidate - row["strike"]) * oi
            elif row["side"] == "PE":
                payout += max(0.0, row["strike"] - candidate) * oi
        if best_payout is None or payout < best_payout:
            best_payout = payout
            best_strike = candidate
    return best_strike


# --------------------------------------------------------------------------
# Premium + OI classification (rulebook Section 16)
# --------------------------------------------------------------------------


def classify_premium_oi(
    current_premium: float | None,
    previous_premium: float | None,
    current_oi: float | None,
    previous_oi: float | None,
    min_move_pct: float,
) -> str | None:
    """Premium up + OI up -> LONG_BUILDUP; premium down + OI up ->
    SHORT_BUILDUP; premium up + OI down -> SHORT_COVERING; premium down +
    OI down -> LONG_UNWINDING. Returns "NEUTRAL" if either move is below
    `min_move_pct` (noise) or the moves don't fit one of the four
    quadrants (e.g. flat premium). Returns None if data is missing."""
    if None in (current_premium, previous_premium, current_oi, previous_oi):
        return None
    if not previous_premium or not previous_oi:
        return None

    premium_change_pct = (current_premium - previous_premium) / previous_premium * 100
    oi_change_pct = (current_oi - previous_oi) / previous_oi * 100

    premium_up = premium_change_pct > min_move_pct
    premium_down = premium_change_pct < -min_move_pct
    oi_up = oi_change_pct > min_move_pct
    oi_down = oi_change_pct < -min_move_pct

    if premium_up and oi_up:
        return "LONG_BUILDUP"
    if premium_down and oi_up:
        return "SHORT_BUILDUP"
    if premium_up and oi_down:
        return "SHORT_COVERING"
    if premium_down and oi_down:
        return "LONG_UNWINDING"
    return "NEUTRAL"


def classify_chain(chain: list[dict], previous_snapshot: list[dict] | None, config: OptionsConfig) -> list[dict]:
    """classify_premium_oi() applied per contract, using `previous_snapshot`
    (immediately preceding cycle) as the comparison point."""
    prev_idx = _index_by_strike_side(previous_snapshot or [])
    classified = []
    for row in chain:
        prev = prev_idx.get((row["strike"], row["side"]))
        classification = classify_premium_oi(
            row.get("ltp"),
            prev.get("ltp") if prev else None,
            row.get("oi"),
            prev.get("oi") if prev else None,
            config.classification_min_move_pct,
        )
        classified.append({**row, "classification": classification})
    return classified


# --------------------------------------------------------------------------
# Support / resistance (rulebook Sections 19-20, 28)
# --------------------------------------------------------------------------


def detect_oi_support_resistance(chain: list[dict], top_n: int = 1) -> dict:
    """Strongest PE OI strike(s) -> support candidates; strongest CE OI
    strike(s) -> resistance candidates. A hypothesis, not a guarantee
    (rulebook Section 17's golden rule)."""
    pe_rows = sorted((row for row in chain if row["side"] == "PE" and row.get("oi")), key=lambda r: r["oi"], reverse=True)
    ce_rows = sorted((row for row in chain if row["side"] == "CE" and row.get("oi")), key=lambda r: r["oi"], reverse=True)
    return {
        "support": [row["strike"] for row in pe_rows[:top_n]],
        "resistance": [row["strike"] for row in ce_rows[:top_n]],
    }


def detect_next_level(chain: list[dict], side: str, beyond_level: float, direction: str, config: OptionsConfig) -> float | None:
    """Next OI concentration beyond the current wall, same option side --
    e.g. the next PE-OI cluster below a support wall, shown in alerts as
    a forward-looking target if the wall breaks (Part B). Purely
    informational/display, never a trade trigger: requires the strongest
    beyond-the-wall strike to hold at least config.next_level_min_share of
    the total OI out there, so a negligible strike isn't flagged as a
    meaningful level. 'Beyond' means further from spot in the breakout
    direction -- lower strikes for a support/DOWN wall, higher strikes for
    a resistance/UP wall."""
    beyond_rows = [
        row
        for row in chain
        if row["side"] == side and (row["strike"] < beyond_level if direction == "DOWN" else row["strike"] > beyond_level)
    ]
    if not beyond_rows:
        return None
    total_oi = sum(row.get("oi") or 0 for row in beyond_rows)
    if total_oi <= 0:
        return None
    best = max(beyond_rows, key=lambda r: r.get("oi") or 0)
    best_oi = best.get("oi") or 0
    if best_oi < total_oi * config.next_level_min_share:
        return None
    return best["strike"]


def detect_swing_levels(candles: pd.DataFrame, lookback: int = 20) -> dict:
    """Simple price-structure swing high/low over the trailing `lookback`
    candles, to merge with OI-based levels."""
    if candles is None or candles.empty:
        return {"swing_high": None, "swing_low": None}
    highs = candles["high"].dropna().tail(lookback)
    lows = candles["low"].dropna().tail(lookback)
    return {
        "swing_high": float(highs.max()) if not highs.empty else None,
        "swing_low": float(lows.min()) if not lows.empty else None,
    }


def compute_atr(candles: pd.DataFrame, period: int = 14) -> float | None:
    """Standard Average True Range over the trailing `period` candles —
    used for proximity_mode='atr' and the Trade Selector's holding-period
    estimate. Returns None if there isn't enough candle history."""
    if candles is None or len(candles) < period + 1:
        return None
    high, low, prev_close = candles["high"], candles["low"], candles["close"].shift(1)
    true_range = pd.concat(
        [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = true_range.tail(period).mean()
    return float(atr) if pd.notna(atr) else None


def detect_zone_merge(oi_level: float, structure_level: float | None, spot: float, config: OptionsConfig) -> dict | None:
    """Whether an OI-wall level and a price-structure level (for the same
    side) sit close enough together to be treated as one zone: a real
    incident showed an alert watching a structure level 22 points from
    the actual OI wall it was supposedly guarding, with the wall itself
    still untouched -- a structure edge that close to the wall has no
    room to be an independent trade trigger.

    Returns None if `structure_level` is missing or the two aren't within
    `config.zone_merge_threshold_pct` of spot; otherwise a dict with both
    edges and the zone width in points. **The OI wall is always the
    trigger** -- callers must never watch/confirm a breakout against the
    structure edge once a zone is detected.
    """
    if structure_level is None:
        return None
    threshold = spot * config.zone_merge_threshold_pct / 100
    if abs(structure_level - oi_level) >= threshold:
        return None
    return {"oi_wall": oi_level, "structure_level": structure_level, "width_points": abs(structure_level - oi_level)}


def merge_support_resistance(oi_based: dict, price_based: dict, spot: float, config: OptionsConfig) -> dict:
    """Merge OI-derived levels with price-structure swing highs/lows into
    one candidate set per side. A structure level that zone-merges with
    an OI wall for the same side (detect_zone_merge) is dropped from the
    candidate set entirely -- it's zone context, never an independent
    trigger; the OI wall alone remains the actionable level."""
    oi_support = oi_based.get("support") or []
    oi_resistance = oi_based.get("resistance") or []
    support = set(oi_support)
    resistance = set(oi_resistance)

    swing_low = price_based.get("swing_low")
    if swing_low is not None and not any(detect_zone_merge(level, swing_low, spot, config) for level in oi_support):
        support.add(swing_low)

    swing_high = price_based.get("swing_high")
    if swing_high is not None and not any(detect_zone_merge(level, swing_high, spot, config) for level in oi_resistance):
        resistance.add(swing_high)

    return {"support": sorted(support), "resistance": sorted(resistance)}


def detect_level_migration(current_levels: dict, previous_levels: dict | None) -> dict:
    """Did the strongest support/resistance strike shift between cycles?
    (rulebook Section 28 — never keep stale levels all day.) Pure
    comparison of the top candidate per side; no directional judgment."""
    previous_levels = previous_levels or {}
    migration = {}
    for key in ("support", "resistance"):
        current_top = (current_levels.get(key) or [None])[0]
        previous_top = (previous_levels.get(key) or [None])[0]
        migration[key] = {
            "current": current_top,
            "previous": previous_top,
            "migrated": current_top is not None and previous_top is not None and current_top != previous_top,
        }
    return migration


# --------------------------------------------------------------------------
# Volume confirmation (config.volume_confirm_multiplier, default 1.5x)
# --------------------------------------------------------------------------


def check_volume_confirmation(candles: pd.DataFrame, config: OptionsConfig, lookback: int = 20) -> dict:
    """Current candle's volume vs the trailing `lookback`-candle rolling
    average (excluding the current candle itself)."""
    if candles is None or len(candles) < 2:
        return {"confirmed": False, "current_volume": None, "average_volume": None}

    volumes = candles["volume"].dropna()
    if len(volumes) < 2:
        return {"confirmed": False, "current_volume": None, "average_volume": None}

    current = float(volumes.iloc[-1])
    history = volumes.iloc[:-1].tail(lookback)
    average = float(history.mean()) if not history.empty else None

    confirmed = average is not None and average > 0 and current >= average * config.volume_confirm_multiplier
    return {"confirmed": confirmed, "current_volume": current, "average_volume": average}


# --------------------------------------------------------------------------
# Greeks sanity cross-check via py_vollib (config-gated, informational only)
# --------------------------------------------------------------------------


def years_to_expiry(expiry: str, now: datetime) -> float:
    """`expiry` in Angel One's scrip-master DDMMMYYYY format (e.g.
    '24JUL2025'). Assumes a 15:30 IST market-close expiry instant. Floors
    at ~1 minute to avoid a zero/negative time-to-expiry on expiry day
    itself, which would break py_vollib's Black-Scholes math."""
    expiry_date = datetime.strptime(expiry, "%d%b%Y").date()
    expiry_dt = datetime.combine(expiry_date, datetime.min.time().replace(hour=15, minute=30), tzinfo=now.tzinfo)
    seconds_remaining = max((expiry_dt - now).total_seconds(), 60)
    return seconds_remaining / (365.0 * 24 * 3600)


def crosscheck_greeks(row: dict, spot: float, time_to_expiry_years: float, config: OptionsConfig) -> dict | None:
    """Recompute delta via py_vollib Black-Scholes for one contract and
    compare against the broker-supplied delta — informational only, never
    blocks (rulebook implementation notes). Returns None if inputs are
    insufficient or py_vollib fails to converge (e.g. deep ITM/OTM
    numerical edge cases), never raises."""
    if not config.greeks_crosscheck_enabled:
        return None

    ltp = row.get("ltp")
    strike = row.get("strike")
    broker_delta = row.get("delta")
    broker_iv = row.get("iv")
    if not (spot and strike and time_to_expiry_years and ltp and broker_delta is not None):
        return None

    flag = "c" if row["side"] == "CE" else "p"
    try:
        iv_fraction = (broker_iv / 100) if broker_iv else bs_iv(ltp, spot, strike, time_to_expiry_years, config.risk_free_rate, flag)
        recomputed_delta = bs_delta(flag, spot, strike, time_to_expiry_years, config.risk_free_rate, iv_fraction)
    except Exception:
        return None

    divergence = abs(recomputed_delta - broker_delta)
    return {
        # py_vollib returns numpy scalar types; cast to plain Python types
        # so this dict JSON-serializes cleanly once it reaches
        # options_advisories.payload_json (Task 5+) without a custom encoder.
        "recomputed_delta": float(recomputed_delta),
        "broker_delta": broker_delta,
        "divergence": float(divergence),
        "large_divergence": bool(divergence > config.greeks_divergence_threshold),
    }


def crosscheck_greeks_near_atm(
    chain: list[dict], spot: float, time_to_expiry_years: float, config: OptionsConfig, strikes_range: int = 2
) -> list[dict]:
    """crosscheck_greeks() for ATM ± strikes_range listed strikes only."""
    if not config.greeks_crosscheck_enabled:
        return []

    strikes = sorted({row["strike"] for row in chain})
    if not strikes:
        return []
    atm = compute_atm(strikes, spot)
    atm_index = strikes.index(atm)
    lo = max(0, atm_index - strikes_range)
    hi = min(len(strikes), atm_index + strikes_range + 1)
    near_atm_strikes = set(strikes[lo:hi])

    results = []
    for row in chain:
        if row["strike"] not in near_atm_strikes:
            continue
        result = crosscheck_greeks(row, spot, time_to_expiry_years, config)
        if result:
            results.append({**result, "strike": row["strike"], "side": row["side"]})
    return results


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def build_market_analysis(
    chain: list[dict],
    spot: float,
    config: OptionsConfig,
    candles: pd.DataFrame | None = None,
    previous_snapshot: list[dict] | None = None,
    first_of_day_snapshot: list[dict] | None = None,
    prior_day_last_snapshot: list[dict] | None = None,
    previous_levels: dict | None = None,
    time_to_expiry_years: float | None = None,
) -> dict:
    """Ties every analytics building block together into the structured
    result options_rules_engine.py (Task 5) consumes. Every optional
    input degrades gracefully (None/empty) rather than raising, since a
    missing history slice (e.g. first cycle of the day) is expected, not
    an error.
    """
    strikes = sorted({row["strike"] for row in chain})
    atm = compute_atm(strikes, spot) if strikes else None
    window = select_strike_window(chain, atm, config) if atm is not None else []

    annotated = annotate_change_in_oi(window, previous_snapshot, first_of_day_snapshot, prior_day_last_snapshot)
    classified = classify_chain(annotated, previous_snapshot, config)

    oi_levels = detect_oi_support_resistance(window)
    price_levels = detect_swing_levels(candles) if candles is not None else {"swing_high": None, "swing_low": None}
    levels = merge_support_resistance(oi_levels, price_levels, spot, config)
    migration = detect_level_migration(levels, previous_levels)

    volume = check_volume_confirmation(candles, config) if candles is not None else {
        "confirmed": False,
        "current_volume": None,
        "average_volume": None,
    }

    greeks_crosscheck = (
        crosscheck_greeks_near_atm(window, spot, time_to_expiry_years, config)
        if time_to_expiry_years is not None
        else []
    )

    return {
        "atm": atm,
        "strike_window": sorted({row["strike"] for row in window}),
        "chain": classified,
        "pcr": compute_pcr(window),
        "max_pain": compute_max_pain(window),
        "support_resistance": levels,
        # Unmerged sources behind `support_resistance`, for callers that
        # want to label which type of evidence a level came from (OI wall
        # vs. price-structure swing high/low) rather than a flat list.
        "oi_levels": oi_levels,
        "price_levels": price_levels,
        "level_migration": migration,
        "volume": volume,
        "greeks_crosscheck": greeks_crosscheck,
    }
