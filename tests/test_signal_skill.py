"""Tests for skills/signal_skill.py.

The Anthropic client is mocked so these run offline without a real API key.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

from skills.signal_skill import (
    SUGGESTION_SCHEMA,
    SUGGESTION_SCHEMA_WITH_REGIME,
    SYSTEM_PROMPT,
    _build_user_prompt,
    generate_suggestion,
    main,
    save_suggestion,
    shortlist,
)


def _mock_response(payload: dict, stop_reason: str = "end_turn"):
    text_block = MagicMock(type="text", text=json.dumps(payload))
    return MagicMock(content=[text_block], stop_reason=stop_reason, stop_details=None)


def test_system_prompt_names_expected_chart_patterns():
    prompt_lower = SYSTEM_PROMPT.lower()
    assert "pullback" in prompt_lower
    assert "golden/death cross" in prompt_lower
    assert "squeeze" in prompt_lower
    assert "recent history" in prompt_lower or "history window" in prompt_lower
    assert "volume_ratio_20" in prompt_lower
    assert "overnight global cues" in prompt_lower


def test_build_user_prompt_includes_recent_history():
    recent = [{"close": 100.0, "ema_14d": 99.0}, {"close": 101.0, "ema_14d": 99.5}]

    prompt = _build_user_prompt("AAPL", {"rsi_14": 50.0}, [], recent, {})

    assert json.dumps(recent) in prompt
    assert "Recent history" in prompt


def test_build_user_prompt_includes_global_cues():
    global_cues = {"vix": {"level": 16.75}, "sp500": {"close": 7515.34, "change_pct": -0.79}}

    prompt = _build_user_prompt("AAPL", {"rsi_14": 50.0}, [], [], global_cues)

    assert "Overnight Global Cues" in prompt
    assert json.dumps(global_cues, sort_keys=True) in prompt


@patch("skills.signal_skill.anthropic.Anthropic")
@patch("skills.signal_skill.Settings.load")
def test_generate_suggestion_returns_structured_dict(mock_settings_load, mock_anthropic_cls):
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        {
            "action": "BUY",
            "entry": 100.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "confidence": 0.8,
            "rationale": "RSI oversold with bullish EMA crossover.",
        }
    )
    mock_anthropic_cls.return_value = mock_client

    suggestion = generate_suggestion("AAPL", {"rsi_14": 28.0}, [], [], {})

    assert suggestion["ticker"] == "AAPL"
    assert suggestion["action"] == "BUY"
    assert suggestion["entry"] == 100.0
    mock_client.messages.create.assert_called_once()
    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["output_config"]["format"]["type"] == "json_schema"


@patch("skills.signal_skill.anthropic.Anthropic")
@patch("skills.signal_skill.Settings.load")
def test_generate_suggestion_default_call_is_unchanged_by_task_4(mock_settings_load, mock_anthropic_cls):
    """include_risk_regime defaults to False -- system prompt and schema
    must be byte-identical to the pre-Task-4 call, confirming zero
    behavior change when the feature isn't explicitly requested.
    """
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        {"action": "HOLD", "entry": None, "stop_loss": None, "target": None, "confidence": 0.5, "rationale": "x"}
    )
    mock_anthropic_cls.return_value = mock_client

    generate_suggestion("AAPL", {"rsi_14": 50.0}, [], [], {})

    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["system"] == SYSTEM_PROMPT
    assert kwargs["output_config"]["format"]["schema"] == SUGGESTION_SCHEMA


@patch("skills.signal_skill.anthropic.Anthropic")
@patch("skills.signal_skill.Settings.load")
def test_generate_suggestion_with_risk_regime_uses_extended_schema_and_prompt(mock_settings_load, mock_anthropic_cls):
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_client.messages.create.return_value = _mock_response(
        {
            "action": "BUY",
            "entry": 100.0,
            "stop_loss": 95.0,
            "target": 110.0,
            "confidence": 0.8,
            "rationale": "x",
            "risk_regime": "risk_on",
        }
    )
    mock_anthropic_cls.return_value = mock_client

    suggestion = generate_suggestion("AAPL", {"rsi_14": 28.0}, [], [], {}, include_risk_regime=True)

    _, kwargs = mock_client.messages.create.call_args
    assert kwargs["system"] != SYSTEM_PROMPT
    assert kwargs["system"].startswith(SYSTEM_PROMPT)
    assert kwargs["output_config"]["format"]["schema"] == SUGGESTION_SCHEMA_WITH_REGIME
    assert suggestion["risk_regime"] == "risk_on"


@patch("skills.signal_skill.anthropic.Anthropic")
@patch("skills.signal_skill.Settings.load")
def test_generate_suggestion_raises_on_refusal(mock_settings_load, mock_anthropic_cls):
    mock_settings_load.return_value = MagicMock(anthropic_api_key="fake-key")
    mock_client = MagicMock()
    mock_response = MagicMock(content=[], stop_reason="refusal", stop_details=MagicMock())
    mock_client.messages.create.return_value = mock_response
    mock_anthropic_cls.return_value = mock_client

    with pytest.raises(RuntimeError):
        generate_suggestion("AAPL", {}, [], [], {})


@patch("skills.signal_skill.execute")
def test_save_suggestion_passes_expected_params(mock_execute):
    mock_execute.return_value = 42
    suggestion = {
        "ticker": "AAPL",
        "action": "BUY",
        "entry": 100.0,
        "stop_loss": 95.0,
        "target": 110.0,
        "confidence": 0.8,
        "rationale": "test",
    }

    row_id = save_suggestion(suggestion, timeframe="1d")

    assert row_id == 42
    args, _ = mock_execute.call_args
    query, params = args
    assert "INSERT INTO suggestions" in query
    assert params == ("AAPL", "BUY", 100.0, 95.0, 110.0, 0.8, "test", "1d")


def test_shortlist_excludes_hold_and_sorts_by_confidence():
    suggestions = [
        {"ticker": "A", "action": "HOLD", "confidence": 0.9},
        {"ticker": "B", "action": "BUY", "confidence": 0.6},
        {"ticker": "C", "action": "SELL", "confidence": 0.95},
    ]

    result = shortlist(suggestions, limit=5)

    assert [s["ticker"] for s in result] == ["C", "B"]


def test_shortlist_respects_limit():
    suggestions = [{"ticker": str(i), "action": "BUY", "confidence": i} for i in range(10)]

    result = shortlist(suggestions, limit=3)

    assert len(result) == 3
    assert result[0]["ticker"] == "9"


def test_shortlist_boosts_regime_aligned_suggestion_above_equal_confidence():
    # Same confidence, one BUY tagged risk_on (aligned, boosted) and one
    # SELL tagged risk_on (not the aligned pairing) -- the aligned one
    # must rank first purely from the regime tag breaking the tie.
    suggestions = [
        {"ticker": "MISALIGNED_SELL", "action": "SELL", "confidence": 0.7, "risk_regime": "risk_on"},
        {"ticker": "ALIGNED_BUY", "action": "BUY", "confidence": 0.7, "risk_regime": "risk_on"},
    ]

    result = shortlist(suggestions, limit=5)

    assert [s["ticker"] for s in result] == ["ALIGNED_BUY", "MISALIGNED_SELL"]
    # The boost only reorders -- it must never rewrite the action Claude chose.
    assert result[0]["action"] == "BUY"
    assert result[1]["action"] == "SELL"


def test_shortlist_never_flips_a_higher_raw_confidence_below_a_lower_one():
    # A strong SELL must still outrank a weak, regime-aligned BUY -- the
    # boost nudges ties, it never overrides a clear confidence gap.
    suggestions = [
        {"ticker": "STRONG_SELL", "action": "SELL", "confidence": 0.9, "risk_regime": "risk_off"},
        {"ticker": "WEAK_BUY", "action": "BUY", "confidence": 0.2, "risk_regime": "risk_on"},
    ]

    result = shortlist(suggestions, limit=5)

    assert [s["ticker"] for s in result] == ["STRONG_SELL", "WEAK_BUY"]


def test_shortlist_unaffected_when_regime_tag_absent():
    # No risk_regime key at all (flag off -- the default) must rank
    # identically to plain confidence, same as before Task 4.
    suggestions = [
        {"ticker": "A", "action": "BUY", "confidence": 0.6},
        {"ticker": "B", "action": "SELL", "confidence": 0.95},
    ]

    result = shortlist(suggestions, limit=5)

    assert [s["ticker"] for s in result] == ["B", "A"]


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.global_cues_skill.fetch_global_cues")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_notifies_only_shortlisted_suggestions(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_fetch_global_cues,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    mock_startup_cls.return_value.start.return_value = MagicMock(
        watchlist=["AAPL", "MSFT"], default_timeframe="1h", enable_risk_regime_bias=False
    )
    mock_fetch_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    mock_fetch_global_cues.return_value = {"vix": {"level": 16.75}}
    buy_suggestion = {"ticker": "AAPL", "action": "BUY", "confidence": 0.8}
    hold_suggestion = {"ticker": "MSFT", "action": "HOLD", "confidence": 0.5}
    mock_generate_suggestion.side_effect = [buy_suggestion, hold_suggestion]

    main([])

    assert mock_save_suggestion.call_count == 2
    mock_notify_suggestion.assert_called_once_with(buy_suggestion)
    mock_fetch_global_cues.assert_called_once()
    for call in mock_generate_suggestion.call_args_list:
        assert call.args[4] == {"vix": {"level": 16.75}}


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.global_cues_skill.fetch_global_cues")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_one_suggestion_delivery_failure_does_not_block_the_rest(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_fetch_global_cues,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    # Found live: a Telegram delivery crash on the first shortlisted
    # suggestion (Markdown parser choking on Claude's rationale text)
    # aborted the whole run, silently dropping every other real signal
    # that day. One bad delivery must not block the rest.
    mock_startup_cls.return_value.start.return_value = MagicMock(
        watchlist=["AAPL", "MSFT"], default_timeframe="1h", enable_risk_regime_bias=False
    )
    mock_fetch_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    mock_fetch_global_cues.return_value = {}
    buy_suggestion = {"ticker": "AAPL", "action": "BUY", "confidence": 0.8}
    sell_suggestion = {"ticker": "MSFT", "action": "SELL", "confidence": 0.7}
    mock_generate_suggestion.side_effect = [buy_suggestion, sell_suggestion]
    mock_notify_suggestion.side_effect = [RuntimeError("telegram markdown crash"), None]

    main([])  # must not raise

    assert mock_notify_suggestion.call_count == 2


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.global_cues_skill.fetch_global_cues")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_skips_ticker_whose_fetch_fails_and_continues_watchlist(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_fetch_global_cues,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    mock_startup_cls.return_value.start.return_value = MagicMock(
        watchlist=["DELISTED", "AAPL"], default_timeframe="1h", enable_risk_regime_bias=False
    )
    mock_fetch_snapshot.side_effect = [ValueError("No OHLCV data returned for DELISTED (1h)"), {"ohlcv": MagicMock(), "news": []}]
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    mock_fetch_global_cues.return_value = {}
    buy_suggestion = {"ticker": "AAPL", "action": "BUY", "confidence": 0.8}
    mock_generate_suggestion.return_value = buy_suggestion

    main([])

    mock_generate_suggestion.assert_called_once()
    mock_save_suggestion.assert_called_once()
    mock_notify_suggestion.assert_called_once_with(buy_suggestion)


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.global_cues_skill.fetch_global_cues")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_continues_when_global_cues_fetch_returns_empty(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_fetch_global_cues,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    # fetch_global_cues() never raises (isolated internally) -- worst case
    # it returns {}, and the run must still proceed normally.
    mock_startup_cls.return_value.start.return_value = MagicMock(
        watchlist=["AAPL"], default_timeframe="1h", enable_risk_regime_bias=False
    )
    mock_fetch_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    mock_fetch_global_cues.return_value = {}
    mock_generate_suggestion.return_value = {"ticker": "AAPL", "action": "HOLD", "confidence": 0.5}

    main([])

    mock_generate_suggestion.assert_called_once()
    assert mock_generate_suggestion.call_args.args[4] == {}


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.global_cues_skill.fetch_global_cues")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_regime_check_inactive_when_flag_disabled(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_fetch_global_cues,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    # --regime-check passed, but Settings.enable_risk_regime_bias is False:
    # must stay off. Neither switch alone is enough.
    mock_startup_cls.return_value.start.return_value = MagicMock(
        watchlist=["AAPL"], default_timeframe="1h", enable_risk_regime_bias=False
    )
    mock_fetch_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    mock_fetch_global_cues.return_value = {}
    mock_generate_suggestion.return_value = {"ticker": "AAPL", "action": "HOLD", "confidence": 0.5}

    main(["--regime-check"])

    assert mock_generate_suggestion.call_args.args[5] is False


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.global_cues_skill.fetch_global_cues")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_regime_check_inactive_without_cli_flag(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_fetch_global_cues,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    # Settings.enable_risk_regime_bias is True, but --regime-check wasn't
    # passed on this invocation (e.g. midday_run/pre_close_run): must stay
    # off. This is how "once daily, paired with pre_market_run" is enforced.
    mock_startup_cls.return_value.start.return_value = MagicMock(
        watchlist=["AAPL"], default_timeframe="1h", enable_risk_regime_bias=True
    )
    mock_fetch_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    mock_fetch_global_cues.return_value = {}
    mock_generate_suggestion.return_value = {"ticker": "AAPL", "action": "HOLD", "confidence": 0.5}

    main([])

    assert mock_generate_suggestion.call_args.args[5] is False


@patch("skills.notify_skill.notify_suggestion")
@patch("skills.signal_skill.save_suggestion")
@patch("skills.signal_skill.generate_suggestion")
@patch("skills.global_cues_skill.fetch_global_cues")
@patch("skills.indicator_engine.summarize_recent")
@patch("skills.indicator_engine.summarize_latest")
@patch("skills.indicator_engine.compute_indicators")
@patch("skills.data_fetch_skill.fetch_ticker_snapshot")
@patch("config.startup.StartupService")
def test_main_regime_check_active_when_flag_and_cli_arg_both_set(
    mock_startup_cls,
    mock_fetch_snapshot,
    mock_compute_indicators,
    mock_summarize_latest,
    mock_summarize_recent,
    mock_fetch_global_cues,
    mock_generate_suggestion,
    mock_save_suggestion,
    mock_notify_suggestion,
):
    mock_startup_cls.return_value.start.return_value = MagicMock(
        watchlist=["AAPL"], default_timeframe="1h", enable_risk_regime_bias=True
    )
    mock_fetch_snapshot.return_value = {"ohlcv": MagicMock(), "news": []}
    mock_summarize_latest.return_value = {"rsi_14": 50.0}
    mock_summarize_recent.return_value = [{"rsi_14": 50.0}]
    mock_fetch_global_cues.return_value = {}
    mock_generate_suggestion.return_value = {"ticker": "AAPL", "action": "HOLD", "confidence": 0.5}

    main(["--regime-check"])

    assert mock_generate_suggestion.call_args.args[5] is True
