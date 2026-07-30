"""Tests for skills/options_data_fetch.py — yfinance backup spot/candles/VIX,
the shared DataInsufficientError exception, and snapshot-retention cleanup.

No live network calls: yfinance (via fetch_ohlcv) is always mocked.
cleanup_old_snapshots runs against a real tmp-path database, mirroring
test_cleanup_skill.py's pattern.
"""

from datetime import date
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from config.options_config import OptionsConfig
from skills.options_data_fetch import (
    check_spot_divergence,
    cleanup_old_snapshots,
    fetch_india_vix,
    fetch_india_vix_with_change,
    fetch_nifty_spot_backup,
    filter_session_candles,
)


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


# --------------------------------------------------------------------------
# yfinance helpers
# --------------------------------------------------------------------------


def test_fetch_nifty_spot_backup_returns_latest_close():
    df = pd.DataFrame({"close": [25000.0, 25010.5]})

    with patch("skills.options_data_fetch.fetch_nifty_candles", return_value=df):
        assert fetch_nifty_spot_backup() == 25010.5


def test_fetch_nifty_spot_backup_handles_fetch_failure():
    with patch("skills.options_data_fetch.fetch_nifty_candles", side_effect=ValueError("no data")):
        assert fetch_nifty_spot_backup() is None


def test_fetch_india_vix_returns_latest_close():
    df = pd.DataFrame({"close": [13.2, 13.5]})

    with patch("skills.options_data_fetch.fetch_ohlcv", return_value=df):
        assert fetch_india_vix() == 13.5


def test_fetch_india_vix_handles_fetch_failure():
    with patch("skills.options_data_fetch.fetch_ohlcv", side_effect=ValueError("no data")):
        assert fetch_india_vix() is None


def test_fetch_india_vix_with_change_computes_day_change():
    df = pd.DataFrame({"close": [13.2, 13.5, 13.8]})

    with patch("skills.options_data_fetch.fetch_ohlcv", return_value=df):
        result = fetch_india_vix_with_change()

    assert result == {"latest": 13.8, "change": pytest.approx(0.3)}


def test_fetch_india_vix_with_change_insufficient_history_returns_none():
    df = pd.DataFrame({"close": [13.8]})

    with patch("skills.options_data_fetch.fetch_ohlcv", return_value=df):
        assert fetch_india_vix_with_change() is None


def test_fetch_india_vix_with_change_handles_fetch_failure():
    with patch("skills.options_data_fetch.fetch_ohlcv", side_effect=ValueError("no data")):
        assert fetch_india_vix_with_change() is None


def test_check_spot_divergence_within_threshold():
    config = _config(spot_divergence_threshold_pct=0.5)

    result = check_spot_divergence(25000.0, 25010.0, config)

    assert result["checked"] is True
    assert result["diverged"] is False


def test_check_spot_divergence_beyond_threshold():
    config = _config(spot_divergence_threshold_pct=0.1)

    result = check_spot_divergence(25000.0, 25200.0, config)

    assert result["checked"] is True
    assert result["diverged"] is True


def test_check_spot_divergence_missing_data_not_checked():
    config = _config()

    assert check_spot_divergence(None, 25000.0, config) == {"checked": False, "diverged": False}
    assert check_spot_divergence(25000.0, None, config) == {"checked": False, "diverged": False}


# --------------------------------------------------------------------------
# Snapshot retention (real tmp-path DB)
# --------------------------------------------------------------------------


def test_cleanup_old_snapshots_deletes_only_stale_rows(real_db):
    import sqlite3

    conn = sqlite3.connect(real_db)
    conn.execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, spot) "
        "VALUES ('2026-07-01T09:15:00', '2026-07-01', '2026-07-03', 25000, 'CE', 25000)"
    )
    conn.execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, spot) "
        "VALUES ('2026-07-20T09:15:00', '2026-07-20', '2026-07-24', 25000, 'CE', 25000)"
    )
    conn.commit()
    conn.close()

    config = _config(snapshot_retention_days=10)
    deleted = cleanup_old_snapshots(config, today=date(2026, 7, 20))

    assert deleted == 1
    conn = sqlite3.connect(real_db)
    remaining = conn.execute("SELECT trading_date FROM option_chain_snapshots").fetchall()
    conn.close()
    assert remaining == [("2026-07-20",)]


def test_cleanup_old_snapshots_keeps_everything_within_retention_window(real_db):
    import sqlite3

    conn = sqlite3.connect(real_db)
    conn.execute(
        "INSERT INTO option_chain_snapshots (snapshot_ts, trading_date, expiry_date, strike, side, spot) "
        "VALUES ('2026-07-15T09:15:00', '2026-07-15', '2026-07-17', 25000, 'CE', 25000)"
    )
    conn.commit()
    conn.close()

    config = _config(snapshot_retention_days=10)
    deleted = cleanup_old_snapshots(config, today=date(2026, 7, 20))

    assert deleted == 0


# --------------------------------------------------------------------------
# filter_session_candles -- day-wise S/R/ATR/trend inputs (2026-07-30)
# --------------------------------------------------------------------------


def _multi_day_candles() -> pd.DataFrame:
    """Straddles two IST calendar days: 3 candles late on 2026-07-29, 4
    candles on 2026-07-30 -- mirrors what fetch_nifty_candles(period='5d')
    actually returns near the start of a session."""
    index = pd.DatetimeIndex(
        [
            "2026-07-29 14:50:00", "2026-07-29 14:55:00", "2026-07-29 15:00:00",
            "2026-07-30 09:15:00", "2026-07-30 09:20:00", "2026-07-30 09:25:00", "2026-07-30 09:30:00",
        ],
        tz="Asia/Kolkata",
    )
    closes = [24100.0, 24110.0, 24120.0, 24200.0, 24210.0, 24220.0, 24230.0]
    return pd.DataFrame(
        {"open": closes, "high": [c + 5 for c in closes], "low": [c - 5 for c in closes], "close": closes, "volume": [1000] * 7},
        index=index,
    )


def test_filter_session_candles_drops_prior_day_rows():
    candles = _multi_day_candles()
    result = filter_session_candles(candles, date(2026, 7, 30))
    assert len(result) == 4
    assert list(result["close"]) == [24200.0, 24210.0, 24220.0, 24230.0]


def test_filter_session_candles_handles_tz_naive_index():
    candles = _multi_day_candles()
    candles.index = candles.index.tz_localize(None)
    result = filter_session_candles(candles, date(2026, 7, 30))
    assert len(result) == 4


def test_filter_session_candles_noop_on_synthetic_index():
    """RangeIndex fixtures (as used throughout test_options_engine_skill.py)
    must pass through unchanged -- filter_session_candles only acts on a
    real DatetimeIndex."""
    candles = pd.DataFrame({"open": [1], "high": [2], "low": [0], "close": [1], "volume": [100]})
    result = filter_session_candles(candles, date(2026, 7, 30))
    assert len(result) == 1


def test_filter_session_candles_empty_input_returns_empty():
    empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    assert filter_session_candles(empty, date(2026, 7, 30)).empty
