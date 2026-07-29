"""Tests for skills/options_rules_engine.py — Phase 15 Task 5.

Confirmation fixtures reproduce docs/options-rulebook.md's worked
examples (Sections 21/46 range, 22 bullish breakout, 24 false breakout,
25 bearish breakdown, 27 false breakdown). Candle fixtures use a wide
low/high offset (close +/- 1000) so the retest-detection logic never
accidentally fires inside these composite tests — retest True/False/None
behavior is covered separately, in isolation, with tightly controlled
numbers.
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from config.options_config import OptionsConfig
from skills.options_rules_engine import (
    build_trade_checklist,
    check_level_strike_oi_behavior,
    check_premium_breakout,
    check_proximity,
    check_retest_hold,
    check_sustain,
    classify_regime,
    decide_transition,
    detect_price_cross,
    detect_price_trend,
    evaluate_breakout_confirmation,
    evaluate_market_rejections,
    find_nearest_level,
    has_crossed_level,
    score_confirmation,
    score_delta_oi,
    score_iv_greeks,
    score_oi_structure,
    score_pcr,
    score_setup,
    score_trend_pa,
    score_volume,
    summarize_confirmation_evidence,
)

IST = ZoneInfo("Asia/Kolkata")


def _config(**overrides) -> OptionsConfig:
    return OptionsConfig(**overrides)


def _wide_candles(closes: list[float]) -> pd.DataFrame:
    """Candle fixture where low/high are always +/-1000 from close --
    guarantees check_retest_hold never fires (see module docstring)."""
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1000 for c in closes],
            "low": [c - 1000 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )


# --------------------------------------------------------------------------
# Regime classification
# --------------------------------------------------------------------------


def test_detect_price_trend_up():
    candles = _wide_candles([100, 101, 102, 110, 115, 120])
    assert detect_price_trend(candles) == "UP"


def test_detect_price_trend_down():
    candles = _wide_candles([120, 115, 110, 102, 101, 100])
    assert detect_price_trend(candles) == "DOWN"


def test_detect_price_trend_flat_insufficient_data():
    assert detect_price_trend(_wide_candles([100, 101])) == "FLAT"


def test_classify_regime_bullish():
    assert classify_regime(25250.0, 24800.0, 25200.0, "UP") == "BULLISH"


def test_classify_regime_bearish():
    assert classify_regime(24750.0, 24800.0, 25200.0, "DOWN") == "BEARISH"


def test_classify_regime_range_section_21_and_46():
    # Rulebook Sections 21/46: NIFTY oscillating between 24,800 support
    # and 25,200 resistance, no sustained breakout.
    assert classify_regime(25000.0, 24800.0, 25200.0, "FLAT") == "RANGE"


def test_classify_regime_uncertain_on_conflicting_signals():
    # Broke resistance but trend disagrees -- Rule 28: "if signals conflict, WAIT".
    assert classify_regime(25250.0, 24800.0, 25200.0, "DOWN") == "UNCERTAIN"


def test_classify_regime_uncertain_on_missing_levels():
    assert classify_regime(25000.0, None, 25200.0, "UP") == "UNCERTAIN"


def test_section_46_no_trade_via_market_rejections():
    config = _config()
    regime = classify_regime(25010.0, 24800.0, 25200.0, "FLAT")
    reasons = evaluate_market_rejections({"mid_range": regime == "RANGE"}, config)
    assert "market in middle of range" in reasons


# --------------------------------------------------------------------------
# Proximity + nearest level
# --------------------------------------------------------------------------


def test_check_proximity_pct_mode_within_threshold():
    config = _config(proximity_threshold_pct=0.20, proximity_mode="pct")
    assert check_proximity(25190.0, 25200.0, config) is True  # 0.04% away


def test_check_proximity_pct_mode_beyond_threshold():
    config = _config(proximity_threshold_pct=0.20, proximity_mode="pct")
    assert check_proximity(25000.0, 25200.0, config) is False  # 0.79% away


def test_check_proximity_atr_mode():
    config = _config(proximity_mode="atr", atr_multiplier=0.5)
    assert check_proximity(25190.0, 25200.0, config, atr=30.0) is True  # within 15 pts
    assert check_proximity(25150.0, 25200.0, config, atr=30.0) is False


def test_check_proximity_atr_mode_missing_atr_returns_false():
    config = _config(proximity_mode="atr", atr_multiplier=0.5)
    assert check_proximity(25190.0, 25200.0, config, atr=None) is False


def test_has_crossed_level_up_true_when_spot_above():
    assert has_crossed_level(24205.0, 24200.0, "UP") is True


def test_has_crossed_level_up_false_when_spot_below():
    assert has_crossed_level(24195.0, 24200.0, "UP") is False


def test_has_crossed_level_up_false_when_spot_equals_level():
    assert has_crossed_level(24200.0, 24200.0, "UP") is False


def test_has_crossed_level_down_true_when_spot_below():
    assert has_crossed_level(23995.0, 24000.0, "DOWN") is True


def test_has_crossed_level_down_false_when_spot_above():
    assert has_crossed_level(24005.0, 24000.0, "DOWN") is False


def test_find_nearest_level_picks_closer_of_two():
    config = _config(proximity_threshold_pct=1.0)
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}
    nearest = find_nearest_level(25190.0, support_resistance, config)
    assert nearest == {"level": 25200.0, "direction": "UP"}


def test_find_nearest_level_none_when_nothing_in_proximity():
    config = _config(proximity_threshold_pct=0.20)
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}
    assert find_nearest_level(25000.0, support_resistance, config) is None


def test_find_nearest_level_picks_closest_among_multiple_resistances():
    # Regression test: with multiple ascending-sorted resistance candidates,
    # the nearest to spot must win regardless of list position -- a prior
    # version picked the list's last (farthest) element.
    config = _config(proximity_threshold_pct=1.0)
    support_resistance = {"support": [], "resistance": [25200.0, 26190.0]}
    nearest = find_nearest_level(25190.0, support_resistance, config)
    assert nearest == {"level": 25200.0, "direction": "UP"}


def test_find_nearest_level_picks_closest_among_multiple_supports():
    config = _config(proximity_threshold_pct=1.0)
    support_resistance = {"support": [24000.0, 24800.0], "resistance": []}
    nearest = find_nearest_level(24810.0, support_resistance, config)
    assert nearest == {"level": 24800.0, "direction": "DOWN"}


# --------------------------------------------------------------------------
# Dual-cadence state machine
# --------------------------------------------------------------------------


def test_decide_transition_escalates_from_normal():
    config = _config(proximity_threshold_pct=1.0)
    now = datetime(2026, 7, 20, 10, 0, tzinfo=IST)
    engine_state = {"mode": "NORMAL"}
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}

    result = decide_transition(engine_state, 25190.0, support_resistance, now, config)

    assert result["action"] == "ESCALATE"
    assert result["next_state"]["mode"] == "WATCH"
    assert result["next_state"]["watch_level"] == 25200.0
    assert result["next_state"]["watch_direction"] == "UP"
    assert result["next_state"]["watch_started_ts"] == now.isoformat()


def test_decide_transition_stays_normal_when_nothing_in_proximity():
    config = _config(proximity_threshold_pct=0.20)
    now = datetime(2026, 7, 20, 10, 0, tzinfo=IST)
    engine_state = {"mode": "NORMAL"}
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}

    result = decide_transition(engine_state, 25000.0, support_resistance, now, config)

    assert result["action"] == "NONE"
    assert result["next_state"]["mode"] == "NORMAL"


def test_decide_transition_deescalates_on_distance():
    config = _config(deescalate_threshold_pct=0.40, proximity_threshold_pct=0.20)
    now = datetime(2026, 7, 20, 10, 5, tzinfo=IST)
    engine_state = {
        "mode": "WATCH",
        "watch_level": 25200.0,
        "watch_direction": "UP",
        "watch_started_ts": datetime(2026, 7, 20, 10, 0, tzinfo=IST).isoformat(),
    }
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}

    # 25200 * 0.40% = ~100.8 pts; 200 pts away exceeds that.
    result = decide_transition(engine_state, 25000.0, support_resistance, now, config)

    assert result["action"] == "DEESCALATE"
    assert result["next_state"]["mode"] == "NORMAL"
    assert result["next_state"]["watch_level"] is None


def test_decide_transition_deescalates_on_timeout():
    config = _config(watch_timeout_minutes=45, proximity_threshold_pct=1.0)
    started = datetime(2026, 7, 20, 9, 0, tzinfo=IST)
    now = datetime(2026, 7, 20, 9, 50, tzinfo=IST)  # 50 min later, still near the level
    engine_state = {"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": started.isoformat()}
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}

    result = decide_transition(engine_state, 25190.0, support_resistance, now, config)

    assert result["action"] == "DEESCALATE"
    assert "timed out" in result["reason"]


def test_decide_transition_stays_watch_without_confirmation_evidence():
    config = _config(proximity_threshold_pct=1.0, watch_timeout_minutes=45)
    started = datetime(2026, 7, 20, 10, 0, tzinfo=IST)
    now = datetime(2026, 7, 20, 10, 5, tzinfo=IST)
    engine_state = {"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": started.isoformat()}
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}

    result = decide_transition(engine_state, 25190.0, support_resistance, now, config, confirmation=None)

    assert result["action"] == "NONE"
    assert result["next_state"] == engine_state


def test_decide_transition_confirmed_hands_off():
    config = _config(proximity_threshold_pct=1.0, watch_timeout_minutes=45)
    started = datetime(2026, 7, 20, 10, 0, tzinfo=IST)
    now = datetime(2026, 7, 20, 10, 20, tzinfo=IST)
    engine_state = {"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": started.isoformat()}
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}

    result = decide_transition(
        engine_state, 25250.0, support_resistance, now, config, confirmation={"confirmed": True, "false_breakout": False}
    )

    assert result["action"] == "CONFIRMED"
    assert result["next_state"]["mode"] == "NORMAL"


def test_decide_transition_false_breakout_deescalates():
    config = _config(proximity_threshold_pct=1.0, watch_timeout_minutes=45)
    started = datetime(2026, 7, 20, 10, 0, tzinfo=IST)
    now = datetime(2026, 7, 20, 10, 20, tzinfo=IST)
    engine_state = {"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": started.isoformat()}
    support_resistance = {"support": [24800.0], "resistance": [25200.0]}

    result = decide_transition(
        engine_state, 25210.0, support_resistance, now, config, confirmation={"confirmed": False, "false_breakout": True}
    )

    assert result["action"] == "FALSE_BREAKOUT"
    assert result["next_state"]["mode"] == "NORMAL"


# --------------------------------------------------------------------------
# Cross / sustain / retest — isolated unit tests
# --------------------------------------------------------------------------


def test_detect_price_cross_true_even_if_reversed_later():
    candles = _wide_candles([25180, 25210, 25220, 25180, 25150])
    assert detect_price_cross(candles, 25200.0, "UP") is True


def test_detect_price_cross_false_when_never_crossed():
    candles = _wide_candles([25180, 25190, 25150, 25100])
    assert detect_price_cross(candles, 25200.0, "UP") is False


def test_check_sustain_true_when_all_recent_beyond_level():
    candles = _wide_candles([25180, 25225, 25230, 25235, 25240, 25245])
    assert check_sustain(candles, 25200.0, "UP", sustain_minutes=5) is True


def test_check_sustain_false_when_reversed():
    candles = _wide_candles([25180, 25210, 25220, 25180, 25150])
    assert check_sustain(candles, 25200.0, "UP", sustain_minutes=5) is False


def test_check_retest_hold_returns_true_on_successful_retest():
    config = _config(proximity_threshold_pct=0.20)
    # bar 0's low (25198) touches level 25200 (tolerance 50.4); bar 1 closes
    # back above the level -> retest held.
    candles = pd.DataFrame(
        {
            "open": [25199, 25230],
            "high": [25202, 25235],
            "low": [25198, 25225],
            "close": [25199, 25230],
        }
    )
    assert check_retest_hold(candles, 25200.0, "UP", config) is True


def test_check_retest_hold_returns_false_on_failed_retest():
    config = _config(proximity_threshold_pct=0.20)
    # bar 0's low (25198) touches the level; bar 1 closes back below it ->
    # retest failed.
    candles = pd.DataFrame(
        {
            "open": [25199, 25190],
            "high": [25202, 25195],
            "low": [25198, 25180],
            "close": [25199, 25185],
        }
    )
    assert check_retest_hold(candles, 25200.0, "UP", config) is False


def test_check_retest_hold_returns_none_when_no_touch():
    config = _config(proximity_threshold_pct=0.20)
    candles = _wide_candles([25210, 25230, 25250])
    assert check_retest_hold(candles, 25200.0, "UP", config) is None


# --------------------------------------------------------------------------
# Premium-chart breakout confirmation (2026-07-28 addition -- "underlying
# gives direction, option premium confirms execution")
# --------------------------------------------------------------------------


def test_check_premium_breakout_confirms_on_sustained_rise():
    # prior range tops out at 55; last 2 readings both close above it.
    assert check_premium_breakout([35, 45, 55, 50, 60, 62], "UP", sustain_count=2) is True


def test_check_premium_breakout_fails_when_premium_lags():
    # underlying broke out but this contract's own premium never cleared
    # its prior high of 55 -- rulebook Section 8's "wait" example.
    assert check_premium_breakout([35, 45, 55, 50, 53, 51], "UP", sustain_count=2) is False


def test_check_premium_breakout_confirms_on_sustained_fall_for_pe():
    assert check_premium_breakout([90, 70, 65, 60, 58, 55], "DOWN", sustain_count=2) is True


def test_check_premium_breakout_none_when_insufficient_history():
    assert check_premium_breakout([50, 55], "UP", sustain_count=2) is None
    assert check_premium_breakout(None, "UP", sustain_count=2) is None
    assert check_premium_breakout([], "UP", sustain_count=2) is None


# --------------------------------------------------------------------------
# Level-strike OI behavior
# --------------------------------------------------------------------------


def test_check_level_strike_oi_behavior_short_covering():
    chain = [{"strike": 25200.0, "side": "CE", "ltp": 75, "oi": 4_000_000}]
    previous = [{"strike": 25200.0, "side": "CE", "ltp": 55, "oi": 5_500_000}]
    config = _config()
    assert check_level_strike_oi_behavior(chain, previous, 25200.0, "CE", config) == "SHORT_COVERING"


def test_check_level_strike_oi_behavior_missing_row_returns_none():
    config = _config()
    assert check_level_strike_oi_behavior([], [], 25200.0, "CE", config) is None


# --------------------------------------------------------------------------
# Full breakout/breakdown confirmation — rulebook worked examples
# --------------------------------------------------------------------------


def test_section_22_bullish_breakout_confirmed():
    candles = _wide_candles([25180, 25225, 25230, 25235, 25240, 25245])
    chain = [{"strike": 25200.0, "side": "CE", "ltp": 75, "oi": 4_000_000}]
    previous = [{"strike": 25200.0, "side": "CE", "ltp": 55, "oi": 5_500_000}]
    config = _config()

    result = evaluate_breakout_confirmation(
        candles=candles,
        level=25200.0,
        direction="UP",
        level_strike=25200.0,
        option_side="CE",
        chain=chain,
        previous_snapshot=previous,
        volume_result={"confirmed": True},
        regime="BULLISH",
        config=config,
    )

    assert result["confirmed"] is True
    assert result["false_breakout"] is False
    assert result["oi_classification"] == "SHORT_COVERING"


def test_section_24_false_bullish_breakout():
    candles = _wide_candles([25180, 25210, 25220, 25180, 25150])
    chain = [{"strike": 25200.0, "side": "CE", "ltp": 28, "oi": 9_500_000}]
    previous = [{"strike": 25200.0, "side": "CE", "ltp": 35, "oi": 8_000_000}]
    config = _config()

    result = evaluate_breakout_confirmation(
        candles=candles,
        level=25200.0,
        direction="UP",
        level_strike=25200.0,
        option_side="CE",
        chain=chain,
        previous_snapshot=previous,
        volume_result={"confirmed": True},
        regime="UNCERTAIN",
        config=config,
    )

    assert result["confirmed"] is False
    assert result["false_breakout"] is True
    assert result["oi_classification"] == "SHORT_BUILDUP"


def test_section_25_bearish_breakdown_confirmed():
    candles = _wide_candles([24900, 24850, 24790, 24785, 24780, 24775, 24760])
    chain = [{"strike": 24800.0, "side": "PE", "ltp": 90, "oi": 2_500_000}]
    previous = [{"strike": 24800.0, "side": "PE", "ltp": 65, "oi": 4_500_000}]
    config = _config()

    result = evaluate_breakout_confirmation(
        candles=candles,
        level=24800.0,
        direction="DOWN",
        level_strike=24800.0,
        option_side="PE",
        chain=chain,
        previous_snapshot=previous,
        volume_result={"confirmed": True},
        regime="BEARISH",
        config=config,
    )

    assert result["confirmed"] is True
    assert result["false_breakout"] is False


def test_section_27_false_breakdown_support_defended():
    candles = _wide_candles([24820, 24790, 24770, 24820, 24900])
    chain = [{"strike": 24800.0, "side": "PE", "ltp": 30, "oi": 6_000_000}]
    previous = [{"strike": 24800.0, "side": "PE", "ltp": 45, "oi": 4_500_000}]
    config = _config()

    result = evaluate_breakout_confirmation(
        candles=candles,
        level=24800.0,
        direction="DOWN",
        level_strike=24800.0,
        option_side="PE",
        chain=chain,
        previous_snapshot=previous,
        volume_result={"confirmed": True},
        regime="UNCERTAIN",
        config=config,
    )

    assert result["confirmed"] is False
    assert result["false_breakout"] is True
    assert result["oi_classification"] == "SHORT_BUILDUP"


def test_evaluate_breakout_confirmation_reports_opposite_side_writing():
    """2026-07-28 addition: PE OI at the resistance strike showing fresh
    writing (premium down, OI up) surfaces as `opposite_oi_classification`
    -- informational only, doesn't affect `confirmed`."""
    candles = _wide_candles([25180, 25225, 25230, 25235, 25240, 25245])
    chain = [
        {"strike": 25200.0, "side": "CE", "ltp": 75, "oi": 4_000_000},
        {"strike": 25200.0, "side": "PE", "ltp": 60, "oi": 3_500_000},
    ]
    previous = [
        {"strike": 25200.0, "side": "CE", "ltp": 55, "oi": 5_500_000},
        {"strike": 25200.0, "side": "PE", "ltp": 70, "oi": 3_000_000},
    ]
    config = _config()

    result = evaluate_breakout_confirmation(
        candles=candles, level=25200.0, direction="UP", level_strike=25200.0, option_side="CE",
        chain=chain, previous_snapshot=previous, volume_result={"confirmed": True}, regime="BULLISH", config=config,
    )

    assert result["opposite_oi_classification"] == "SHORT_BUILDUP"
    assert result["confirmed"] is True


def test_evaluate_breakout_confirmation_blocked_by_lagging_premium():
    """Otherwise-identical to test_section_22_bullish_breakout_confirmed,
    but the watched contract's own premium never cleared its own prior
    high -- 2026-07-28 confirmation-framework addition: underlying
    breakout alone must not be enough."""
    candles = _wide_candles([25180, 25225, 25230, 25235, 25240, 25245])
    chain = [{"strike": 25200.0, "side": "CE", "ltp": 75, "oi": 4_000_000}]
    previous = [{"strike": 25200.0, "side": "CE", "ltp": 55, "oi": 5_500_000}]
    config = _config()

    result = evaluate_breakout_confirmation(
        candles=candles,
        level=25200.0,
        direction="UP",
        level_strike=25200.0,
        option_side="CE",
        chain=chain,
        previous_snapshot=previous,
        volume_result={"confirmed": True},
        regime="BULLISH",
        config=config,
        premium_history=[35, 45, 55, 50, 53, 51],
    )

    assert result["premium_confirms"] is False
    assert result["confirmed"] is False


def test_evaluate_breakout_confirmation_missing_premium_history_does_not_block():
    """No premium_history supplied (e.g. a fresh WATCH with < 3 snapshots
    so far) must not block confirmation -- absence is "not applicable",
    same precedent as check_retest_hold's None."""
    candles = _wide_candles([25180, 25225, 25230, 25235, 25240, 25245])
    chain = [{"strike": 25200.0, "side": "CE", "ltp": 75, "oi": 4_000_000}]
    previous = [{"strike": 25200.0, "side": "CE", "ltp": 55, "oi": 5_500_000}]
    config = _config()

    result = evaluate_breakout_confirmation(
        candles=candles,
        level=25200.0,
        direction="UP",
        level_strike=25200.0,
        option_side="CE",
        chain=chain,
        previous_snapshot=previous,
        volume_result={"confirmed": True},
        regime="BULLISH",
        config=config,
    )

    assert result["premium_confirms"] is None
    assert result["confirmed"] is True


# --------------------------------------------------------------------------
# Market-level hard rejections (Section 42, rules 1-9, 18)
# --------------------------------------------------------------------------


def test_evaluate_market_rejections_flags_each_rule():
    config = _config(max_spread_pct=2.0, min_oi=1000, min_volume=500)
    evidence = {
        "data_stale": True,
        "critical_data_missing": True,
        "no_clear_setup": True,
        "mid_range": True,
        "breakout_unconfirmed": True,
        "breakdown_unconfirmed": True,
        "conflicting_signals": True,
        "spread_pct": 3.0,
        "oi": 500,
        "volume": 100,
        "market_structure_changed": True,
    }
    reasons = evaluate_market_rejections(evidence, config)
    assert len(reasons) == 11


def test_evaluate_market_rejections_empty_evidence_no_rejections():
    assert evaluate_market_rejections({}, _config()) == []


def test_evaluate_market_rejections_liquidity_within_limits_not_flagged():
    config = _config(max_spread_pct=2.0, min_oi=1000, min_volume=500)
    evidence = {"spread_pct": 1.0, "oi": 5000, "volume": 2000}
    assert evaluate_market_rejections(evidence, config) == []


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


def test_score_setup_full_marks_sums_to_100():
    config = _config()
    sub_scores = {key: 100 for key in ("trend_pa", "confirmation", "oi_structure", "delta_oi", "volume", "rr", "iv_greeks", "pcr", "liquidity")}
    result = score_setup(sub_scores, config)
    assert result["score"] == 100


def test_score_setup_zero_marks_sums_to_0():
    config = _config()
    result = score_setup({}, config)
    assert result["score"] == 0


def test_score_setup_missing_categories_score_zero_for_those():
    config = _config()
    # Only trend_pa (20) and confirmation (20) supplied -- rr/liquidity not
    # yet available before Task 6's Trade Selector runs.
    result = score_setup({"trend_pa": 100, "confirmation": 100}, config)
    assert result["score"] == 40


def test_score_trend_pa():
    assert score_trend_pa("BULLISH") == 100.0
    assert score_trend_pa("RANGE") == 0.0


def test_score_confirmation_all_checks_pass():
    confirmation = {"crossed": True, "sustained": True, "volume_confirmed": True, "oi_supports": True, "retest": True}
    assert score_confirmation(confirmation) == 100.0


def test_score_confirmation_none_returns_zero():
    assert score_confirmation(None) == 0.0


def test_score_oi_structure():
    assert score_oi_structure("SHORT_COVERING") == 100.0
    assert score_oi_structure("SHORT_BUILDUP") == 0.0
    assert score_oi_structure("LONG_BUILDUP") == 50.0


def test_score_delta_oi_scales_with_pct_move():
    assert score_delta_oi(1_000_000, 5_000_000) == pytest.approx(min(100.0, 20 * 5))
    assert score_delta_oi(None, 5_000_000) == 0.0


def test_score_volume():
    assert score_volume({"confirmed": True}) == 100.0
    assert score_volume({"confirmed": False}) == 0.0


def test_score_iv_greeks_no_divergence():
    assert score_iv_greeks([{"large_divergence": False}]) == 100.0
    assert score_iv_greeks([{"large_divergence": True}]) == 0.0
    assert score_iv_greeks([]) == 50.0


def test_score_pcr_within_band():
    assert score_pcr(1.0) == 100.0
    assert score_pcr(2.5) == 50.0
    assert score_pcr(None) == 50.0


# --------------------------------------------------------------------------
# Evidence summarization ("why" for Discord/Telegram messages)
# --------------------------------------------------------------------------


def test_summarize_confirmation_evidence_full_confirmation():
    confirmation = {
        "crossed": True, "sustained": True, "volume_confirmed": True,
        "oi_classification": "SHORT_COVERING", "retest": True, "structure_agrees": True,
    }
    reasons = summarize_confirmation_evidence(confirmation)
    assert "Price crossed the level" in reasons
    assert any("short covering" in r for r in reasons)
    assert "Retest of the level held" in reasons
    assert "Broader trend structure agrees" in reasons


def test_summarize_confirmation_evidence_short_buildup_and_failed_retest():
    confirmation = {"oi_classification": "SHORT_BUILDUP", "retest": False}
    reasons = summarize_confirmation_evidence(confirmation)
    assert any("fresh short build-up" in r for r in reasons)
    assert "Retest of the level failed" in reasons


def test_summarize_confirmation_evidence_premium_confirms():
    assert "Option premium broke its own range and sustained" in summarize_confirmation_evidence(
        {"premium_confirms": True}
    )
    assert "Option premium has NOT confirmed the underlying move" in summarize_confirmation_evidence(
        {"premium_confirms": False}
    )
    assert summarize_confirmation_evidence({"premium_confirms": None}) == []


def test_summarize_confirmation_evidence_empty():
    assert summarize_confirmation_evidence({}) == []


# --------------------------------------------------------------------------
# Trade checklist (2026-07-28 request -- the user's own 9-line CE/PE
# confirmation-framework wording, e.g. "Trend -> Bullish", "Call
# unwinding", "Put writing", ... rendered in the BUY advisory message)
# --------------------------------------------------------------------------


def _full_confirmation() -> dict:
    return {
        "crossed": True, "sustained": True, "volume_confirmed": True,
        "oi_classification": "SHORT_COVERING", "opposite_oi_classification": "SHORT_BUILDUP",
        "premium_confirms": True,
    }


def test_build_trade_checklist_bullish_matches_user_example():
    trade = {"moneyness": "ATM", "risk_reward": 2.5}
    checklist = build_trade_checklist(_full_confirmation(), "BULLISH", "CE", trade, iv_acceptable=True, min_rr=2.0)
    assert checklist == [
        ("Trend", "Bullish", True),
        ("Resistance breakout", None, True),
        ("Volume", "Above average", True),
        ("Call unwinding", None, True),
        ("Put writing", None, True),
        ("Premium resistance breakout", None, True),
        ("ATM/ITM strike", None, True),
        ("IV acceptable", None, True),
        ("Risk:Reward ≥ 1:2", None, True),
    ]


def test_build_trade_checklist_bearish_matches_user_example():
    trade = {"moneyness": "ITM", "risk_reward": 2.2}
    checklist = build_trade_checklist(_full_confirmation(), "BEARISH", "PE", trade, iv_acceptable=True, min_rr=2.0)
    assert checklist == [
        ("Trend", "Bearish", True),
        ("Support breakdown", None, True),
        ("Volume", "Strong", True),
        ("Put unwinding", None, True),
        ("Call writing", None, True),
        ("PE premium breakout", None, True),
        ("ATM/ITM PE", None, True),
        ("IV acceptable", None, True),
        ("Risk:Reward ≥ 1:2", None, True),
    ]


def test_build_trade_checklist_flags_unmet_writing_and_iv():
    """Put/Call writing and IV acceptable are informational, not gating --
    they can legitimately read unmet (not just pending) even on a
    delivered BUY message, when the opposite side was actually evaluated
    and did NOT show writing."""
    confirmation = {**_full_confirmation(), "opposite_oi_classification": "LONG_UNWINDING"}
    trade = {"moneyness": "ATM", "risk_reward": 2.5}
    checklist = build_trade_checklist(confirmation, "BULLISH", "CE", trade, iv_acceptable=False, min_rr=2.0)
    by_label = {label: met for label, _, met in checklist}
    assert by_label["Put writing"] is False
    assert by_label["IV acceptable"] is False


def test_build_trade_checklist_writing_pending_when_opposite_side_data_missing():
    """When the opposite side's row simply wasn't in the fetched chain
    (data missing, not evaluated), the row must read pending (⏳), not a
    false "not met" -- absence of data is not evidence against it."""
    confirmation = {**_full_confirmation(), "opposite_oi_classification": None}
    trade = {"moneyness": "ATM", "risk_reward": 2.5}
    checklist = build_trade_checklist(confirmation, "BULLISH", "CE", trade, iv_acceptable=True, min_rr=2.0)
    by_label = {label: met for label, _, met in checklist}
    assert by_label["Put writing"] is None


def test_build_trade_checklist_risk_reward_below_min_fails():
    trade = {"moneyness": "ATM", "risk_reward": 1.5}
    checklist = build_trade_checklist(_full_confirmation(), "BULLISH", "CE", trade, iv_acceptable=True, min_rr=2.0)
    by_label = {label: met for label, _, met in checklist}
    assert by_label["Risk:Reward ≥ 1:2"] is False
