"""Tests for skills/indicator_engine.py."""

import numpy as np
import pandas as pd
import pytest

from skills.indicator_engine import compute_indicators, summarize_latest

EXPECTED_COLUMNS = [
    "rsi_14",
    "MACD_12_26_9",
    "ema_20",
    "ema_50",
    "ema_crossover_bullish",
    "BBU_20_2.0_2.0",
    "BBL_20_2.0_2.0",
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


def test_compute_indicators_adds_expected_columns(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1d")
    for column in EXPECTED_COLUMNS:
        assert column in out.columns


def test_compute_indicators_is_case_insensitive(sample_ohlcv):
    upper = sample_ohlcv.rename(columns=str.upper)
    out = compute_indicators(upper)
    assert "close" in out.columns


def test_rsi_is_bounded(sample_ohlcv):
    out = compute_indicators(sample_ohlcv)
    rsi = out["rsi_14"].dropna()
    assert not rsi.empty
    assert (rsi >= 0).all() and (rsi <= 100).all()


def test_volume_anomaly_is_boolean(sample_ohlcv):
    out = compute_indicators(sample_ohlcv)
    assert out["volume_anomaly"].dtype == bool


def test_support_never_exceeds_resistance(sample_ohlcv):
    out = compute_indicators(sample_ohlcv).dropna(subset=["support", "resistance"])
    assert (out["support"] <= out["resistance"]).all()


def test_missing_columns_raises():
    with pytest.raises(ValueError):
        compute_indicators(pd.DataFrame({"close": [1, 2, 3]}))


def test_timeframe_is_tagged_on_output(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1wk")
    assert out.attrs["timeframe"] == "1wk"


def test_summarize_latest_returns_flat_dict(sample_ohlcv):
    out = compute_indicators(sample_ohlcv, timeframe="1d")
    summary = summarize_latest(out)
    assert summary["timeframe"] == "1d"
    assert isinstance(summary["close"], float)
    assert isinstance(summary["ema_crossover_bullish"], bool)
    assert isinstance(summary["volume_anomaly"], bool)
