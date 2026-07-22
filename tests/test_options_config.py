"""Tests for config/options_config.py — Phase 15 Option Trading settings."""

import pytest

from config.options_config import OptionsConfig, OptionsConfigError


def test_load_with_no_env_vars_uses_safe_defaults(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("ANGEL_") or name.startswith("OPTIONS_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("config.options_config.load_dotenv", lambda: None)

    config = OptionsConfig.load()

    assert config.angel_api_key == ""
    assert config.min_score == 70
    assert config.min_rr == 2.0
    assert config.strike_window_primary == 5
    assert config.strike_window_extended == 10
    assert config.greeks_crosscheck_enabled is True
    assert config.expiries_to_fetch == []
    assert config.zone_merge_threshold_pct == 0.15


def test_load_reads_overrides_from_env(monkeypatch):
    monkeypatch.setattr("config.options_config.load_dotenv", lambda: None)
    monkeypatch.setenv("OPTIONS_MIN_SCORE", "80")
    monkeypatch.setenv("OPTIONS_MIN_RR", "1.5")
    monkeypatch.setenv("OPTIONS_GREEKS_CROSSCHECK_ENABLED", "false")
    monkeypatch.setenv("OPTIONS_EXPIRIES_TO_FETCH", "2026-07-24, 2026-07-31")
    monkeypatch.setenv("ANGEL_API_KEY", "test-key")

    config = OptionsConfig.load()

    assert config.min_score == 80
    assert config.min_rr == 1.5
    assert config.greeks_crosscheck_enabled is False
    assert config.expiries_to_fetch == ["2026-07-24", "2026-07-31"]
    assert config.angel_api_key == "test-key"


def test_load_raises_options_config_error_on_invalid_numeric(monkeypatch):
    monkeypatch.setattr("config.options_config.load_dotenv", lambda: None)
    monkeypatch.setenv("OPTIONS_MIN_SCORE", "not-a-number")

    with pytest.raises(OptionsConfigError):
        OptionsConfig.load()
