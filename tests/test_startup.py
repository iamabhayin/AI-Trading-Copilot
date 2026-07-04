"""Tests for config/startup.py.

Config loading and DB init are mocked for the failure-path tests; the
logging test writes to a temp directory instead of the real logs/ dir.
"""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

from config.settings import ConfigError
from config.startup import StartupError, StartupService


@patch("config.startup.init_db")
@patch("config.startup.Config")
def test_start_runs_full_sequence_and_returns_config(mock_config_cls, mock_init_db, tmp_path):
    mock_config = MagicMock(log_level="INFO")
    mock_config_cls.load.return_value = mock_config

    with patch("config.startup.LOGS_DIR", tmp_path):
        result = StartupService().start()

    assert result is mock_config
    mock_init_db.assert_called_once()
    assert (tmp_path / "app.log").exists()


@patch("config.startup.Config")
def test_start_raises_startup_error_on_config_failure(mock_config_cls):
    mock_config_cls.load.side_effect = ConfigError("Invalid configuration")

    with pytest.raises(StartupError) as exc_info:
        StartupService().start()

    assert isinstance(exc_info.value.__cause__, ConfigError)


@patch("config.startup.Config")
def test_start_raises_startup_error_on_logging_failure(mock_config_cls, tmp_path):
    mock_config_cls.load.return_value = MagicMock(log_level="INFO")
    broken_logs_dir = MagicMock()
    broken_logs_dir.mkdir.side_effect = OSError("disk full")

    with patch("config.startup.LOGS_DIR", broken_logs_dir), pytest.raises(StartupError) as exc_info:
        StartupService().start()

    assert isinstance(exc_info.value.__cause__, OSError)


@patch("config.startup.init_db")
@patch("config.startup.Config")
def test_start_raises_startup_error_on_db_failure(mock_config_cls, mock_init_db, tmp_path):
    mock_config_cls.load.return_value = MagicMock(log_level="INFO")
    mock_init_db.side_effect = sqlite3.OperationalError("database is locked")

    with patch("config.startup.LOGS_DIR", tmp_path), pytest.raises(StartupError) as exc_info:
        StartupService().start()

    assert isinstance(exc_info.value.__cause__, sqlite3.OperationalError)


@patch("config.startup.init_db")
@patch("config.startup.Config")
def test_start_creates_logs_directory_if_missing(mock_config_cls, mock_init_db, tmp_path):
    mock_config_cls.load.return_value = MagicMock(log_level="INFO")
    nested_logs_dir = tmp_path / "nested" / "logs"

    with patch("config.startup.LOGS_DIR", nested_logs_dir):
        StartupService().start()

    assert nested_logs_dir.is_dir()
