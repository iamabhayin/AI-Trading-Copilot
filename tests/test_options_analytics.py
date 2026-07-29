"""Tests for skills/options_analytics.py — Phase 15 Task 4.

The Section 18 fixture reproduces docs/options-rulebook.md's base
example table verbatim (OI/premium values, in raw units rather than
lakhs). NOTE: the rulebook's prose states "PCR = 1.20" for this table,
but PCR = total PE OI / total CE OI computed from the table's own numbers
is 173L/205L = 0.8439 — the two don't match, and the S/R/Max-Pain
narrative in the rulebook otherwise checks out arithmetically against
this same table. This looks like an inconsistency in the source
document rather than a different PCR formula, so these tests assert the
mathematically-derived value from the table, not the prose's stated
1.20. Flagged for the user to check against the rulebook's source.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from config.options_config import OptionsConfig
from skills.options_analytics import (
    annotate_change_in_oi,
    build_market_analysis,
    check_volume_confirmation,
    classify_chain,
    classify_premium_oi,
    compute_atm,
    compute_atr,
    compute_max_pain,
    compute_pcr,
    crosscheck_greeks,
    crosscheck_greeks_near_atm,
    detect_level_migration,
    detect_oi_support_resistance,
    detect_swing_levels,
    merge_support_resistance,
    select_strike_window,
    years_to_expiry,
)

IST = ZoneInfo("Asia/Kolkata")


def _config(**overrides) -> OptionsConfig:
    return OptionsConfig(**overrides)


# --------------------------------------------------------------------------
# Rulebook Section 18 — base example table
# --------------------------------------------------------------------------


def _section_18_chain() -> list[dict]:
    # Strike: (CE OI, CE premium, PE premium, PE OI) — units, not lakhs.
    table = {
        24700: (800_000, 330, 25, 1_500_000),
        24800: (1_200_000, 245, 40, 6_000_000),
        24900: (2_000_000, 170, 70, 3_500_000),
        25000: (3_000_000, 110, 105, 3_000_000),
        25100: (4_000_000, 65, 165, 1_800_000),
        25200: (7_000_000, 35, 240, 1_000_000),
        25300: (2_500_000, 18, 325, 500_000),
    }
    chain = []
    for strike, (ce_oi, ce_prem, pe_prem, pe_oi) in table.items():
        chain.append({"strike": float(strike), "side": "CE", "ltp": ce_prem, "oi": ce_oi})
        chain.append({"strike": float(strike), "side": "PE", "ltp": pe_prem, "oi": pe_oi})
    return chain


def _section_18_previous_snapshot() -> list[dict]:
    # Reconstructed from the table's stated CE/PE ΔOI columns:
    # previous_oi = current_oi - delta_oi.
    ce_delta = {24700: 0, 24800: -100_000, 24900: 200_000, 25000: 400_000, 25100: 800_000, 25200: 2_000_000, 25300: 300_000}
    pe_delta = {24700: 200_000, 24800: 1_500_000, 24900: 500_000, 25000: 500_000, 25100: 200_000, 25200: -100_000, 25300: -200_000}
    previous = []
    for strike in ce_delta:
        ce_row = next(r for r in _section_18_chain() if r["strike"] == strike and r["side"] == "CE")
        previous.append({"strike": float(strike), "side": "CE", "ltp": ce_row["ltp"], "oi": ce_row["oi"] - ce_delta[strike]})
    for strike in pe_delta:
        pe_row = next(r for r in _section_18_chain() if r["strike"] == strike and r["side"] == "PE")
        previous.append({"strike": float(strike), "side": "PE", "ltp": pe_row["ltp"], "oi": pe_row["oi"] - pe_delta[strike]})
    return previous


def test_section_18_atm_is_25000():
    chain = _section_18_chain()
    strikes = sorted({row["strike"] for row in chain})
    assert compute_atm(strikes, 25000.0) == 25000.0


def test_section_18_strike_window_is_clear_and_unexpanded():
    chain = _section_18_chain()
    config = _config()
    window = select_strike_window(chain, 25000.0, config)
    # Only 7 strikes exist total, so the "window" is the whole chain either way,
    # but this confirms the boundary-clarity check doesn't misfire and expand
    # into something larger than what's available.
    assert {row["strike"] for row in window} == {24700, 24800, 24900, 25000, 25100, 25200, 25300}


def test_section_18_pcr_matches_table_arithmetic_not_rulebook_prose():
    chain = _section_18_chain()
    # 173L PE / 205L CE = 0.8439... — see module docstring re: the rulebook's
    # stated "PCR = 1.20" not matching its own table.
    assert compute_pcr(chain) == pytest.approx(0.8439024390243902, rel=1e-9)


def test_section_18_max_pain_is_25000():
    # Verified by hand: aggregate intrinsic payout is minimized at 25000
    # among the table's 7 candidate strikes.
    chain = _section_18_chain()
    assert compute_max_pain(chain) == 25000.0


def test_section_18_support_resistance_matches_rulebook_narrative():
    chain = _section_18_chain()
    levels = detect_oi_support_resistance(chain)
    assert levels["support"] == [24800.0]  # strongest PE OI = 60L
    assert levels["resistance"] == [25200.0]  # strongest CE OI = 70L


def test_section_18_change_in_oi_matches_stated_deltas():
    chain = _section_18_chain()
    previous = _section_18_previous_snapshot()
    annotated = annotate_change_in_oi(chain, previous_snapshot=previous)

    expected_ce_delta = {24700: 0, 24800: -100_000, 24900: 200_000, 25000: 400_000, 25100: 800_000, 25200: 2_000_000, 25300: 300_000}
    expected_pe_delta = {24700: 200_000, 24800: 1_500_000, 24900: 500_000, 25000: 500_000, 25100: 200_000, 25200: -100_000, 25300: -200_000}

    for row in annotated:
        expected = expected_ce_delta if row["side"] == "CE" else expected_pe_delta
        assert row["oi_change_vs_previous"] == expected[int(row["strike"])]
        assert row["oi_change_vs_first_of_day"] is None
        assert row["oi_change_vs_prior_day"] is None


def test_build_market_analysis_exposes_unmerged_oi_and_price_levels():
    # Regression coverage for the OI-wall vs. price-structure distinction
    # in support/resistance -- callers (e.g. alert formatting) need both
    # the merged support_resistance list AND which source each came from.
    chain = _section_18_chain()
    config = _config()
    candles = pd.DataFrame(
        {
            "high": [25190.0, 25210.0, 25195.0],
            "low": [25150.0, 25160.0, 25155.0],
            "close": [25170.0, 25180.0, 25175.0],
            "volume": [1000, 1200, 1100],
        }
    )

    analysis = build_market_analysis(chain, spot=25000.0, config=config, candles=candles)

    assert analysis["oi_levels"] == {"support": [24800.0], "resistance": [25200.0]}
    assert analysis["price_levels"]["swing_high"] == 25210.0
    assert analysis["price_levels"]["swing_low"] == 25150.0
    # The merged list still contains both -- unaffected by this addition.
    assert 24800.0 in analysis["support_resistance"]["support"]
    assert 25150.0 in analysis["support_resistance"]["support"]


# --------------------------------------------------------------------------
# Premium + OI classification — rulebook worked examples (Sections 22/24/25)
# --------------------------------------------------------------------------


def test_classify_section_22_bullish_breakout_short_covering():
    # 25200 CE: OI 70L -> 55L -> 40L, premium 35 -> 55 -> 75.
    result = classify_premium_oi(current_premium=75, previous_premium=55, current_oi=4_000_000, previous_oi=5_500_000, min_move_pct=2.0)
    assert result == "SHORT_COVERING"


def test_classify_section_24_false_breakout_short_buildup():
    # 25200 CE: OI 70L -> 80L -> 95L, premium 40 -> 35 -> 28.
    result = classify_premium_oi(current_premium=28, previous_premium=35, current_oi=9_500_000, previous_oi=8_000_000, min_move_pct=2.0)
    assert result == "SHORT_BUILDUP"


def test_classify_section_25_bearish_breakdown_pe_short_covering():
    # 24800 PE: OI 60L -> 45L -> 25L, premium 40 -> 65 -> 90.
    result = classify_premium_oi(current_premium=90, previous_premium=65, current_oi=2_500_000, previous_oi=4_500_000, min_move_pct=2.0)
    assert result == "SHORT_COVERING"


def test_classify_premium_oi_all_four_quadrants():
    assert classify_premium_oi(110, 100, 110, 100, 2.0) == "LONG_BUILDUP"
    assert classify_premium_oi(90, 100, 110, 100, 2.0) == "SHORT_BUILDUP"
    assert classify_premium_oi(110, 100, 90, 100, 2.0) == "SHORT_COVERING"
    assert classify_premium_oi(90, 100, 90, 100, 2.0) == "LONG_UNWINDING"


def test_classify_premium_oi_below_threshold_is_neutral():
    assert classify_premium_oi(100.5, 100, 100.5, 100, 2.0) == "NEUTRAL"


def test_classify_premium_oi_missing_data_returns_none():
    assert classify_premium_oi(None, 100, 110, 100, 2.0) is None
    assert classify_premium_oi(110, None, 110, 100, 2.0) is None
    assert classify_premium_oi(110, 100, 110, None, 2.0) is None


def test_classify_chain_applies_per_contract():
    chain = [{"strike": 25000.0, "side": "CE", "ltp": 110, "oi": 3_100_000}]
    previous = [{"strike": 25000.0, "side": "CE", "ltp": 100, "oi": 3_000_000}]
    config = _config()

    classified = classify_chain(chain, previous, config)

    assert classified[0]["classification"] == "LONG_BUILDUP"


# --------------------------------------------------------------------------
# Strike window boundary-expansion logic
# --------------------------------------------------------------------------


def _flat_window_fixture(strikes: list[int], oi_by_strike: dict[int, tuple[int, int]]) -> list[dict]:
    """oi_by_strike: strike -> (CE OI, PE OI)."""
    chain = []
    for strike in strikes:
        ce_oi, pe_oi = oi_by_strike[strike]
        chain.append({"strike": float(strike), "side": "CE", "ltp": 100.0, "oi": ce_oi})
        chain.append({"strike": float(strike), "side": "PE", "ltp": 100.0, "oi": pe_oi})
    return chain


def test_strike_window_expands_when_no_dominant_strike_in_primary():
    strikes = list(range(24400, 25650, 50))  # 25 strikes, ATM=25000 at index 12
    oi = {s: (100_000, 100_000) for s in strikes}  # perfectly flat -- no dominance
    chain = _flat_window_fixture(strikes, oi)
    config = _config(strike_window_primary=5, strike_window_extended=10, boundary_min_share=0.20)

    window = select_strike_window(chain, 25000.0, config)

    window_strikes = {row["strike"] for row in window}
    extended_expected = {float(s) for s in strikes[2:23]}  # atm_index 12 +/- 10
    assert window_strikes == extended_expected


def test_strike_window_expands_on_concentration_outside_primary():
    # ATM=25000 is index 12; primary radius 5 covers indices 7-17 (24750-25250);
    # extended radius 10 covers indices 2-22 (24500-25600). Index 6 (24700) is
    # just outside primary but inside extended -- the case this test targets.
    strikes = list(range(24400, 25650, 50))
    oi = {s: (50_000, 50_000) for s in strikes}
    oi[25000] = (1_000_000, 50_000)  # dominant ATM CE strike inside primary window
    oi[24700] = (2_000_000, 50_000)  # bigger CE concentration just outside primary, inside extended
    chain = _flat_window_fixture(strikes, oi)
    config = _config(strike_window_primary=5, strike_window_extended=10, boundary_min_share=0.20)

    window = select_strike_window(chain, 25000.0, config)

    window_strikes = {row["strike"] for row in window}
    assert 24700.0 in window_strikes  # only present once expanded to the extended window
    assert 24400.0 not in window_strikes  # still outside even the extended window


def test_strike_window_stays_primary_when_clear_and_no_outside_concentration():
    strikes = list(range(24400, 25650, 50))
    oi = {s: (50_000, 50_000) for s in strikes}
    oi[25000] = (1_000_000, 1_000_000)  # clearly dominant ATM strike, both sides
    chain = _flat_window_fixture(strikes, oi)
    config = _config(strike_window_primary=5, strike_window_extended=10, boundary_min_share=0.20)

    window = select_strike_window(chain, 25000.0, config)

    window_strikes = {row["strike"] for row in window}
    primary_expected = {float(s) for s in strikes[7:18]}  # atm_index 12 +/- 5
    assert window_strikes == primary_expected


def test_select_strike_window_atm_not_listed_returns_empty():
    chain = [{"strike": 25000.0, "side": "CE", "ltp": 100.0, "oi": 1000}]
    config = _config()
    assert select_strike_window(chain, 99999.0, config) == []


# --------------------------------------------------------------------------
# Support / resistance migration + price-structure merge
# --------------------------------------------------------------------------


def test_detect_swing_levels():
    candles = pd.DataFrame({"high": [25100, 25150, 25080], "low": [24950, 24980, 24900]})
    levels = detect_swing_levels(candles)
    assert levels["swing_high"] == 25150.0
    assert levels["swing_low"] == 24900.0


def test_compute_atr_constant_range():
    # 15 candles, each with a true range of exactly 50 (no gaps) -> ATR == 50.
    n = 15
    candles = pd.DataFrame({
        "high": [25050.0] * n,
        "low": [25000.0] * n,
        "close": [25025.0] * n,
    })
    assert compute_atr(candles, period=14) == pytest.approx(50.0)


def test_compute_atr_insufficient_data_returns_none():
    candles = pd.DataFrame({"high": [25050.0, 25060.0], "low": [25000.0, 25010.0], "close": [25025.0, 25030.0]})
    assert compute_atr(candles, period=14) is None


def test_detect_swing_levels_empty_candles():
    assert detect_swing_levels(pd.DataFrame()) == {"swing_high": None, "swing_low": None}


def test_merge_support_resistance_combines_oi_and_price():
    oi_based = {"support": [24800.0], "resistance": [25200.0]}
    price_based = {"swing_high": 25250.0, "swing_low": 24750.0}
    merged = merge_support_resistance(oi_based, price_based)
    assert merged == {"support": [24750.0, 24800.0], "resistance": [25200.0, 25250.0]}


def test_detect_level_migration_flags_a_shift():
    current = {"support": [24900.0], "resistance": [25200.0]}
    previous = {"support": [24800.0], "resistance": [25200.0]}
    migration = detect_level_migration(current, previous)
    assert migration["support"]["migrated"] is True
    assert migration["resistance"]["migrated"] is False


def test_detect_level_migration_no_previous_levels():
    current = {"support": [24900.0], "resistance": [25200.0]}
    migration = detect_level_migration(current, None)
    assert migration["support"]["migrated"] is False
    assert migration["support"]["previous"] is None


# --------------------------------------------------------------------------
# Volume confirmation
# --------------------------------------------------------------------------


def test_volume_confirmation_fires_above_multiplier():
    candles = pd.DataFrame({"volume": [1000] * 20 + [1600]})
    config = _config(volume_confirm_multiplier=1.5)
    result = check_volume_confirmation(candles, config)
    assert result["confirmed"] is True
    assert result["current_volume"] == 1600.0
    assert result["average_volume"] == 1000.0


def test_volume_confirmation_does_not_fire_below_multiplier():
    candles = pd.DataFrame({"volume": [1000] * 20 + [1200]})
    config = _config(volume_confirm_multiplier=1.5)
    result = check_volume_confirmation(candles, config)
    assert result["confirmed"] is False


def test_volume_confirmation_insufficient_candles():
    result = check_volume_confirmation(pd.DataFrame({"volume": [1000]}), _config())
    assert result == {"confirmed": False, "current_volume": None, "average_volume": None}


# --------------------------------------------------------------------------
# Greeks cross-check (py_vollib) — informational only, never blocks
# --------------------------------------------------------------------------


def test_years_to_expiry_computes_fraction():
    now = datetime(2026, 7, 20, 9, 15, tzinfo=IST)
    years = years_to_expiry("21JUL2026", now)
    assert 0 < years < (2 / 365)  # about 1 day away


def test_years_to_expiry_floors_at_one_minute_on_expiry_day():
    now = datetime(2026, 7, 21, 15, 29, 30, tzinfo=IST)  # 30s before 15:30 close
    years = years_to_expiry("21JUL2026", now)
    assert years == pytest.approx(60 / (365 * 24 * 3600), rel=1e-6)


def test_crosscheck_greeks_disabled_returns_none():
    config = _config(greeks_crosscheck_enabled=False)
    row = {"strike": 25000.0, "side": "CE", "ltp": 110.0, "delta": 0.53, "iv": 15.0}
    assert crosscheck_greeks(row, 25000.0, 0.02, config) is None


def test_crosscheck_greeks_missing_data_returns_none():
    config = _config()
    row = {"strike": 25000.0, "side": "CE", "ltp": None, "delta": 0.53, "iv": 15.0}
    assert crosscheck_greeks(row, 25000.0, 0.02, config) is None


def test_crosscheck_greeks_small_divergence_not_flagged():
    config = _config(greeks_divergence_threshold=0.15, risk_free_rate=0.065)
    # Broker delta close to the actual Black-Scholes value for these inputs
    # (~0.5286, verified directly against py_vollib during live testing).
    row = {"strike": 25000.0, "side": "CE", "ltp": 300.0, "delta": 0.53, "iv": 15.0}
    result = crosscheck_greeks(row, 25000.0, 0.02, config)
    assert result is not None
    assert result["large_divergence"] is False


def test_crosscheck_greeks_large_divergence_flagged():
    config = _config(greeks_divergence_threshold=0.05, risk_free_rate=0.065)
    row = {"strike": 25000.0, "side": "CE", "ltp": 300.0, "delta": 0.99, "iv": 15.0}
    result = crosscheck_greeks(row, 25000.0, 0.02, config)
    assert result is not None
    assert result["large_divergence"] is True


def test_crosscheck_greeks_near_atm_filters_to_window():
    chain = _section_18_chain()
    for row in chain:
        row["delta"] = 0.5 if row["side"] == "CE" else -0.5
        row["iv"] = 15.0
    config = _config()

    results = crosscheck_greeks_near_atm(chain, 25000.0, 0.02, config, strikes_range=1)

    result_strikes = {r["strike"] for r in results}
    assert result_strikes <= {24900.0, 25000.0, 25100.0}


def test_crosscheck_greeks_near_atm_disabled_returns_empty():
    config = _config(greeks_crosscheck_enabled=False)
    assert crosscheck_greeks_near_atm(_section_18_chain(), 25000.0, 0.02, config) == []
