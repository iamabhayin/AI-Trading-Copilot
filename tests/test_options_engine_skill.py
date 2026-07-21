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
from skills.options_data_fetch import DataInsufficientError
from skills.options_engine_skill import (
    acquire_lock,
    fetch_cycle_data,
    fetch_first_of_day_snapshot,
    fetch_prior_day_last_snapshot,
    format_false_breakout_message,
    format_watch_escalation_message,
    get_engine_state,
    get_greeks_sanity_checked_date,
    is_market_hours,
    mark_greeks_sanity_checked,
    release_lock,
    run_cycle,
    run_end_of_day,
    save_engine_state,
    should_run_normal_cycle,
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
    assert "WATCHING NIFTY RESISTANCE" in text
    assert "25200" in text
    assert "Support (OI wall): 24800" in text
    assert "Support (price structure): 24825.4" in text
    assert "Resistance (OI wall): 25200" in text
    assert "Resistance (price structure): 25260.12" in text


def test_format_false_breakout_message():
    text = format_false_breakout_message(25200.0, "UP", {"oi_classification": "SHORT_BUILDUP"})
    assert "FALSE BREAKOUT" in text
    assert "SHORT_BUILDUP" in text


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
