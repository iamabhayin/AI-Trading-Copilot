"""Tests for skills/options_trade_selector.py — Phase 15 Task 6."""

from datetime import date
from unittest.mock import MagicMock

import pytest

from config.options_config import OptionsConfig
from skills.options_trade_selector import (
    build_advisory_payload,
    check_event_risk,
    compute_entry_zone,
    compute_invalidation,
    compute_option_rr,
    compute_position_size,
    compute_structural_rr,
    compute_targets,
    estimate_holding_days,
    estimate_target_premium,
    evaluate_trade_rejections,
    format_advisory_message,
    lot_size_for_expiry,
    map_to_option_stop,
    moneyness,
    next_level,
    score_liquidity,
    score_rr,
    select_expiry,
    select_strike,
    select_trade,
    write_advisory,
)


def _config(**overrides) -> OptionsConfig:
    return OptionsConfig(**overrides)


def _contracts() -> list[dict]:
    return [
        {"expiry": "24JUL2025", "lotsize": "75"},
        {"expiry": "31JUL2025", "lotsize": "75"},
        {"expiry": "28AUG2025", "lotsize": "75"},
    ]


def _chain_row(strike=25200.0, side="CE", delta=0.60, ltp=100.0, bid=99.0, ask=101.0, oi=100_000, volume=50_000, iv=15.0, expiry="24JUL2025"):
    return {
        "expiry": expiry, "strike": strike, "side": side, "ltp": ltp, "delta": delta,
        "bid": bid, "ask": ask, "oi": oi, "volume": volume, "iv": iv,
    }


# --------------------------------------------------------------------------
# Expiry
# --------------------------------------------------------------------------


def test_estimate_holding_days_scales_with_atr():
    assert estimate_holding_days(distance_to_target=300, atr=100) == 3


def test_estimate_holding_days_no_atr_returns_min():
    assert estimate_holding_days(distance_to_target=300, atr=None) == 1


def test_estimate_holding_days_clamped_to_max():
    assert estimate_holding_days(distance_to_target=10000, atr=1) == 10


def test_select_expiry_picks_first_that_fits():
    config = _config(holding_fit_multiplier=2.0)
    today = date(2025, 7, 20)
    # 24JUL2025 is 4 days out; need >= 2*2=4 days -- exactly fits.
    expiry = select_expiry(["24JUL2025", "31JUL2025"], expected_holding_days=2, config=config, today=today)
    assert expiry == "24JUL2025"


def test_select_expiry_skips_too_near_expiry():
    config = _config(holding_fit_multiplier=2.0)
    today = date(2025, 7, 20)
    # 24JUL2025 (4 days) doesn't fit a 5-day holding period needing 10 days.
    expiry = select_expiry(["24JUL2025", "31JUL2025"], expected_holding_days=5, config=config, today=today)
    assert expiry == "31JUL2025"


def test_select_expiry_none_when_nothing_fits():
    config = _config(holding_fit_multiplier=2.0)
    today = date(2025, 7, 20)
    assert select_expiry(["24JUL2025"], expected_holding_days=100, config=config, today=today) is None


# --------------------------------------------------------------------------
# Strike selection
# --------------------------------------------------------------------------


def test_select_strike_filters_by_delta_band():
    config = _config(delta_band_min=0.50, delta_band_max=0.70, max_spread_pct=5.0, min_oi=0, min_volume=0)
    chain = [
        _chain_row(strike=25000.0, delta=0.30),  # outside band
        _chain_row(strike=25100.0, delta=0.60),  # inside band
        _chain_row(strike=25200.0, delta=0.95),  # outside band
    ]
    result = select_strike(chain, "CE", config)
    assert result["strike"] == 25100.0


def test_select_strike_rejects_wide_spread():
    config = _config(delta_band_min=0.50, delta_band_max=0.70, max_spread_pct=1.0)
    chain = [_chain_row(strike=25100.0, delta=0.60, bid=90.0, ask=110.0)]  # ~22% spread
    assert select_strike(chain, "CE", config) is None


def test_select_strike_rejects_low_liquidity():
    config = _config(delta_band_min=0.50, delta_band_max=0.70, max_spread_pct=5.0, min_oi=1_000_000, min_volume=0)
    chain = [_chain_row(strike=25100.0, delta=0.60, oi=1000)]
    assert select_strike(chain, "CE", config) is None


def test_select_strike_prefers_closest_to_band_midpoint():
    config = _config(delta_band_min=0.50, delta_band_max=0.70, max_spread_pct=5.0)
    chain = [
        _chain_row(strike=25000.0, delta=0.50),
        _chain_row(strike=25100.0, delta=0.60),  # closest to midpoint 0.60
        _chain_row(strike=25200.0, delta=0.70),
    ]
    result = select_strike(chain, "CE", config)
    assert result["strike"] == 25100.0


def test_select_strike_no_candidates_returns_none():
    config = _config(delta_band_min=0.50, delta_band_max=0.70)
    assert select_strike([], "CE", config) is None


def test_moneyness():
    assert moneyness(25000.0, 25000.0, "CE") == "ATM"
    assert moneyness(24800.0, 25000.0, "CE") == "ITM"
    assert moneyness(25200.0, 25000.0, "CE") == "OTM"
    assert moneyness(25200.0, 25000.0, "PE") == "ITM"
    assert moneyness(24800.0, 25000.0, "PE") == "OTM"


# --------------------------------------------------------------------------
# Entry zone / invalidation / option SL
# --------------------------------------------------------------------------


def test_compute_entry_zone_up():
    config = _config(entry_zone_buffer_pct=0.10)
    lo, hi = compute_entry_zone(25200.0, "UP", config)
    assert lo == 25200.0
    assert hi == pytest.approx(25225.2)


def test_compute_entry_zone_down():
    config = _config(entry_zone_buffer_pct=0.10)
    lo, hi = compute_entry_zone(24800.0, "DOWN", config)
    assert hi == 24800.0
    assert lo == pytest.approx(24775.2)


def test_compute_invalidation_up_is_below_level():
    config = _config(invalidation_buffer_pct=0.30)
    assert compute_invalidation(25200.0, "UP", config) < 25200.0


def test_compute_invalidation_down_is_above_level():
    config = _config(invalidation_buffer_pct=0.30)
    assert compute_invalidation(24800.0, "DOWN", config) > 24800.0


def test_map_to_option_stop_ce():
    config = _config(min_option_premium=0.05)
    # entry_spot 25230, invalidation 25170, delta 0.6 -> premium drops by 0.6*60=36
    sl = map_to_option_stop(entry_premium=140.0, delta=0.6, entry_spot=25230.0, invalidation_spot=25170.0, config=config)
    assert sl == pytest.approx(104.0)


def test_map_to_option_stop_pe():
    config = _config(min_option_premium=0.05)
    # entry_spot 24790, invalidation 24850 (reclaim), delta -0.55
    sl = map_to_option_stop(entry_premium=90.0, delta=-0.55, entry_spot=24790.0, invalidation_spot=24850.0, config=config)
    assert sl == pytest.approx(90.0 - 33.0)


def test_map_to_option_stop_floors_at_min():
    config = _config(min_option_premium=0.50)
    sl = map_to_option_stop(entry_premium=10.0, delta=0.9, entry_spot=25000.0, invalidation_spot=24000.0, config=config)
    assert sl == 0.50


# --------------------------------------------------------------------------
# Targets / R:R
# --------------------------------------------------------------------------


def test_next_level_up_picks_nearest_resistance_above():
    sr = {"resistance": [25200.0, 25400.0]}
    assert next_level("UP", 25200.0, sr) == 25400.0


def test_next_level_down_picks_nearest_support_below():
    sr = {"support": [24600.0, 24800.0]}
    assert next_level("DOWN", 24800.0, sr) == 24600.0


def test_next_level_none_when_no_further_level():
    assert next_level("UP", 25200.0, {"resistance": [25200.0]}) is None


def test_compute_targets_section_44_style():
    config = _config(target_split_ratio=0.53)
    targets = compute_targets(entry=25230.0, level_beyond=25400.0, config=config)
    assert targets["t2"] == 25400.0
    assert 25300 < targets["t1"] < 25400


def test_compute_targets_no_next_level():
    config = _config()
    assert compute_targets(25230.0, None, config) == {"t1": None, "t2": None}


def test_compute_structural_rr():
    assert compute_structural_rr(entry=25230.0, invalidation=25170.0, target=25380.0) == pytest.approx(150 / 60)


def test_compute_structural_rr_no_target():
    assert compute_structural_rr(25230.0, 25170.0, None) is None


def test_estimate_target_premium():
    config = _config(min_option_premium=0.05)
    premium = estimate_target_premium(entry_premium=140.0, delta=0.6, entry_spot=25230.0, target_spot=25380.0, config=config)
    assert premium == pytest.approx(140.0 + 0.6 * 150)


def test_compute_option_rr():
    assert compute_option_rr(entry_premium=140.0, option_sl=104.0, target_premium=230.0) == pytest.approx(90 / 36)


def test_compute_option_rr_zero_risk_returns_none():
    assert compute_option_rr(entry_premium=140.0, option_sl=140.0, target_premium=230.0) is None


# --------------------------------------------------------------------------
# Sizing
# --------------------------------------------------------------------------


def test_compute_position_size():
    # max_risk = 200000*0.01=2000; risk_per_unit=36; lot_size=75 -> risk_per_lot=2700 -> 0 lots
    lots = compute_position_size(capital=200_000, risk_pct=0.01, entry_premium=140.0, option_sl=104.0, lot_size=75)
    assert lots == 0


def test_compute_position_size_positive():
    lots = compute_position_size(capital=2_000_000, risk_pct=0.01, entry_premium=140.0, option_sl=104.0, lot_size=75)
    assert lots == 7  # 20000 / (36*75=2700) = 7.4 -> floor 7


def test_compute_position_size_zero_risk_returns_zero():
    assert compute_position_size(200_000, 0.01, 140.0, 140.0, 75) == 0


def test_lot_size_for_expiry():
    contracts = _contracts()
    assert lot_size_for_expiry(contracts, "24JUL2025") == 75
    assert lot_size_for_expiry(contracts, "01JAN2030") is None


# --------------------------------------------------------------------------
# Event risk
# --------------------------------------------------------------------------


def test_check_event_risk_missing_file_returns_false():
    config = _config(iv_event_calendar_path="nonexistent_calendar.json")
    assert check_event_risk("24JUL2025", config, date(2025, 7, 20)) is False


def test_check_event_risk_matching_date(tmp_path):
    calendar = tmp_path / "events.json"
    calendar.write_text('["2025-07-22"]')
    config = _config(iv_event_calendar_path=str(calendar))
    assert check_event_risk("24JUL2025", config, date(2025, 7, 20)) is True


def test_check_event_risk_no_matching_date(tmp_path):
    calendar = tmp_path / "events.json"
    calendar.write_text('["2025-08-15"]')
    config = _config(iv_event_calendar_path=str(calendar))
    assert check_event_risk("24JUL2025", config, date(2025, 7, 20)) is False


def test_check_event_risk_malformed_file_returns_false(tmp_path):
    calendar = tmp_path / "events.json"
    calendar.write_text("not json")
    config = _config(iv_event_calendar_path=str(calendar))
    assert check_event_risk("24JUL2025", config, date(2025, 7, 20)) is False


# --------------------------------------------------------------------------
# Final rejection pass
# --------------------------------------------------------------------------


def test_evaluate_trade_rejections_rr_below_min():
    config = _config(min_rr=2.0)
    trade = {"risk_reward": 1.5}
    assert "risk:reward below threshold" in evaluate_trade_rejections(trade, config)


def test_evaluate_trade_rejections_zero_lots():
    config = _config()
    trade = {"position_size_lots": 0}
    assert "position exceeds risk limit" in evaluate_trade_rejections(trade, config)


def test_evaluate_trade_rejections_clean_trade_no_reasons():
    config = _config(min_rr=1.0)
    trade = {"risk_reward": 2.5, "position_size_lots": 5}
    assert evaluate_trade_rejections(trade, config) == []


# --------------------------------------------------------------------------
# Scoring sub-scores (rr, liquidity)
# --------------------------------------------------------------------------


def test_score_rr_at_minimum_scores_50():
    config = _config(min_rr=2.0)
    assert score_rr(2.0, config) == pytest.approx(50.0)


def test_score_rr_at_double_minimum_scores_100():
    config = _config(min_rr=2.0)
    assert score_rr(4.0, config) == pytest.approx(100.0)


def test_score_rr_none_scores_zero():
    assert score_rr(None, _config(min_rr=2.0)) == 0.0


def test_score_liquidity_zero_spread_scores_100():
    config = _config(max_spread_pct=2.0)
    assert score_liquidity(0.0, config) == 100.0


def test_score_liquidity_at_max_spread_scores_0():
    config = _config(max_spread_pct=2.0)
    assert score_liquidity(2.0, config) == pytest.approx(0.0)


def test_score_liquidity_none_scores_zero():
    assert score_liquidity(None, _config()) == 0.0


# --------------------------------------------------------------------------
# select_trade — full orchestration
# --------------------------------------------------------------------------


def test_select_trade_buy_ce_candidate_happy_path():
    config = _config(
        delta_band_min=0.50, delta_band_max=0.70, max_spread_pct=5.0, min_oi=0, min_volume=0,
        holding_fit_multiplier=1.0, min_rr=1.0, risk_pct=0.5, entry_zone_buffer_pct=0.10, invalidation_buffer_pct=0.30,
    )
    expiry_chain = [_chain_row(strike=25200.0, side="CE", delta=0.60, ltp=140.0, bid=139.0, ask=141.0)]
    contracts = _contracts()
    support_resistance = {"support": [24800.0], "resistance": [25200.0, 25400.0]}

    result = select_trade(
        direction="UP",
        confirmation_level=25200.0,
        option_side="CE",
        expiry_chain=expiry_chain,
        contracts=contracts,
        entry_spot=25230.0,
        support_resistance=support_resistance,
        atr=50.0,
        capital=5_000_000,
        config=config,
        today=date(2025, 7, 20),
    )

    assert result["action"] == "BUY_CE_CANDIDATE"
    assert result["trade"]["strike"] == 25200.0
    assert result["trade"]["position_size_lots"] > 0


def test_select_trade_no_trade_when_no_strike_available():
    config = _config(delta_band_min=0.50, delta_band_max=0.70)
    result = select_trade(
        direction="UP", confirmation_level=25200.0, option_side="CE", expiry_chain=[],
        contracts=_contracts(), entry_spot=25230.0, support_resistance={"resistance": [25400.0]},
        atr=50.0, capital=1_000_000, config=config, today=date(2025, 7, 20),
    )
    assert result["action"] == "NO_TRADE"
    assert "no listed expiries available" in result["reasons"] or "no liquid ATM/slightly-ITM strike available" in result["reasons"]


def test_select_trade_no_trade_zero_capital_gives_zero_lots():
    config = _config(delta_band_min=0.50, delta_band_max=0.70, max_spread_pct=5.0, holding_fit_multiplier=1.0)
    expiry_chain = [_chain_row(strike=25200.0, side="CE", delta=0.60, ltp=140.0, bid=139.0, ask=141.0)]
    result = select_trade(
        direction="UP", confirmation_level=25200.0, option_side="CE", expiry_chain=expiry_chain,
        contracts=_contracts(), entry_spot=25230.0, support_resistance={"resistance": [25200.0, 25400.0]},
        atr=50.0, capital=0.0, config=config, today=date(2025, 7, 20),
    )
    assert result["action"] == "NO_TRADE"
    assert "position exceeds risk limit" in result["reasons"]


# --------------------------------------------------------------------------
# Payload / persistence / formatting
# --------------------------------------------------------------------------


def test_build_advisory_payload_includes_trade_fields():
    result = {
        "action": "BUY_CE_CANDIDATE",
        "trade": {
            "expiry": "24JUL2025", "strike": 25200.0, "side": "CE", "moneyness": "ATM",
            "entry_zone": (25200.0, 25225.2), "underlying_invalidation": 25124.4,
            "option_stop": 104.0, "target_1": 25320.0, "target_2": 25400.0,
            "risk_reward": 2.5, "position_size_lots": 7, "lot_size": 75, "entry_premium": 140.0,
        },
    }
    payload = build_advisory_payload(
        result, "2025-07-20T10:00:00+05:30", 25230.0, "BULLISH", {"support": [24800.0], "resistance": [25200.0]}, 1.1, 85,
        why=["Price crossed the level", "Volume confirmed the move"],
    )
    assert payload["strike"] == 25200.0
    assert payload["lot_size"] == 75
    assert payload["action"] == "BUY_CE_CANDIDATE"
    assert payload["why"] == ["Price crossed the level", "Volume confirmed the move"]

    message = format_advisory_message(payload)
    assert "Volume confirmed the move" in message


def test_build_advisory_payload_no_trade_includes_reasons():
    result = {"action": "NO_TRADE", "reasons": ["risk:reward below threshold"], "trade": None}
    payload = build_advisory_payload(result, "2025-07-20T10:00:00+05:30", 25000.0, "RANGE", {}, None, 40)
    assert payload["reasons"] == ["risk:reward below threshold"]
    assert "strike" not in payload


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    mock_settings = MagicMock(sqlite_db_path=db_path)
    monkeypatch.setattr("config.settings.Settings.load", lambda: mock_settings)

    from db.database import init_db

    init_db()
    return db_path


def test_write_advisory_persists(real_db):
    payload = {"data_timestamp": "2025-07-20T10:00:00+05:30", "action": "WAIT"}
    advisory_id = write_advisory(payload, score=50, rules_log={"checks": []})

    import sqlite3

    conn = sqlite3.connect(real_db)
    row = conn.execute("SELECT action, score FROM options_advisories WHERE id = ?", (advisory_id,)).fetchone()
    conn.close()
    assert row == ("WAIT", 50)


def test_format_advisory_message_states_contract_and_readable_expiry():
    payload = {
        "action": "BUY_PE_CANDIDATE", "strike": 24150.0, "side": "PE", "moneyness": "ATM",
        "expiry": "28JUL2026", "entry_zone": (24100.0, 24115.0), "underlying_invalidation": 24200.0,
        "option_stop": 60.0, "target_1": 90.0, "target_2": 120.0, "risk_reward": 2.2,
        "position_size_lots": 3, "confidence": 78, "data_timestamp": "2026-07-21T10:00:00+05:30",
    }
    message = format_advisory_message(payload)
    assert "Suggest: BUY NIFTY 24150 PE, expiry 28 July" in message
    assert "28JUL2026" in message  # unambiguous full expiry still present


def test_format_advisory_message_is_always_the_deterministic_template():
    # Regression test: format_advisory_message must never go through an
    # LLM rewrite for this message type (found live -- Ollama silently
    # dropped the option stop/target_2/position size/confidence/timestamp
    # from this exact payload shape). Every field must survive verbatim.
    payload = {
        "action": "BUY_CE_CANDIDATE", "strike": 25200.0, "side": "CE", "moneyness": "ATM",
        "expiry": "24JUL2025", "entry_zone": (25200.0, 25225.2), "underlying_invalidation": 25124.4,
        "option_stop": 104.0, "target_1": 25320.0, "target_2": 25400.0, "risk_reward": 2.5,
        "position_size_lots": 7, "confidence": 85, "data_timestamp": "2025-07-20T10:00:00+05:30",
    }
    message = format_advisory_message(payload)
    assert "BUY_CE_CANDIDATE" in message
    assert "25200" in message
    assert "104" in message  # option stop
    assert "25400" in message  # target 2
    assert "7 lot" in message  # position size
    assert "85/100" in message  # confidence
    assert "2025-07-20T10:00:00+05:30" in message  # data timestamp
