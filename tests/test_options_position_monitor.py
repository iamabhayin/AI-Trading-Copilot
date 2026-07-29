"""Tests for skills/options_position_monitor.py — Phase 15 Task 6.

Section 49/50/51 fixtures reproduce docs/options-rulebook.md's Hold /
Exit-thesis-invalidated / Partial-profit worked examples.
"""

from unittest.mock import MagicMock

import pytest

from config.options_config import OptionsConfig
from skills.options_position_monitor import (
    build_position_evidence,
    check_iv_shift,
    check_rr_deterioration,
    check_target_hit,
    check_thesis_validity,
    check_theta_danger,
    close_position_on_exit,
    evaluate_position,
    format_position_update_message,
    record_notified_state,
    should_notify,
)


def _config(**overrides) -> OptionsConfig:
    return OptionsConfig(**overrides)


def _thesis(side="CE", underlying_invalidation=25120.0, target_1=25320.0, target_2=25400.0, risk_reward=2.5) -> dict:
    return {"side": side, "underlying_invalidation": underlying_invalidation, "target_1": target_1, "target_2": target_2, "risk_reward": risk_reward}


# --------------------------------------------------------------------------
# Granular checks
# --------------------------------------------------------------------------


def test_check_thesis_validity_ce_still_valid():
    thesis = _thesis(side="CE", underlying_invalidation=25120.0)
    assert check_thesis_validity(thesis, current_spot=25300.0) is True


def test_check_thesis_validity_ce_invalidated():
    thesis = _thesis(side="CE", underlying_invalidation=25120.0)
    assert check_thesis_validity(thesis, current_spot=25100.0) is False


def test_check_thesis_validity_pe_invalidated():
    thesis = _thesis(side="PE", underlying_invalidation=24850.0)
    assert check_thesis_validity(thesis, current_spot=24900.0) is False


def test_check_thesis_validity_missing_info_defaults_valid():
    assert check_thesis_validity({}, current_spot=25000.0) is True


def test_check_target_hit_ce():
    thesis = _thesis(side="CE", target_1=25320.0, target_2=25400.0)
    result = check_target_hit(thesis, current_spot=25350.0)
    assert result == {"target_1_hit": True, "target_2_hit": False}


def test_check_target_hit_pe():
    thesis = _thesis(side="PE", target_1=24700.0, target_2=24600.0)
    result = check_target_hit(thesis, current_spot=24580.0)
    assert result == {"target_1_hit": True, "target_2_hit": True}


def test_check_iv_shift_material():
    assert check_iv_shift(entry_iv=15.0, current_iv=25.0) is True


def test_check_iv_shift_not_material():
    assert check_iv_shift(entry_iv=15.0, current_iv=16.0) is False


def test_check_iv_shift_missing_data():
    assert check_iv_shift(None, 16.0) is False


def test_check_theta_danger():
    config = _config(theta_danger_days=3)
    assert check_theta_danger(2, config) is True
    assert check_theta_danger(5, config) is False


def test_check_rr_deterioration():
    assert check_rr_deterioration(current_rr=1.0, original_rr=2.5) is True
    assert check_rr_deterioration(current_rr=2.4, original_rr=2.5) is False


def test_check_rr_deterioration_missing_data():
    assert check_rr_deterioration(None, 2.5) is False


# --------------------------------------------------------------------------
# Section 49 — Hold example
# --------------------------------------------------------------------------


def test_section_49_hold_example():
    config = _config(theta_danger_days=3)
    thesis = _thesis(side="CE", underlying_invalidation=25200.0, target_1=25320.0, target_2=25400.0, risk_reward=2.5)

    evidence = build_position_evidence(
        thesis=thesis,
        current_spot=25300.0,  # holding above breakout, below target 1
        current_regime="BULLISH",
        opposite_side_oi_classification=None,
        entry_iv=15.0,
        current_iv=15.5,
        days_to_expiry=10,
        current_rr=2.4,
        config=config,
    )
    decision = evaluate_position(evidence)

    assert decision["action"] == "HOLD"


# --------------------------------------------------------------------------
# Section 50 — Exit, thesis invalidated
# --------------------------------------------------------------------------


def test_section_50_exit_thesis_invalidated():
    config = _config(theta_danger_days=3)
    thesis = _thesis(side="CE", underlying_invalidation=25200.0, target_1=25320.0, target_2=25400.0, risk_reward=2.5)

    evidence = build_position_evidence(
        thesis=thesis,
        current_spot=25160.0,  # decisively lost 25,200
        current_regime="UNCERTAIN",
        opposite_side_oi_classification="LONG_BUILDUP",  # fresh PE buying building against the CE thesis
        entry_iv=15.0,
        current_iv=15.5,
        days_to_expiry=10,
        current_rr=None,
        config=config,
    )
    decision = evaluate_position(evidence)

    assert decision["action"] == "EXIT_THESIS_INVALIDATED"


# --------------------------------------------------------------------------
# Section 51 — Partial profit / trail
# --------------------------------------------------------------------------


def test_section_51_partial_profit_momentum_favorable():
    config = _config(theta_danger_days=3)
    thesis = _thesis(side="CE", underlying_invalidation=25200.0, target_1=25320.0, target_2=25400.0, risk_reward=2.5)

    evidence = build_position_evidence(
        thesis=thesis,
        current_spot=25330.0,  # T1 reached, still bullish
        current_regime="BULLISH",
        opposite_side_oi_classification=None,
        entry_iv=15.0,
        current_iv=15.2,
        days_to_expiry=10,
        current_rr=2.4,
        config=config,
    )
    decision = evaluate_position(evidence)

    assert decision["action"] == "PARTIAL_PROFIT"
    assert "momentum still favorable" in decision["why"]


def test_exit_target_when_t2_hit():
    config = _config(theta_danger_days=3)
    thesis = _thesis(side="CE", underlying_invalidation=25200.0, target_1=25320.0, target_2=25400.0, risk_reward=2.5)

    evidence = build_position_evidence(
        thesis=thesis, current_spot=25410.0, current_regime="BULLISH", opposite_side_oi_classification=None,
        entry_iv=15.0, current_iv=15.2, days_to_expiry=10, current_rr=3.0, config=config,
    )
    decision = evaluate_position(evidence)

    assert decision["action"] == "EXIT_TARGET"


def test_exit_risk_changed_on_theta_danger():
    config = _config(theta_danger_days=3)
    thesis = _thesis(side="CE", underlying_invalidation=25200.0, target_1=25320.0, target_2=25400.0)

    evidence = build_position_evidence(
        thesis=thesis, current_spot=25250.0, current_regime="BULLISH", opposite_side_oi_classification=None,
        entry_iv=15.0, current_iv=15.2, days_to_expiry=1, current_rr=2.4, config=config,
    )
    decision = evaluate_position(evidence)

    assert decision["action"] == "EXIT_RISK_CHANGED"
    assert "theta danger" in decision["why"]


def test_hold_trail_on_weakening_structure():
    config = _config(theta_danger_days=3)
    thesis = _thesis(side="CE", underlying_invalidation=25200.0, target_1=25320.0, target_2=25400.0)

    # Still valid (above invalidation) but regime no longer agrees.
    evidence = build_position_evidence(
        thesis=thesis, current_spot=25250.0, current_regime="RANGE", opposite_side_oi_classification=None,
        entry_iv=15.0, current_iv=15.2, days_to_expiry=10, current_rr=2.4, config=config,
    )
    decision = evaluate_position(evidence)

    assert decision["action"] == "HOLD_TRAIL"


def test_evaluate_position_priority_invalidated_over_target():
    # Even if target hit, an invalidated thesis takes priority.
    evidence = {"thesis_invalidated": True, "invalidation_reason": "lost structure", "target_2_hit": True}
    decision = evaluate_position(evidence)
    assert decision["action"] == "EXIT_THESIS_INVALIDATED"


# --------------------------------------------------------------------------
# Section 53 format, dedup, close
# --------------------------------------------------------------------------


def test_format_position_update_message():
    position = {"contract": "NIFTY 25200 CE 24JUL2025", "entry_premium": 140.0, "qty_lots": 2, "lot_size": 75}
    decision = {"action": "HOLD", "why": "thesis still valid"}
    text = format_position_update_message(position, decision, current_premium=165.0, current_spot=25300.0)
    assert "ACTION: HOLD" in text
    assert "CURRENT SPOT: 25300" in text
    assert "P&L:" in text


def test_should_notify_on_state_change():
    position = {"last_notified_state": "HOLD"}
    assert should_notify(position, {"action": "HOLD_TRAIL"}) is True


def test_should_notify_false_on_identical_state():
    position = {"last_notified_state": "HOLD"}
    assert should_notify(position, {"action": "HOLD"}) is False


def test_should_notify_true_when_never_notified():
    position = {"last_notified_state": None}
    assert should_notify(position, {"action": "HOLD"}) is True


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    mock_settings = MagicMock(sqlite_db_path=db_path)
    monkeypatch.setattr("config.settings.Settings.load", lambda: mock_settings)

    from db.database import init_db

    init_db()
    return db_path


def test_record_notified_state(real_db):
    from db.database import execute, fetch_one

    position_id = execute(
        """INSERT INTO options_positions
           (status, contract, expiry_date, strike, side, qty_lots, lot_size, entry_premium, thesis_json)
           VALUES ('active', 'NIFTY 25200 CE 24JUL2025', '24JUL2025', 25200.0, 'CE', 1, 75, 140.0, '{}')""",
    )

    record_notified_state(position_id, "HOLD_TRAIL")

    row = fetch_one("SELECT last_notified_state FROM options_positions WHERE id = ?", (position_id,))
    assert row["last_notified_state"] == "HOLD_TRAIL"


def test_close_position_on_exit(real_db):
    from db.database import execute, fetch_one

    position_id = execute(
        """INSERT INTO options_positions
           (status, contract, expiry_date, strike, side, qty_lots, lot_size, entry_premium, thesis_json)
           VALUES ('active', 'NIFTY 25200 CE 24JUL2025', '24JUL2025', 25200.0, 'CE', 1, 75, 140.0, '{}')""",
    )

    close_position_on_exit(position_id, exit_premium=230.0)

    row = fetch_one("SELECT status, exit_premium FROM options_positions WHERE id = ?", (position_id,))
    assert row["status"] == "closed"
    assert row["exit_premium"] == 230.0


def test_close_position_on_exit_never_touches_thesis_json(real_db):
    from db.database import execute, fetch_one

    position_id = execute(
        """INSERT INTO options_positions
           (status, contract, expiry_date, strike, side, qty_lots, lot_size, entry_premium, thesis_json)
           VALUES ('active', 'NIFTY 25200 CE 24JUL2025', '24JUL2025', 25200.0, 'CE', 1, 75, 140.0, '{"original": true}')""",
    )

    close_position_on_exit(position_id, exit_premium=230.0)

    row = fetch_one("SELECT thesis_json FROM options_positions WHERE id = ?", (position_id,))
    assert row["thesis_json"] == '{"original": true}'
