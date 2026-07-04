"""Tests for skills/monitor_skill.py.

DB and Anthropic calls are mocked so these run offline without a real
database file or API key.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from skills.monitor_skill import (
    already_alerted,
    check_price_trigger,
    evaluate_risk_judgment,
    monitor_all_positions,
    monitor_position,
    record_alert,
)

POSITION = {
    "id": 1,
    "ticker": "RELIANCE",
    "entry_price": 2950.0,
    "stop_loss": 2800.0,
    "target": 3200.0,
}


def _mock_response(payload: dict, stop_reason: str = "end_turn"):
    text_block = MagicMock(type="text", text=json.dumps(payload))
    return MagicMock(content=[text_block], stop_reason=stop_reason, stop_details=None)


def test_check_price_trigger_fires_stop_loss():
    assert check_price_trigger(POSITION, 2799.0) == "stop_loss"


def test_check_price_trigger_fires_target():
    assert check_price_trigger(POSITION, 3201.0) == "target"


def test_check_price_trigger_returns_none_within_range():
    assert check_price_trigger(POSITION, 3000.0) is None


def test_check_price_trigger_handles_missing_levels():
    position = {"stop_loss": None, "target": None}
    assert check_price_trigger(position, 100.0) is None


@patch("skills.monitor_skill.anthropic.Anthropic")
@patch("skills.monitor_skill.Settings.load")
def test_evaluate_risk_judgment_returns_parsed_dict(mock_settings_load, mock_anthropic_cls):
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        {"should_alert": True, "reason": "Major negative earnings surprise reported overnight."}
    )
    mock_anthropic_cls.return_value = mock_client

    result = evaluate_risk_judgment(POSITION, 2950.0, [])

    assert result == {"should_alert": True, "reason": "Major negative earnings surprise reported overnight."}


@patch("skills.monitor_skill.anthropic.Anthropic")
@patch("skills.monitor_skill.Settings.load")
def test_evaluate_risk_judgment_raises_on_refusal(mock_settings_load, mock_anthropic_cls):
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = MagicMock(content=[], stop_reason="refusal", stop_details=MagicMock())
    mock_anthropic_cls.return_value = mock_client

    with pytest.raises(RuntimeError):
        evaluate_risk_judgment(POSITION, 2950.0, [])


@patch("skills.monitor_skill.fetch_one")
def test_already_alerted_true_when_row_exists(mock_fetch_one):
    mock_fetch_one.return_value = {"id": 5}
    assert already_alerted(1, "stop_loss") is True


@patch("skills.monitor_skill.fetch_one")
def test_already_alerted_false_when_no_row(mock_fetch_one):
    mock_fetch_one.return_value = None
    assert already_alerted(1, "stop_loss") is False


@patch("skills.monitor_skill.execute")
def test_record_alert_inserts_expected_params(mock_execute):
    mock_execute.return_value = 9
    result = record_alert(1, "stop_loss", "RELIANCE hit stop-loss")

    assert result == 9
    args, _ = mock_execute.call_args
    _, params = args
    assert params == (1, "stop_loss", "RELIANCE hit stop-loss")


@patch("skills.monitor_skill.evaluate_risk_judgment")
@patch("skills.monitor_skill.already_alerted", return_value=False)
@patch("skills.monitor_skill.record_alert")
def test_monitor_position_fires_price_alert_and_risk_alert(mock_record_alert, mock_already_alerted, mock_evaluate):
    mock_evaluate.return_value = {"should_alert": True, "reason": "Negative news"}

    alerts = monitor_position(POSITION, 2799.0, [])

    alert_types = {a["alert_type"] for a in alerts}
    assert alert_types == {"stop_loss", "risk_judgment"}
    assert mock_record_alert.call_count == 2


@patch("skills.monitor_skill.evaluate_risk_judgment")
@patch("skills.monitor_skill.already_alerted", return_value=True)
@patch("skills.monitor_skill.record_alert")
def test_monitor_position_dedupes_already_alerted(mock_record_alert, mock_already_alerted, mock_evaluate):
    mock_evaluate.return_value = {"should_alert": True, "reason": "Negative news"}

    alerts = monitor_position(POSITION, 2799.0, [])

    assert alerts == []
    mock_record_alert.assert_not_called()


@patch("skills.monitor_skill.evaluate_risk_judgment", side_effect=RuntimeError("no API key"))
@patch("skills.monitor_skill.already_alerted", return_value=False)
@patch("skills.monitor_skill.record_alert")
def test_monitor_position_price_trigger_survives_claude_failure(mock_record_alert, mock_already_alerted, mock_evaluate):
    alerts = monitor_position(POSITION, 2799.0, [])

    assert len(alerts) == 1
    assert alerts[0]["alert_type"] == "stop_loss"


@patch("skills.monitor_skill.evaluate_risk_judgment")
@patch("skills.monitor_skill.already_alerted", return_value=False)
@patch("skills.monitor_skill.record_alert")
def test_monitor_position_no_alerts_when_nothing_triggers(mock_record_alert, mock_already_alerted, mock_evaluate):
    mock_evaluate.return_value = {"should_alert": False, "reason": "Nothing notable"}

    alerts = monitor_position(POSITION, 3000.0, [])

    assert alerts == []


@patch("skills.monitor_skill.monitor_position")
@patch("skills.monitor_skill.fetch_all")
def test_monitor_all_positions_iterates_active_positions(mock_fetch_all, mock_monitor_position):
    mock_fetch_all.return_value = [dict(POSITION)]
    mock_monitor_position.return_value = [{"position_id": 1, "ticker": "RELIANCE", "alert_type": "stop_loss", "message": "x"}]

    with (
        patch("skills.data_fetch_skill.fetch_ohlcv") as mock_fetch_ohlcv,
        patch("skills.data_fetch_skill.fetch_news", return_value=[]),
        patch("skills.data_fetch_skill.filter_news_relevance", return_value=[]),
    ):
        import pandas as pd

        mock_fetch_ohlcv.return_value = pd.DataFrame({"close": [2950.0, 2900.0]})

        alerts = monitor_all_positions()

    assert len(alerts) == 1
    mock_monitor_position.assert_called_once()
    fetch_all_query = mock_fetch_all.call_args[0][0]
    assert "status = 'active'" in fetch_all_query
