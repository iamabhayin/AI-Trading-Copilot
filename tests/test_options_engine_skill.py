"""Tests for skills/options_engine_skill.py — Phase 15 Task 7.

`fetch_cycle_data` (all Angel One/yfinance I/O) is always mocked as one
unit — no live calls. Everything downstream of it (analytics, rules
engine, trade selector) runs for real against a real tmp-path SQLite DB,
since those modules are already thoroughly unit-tested elsewhere and
this file's job is verifying the ORCHESTRATION (decision flow, DB
writes, which notify function fires) is wired correctly.
"""

from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from config.options_config import OptionsConfig
from skills.angel_client import AngelAuthError
from skills.options_data_fetch import DataInsufficientError
from skills.options_engine_skill import (
    _fetch_next_weekly_chain_fallback,
    _format_confirmation_checklist,
    _format_delta_oi_lines,
    _handle_confirmed,
    _handle_escalation,
    _handle_false_breakout,
    acquire_lock,
    compute_valid_until,
    fetch_contract_premium_history,
    fetch_contract_volume_history,
    fetch_cycle_data,
    fetch_first_of_day_snapshot,
    fetch_prior_day_last_snapshot,
    format_breach_unconfirmed_message,
    format_false_breakout_message,
    format_session_end_message,
    format_session_start_message,
    format_watch_escalation_message,
    get_engine_state,
    get_greeks_sanity_checked_date,
    is_market_hours,
    mark_greeks_sanity_checked,
    notify_session_end,
    notify_session_start,
    record_alert_sent,
    refetch_spot_for_alert,
    release_lock,
    run_cycle,
    run_end_of_day,
    save_engine_state,
    should_run_normal_cycle,
    signed_distance_pts,
)

IST = ZoneInfo("Asia/Kolkata")


def _config(**overrides) -> OptionsConfig:
    return OptionsConfig(**overrides)


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    mock_settings = MagicMock(sqlite_db_path=db_path)
    monkeypatch.setattr("config.settings.Settings.load", lambda: mock_settings)

    from db.database import init_db

    init_db()
    return db_path


def _wide_candles(closes: list[float]) -> pd.DataFrame:
    """Low/high +/-1000 from close -- keeps retest-detection logic from
    firing unintentionally (see test_options_rules_engine.py). Only use
    for candles_1m -- using this for candles_5m corrupts swing-level
    detection (detect_swing_levels reads high/low directly)."""
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 1000 for c in closes],
            "low": [c - 1000 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )


def _realistic_candles(closes: list[float]) -> pd.DataFrame:
    """Small, realistic high/low range around each close -- use for
    candles_5m, which feeds detect_swing_levels/compute_atr for real."""
    return pd.DataFrame(
        {
            "open": closes,
            "high": [c + 10 for c in closes],
            "low": [c - 10 for c in closes],
            "close": closes,
            "volume": [1000] * len(closes),
        }
    )


def _chain_row(strike, side, ltp=100.0, oi=1_000_000, delta=0.5, bid=99.0, ask=101.0, volume=50_000, iv=15.0):
    return {"strike": strike, "side": side, "ltp": ltp, "oi": oi, "delta": delta, "bid": bid, "ask": ask, "volume": volume, "iv": iv, "gamma": 0.002, "theta": -5.0, "vega": 10.0}


def _sample_chain():
    return [
        _chain_row(24800.0, "PE", oi=6_000_000),
        _chain_row(24800.0, "CE", oi=1_000_000),
        _chain_row(25000.0, "CE", oi=2_000_000),
        _chain_row(25000.0, "PE", oi=2_000_000),
        _chain_row(25200.0, "CE", oi=7_000_000),
        _chain_row(25200.0, "PE", oi=1_000_000),
    ]


def _cycle_data(spot, chain=None, contracts=None):
    return {
        "spot": spot,
        "expiry": "24JUL2025",
        "chain": chain if chain is not None else _sample_chain(),
        "contracts": contracts if contracts is not None else [{"expiry": "24JUL2025", "lotsize": "75"}],
        "trading_date": "2025-07-20",
        "snapshot_ts": "2025-07-20T10:00:00+05:30",
        "candles_5m": _realistic_candles([25000, 25050, 25100, 25150, 25190]),
        "candles_1m": _wide_candles([25180, 25225, 25230, 25235, 25240, 25245]),
    }


# --------------------------------------------------------------------------
# Lock
# --------------------------------------------------------------------------


def test_acquire_lock_succeeds_when_no_lock_exists(tmp_path):
    config = _config(engine_lock_path=str(tmp_path / "engine.lock"))
    assert acquire_lock(config, datetime.now(IST)) is True


def test_acquire_lock_fails_when_recently_held(tmp_path):
    config = _config(engine_lock_path=str(tmp_path / "engine.lock"), lock_staleness_seconds=300)
    now = datetime.now(IST)
    assert acquire_lock(config, now) is True
    assert acquire_lock(config, now) is False


def test_acquire_lock_succeeds_when_stale(tmp_path):
    from datetime import timedelta

    config = _config(engine_lock_path=str(tmp_path / "engine.lock"), lock_staleness_seconds=60)
    old = datetime.now(IST) - timedelta(seconds=120)
    assert acquire_lock(config, old) is True
    assert acquire_lock(config, datetime.now(IST)) is True


def test_release_lock_removes_file(tmp_path):
    config = _config(engine_lock_path=str(tmp_path / "engine.lock"))
    acquire_lock(config, datetime.now(IST))
    release_lock(config)
    from pathlib import Path

    assert not Path(config.engine_lock_path).exists()


# --------------------------------------------------------------------------
# Cadence
# --------------------------------------------------------------------------


def test_should_run_normal_cycle_watch_mode_always_true():
    assert should_run_normal_cycle({"mode": "WATCH"}, datetime.now(IST), _config()) is True


def test_should_run_normal_cycle_no_prior_run():
    assert should_run_normal_cycle({"mode": "NORMAL"}, datetime.now(IST), _config()) is True


def test_should_run_normal_cycle_five_minute_boundary():
    now = datetime(2026, 7, 20, 10, 5, tzinfo=IST)
    state = {"mode": "NORMAL", "last_run_ts": datetime(2026, 7, 20, 10, 4, tzinfo=IST).isoformat()}
    assert should_run_normal_cycle(state, now, _config()) is True


def test_should_run_normal_cycle_not_boundary_and_recent():
    now = datetime(2026, 7, 20, 10, 2, tzinfo=IST)
    state = {"mode": "NORMAL", "last_run_ts": datetime(2026, 7, 20, 10, 1, tzinfo=IST).isoformat()}
    assert should_run_normal_cycle(state, now, _config()) is False


# --------------------------------------------------------------------------
# Engine state persistence (real DB)
# --------------------------------------------------------------------------


def test_get_engine_state_default_row(real_db):
    state = get_engine_state()
    assert state["mode"] == "NORMAL"


def test_save_and_get_engine_state_roundtrip(real_db):
    save_engine_state({"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": "2025-07-20T10:00:00+05:30"}, "2025-07-20T10:00:00+05:30")
    state = get_engine_state()
    assert state["mode"] == "WATCH"
    assert state["watch_level"] == 25200.0
    assert state["watch_direction"] == "UP"


# --------------------------------------------------------------------------
# Historical snapshot queries (real DB)
# --------------------------------------------------------------------------


def _insert_snapshot(trading_date, snapshot_ts, expiry_date, strike, side, oi):
    from db.database import execute

    execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot) VALUES (?,?,?,?,?,?,?)",
        (snapshot_ts, trading_date, expiry_date, strike, side, oi, 25000.0),
    )


def test_fetch_first_of_day_snapshot(real_db):
    _insert_snapshot("2025-07-20", "2025-07-20T09:15:00", "24JUL2025", 25200.0, "CE", 100)
    _insert_snapshot("2025-07-20", "2025-07-20T09:20:00", "24JUL2025", 25200.0, "CE", 200)

    rows = fetch_first_of_day_snapshot("24JUL2025", "2025-07-20")
    assert rows[0]["oi"] == 100


def test_fetch_contract_volume_history_oldest_to_newest(real_db):
    from db.database import execute

    for ts, vol in [("2025-07-20T09:45:00+05:30", 10_000), ("2025-07-20T09:50:00+05:30", 10_500), ("2025-07-20T09:55:00+05:30", 11_500)]:
        execute(
            "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot, volume) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "2025-07-20", "24JUL2025", 25200.0, "CE", 1_000_000, 25000.0, vol),
        )

    history = fetch_contract_volume_history("24JUL2025", 25200.0, "CE", "2025-07-20T09:55:00+05:30")
    assert history == [10_000, 10_500, 11_500]


def test_fetch_contract_volume_history_respects_before_ts(real_db):
    from db.database import execute

    for ts, vol in [("2025-07-20T09:45:00+05:30", 10_000), ("2025-07-20T10:05:00+05:30", 99_999)]:
        execute(
            "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot, volume) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "2025-07-20", "24JUL2025", 25200.0, "CE", 1_000_000, 25000.0, vol),
        )

    history = fetch_contract_volume_history("24JUL2025", 25200.0, "CE", "2025-07-20T10:00:00+05:30")
    assert history == [10_000]


def test_fetch_contract_premium_history_oldest_to_newest(real_db):
    from db.database import execute

    for ts, ltp in [("2025-07-20T09:45:00+05:30", 35.0), ("2025-07-20T09:50:00+05:30", 45.0), ("2025-07-20T09:55:00+05:30", 55.0)]:
        execute(
            "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot, ltp) VALUES (?,?,?,?,?,?,?,?)",
            (ts, "2025-07-20", "24JUL2025", 25200.0, "CE", 1_000_000, 25000.0, ltp),
        )

    history = fetch_contract_premium_history("24JUL2025", 25200.0, "CE", "2025-07-20T09:55:00+05:30")
    assert history == [35.0, 45.0, 55.0]


def test_fetch_contract_premium_history_drops_null_rows(real_db):
    from db.database import execute

    execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot, ltp) VALUES (?,?,?,?,?,?,?,?)",
        ("2025-07-20T09:45:00+05:30", "2025-07-20", "24JUL2025", 25200.0, "CE", 1_000_000, 25000.0, None),
    )
    execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot, ltp) VALUES (?,?,?,?,?,?,?,?)",
        ("2025-07-20T09:50:00+05:30", "2025-07-20", "24JUL2025", 25200.0, "CE", 1_000_000, 25000.0, 45.0),
    )

    history = fetch_contract_premium_history("24JUL2025", 25200.0, "CE", "2025-07-20T09:50:00+05:30")
    assert history == [45.0]


def test_fetch_prior_day_last_snapshot(real_db):
    _insert_snapshot("2025-07-19", "2025-07-19T15:25:00", "24JUL2025", 25200.0, "CE", 500)
    _insert_snapshot("2025-07-20", "2025-07-20T09:15:00", "24JUL2025", 25200.0, "CE", 100)

    rows = fetch_prior_day_last_snapshot("24JUL2025", "2025-07-20")
    assert rows[0]["oi"] == 500


def test_fetch_prior_day_last_snapshot_none_when_first_day(real_db):
    _insert_snapshot("2025-07-20", "2025-07-20T09:15:00", "24JUL2025", 25200.0, "CE", 100)
    assert fetch_prior_day_last_snapshot("24JUL2025", "2025-07-20") == []


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------


def test_format_watch_escalation_message():
    analysis = {
        "regime": "BULLISH",
        "support_resistance": {"support": [24800.0, 24825.4], "resistance": [25200.0, 25260.1]},
        "oi_levels": {"support": [24800.0], "resistance": [25200.0]},
        "price_levels": {"swing_low": 24825.4, "swing_high": 25260.123456},
        "pcr": 1.1,
    }
    text = format_watch_escalation_message(25200.0, "UP", 25190.0, analysis)
    assert "\U0001F7E1 WATCHING NIFTY RESISTANCE" in text
    assert "25200" in text
    assert "RESISTANCE: 25200 (OI wall)" in text
    assert "RESISTANCE (price structure): 25260.12" in text
    assert "Support: 24800 (OI wall)" in text
    assert "Spot: 25,190.00" in text  # 2026-07-28: bumped from 1 to 2 decimals


def test_format_watch_escalation_message_includes_timestamps_when_provided():
    analysis = {"regime": "BULLISH", "support_resistance": {}, "oi_levels": {}, "price_levels": {}, "pcr": 1.1}
    text = format_watch_escalation_message(
        25200.0, "UP", 25190.0, analysis,
        data_ts=datetime(2026, 7, 21, 14, 24, 59, tzinfo=IST),
        sent_ts=datetime(2026, 7, 21, 14, 25, 3, tzinfo=IST),
    )
    assert "Data as of: 14:24:59 IST" in text
    assert "Sent at: 14:25:03 IST" in text


def test_format_watch_escalation_message_omits_timestamps_when_not_provided():
    analysis = {"regime": "BULLISH", "support_resistance": {}, "oi_levels": {}, "price_levels": {}, "pcr": 1.1}
    text = format_watch_escalation_message(25200.0, "UP", 25190.0, analysis)
    assert "Data as of" not in text
    assert "Sent at" not in text


def test_format_watch_escalation_message_shows_refetch_failed_warning():
    analysis = {"regime": "BULLISH", "support_resistance": {}, "oi_levels": {}, "price_levels": {}, "pcr": 1.1}
    text = format_watch_escalation_message(25200.0, "UP", 25190.0, analysis, spot_refetch_ok=False)
    assert "spot re-check failed" in text


def test_format_breach_unconfirmed_message():
    analysis = {"regime": "BEARISH", "support_resistance": {}, "oi_levels": {}, "price_levels": {}, "pcr": 0.8}
    text = format_breach_unconfirmed_message(24022.2, "DOWN", 24015.0, analysis)
    assert "LEVEL BREACHED -- AWAITING CONFIRMATION (SUPPORT)" in text
    assert "already past 24022.2 support" in text
    assert "bearish breakdown -> PE" in text


def test_format_false_breakout_message():
    text = format_false_breakout_message(25200.0, "UP", {"oi_classification": "SHORT_BUILDUP"})
    assert "FALSE BREAKOUT" in text
    assert "SHORT_BUILDUP" in text


def test_format_false_breakout_message_includes_checklist_and_valid_until():
    confirmation = {
        "crossed": True, "sustained": False, "volume_confirmed": True,
        "oi_supports": False, "structure_agrees": True, "retest": False,
        "oi_classification": "SHORT_BUILDUP",
    }
    text = format_false_breakout_message(25200.0, "UP", confirmation, valid_until=datetime(2026, 7, 22, 10, 5, tzinfo=IST))
    assert "Resistance breakout → ✘" in text  # crossed=True but sustained=False -> not met
    assert "Call unwinding → ✘" in text  # oi_classification=SHORT_BUILDUP, not SHORT_COVERING
    assert "VERDICT: FALSE BREAKOUT" in text
    assert "Valid until: 10:05:00 IST (next scan)" in text


# --------------------------------------------------------------------------
# Part B: signed distance, ΔOI-at-strike, confirmation checklist
# --------------------------------------------------------------------------


def test_signed_distance_pts_up_not_yet_reached_is_positive():
    assert signed_distance_pts(24190.0, 24200.0, "UP") == pytest.approx(10.0)


def test_signed_distance_pts_up_already_past_is_negative():
    assert signed_distance_pts(24210.0, 24200.0, "UP") == pytest.approx(-10.0)


def test_signed_distance_pts_down_matches_real_incident():
    # The actual incident numbers: spot 24015, level 24022.2, already
    # breached by 7.2 pts -- must render as a real negative number, never
    # rounded away to "0.00%".
    assert signed_distance_pts(24015.0, 24022.2, "DOWN") == pytest.approx(-7.2)


def test_signed_distance_pts_down_still_safe_is_positive():
    assert signed_distance_pts(24030.0, 24022.2, "DOWN") == pytest.approx(7.8)


def test_format_confirmation_checklist_all_pending_when_no_confirmation():
    """A fresh WATCH escalation: no evidence evaluated yet, regime unknown
    -- every row must render as pending (⏳), never a false negative."""
    lines = _format_confirmation_checklist(None, None, "CE")
    assert all("⏳" in line for line in lines[1:])


def test_format_confirmation_checklist_mixed_states():
    confirmation = {
        "crossed": True, "sustained": False, "volume_confirmed": True,
        "oi_classification": "SHORT_COVERING", "opposite_oi_classification": "SHORT_BUILDUP",
        "premium_confirms": True,
    }
    lines = _format_confirmation_checklist(confirmation, "BULLISH", "CE")
    text = "\n".join(lines)
    assert "Trend → Bullish ✔" in text
    assert "Resistance breakout → ✘" in text  # crossed=True but sustained=False -> not met
    assert "Volume → Above average ✔" in text
    assert "Call unwinding → ✔" in text
    assert "Put writing → ✔" in text
    assert "Premium resistance breakout → ✔" in text


def test_format_confirmation_checklist_never_includes_trade_dependent_rows():
    """WATCH-mode alerts never have a selected trade candidate yet, so the
    strike/IV/R:R rows the final BUY message adds must never appear here."""
    confirmation = {"crossed": True, "sustained": True, "volume_confirmed": True}
    text = "\n".join(_format_confirmation_checklist(confirmation, "BULLISH", "CE"))
    assert "ATM/ITM strike" not in text
    assert "IV acceptable" not in text
    assert "Risk:Reward" not in text


def test_format_delta_oi_lines_tags_primary_side_defending():
    chain = [{"strike": 24000.0, "side": "PE", "oi": 6_000_000}, {"strike": 24000.0, "side": "CE", "oi": 1_300_000}]
    previous = [{"strike": 24000.0, "side": "PE", "oi": 4_800_000}, {"strike": 24000.0, "side": "CE", "oi": 1_000_000}]
    lines = _format_delta_oi_lines(24000.0, "PE", chain, previous)
    assert "24000 PE: +12.0L  -> writers DEFENDING the wall" in lines[0]
    assert "24000 CE: +3.0L" in lines[1]
    assert "writers" not in lines[1]


def test_format_delta_oi_lines_tags_unwinding_on_negative_change():
    chain = [{"strike": 24000.0, "side": "PE", "oi": 4_000_000}]
    previous = [{"strike": 24000.0, "side": "PE", "oi": 6_000_000}]
    lines = _format_delta_oi_lines(24000.0, "PE", chain, previous)
    assert "-20.0L  -> writers UNWINDING" in lines[0]


def test_format_delta_oi_lines_skips_missing_data():
    assert _format_delta_oi_lines(24000.0, "PE", [], None) == []


def test_format_watch_escalation_message_full_rich_rendering():
    """End-to-end: the real incident's numbers, with a zone-merge, a next
    level, ΔOI, PCR/VIX, and an all-pending checklist all rendering
    together in one alert."""
    analysis = {
        "regime": "RANGE",
        "oi_levels": {"support": [24000.0], "resistance": [24200.0]},
        "price_levels": {"swing_low": 24022.2, "swing_high": 24197.2},
        "pcr": 0.86265,
        "vix": {"latest": 13.8, "change": 0.2},
    }
    chain = [
        {"strike": 24000.0, "side": "PE", "oi": 6_000_000},
        {"strike": 24000.0, "side": "CE", "oi": 1_300_000},
        {"strike": 23900.0, "side": "PE", "oi": 4_000_000},
    ]
    previous_snapshot = [
        {"strike": 24000.0, "side": "PE", "oi": 4_800_000},
        {"strike": 24000.0, "side": "CE", "oi": 1_000_000},
    ]
    config = OptionsConfig(zone_merge_threshold_pct=0.15, next_level_min_share=0.15)

    text = format_watch_escalation_message(
        24000.0, "DOWN", 24023.1, analysis,
        chain=chain, previous_snapshot=previous_snapshot, confirmation=None, config=config,
        valid_until=datetime(2026, 7, 21, 14, 26, tzinfo=IST),
    )

    assert "SUPPORT ZONE: 24000 (OI wall) -- 22 pts -- 24022.2 (structure)" in text
    assert "Next level below: 23900 (next PE-OI cluster)" in text
    assert "Resistance: 24200 (OI wall)" in text
    assert "PCR: 0.86        VIX: 13.8 (+0.2)" in text
    assert "24000 PE: +12.0L  -> writers DEFENDING the wall" in text
    assert "24000 CE: +3.0L" in text
    assert "CONFIRMATION CHECKLIST" in text
    assert "Trend → Bearish ✘" in text  # regime is RANGE, not BEARISH -- known and not met, not pending
    assert "Support breakdown → ⏳" in text  # crossed/sustained not evaluated yet (confirmation=None)
    assert "VERDICT: WATCH" in text
    assert "Valid until: 14:26:00 IST (next scan)" in text


# --------------------------------------------------------------------------
# Alert staleness fix: fresh-spot re-check, escalation branching, valid_until
# --------------------------------------------------------------------------


def test_refetch_spot_for_alert_uses_fresh_value():
    with patch("skills.options_engine_skill.fetch_nifty_spot_backup", return_value=24016.5):
        spot, ok = refetch_spot_for_alert(cycle_spot=24023.1)
    assert spot == 24016.5
    assert ok is True


def test_refetch_spot_for_alert_falls_back_on_failure():
    with patch("skills.options_engine_skill.fetch_nifty_spot_backup", return_value=None):
        spot, ok = refetch_spot_for_alert(cycle_spot=24023.1)
    assert spot == 24023.1
    assert ok is False


def test_compute_valid_until_watch_mode_is_one_minute():
    now = datetime(2026, 7, 22, 10, 0, 0, tzinfo=IST)
    assert compute_valid_until(now, "WATCH") == datetime(2026, 7, 22, 10, 1, 0, tzinfo=IST)


def test_compute_valid_until_normal_mode_is_five_minutes():
    now = datetime(2026, 7, 22, 10, 0, 0, tzinfo=IST)
    assert compute_valid_until(now, "NORMAL") == datetime(2026, 7, 22, 10, 5, 0, tzinfo=IST)


def test_record_alert_sent_persists_row(real_db):
    record_alert_sent("WATCHING", "UP", 25200.0, "2026-07-22T10:00:00+05:30", datetime(2026, 7, 22, 10, 1, 0, tzinfo=IST))

    from db.database import fetch_one

    row = fetch_one("SELECT alert_type, direction, level, data_ts, valid_until FROM options_alerts_sent")
    assert row["alert_type"] == "WATCHING"
    assert row["direction"] == "UP"
    assert row["level"] == 25200.0
    assert row["valid_until"] == "2026-07-22T10:01:00+05:30"


def test_handle_escalation_sends_watching_when_not_yet_crossed(real_db):
    next_state = {"watch_level": 25200.0, "watch_direction": "UP"}
    analysis = {"regime": "BULLISH", "support_resistance": {}, "oi_levels": {}, "price_levels": {}, "pcr": 1.0}
    cycle_data = {"snapshot_ts": "2026-07-22T10:00:00+05:30", "chain": []}
    now = datetime(2026, 7, 22, 10, 0, 5, tzinfo=IST)

    with (
        patch("skills.options_engine_skill.fetch_nifty_spot_backup", return_value=25190.0),
        patch("skills.options_engine_skill.fetch_india_vix_with_change", return_value=None),
        patch("skills.options_engine_skill.notify_watch_escalation") as mock_watching,
        patch("skills.options_engine_skill.notify_breach_unconfirmed") as mock_breach,
    ):
        _handle_escalation(next_state, cycle_spot=25185.0, analysis=analysis, cycle_data=cycle_data, now=now, previous_snapshot=[], config=OptionsConfig())

    mock_watching.assert_called_once()
    mock_breach.assert_not_called()
    assert mock_watching.call_args[0][2] == 25190.0  # fresh spot, not the stale cycle spot

    from db.database import fetch_one

    row = fetch_one("SELECT alert_type FROM options_alerts_sent")
    assert row["alert_type"] == "WATCHING"


def test_handle_escalation_sends_breach_unconfirmed_when_already_crossed(real_db):
    """The exact incident this fix targets: cycle spot (24023.1) hadn't
    crossed the level, but a fresh re-check shows the market already has."""
    next_state = {"watch_level": 24022.2, "watch_direction": "DOWN"}
    analysis = {"regime": "BEARISH", "support_resistance": {}, "oi_levels": {}, "price_levels": {}, "pcr": 0.9}
    cycle_data = {"snapshot_ts": "2026-07-21T14:24:59+05:30", "chain": []}
    now = datetime(2026, 7, 21, 14, 25, 3, tzinfo=IST)

    with (
        patch("skills.options_engine_skill.fetch_nifty_spot_backup", return_value=24015.0),
        patch("skills.options_engine_skill.fetch_india_vix_with_change", return_value=None),
        patch("skills.options_engine_skill.notify_watch_escalation") as mock_watching,
        patch("skills.options_engine_skill.notify_breach_unconfirmed") as mock_breach,
    ):
        _handle_escalation(next_state, cycle_spot=24023.1, analysis=analysis, cycle_data=cycle_data, now=now, previous_snapshot=[], config=OptionsConfig())

    mock_watching.assert_not_called()
    mock_breach.assert_called_once()
    assert mock_breach.call_args[0][2] == 24015.0

    from db.database import fetch_one

    row = fetch_one("SELECT alert_type, level FROM options_alerts_sent")
    assert row["alert_type"] == "BREACH_UNCONFIRMED"
    assert row["level"] == 24022.2


def test_handle_escalation_passes_refetch_failure_through(real_db):
    next_state = {"watch_level": 25200.0, "watch_direction": "UP"}
    analysis = {"regime": "BULLISH", "support_resistance": {}, "oi_levels": {}, "price_levels": {}, "pcr": 1.0}
    cycle_data = {"snapshot_ts": "2026-07-22T10:00:00+05:30", "chain": []}
    now = datetime(2026, 7, 22, 10, 0, 5, tzinfo=IST)

    with (
        patch("skills.options_engine_skill.fetch_nifty_spot_backup", return_value=None),
        patch("skills.options_engine_skill.fetch_india_vix_with_change", return_value=None),
        patch("skills.options_engine_skill.notify_watch_escalation") as mock_watching,
    ):
        _handle_escalation(next_state, cycle_spot=25190.0, analysis=analysis, cycle_data=cycle_data, now=now, previous_snapshot=[], config=OptionsConfig())

    assert mock_watching.call_args[0][2] == 25190.0  # fell back to cycle spot
    assert mock_watching.call_args[0][6] is False  # spot_refetch_ok


def test_handle_false_breakout_records_alert_sent(real_db):
    engine_state = {"watch_level": 25200.0, "watch_direction": "UP"}
    confirmation = {"oi_classification": "SHORT_BUILDUP"}
    now = datetime(2026, 7, 22, 10, 0, 0, tzinfo=IST)

    with patch("skills.options_engine_skill.notify_false_breakout") as mock_notify:
        _handle_false_breakout(engine_state, confirmation, now)

    mock_notify.assert_called_once()

    from db.database import fetch_one

    row = fetch_one("SELECT alert_type, valid_until FROM options_alerts_sent")
    assert row["alert_type"] == "FALSE_BREAKOUT"
    assert row["valid_until"] == "2026-07-22T10:05:00+05:30"


# --------------------------------------------------------------------------
# Rulebook Section 34 fallback: next-weekly expiry when the primary weekly
# doesn't have enough runway for a new trade
# --------------------------------------------------------------------------


def test_fetch_next_weekly_chain_fallback_returns_none_without_later_expiry():
    cycle_data = _cycle_data(spot=25010.0)
    config = _config()

    with (
        patch("skills.options_engine_skill.list_expiries", return_value=["24JUL2025"]),
        patch("skills.options_engine_skill.select_next_weekly_expiry", return_value=None) as mock_select,
    ):
        result = _fetch_next_weekly_chain_fallback(cycle_data, spot=25010.0, config=config, today=date(2025, 7, 24))

    assert result is None
    mock_select.assert_called_once()


def test_fetch_next_weekly_chain_fallback_fetches_next_expiry():
    cycle_data = _cycle_data(spot=25010.0)
    config = _config()
    fallback_chain = [_chain_row(25200.0, "CE")]

    with (
        patch("skills.options_engine_skill.list_expiries", return_value=["24JUL2025", "31JUL2025"]),
        patch("skills.options_engine_skill.select_next_weekly_expiry", return_value="31JUL2025"),
        patch("skills.options_engine_skill.ensure_session", return_value={"jwt_token": "j"}),
        patch("skills.options_engine_skill.build_client", return_value=MagicMock()),
        patch("skills.options_engine_skill.fetch_expiry_chain", return_value=fallback_chain) as mock_fetch,
    ):
        result = _fetch_next_weekly_chain_fallback(cycle_data, spot=25010.0, config=config, today=date(2025, 7, 24))

    assert result == fallback_chain
    assert mock_fetch.call_args[0][2] == "31JUL2025"


def test_fetch_next_weekly_chain_fallback_returns_none_on_fetch_failure():
    cycle_data = _cycle_data(spot=25010.0)
    config = _config()

    with (
        patch("skills.options_engine_skill.list_expiries", return_value=["24JUL2025", "31JUL2025"]),
        patch("skills.options_engine_skill.select_next_weekly_expiry", return_value="31JUL2025"),
        patch("skills.options_engine_skill.ensure_session", side_effect=AngelAuthError("boom")),
        patch("skills.options_engine_skill.route_message") as mock_route,
    ):
        result = _fetch_next_weekly_chain_fallback(cycle_data, spot=25010.0, config=config, today=date(2025, 7, 24))

    assert result is None
    mock_route.assert_called_once()
    assert mock_route.call_args[0][0] == "monitoring"


def test_handle_confirmed_retries_with_fallback_chain_when_insufficient_time():
    engine_state = {"watch_direction": "UP", "watch_level": 25200.0}
    chain = _sample_chain()
    cycle_data = _cycle_data(spot=25230.0, chain=chain)
    analysis = {"support_resistance": {"resistance": [25200.0]}, "volume": {"confirmed": True}, "pcr": 1.0, "greeks_crosscheck": []}
    confirmation = {"confirmed": True, "oi_classification": "LONG_BUILDUP"}
    config = _config(min_score=0)
    fallback_chain = [_chain_row(25200.0, "CE")]
    first_result = {"action": "NO_TRADE", "reasons": ["insufficient time to expiry"], "trade": None}
    second_result = {
        "action": "BUY_CE_CANDIDATE",
        "trade": {
            "expiry": "31JUL2025", "strike": 25200.0, "side": "CE", "moneyness": "ATM",
            "entry_zone": (25200.0, 25225.0), "underlying_invalidation": 25100.0,
            "option_stop": 60.0, "target_1": 90.0, "target_2": 120.0, "risk_reward": 2.0,
            "position_size_lots": 2, "lot_size": 75, "entry_premium": 75.0,
        },
    }

    with (
        patch("skills.options_engine_skill.select_trade", side_effect=[first_result, second_result]) as mock_select_trade,
        patch("skills.options_engine_skill._fetch_next_weekly_chain_fallback", return_value=fallback_chain) as mock_fallback,
        patch("skills.options_engine_skill.write_advisory") as mock_write,
        patch("skills.options_engine_skill.notify_advisory") as mock_notify,
    ):
        _handle_confirmed(
            engine_state, chain, [], cycle_data, analysis, confirmation, 25230.0, atr=10.0, regime="BULLISH",
            now=datetime(2026, 7, 20, 10, 20, tzinfo=IST), config=config, today=date(2026, 7, 20),
        )

    assert mock_select_trade.call_count == 2
    mock_fallback.assert_called_once()
    assert mock_select_trade.call_args_list[1].kwargs["expiry_chain"] == fallback_chain
    mock_write.assert_called_once()
    mock_notify.assert_called_once()


def test_handle_confirmed_no_retry_when_fallback_unavailable():
    engine_state = {"watch_direction": "UP", "watch_level": 25200.0}
    chain = _sample_chain()
    cycle_data = _cycle_data(spot=25230.0, chain=chain)
    analysis = {"support_resistance": {"resistance": [25200.0]}, "volume": {}, "pcr": 1.0, "greeks_crosscheck": []}
    confirmation = {"confirmed": True, "oi_classification": "LONG_BUILDUP"}
    config = _config()
    first_result = {"action": "NO_TRADE", "reasons": ["insufficient time to expiry"], "trade": None}

    with (
        patch("skills.options_engine_skill.select_trade", return_value=first_result) as mock_select_trade,
        patch("skills.options_engine_skill._fetch_next_weekly_chain_fallback", return_value=None) as mock_fallback,
        patch("skills.options_engine_skill.write_advisory") as mock_write,
        patch("skills.options_engine_skill.notify_advisory") as mock_notify,
    ):
        _handle_confirmed(
            engine_state, chain, [], cycle_data, analysis, confirmation, 25230.0, atr=10.0, regime="BULLISH",
            now=datetime(2026, 7, 20, 10, 20, tzinfo=IST), config=config, today=date(2026, 7, 20),
        )

    assert mock_select_trade.call_count == 1
    mock_fallback.assert_called_once()
    mock_write.assert_called_once()
    mock_notify.assert_not_called()


# --------------------------------------------------------------------------
# run_cycle scenarios
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Market-hours guard
# --------------------------------------------------------------------------


def test_is_market_hours_true_within_window_on_weekday():
    assert is_market_hours(datetime(2026, 7, 20, 10, 5, tzinfo=IST))  # Monday


def test_is_market_hours_true_at_open_and_close_boundaries():
    assert is_market_hours(datetime(2026, 7, 20, 9, 15, tzinfo=IST))
    assert is_market_hours(datetime(2026, 7, 20, 15, 30, tzinfo=IST))


def test_is_market_hours_false_before_open():
    assert not is_market_hours(datetime(2026, 7, 20, 9, 14, tzinfo=IST))


def test_is_market_hours_false_after_close():
    assert not is_market_hours(datetime(2026, 7, 20, 15, 31, tzinfo=IST))


def test_is_market_hours_false_on_weekend():
    assert not is_market_hours(datetime(2026, 7, 18, 10, 5, tzinfo=IST))  # Saturday


def test_run_cycle_outside_market_hours_skips(real_db, tmp_path):
    config = _config(engine_lock_path=str(tmp_path / "engine.lock"))
    now = datetime(2026, 7, 20, 8, 0, tzinfo=IST)

    with patch("skills.options_engine_skill.fetch_cycle_data") as mock_fetch:
        result = run_cycle(config, now)

    assert result == {"status": "outside_market_hours"}
    mock_fetch.assert_not_called()


def test_run_cycle_locked_returns_immediately(real_db, tmp_path):
    config = _config(engine_lock_path=str(tmp_path / "engine.lock"), lock_staleness_seconds=300)
    now = datetime.now(IST)
    acquire_lock(config, now)  # simulate another run holding the lock

    result = run_cycle(config, now)

    assert result == {"status": "locked"}


def test_run_cycle_skips_on_normal_cadence(real_db, tmp_path):
    config = _config(engine_lock_path=str(tmp_path / "engine.lock"))
    now = datetime(2026, 7, 20, 10, 2, tzinfo=IST)
    save_engine_state({"mode": "NORMAL"}, datetime(2026, 7, 20, 10, 1, tzinfo=IST).isoformat())

    result = run_cycle(config, now)

    assert result == {"status": "skipped_cadence"}


def test_run_cycle_data_error_notifies_monitoring(real_db, tmp_path):
    config = _config(engine_lock_path=str(tmp_path / "engine.lock"))
    now = datetime(2026, 7, 20, 10, 5, tzinfo=IST)

    with (
        patch("skills.options_engine_skill.fetch_cycle_data", side_effect=DataInsufficientError("no data")),
        patch("skills.options_engine_skill.route_message") as mock_route,
    ):
        result = run_cycle(config, now)

    assert result["status"] == "data_error"
    mock_route.assert_called_once()
    assert mock_route.call_args[0][0] == "monitoring"


def test_run_cycle_escalates_when_near_resistance(real_db, tmp_path):
    config = _config(
        engine_lock_path=str(tmp_path / "engine.lock"), proximity_threshold_pct=1.0,
        strike_window_primary=5, strike_window_extended=10, boundary_min_share=0.20,
    )
    now = datetime(2026, 7, 20, 10, 5, tzinfo=IST)
    cycle_data = _cycle_data(spot=25190.0)

    with (
        patch("skills.options_engine_skill.fetch_cycle_data", return_value=cycle_data),
        patch("skills.options_engine_skill.fetch_nifty_spot_backup", return_value=25191.0),
        patch("skills.options_engine_skill.fetch_india_vix_with_change", return_value=None),
        patch("skills.options_engine_skill.notify_watch_escalation") as mock_notify,
    ):
        result = run_cycle(config, now)

    assert result["status"] == "ok"
    assert result["action"] == "ESCALATE"
    mock_notify.assert_called_once()

    state = get_engine_state()
    assert state["mode"] == "WATCH"
    assert state["watch_level"] == 25200.0


def test_run_cycle_confirmed_breakout_writes_and_notifies_advisory(real_db, tmp_path):
    config = _config(
        engine_lock_path=str(tmp_path / "engine.lock"), proximity_threshold_pct=1.0, deescalate_threshold_pct=5.0,
        watch_timeout_minutes=45, sustain_minutes=5, classification_min_move_pct=2.0, delta_band_min=0.30,
        delta_band_max=0.70, max_spread_pct=5.0, holding_fit_multiplier=1.0, min_rr=0.5, risk_pct=0.5,
        min_score=0, volume_confirm_multiplier=1.0,
    )
    now = datetime(2026, 7, 20, 10, 20, tzinfo=IST)
    save_engine_state(
        {"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": datetime(2026, 7, 20, 10, 0, tzinfo=IST).isoformat()},
        datetime(2026, 7, 20, 10, 15, tzinfo=IST).isoformat(),
    )

    chain = _sample_chain()
    for row in chain:
        if row["strike"] == 25200.0 and row["side"] == "CE":
            row["ltp"] = 75.0
            row["oi"] = 4_000_000

    _insert_snapshot("2025-07-20", "2025-07-20T09:55:00+05:30", "24JUL2025", 25200.0, "CE", 5_500_000)
    from db.database import execute

    execute(
        "UPDATE option_chain_snapshots SET ltp = 55 WHERE snapshot_ts = '2025-07-20T09:55:00+05:30' AND strike = 25200.0 AND side = 'CE'"
    )
    # Real-volume history for the watched contract (25200 CE): confirmation
    # now comes from the option's own traded volume, not NIFTY-index candle
    # volume (permanently 0, see check_volume_confirmation's docstring).
    # Two prior +500 deltas, then a +1000 surge as the "current" reading
    # (matching cycle_data's own snapshot_ts) -- confirms at multiplier=1.0.
    execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot, volume) VALUES (?,?,?,?,?,?,?,?)",
        ("2025-07-20T09:45:00+05:30", "2025-07-20", "24JUL2025", 25200.0, "CE", 5_000_000, 25000.0, 10_000),
    )
    execute(
        "UPDATE option_chain_snapshots SET volume = 10500 WHERE snapshot_ts = '2025-07-20T09:55:00+05:30' AND strike = 25200.0 AND side = 'CE'"
    )
    execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, oi, spot, volume) VALUES (?,?,?,?,?,?,?,?)",
        ("2025-07-20T10:00:00+05:30", "2025-07-20", "24JUL2025", 25200.0, "CE", 4_000_000, 25230.0, 11_500),
    )

    candles_1m = _wide_candles([25180, 25225, 25230, 25235, 25240, 25245])
    contracts = [{"expiry": "24JUL2025", "lotsize": "75"}]
    cycle_data = _cycle_data(spot=25230.0, chain=chain, contracts=contracts)
    cycle_data["candles_1m"] = candles_1m

    with (
        patch("skills.options_engine_skill.fetch_cycle_data", return_value=cycle_data),
        patch("skills.options_engine_skill.notify_advisory") as mock_notify,
    ):
        result = run_cycle(config, now)

    assert result["action"] in ("CONFIRMED",)

    import sqlite3

    conn = sqlite3.connect(real_db)
    row = conn.execute("SELECT action FROM options_advisories ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert row is not None
    assert row[0] in ("BUY_CE_CANDIDATE", "NO_TRADE")
    if row[0] == "BUY_CE_CANDIDATE":
        mock_notify.assert_called_once()


def test_run_cycle_false_breakout_notifies_and_deescalates(real_db, tmp_path):
    config = _config(
        engine_lock_path=str(tmp_path / "engine.lock"), proximity_threshold_pct=1.0, deescalate_threshold_pct=5.0,
        watch_timeout_minutes=45, sustain_minutes=5, classification_min_move_pct=2.0,
    )
    now = datetime(2026, 7, 20, 10, 20, tzinfo=IST)
    save_engine_state(
        {"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": datetime(2026, 7, 20, 10, 0, tzinfo=IST).isoformat()},
        datetime(2026, 7, 20, 10, 15, tzinfo=IST).isoformat(),
    )

    chain = _sample_chain()
    for row in chain:
        if row["strike"] == 25200.0 and row["side"] == "CE":
            row["ltp"] = 28.0
            row["oi"] = 9_500_000

    _insert_snapshot("2025-07-20", "2025-07-20T09:55:00+05:30", "24JUL2025", 25200.0, "CE", 8_000_000)
    from db.database import execute

    execute(
        "UPDATE option_chain_snapshots SET ltp = 35 WHERE snapshot_ts = '2025-07-20T09:55:00+05:30' AND strike = 25200.0 AND side = 'CE'"
    )

    candles_1m = _wide_candles([25180, 25210, 25220, 25180, 25150])
    cycle_data = _cycle_data(spot=25150.0, chain=chain)
    cycle_data["candles_1m"] = candles_1m

    with (
        patch("skills.options_engine_skill.fetch_cycle_data", return_value=cycle_data),
        patch("skills.options_engine_skill.notify_false_breakout") as mock_notify,
    ):
        result = run_cycle(config, now)

    assert result["action"] == "FALSE_BREAKOUT"
    mock_notify.assert_called_once()

    state = get_engine_state()
    assert state["mode"] == "NORMAL"


# --------------------------------------------------------------------------
# Session start / end heartbeat
# --------------------------------------------------------------------------


def test_format_session_start_message():
    text = format_session_start_message(date(2026, 7, 21))
    assert "SESSION START" in text
    assert "2026-07-21" in text
    assert "09:15-15:30 IST" in text


def test_notify_session_start_routes_discord_only():
    config = _config()
    with patch("skills.options_engine_skill.route_message") as mock_route:
        notify_session_start(config, today=date(2026, 7, 21))

    mock_route.assert_called_once()
    assert mock_route.call_args[0][0] == "option_trading"
    assert mock_route.call_args.kwargs["send_telegram"] is False


def test_format_session_end_message_counts_todays_activity(real_db):
    from db.database import execute

    today = date(2026, 7, 21)
    execute(
        "INSERT INTO options_advisories (created_ts, action, payload_json, score) VALUES (?,?,?,?)",
        (f"{today.isoformat()}T10:00:00+05:30", "BUY_CE_CANDIDATE", "{}", 80),
    )
    execute(
        "INSERT INTO options_advisories (created_ts, action, payload_json, score) VALUES (?,?,?,?)",
        (f"{today.isoformat()}T11:00:00+05:30", "NO_TRADE", "{}", 40),
    )
    execute(
        """INSERT INTO options_positions
           (status, contract, expiry_date, strike, side, qty_lots, lot_size, entry_premium, thesis_json, opened_ts)
           VALUES ('active', 'NIFTY 25200 CE 24 July', '24JUL2025', 25200.0, 'CE', 1, 75, 100.0, '{}', datetime('now'))"""
    )

    result = format_session_end_message(today)

    assert "SESSION END" in result["text"]
    assert result["advisory_counts"]["BUY_CE_CANDIDATE"] == 1
    assert result["advisory_counts"]["NO_TRADE"] == 1
    assert "Positions opened today: 1" in result["text"]
    assert "Still active: 1" in result["text"]


def test_notify_session_end_routes_discord_only(real_db):
    config = _config()
    with patch("skills.options_engine_skill.route_message") as mock_route:
        notify_session_end(config, today=date(2026, 7, 21))

    mock_route.assert_called_once()
    assert mock_route.call_args[0][0] == "option_trading"
    assert mock_route.call_args.kwargs["send_telegram"] is False


# --------------------------------------------------------------------------
# End of day
# --------------------------------------------------------------------------


def test_run_end_of_day_resets_state_and_cleans_snapshots(real_db, tmp_path):
    config = _config(snapshot_retention_days=1)
    save_engine_state({"mode": "WATCH", "watch_level": 25200.0, "watch_direction": "UP", "watch_started_ts": "x"}, "x")
    _insert_snapshot("2020-01-01", "2020-01-01T09:15:00", "24JUL2025", 25200.0, "CE", 100)

    result = run_end_of_day(config, today=date(2026, 7, 20))

    assert result["status"] == "ok"
    assert result["snapshots_deleted"] == 1
    state = get_engine_state()
    assert state["mode"] == "NORMAL"
    assert state["watch_level"] is None


# --------------------------------------------------------------------------
# Daily Greeks sanity-check skip (rate-limit fix)
# --------------------------------------------------------------------------


def test_get_greeks_sanity_checked_date_default_none(real_db):
    assert get_greeks_sanity_checked_date() is None


def test_mark_and_get_greeks_sanity_checked_date_roundtrip(real_db):
    mark_greeks_sanity_checked(date(2026, 7, 20))
    assert get_greeks_sanity_checked_date() == "2026-07-20"


def test_run_end_of_day_resets_greeks_sanity_checked_date(real_db, tmp_path):
    config = _config(snapshot_retention_days=1)
    mark_greeks_sanity_checked(date(2026, 7, 20))

    run_end_of_day(config, today=date(2026, 7, 20))

    assert get_greeks_sanity_checked_date() is None


def test_fetch_cycle_data_skips_sanity_check_when_already_done_today(real_db):
    config = _config()
    mark_greeks_sanity_checked(date(2026, 7, 20))
    contracts = [{"expiry": "24JUL2025", "lotsize": "75"}]

    with (
        patch("skills.options_engine_skill.ensure_instruments_master", return_value=MagicMock()),
        patch("skills.options_engine_skill.filter_nifty_option_contracts", return_value=contracts),
        patch("skills.options_engine_skill.select_weekly_and_monthly_expiry", return_value=("24JUL2025", "28AUG2025")),
        patch("skills.options_engine_skill.run_market_data_cycle") as mock_cycle,
        patch("skills.options_engine_skill.fetch_nifty_candles", return_value=_wide_candles([25000])),
    ):
        mock_cycle.return_value = {
            "spot": 25000.0, "expiry": "24JUL2025", "chain": [], "trading_date": "2026-07-20",
            "snapshot_ts": "2026-07-20T10:00:00+05:30", "greeks_sanity_checked": False,
        }
        fetch_cycle_data(config, date(2026, 7, 20))

    assert mock_cycle.call_args.kwargs["skip_greeks_sanity_check"] is True


def test_fetch_cycle_data_marks_checked_when_cycle_ran_it(real_db):
    config = _config()
    contracts = [{"expiry": "24JUL2025", "lotsize": "75"}]

    with (
        patch("skills.options_engine_skill.ensure_instruments_master", return_value=MagicMock()),
        patch("skills.options_engine_skill.filter_nifty_option_contracts", return_value=contracts),
        patch("skills.options_engine_skill.select_weekly_and_monthly_expiry", return_value=("24JUL2025", "28AUG2025")),
        patch("skills.options_engine_skill.run_market_data_cycle") as mock_cycle,
        patch("skills.options_engine_skill.fetch_nifty_candles", return_value=_wide_candles([25000])),
    ):
        mock_cycle.return_value = {
            "spot": 25000.0, "expiry": "24JUL2025", "chain": [], "trading_date": "2026-07-20",
            "snapshot_ts": "2026-07-20T10:00:00+05:30", "greeks_sanity_checked": True,
        }
        fetch_cycle_data(config, date(2026, 7, 20))

    assert mock_cycle.call_args.kwargs["skip_greeks_sanity_check"] is False
    assert get_greeks_sanity_checked_date() == "2026-07-20"
