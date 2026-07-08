"""Tests for skills/indicator_engine.py."""

from unittest.mock import patch

import numpy as np
import pandas as pd
import pandas_ta as ta
import pytest

from skills.indicator_engine import _period_for_days, compute_indicators, summarize_latest, summarize_recent

# Captured before any test patches skills.indicator_engine.ta (the same
# pandas_ta module object) so side_effect can delegate to the real
# implementation instead of recursing into the mock.
_REAL_EMA = ta.ema
_REAL_RSI = ta.rsi
_REAL_BBANDS = ta.bbands

EXPECTED_COLUMNS = [
    "rsi_14",
    "ema_14d",
    "ema_50d",
    "BBU_20_2.0_2.0",
    "BBL_20_2.0_2.0",
]

REMOVED_COLUMNS = [
    "MACD_12_26_9",
    "ema_20",
    "ema_50",
    "ema_crossover_bullish",
    "adx_14",
    "volume_anomaly",
    "support",
    "resistance",
]


@pytest.fixture
def sample_ohlcv() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 120
    close = 100 + np.cumsum(rng.normal(0, 1, n))
    high = close + rng.uniform(0, 2, n)
    low = close - rng.uniform(0, 2, n)
    open_ = close + rng.normal(0, 0.5, n)
    volume = rng.integers(1000, 5000, n)
    index = pd.date_range("2025-01-01", periods=n, freq="D")
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


@pytest.mark.parametrize(
    "days,timeframe,expected",
    [
        (14, "1h", 88),  # 14 * 6.25 = 87.5 -> 88
        (50, "1h", 312),  # 50 * 6.25 = 312.5 -> 312
        (14, "1d", 14),
        (50, "1d", 50),
    ],
)
def test_period_for_days_converts_calendar_days_to_candles(days, timeframe, expected):
    assert _period_for_days(days, timeframe) == expected


def test_compute_indicators_defaults_to_1h_timeframe(sample_ohlcv):
    out = compute_indicators(sample_ohlcv)
    assert out.attrs["timeframe"] == "1h"


def test_compute_indicators_adds_expected_columns(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1d")
    for column in EXPECTED_COLUMNS:
        assert column in out.columns


def test_compute_indicators_excludes_removed_indicators(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1d")
    for column in REMOVED_COLUMNS:
        assert column not in out.columns


def test_compute_indicators_is_case_insensitive(sample_ohlcv):
    upper = sample_ohlcv.rename(columns=str.upper)
    out = compute_indicators(upper)
    assert "close" in out.columns


def test_rsi_is_bounded(sample_ohlcv):
    out = compute_indicators(sample_ohlcv)
    rsi = out["rsi_14"].dropna()
    assert not rsi.empty
    assert (rsi >= 0).all() and (rsi <= 100).all()


def test_missing_columns_raises():
    with pytest.raises(ValueError):
        compute_indicators(pd.DataFrame({"close": [1, 2, 3]}))


def test_timeframe_is_tagged_on_output(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1wk")
    assert out.attrs["timeframe"] == "1wk"


@patch("skills.indicator_engine.ta.ema")
def test_ema_periods_convert_to_hourly_candles_on_1h_timeframe(mock_ema, sample_ohlcv):
    mock_ema.side_effect = _REAL_EMA

    compute_indicators(sample_ohlcv, timeframe="1h")

    lengths = [call.kwargs["length"] for call in mock_ema.call_args_list]
    assert lengths == [88, 312]


@patch("skills.indicator_engine.ta.ema")
def test_ema_periods_use_raw_day_count_on_1d_timeframe(mock_ema, sample_ohlcv):
    mock_ema.side_effect = _REAL_EMA

    compute_indicators(sample_ohlcv, timeframe="1d")

    lengths = [call.kwargs["length"] for call in mock_ema.call_args_list]
    assert lengths == [14, 50]


@patch("skills.indicator_engine.ta.rsi")
def test_rsi_period_is_not_converted_for_timeframe(mock_rsi, sample_ohlcv):
    mock_rsi.side_effect = _REAL_RSI

    compute_indicators(sample_ohlcv, timeframe="1h")

    assert mock_rsi.call_args.kwargs["length"] == 14


@patch("skills.indicator_engine.ta.bbands")
def test_bollinger_period_is_not_converted_for_timeframe(mock_bbands, sample_ohlcv):
    mock_bbands.side_effect = _REAL_BBANDS

    compute_indicators(sample_ohlcv, timeframe="1h")

    assert mock_bbands.call_args.kwargs["length"] == 20


def test_summarize_latest_returns_flat_dict(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1d")
    summary = summarize_latest(out)

    assert summary["timeframe"] == "1d"
    assert isinstance(summary["close"], float)
    assert set(summary) == {"timeframe", "close", "rsi_14", "ema_14d", "ema_50d", "bb_upper", "bb_lower"}


def test_summarize_recent_returns_n_rows_oldest_to_newest(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1d")
    recent = summarize_recent(out, n=10)

    assert len(recent) == 10
    assert set(recent[0]) == {"timeframe", "close", "ema_14d", "ema_50d", "bb_upper", "bb_lower"}
    assert [row["close"] for row in recent] == out["close"].iloc[-10:].tolist()
    assert recent[-1]["timeframe"] == "1d"


def test_summarize_recent_handles_nan_via_safe_float(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1d")

    # The first rows haven't warmed up yet (EMA/Bollinger need history), so
    # they carry NaNs that _safe_float must convert to None.
    recent = summarize_recent(out.iloc[:5], n=10)

    assert len(recent) == 5
    assert recent[0]["ema_50d"] is None
    assert recent[0]["bb_upper"] is None
