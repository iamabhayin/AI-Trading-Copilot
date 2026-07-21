"""Tests for skills/angel_client.py — Phase 15 Angel One SmartAPI client.

No live network/API calls: SmartConnect, requests, and the instruments
master fetch are always mocked. Snapshot write + change-in-OI/delta/IV
tests run against a real tmp-path SQLite database (mirrors
test_options_data_fetch.py's pattern), since their correctness matters
more than mocking would verify.
"""

from datetime import date, datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from SmartApi.smartExceptions import PermissionException, TokenException

from config.options_config import OptionsConfig
from skills.angel_client import (
    AngelAuthError,
    GreeksSanityError,
    _call_with_backoff,
    _is_rate_limit_error,
    _load_cached_session,
    _save_cached_session,
    _session_is_fresh,
    assert_greeks_sanity,
    compute_change_since_previous,
    compute_pcr,
    ensure_instruments_master,
    ensure_session,
    fetch_market_data,
    fetch_market_data_batched,
    fetch_nifty_spot,
    fetch_option_greeks,
    filter_nifty_option_contracts,
    format_expiry_readable,
    join_greeks_and_market_data,
    list_expiries,
    login,
    resolve_nifty_spot_token,
    resolve_symboltoken,
    run_market_data_cycle,
    select_atm_strikes,
    select_weekly_and_monthly_expiry,
    write_snapshot,
)
from skills.options_data_fetch import DataInsufficientError

IST = ZoneInfo("Asia/Kolkata")


def _config(**overrides) -> OptionsConfig:
    base = {
        "angel_api_key": "api-key",
        "angel_client_code": "A123456",
        "angel_pin": "1234",
        "angel_totp_secret": "JBSWY3DPEHPK3PXP",
    }
    base.update(overrides)
    return OptionsConfig(**base)


def _sample_instruments() -> list[dict]:
    return [
        {
            "token": "45526",
            "symbol": "NIFTY24JUL2525000CE",
            "name": "NIFTY",
            "expiry": "24JUL2025",
            "strike": "2500000.000000",
            "lotsize": "25",
            "instrumenttype": "OPTIDX",
            "exch_seg": "NFO",
        },
        {
            "token": "45527",
            "symbol": "NIFTY24JUL2525000PE",
            "name": "NIFTY",
            "expiry": "24JUL2025",
            "strike": "2500000.000000",
            "lotsize": "25",
            "instrumenttype": "OPTIDX",
            "exch_seg": "NFO",
        },
        {
            "token": "45528",
            "symbol": "NIFTY24JUL2525100CE",
            "name": "NIFTY",
            "expiry": "24JUL2025",
            "strike": "2510000.000000",
            "lotsize": "25",
            "instrumenttype": "OPTIDX",
            "exch_seg": "NFO",
        },
        {
            "token": "45529",
            "symbol": "NIFTY24JUL2525100PE",
            "name": "NIFTY",
            "expiry": "24JUL2025",
            "strike": "2510000.000000",
            "lotsize": "25",
            "instrumenttype": "OPTIDX",
            "exch_seg": "NFO",
        },
        {
            "token": "26000",
            "symbol": "Nifty 50",
            "name": "NIFTY",
            "expiry": "",
            "strike": "-1.000000",
            "lotsize": "1",
            "instrumenttype": "",
            "exch_seg": "NSE",
        },
        {
            "token": "99999",
            "symbol": "RELIANCE-EQ",
            "name": "RELIANCE",
            "expiry": "",
            "strike": "-1.000000",
            "lotsize": "1",
            "instrumenttype": "",
            "exch_seg": "NSE",
        },
    ]


def _sample_greeks(theta="-8.1", iv="14.2") -> list[dict]:
    return [
        {
            "name": "NIFTY",
            "expiry": "24JUL2025",
            "strikePrice": "25000",
            "optionType": "CE",
            "delta": "0.51",
            "gamma": "0.002",
            "theta": theta,
            "vega": "12.3",
            "impliedVolatility": iv,
            "tradeVolume": "50000",
        },
        {
            "name": "NIFTY",
            "expiry": "24JUL2025",
            "strikePrice": "25000",
            "optionType": "PE",
            "delta": "-0.49",
            "gamma": "0.002",
            "theta": "-7.9",
            "vega": "12.1",
            "impliedVolatility": "14.0",
            "tradeVolume": "42000",
        },
    ]


def _sample_market_data_response(tokens: list[str]) -> dict:
    fetched = [
        {
            "symbolToken": token,
            "ltp": 110.0,
            "opnInterest": 3000000,
            "tradeVolume": 50000,
            "depth": {"buy": [{"price": 109.5, "quantity": 75}], "sell": [{"price": 110.5, "quantity": 75}]},
        }
        for token in tokens
    ]
    return {"status": True, "message": "SUCCESS", "errorcode": "", "data": {"fetched": fetched, "unfetched": []}}


# --------------------------------------------------------------------------
# Token cache
# --------------------------------------------------------------------------


def test_save_and_load_cached_session_roundtrip(tmp_path):
    cache_path = str(tmp_path / "session.json")
    session = {"jwt_token": "jwt", "refresh_token": "r", "feed_token": "f", "obtained_ts": "2026-07-20T09:00:00+05:30"}
    _save_cached_session(cache_path, session)

    assert _load_cached_session(cache_path) == session


def test_load_cached_session_missing_file_returns_none(tmp_path):
    assert _load_cached_session(str(tmp_path / "missing.json")) is None


def test_session_is_fresh_same_ist_day():
    now = datetime(2026, 7, 20, 14, 0, tzinfo=IST)
    cached = {"jwt_token": "jwt", "obtained_ts": "2026-07-20T09:00:00+05:30"}
    assert _session_is_fresh(cached, now=now) is True


def test_session_is_fresh_different_day_is_stale():
    now = datetime(2026, 7, 21, 9, 0, tzinfo=IST)
    cached = {"jwt_token": "jwt", "obtained_ts": "2026-07-20T09:00:00+05:30"}
    assert _session_is_fresh(cached, now=now) is False


def test_session_is_fresh_missing_fields_is_stale():
    assert _session_is_fresh({}) is False


# --------------------------------------------------------------------------
# Rate-limit backoff (empirically observed live: two manual test runs a
# few minutes apart were enough to trip Angel One's rate limit)
# --------------------------------------------------------------------------


def test_is_rate_limit_error_matches_observed_message():
    from SmartApi.smartExceptions import DataException

    exc = DataException("Couldn't parse the JSON response received from the server: b'Access denied because of exceeding access rate'")
    assert _is_rate_limit_error(exc) is True


def test_is_rate_limit_error_does_not_match_other_errors():
    from SmartApi.smartExceptions import DataException

    assert _is_rate_limit_error(DataException("Couldn't parse the JSON response received from the server: b''")) is False


def test_call_with_backoff_retries_on_rate_limit_then_succeeds():
    from SmartApi.smartExceptions import DataException

    sleeps = []
    calls = {"count": 0}

    def flaky():
        calls["count"] += 1
        if calls["count"] < 3:
            raise DataException("Access denied because of exceeding access rate")
        return "ok"

    result = _call_with_backoff(flaky, sleep_fn=sleeps.append)

    assert result == "ok"
    assert calls["count"] == 3
    assert len(sleeps) == 2


def test_call_with_backoff_raises_after_max_retries():
    from SmartApi.smartExceptions import DataException

    def always_rate_limited():
        raise DataException("Access denied because of exceeding access rate")

    with pytest.raises(DataException):
        _call_with_backoff(always_rate_limited, max_retries=2, sleep_fn=lambda _: None)


def test_call_with_backoff_does_not_retry_non_rate_limit_errors():
    from SmartApi.smartExceptions import TokenException

    calls = {"count": 0}

    def unauthorized():
        calls["count"] += 1
        raise TokenException("session expired")

    with pytest.raises(TokenException):
        _call_with_backoff(unauthorized, sleep_fn=lambda _: None)

    assert calls["count"] == 1  # no retry attempted


# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


def test_login_missing_credentials_raises_auth_error():
    config = _config(angel_pin="")

    with pytest.raises(AngelAuthError):
        login(config)


def test_login_success_caches_session(tmp_path):
    config = _config(angel_token_cache_path=str(tmp_path / "session.json"))
    mock_smart = MagicMock()
    mock_smart.generateSession.return_value = {
        "status": True,
        "data": {"jwtToken": "jwt-1", "refreshToken": "refresh-1", "feedToken": "feed-1", "clientcode": "A123456"},
    }

    with patch("skills.angel_client.SmartConnect", return_value=mock_smart):
        session = login(config)

    assert session["jwt_token"] == "jwt-1"
    cached = _load_cached_session(config.angel_token_cache_path)
    assert cached["jwt_token"] == "jwt-1"


def test_login_strips_bearer_prefix_that_generatesession_adds(tmp_path):
    """Regression test: found via a live smoke test. SmartConnect.generateSession()
    sets the client's own access_token to the raw JWT internally, but mutates
    the *returned* dict's data['jwtToken'] to "Bearer " + jwtToken. Caching
    that prefixed value produces a malformed "Bearer Bearer ..." Authorization
    header on every subsequent authenticated call via build_client()."""
    config = _config(angel_token_cache_path=str(tmp_path / "session.json"))
    mock_smart = MagicMock()
    mock_smart.generateSession.return_value = {
        "status": True,
        "data": {
            "jwtToken": "Bearer raw-jwt-value",
            "refreshToken": "refresh-1",
            "feedToken": "feed-1",
            "clientcode": "A123456",
        },
    }

    with patch("skills.angel_client.SmartConnect", return_value=mock_smart):
        session = login(config)

    assert session["jwt_token"] == "raw-jwt-value"
    cached = _load_cached_session(config.angel_token_cache_path)
    assert cached["jwt_token"] == "raw-jwt-value"


def test_login_rejected_status_false_raises_auth_error(tmp_path):
    config = _config(angel_token_cache_path=str(tmp_path / "session.json"))
    mock_smart = MagicMock()
    mock_smart.generateSession.return_value = {"status": False, "message": "Invalid totp"}

    with patch("skills.angel_client.SmartConnect", return_value=mock_smart), pytest.raises(AngelAuthError):
        login(config)


def test_login_forbidden_exception_includes_public_ip_diagnostic(tmp_path):
    config = _config(angel_token_cache_path=str(tmp_path / "session.json"))
    mock_smart = MagicMock()
    mock_smart.generateSession.side_effect = TokenException("session expired")

    with (
        patch("skills.angel_client.SmartConnect", return_value=mock_smart),
        patch("skills.angel_client._public_ip", return_value="203.0.113.7"),pytest.raises(AngelAuthError) as exc_info
    ):
        login(config)

    assert "203.0.113.7" in str(exc_info.value)
    assert "whitelist" in str(exc_info.value).lower()


def test_login_permission_exception_also_triggers_ip_diagnostic(tmp_path):
    config = _config(angel_token_cache_path=str(tmp_path / "session.json"))
    mock_smart = MagicMock()
    mock_smart.generateSession.side_effect = PermissionException("forbidden")

    with (
        patch("skills.angel_client.SmartConnect", return_value=mock_smart),
        patch("skills.angel_client._public_ip", return_value=None),pytest.raises(AngelAuthError) as exc_info
    ):
        login(config)

    assert "could not determine current public ip" in str(exc_info.value).lower()


def test_ensure_session_reuses_fresh_cache(tmp_path):
    cache_path = str(tmp_path / "session.json")
    _save_cached_session(
        cache_path,
        {
            "jwt_token": "cached-jwt",
            "refresh_token": "r",
            "feed_token": "f",
            "obtained_ts": datetime.now(IST).isoformat(),
        },
    )
    config = _config(angel_token_cache_path=cache_path)

    with patch("skills.angel_client.login") as mock_login:
        session = ensure_session(config)

    assert session["jwt_token"] == "cached-jwt"
    mock_login.assert_not_called()


def test_ensure_session_logs_in_when_stale(tmp_path):
    cache_path = str(tmp_path / "session.json")
    _save_cached_session(cache_path, {"jwt_token": "old", "obtained_ts": "2020-01-01T09:00:00+05:30"})
    config = _config(angel_token_cache_path=cache_path)

    with patch("skills.angel_client.login", return_value={"jwt_token": "new"}) as mock_login:
        session = ensure_session(config)

    assert session["jwt_token"] == "new"
    mock_login.assert_called_once()


# --------------------------------------------------------------------------
# Instruments master
# --------------------------------------------------------------------------


def test_ensure_instruments_master_fetches_and_caches(tmp_path):
    config = _config(angel_instruments_cache_path=str(tmp_path / "instruments.json"))
    instruments = _sample_instruments()

    with patch("skills.angel_client.fetch_instruments_master", return_value=instruments):
        result = ensure_instruments_master(config, today=date(2026, 7, 20))

    assert result == instruments


def test_ensure_instruments_master_reuses_same_day_cache(tmp_path):
    config = _config(angel_instruments_cache_path=str(tmp_path / "instruments.json"))
    instruments = _sample_instruments()

    with patch("skills.angel_client.fetch_instruments_master", return_value=instruments) as mock_fetch:
        ensure_instruments_master(config, today=date(2026, 7, 20))
        ensure_instruments_master(config, today=date(2026, 7, 20))

    mock_fetch.assert_called_once()


def test_ensure_instruments_master_refuses_stale_cache_and_refetches(tmp_path):
    config = _config(angel_instruments_cache_path=str(tmp_path / "instruments.json"))
    instruments = _sample_instruments()

    with patch("skills.angel_client.fetch_instruments_master", return_value=instruments) as mock_fetch:
        ensure_instruments_master(config, today=date(2026, 7, 20))
        ensure_instruments_master(config, today=date(2026, 7, 21))

    assert mock_fetch.call_count == 2


def test_ensure_instruments_master_fetch_failure_with_no_cache_raises(tmp_path):
    import requests

    config = _config(angel_instruments_cache_path=str(tmp_path / "instruments.json"))

    with (
        patch("skills.angel_client.fetch_instruments_master", side_effect=requests.ConnectionError("boom")),
        pytest.raises(DataInsufficientError),
    ):
        ensure_instruments_master(config, today=date(2026, 7, 20))


def test_filter_nifty_option_contracts():
    contracts = filter_nifty_option_contracts(_sample_instruments())

    assert len(contracts) == 4
    assert all(c["exch_seg"] == "NFO" and c["instrumenttype"] == "OPTIDX" for c in contracts)


def test_resolve_nifty_spot_token():
    row = resolve_nifty_spot_token(_sample_instruments())

    assert row["token"] == "26000"
    assert row["exch_seg"] == "NSE"


def test_resolve_nifty_spot_token_missing_returns_none():
    assert resolve_nifty_spot_token([{"exch_seg": "NFO", "symbol": "NIFTY24JUL2525000CE"}]) is None


def test_list_expiries_sorted_by_actual_date():
    contracts = [{"expiry": "31JUL2025"}, {"expiry": "24JUL2025"}, {"expiry": "24JUL2025"}]

    assert list_expiries(contracts) == ["24JUL2025", "31JUL2025"]


def test_format_expiry_readable():
    assert format_expiry_readable("21JUL2026") == "21 July"
    assert format_expiry_readable("04AUG2026") == "4 August"


def test_select_weekly_and_monthly_expiry_same_month():
    from datetime import date

    expiries = ["24JUL2025", "31JUL2025", "07AUG2025"]
    weekly, monthly = select_weekly_and_monthly_expiry(expiries, date(2025, 7, 20))

    assert weekly == "24JUL2025"
    assert monthly == "31JUL2025"  # last expiry within July


def test_select_weekly_and_monthly_expiry_differs_from_weekly():
    from datetime import date

    expiries = ["04AUG2025", "11AUG2025", "18AUG2025", "25AUG2025"]
    weekly, monthly = select_weekly_and_monthly_expiry(expiries, date(2025, 8, 1))

    assert weekly == "04AUG2025"
    assert monthly == "25AUG2025"


def test_select_weekly_and_monthly_expiry_no_future_raises():
    from datetime import date

    with pytest.raises(ValueError):
        select_weekly_and_monthly_expiry(["01JUL2025"], date(2025, 7, 20))


def test_resolve_symboltoken_found_and_missing():
    contracts = filter_nifty_option_contracts(_sample_instruments())

    assert resolve_symboltoken(contracts, "24JUL2025", 25000.0, "CE") == "45526"
    assert resolve_symboltoken(contracts, "24JUL2025", 25000.0, "PE") == "45527"
    assert resolve_symboltoken(contracts, "24JUL2025", 25200.0, "CE") is None


def test_select_atm_strikes_window():
    contracts = filter_nifty_option_contracts(_sample_instruments())

    strikes = select_atm_strikes(contracts, "24JUL2025", spot=25010.0, window=10)

    assert strikes == [25000.0, 25100.0]


def test_select_atm_strikes_no_listed_strikes_returns_empty():
    assert select_atm_strikes([], "24JUL2025", spot=25000.0, window=10) == []


# --------------------------------------------------------------------------
# Data fetch
# --------------------------------------------------------------------------


def test_fetch_nifty_spot_success():
    client = MagicMock()
    client.ltpData.return_value = {"status": True, "data": {"ltp": 25010.5}}
    spot_row = {"exch_seg": "NSE", "symbol": "Nifty 50", "token": "26000"}

    assert fetch_nifty_spot(client, spot_row) == 25010.5


def test_fetch_nifty_spot_forbidden_raises_auth_error():
    client = MagicMock()
    client.ltpData.side_effect = TokenException("expired")
    spot_row = {"exch_seg": "NSE", "symbol": "Nifty 50", "token": "26000"}

    with patch("skills.angel_client._public_ip", return_value=None), pytest.raises(AngelAuthError):
        fetch_nifty_spot(client, spot_row)


def test_fetch_nifty_spot_no_data_raises_data_insufficient():
    client = MagicMock()
    client.ltpData.return_value = {"status": False, "data": None}
    spot_row = {"exch_seg": "NSE", "symbol": "Nifty 50", "token": "26000"}

    with pytest.raises(DataInsufficientError):
        fetch_nifty_spot(client, spot_row)


def test_fetch_option_greeks_success():
    client = MagicMock()
    client.optionGreek.return_value = {"status": True, "data": _sample_greeks()}

    result = fetch_option_greeks(client, "24JUL2025")

    assert len(result) == 2


def test_fetch_option_greeks_no_data_raises():
    client = MagicMock()
    client.optionGreek.return_value = {"status": True, "data": []}

    with pytest.raises(DataInsufficientError):
        fetch_option_greeks(client, "24JUL2025")


def test_fetch_market_data_success():
    client = MagicMock()
    client.getMarketData.return_value = _sample_market_data_response(["45526", "45527"])

    result = fetch_market_data(client, ["45526", "45527"])

    assert set(result.keys()) == {"45526", "45527"}
    assert result["45526"]["ltp"] == 110.0


def test_fetch_market_data_rejects_more_than_50_tokens():
    client = MagicMock()

    with pytest.raises(ValueError):
        fetch_market_data(client, [str(i) for i in range(51)])


def test_fetch_market_data_batched_chunks_at_50():
    client = MagicMock()
    client.getMarketData.side_effect = lambda mode, exchange_tokens: _sample_market_data_response(
        exchange_tokens["NFO"]
    )
    tokens = [str(i) for i in range(120)]

    result = fetch_market_data_batched(client, tokens)

    assert len(result) == 120
    assert client.getMarketData.call_count == 3


def test_join_greeks_and_market_data():
    contracts = filter_nifty_option_contracts(_sample_instruments())
    market_by_token = _sample_market_data_response(["45526", "45527"])["data"]["fetched"]
    market_by_token = {row["symbolToken"]: row for row in market_by_token}

    joined = join_greeks_and_market_data(_sample_greeks(), market_by_token, contracts, "24JUL2025")

    assert len(joined) == 2
    ce_row = next(row for row in joined if row["side"] == "CE")
    assert ce_row["strike"] == 25000.0
    assert ce_row["ltp"] == 110.0
    assert ce_row["oi"] == 3000000
    assert ce_row["bid"] == 109.5
    assert ce_row["delta"] == 0.51


def test_join_greeks_and_market_data_unmatched_token_has_none_market_fields():
    joined = join_greeks_and_market_data(_sample_greeks(), {}, [], "24JUL2025")

    assert all(row["ltp"] is None for row in joined)
    assert all(row["delta"] is not None for row in joined)  # Greeks still present


def test_compute_pcr():
    joined = [
        {"side": "CE", "oi": 1000000},
        {"side": "PE", "oi": 1500000},
    ]

    assert compute_pcr(joined) == pytest.approx(1.5)


def test_compute_pcr_zero_call_oi_returns_none():
    joined = [{"side": "PE", "oi": 1000000}]

    assert compute_pcr(joined) is None


# --------------------------------------------------------------------------
# Weekly/monthly Greeks sanity check
# --------------------------------------------------------------------------


def test_assert_greeks_sanity_passes_when_greeks_differ():
    weekly = _sample_greeks(theta="-8.1", iv="14.2")
    monthly = _sample_greeks(theta="-3.5", iv="16.8")

    assert_greeks_sanity(weekly, monthly, atm_strike=25000.0)  # should not raise


def test_assert_greeks_sanity_fires_on_identical_greeks():
    weekly = _sample_greeks(theta="-8.1", iv="14.2")
    monthly = _sample_greeks(theta="-8.1", iv="14.2")  # bug: monthly bled into weekly

    with pytest.raises(GreeksSanityError):
        assert_greeks_sanity(weekly, monthly, atm_strike=25000.0)


def test_assert_greeks_sanity_missing_atm_strike_raises_data_insufficient():
    weekly = _sample_greeks()
    monthly = _sample_greeks()

    with pytest.raises(DataInsufficientError):
        assert_greeks_sanity(weekly, monthly, atm_strike=99999.0)


# --------------------------------------------------------------------------
# Snapshot write + change-in-OI/delta/IV (real tmp-path DB)
# --------------------------------------------------------------------------


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    mock_settings = MagicMock(sqlite_db_path=db_path)
    monkeypatch.setattr("config.settings.Settings.load", lambda: mock_settings)

    from db.database import init_db

    init_db()
    return db_path


def _joined_row(strike=25000.0, side="CE", oi=3000000, delta=0.51, iv=14.2):
    return {
        "expiry": "2026-07-24",
        "strike": strike,
        "side": side,
        "ltp": 110.0,
        "volume": 50000,
        "oi": oi,
        "bid": 109.5,
        "bid_qty": 75,
        "ask": 110.5,
        "ask_qty": 75,
        "iv": iv,
        "delta": delta,
        "gamma": 0.002,
        "theta": -8.1,
        "vega": 12.3,
    }


def test_write_snapshot_inserts_rows(real_db):
    written = write_snapshot([_joined_row()], "2026-07-20", "2026-07-20T09:15:00+05:30", spot=25010.5)

    assert written == 1
    import sqlite3

    conn = sqlite3.connect(real_db)
    count = conn.execute("SELECT COUNT(*) FROM option_chain_snapshots").fetchone()[0]
    conn.close()
    assert count == 1


def test_write_snapshot_no_rows_raises(real_db):
    with pytest.raises(DataInsufficientError):
        write_snapshot([], "2026-07-20", "2026-07-20T09:15:00+05:30", spot=25010.5)


def test_compute_change_since_previous_no_prior_snapshot_returns_none(real_db):
    write_snapshot([_joined_row()], "2026-07-20", "2026-07-20T09:15:00+05:30", spot=25010.5)

    result = compute_change_since_previous("2026-07-24", 25000.0, "CE", "2026-07-20T09:15:00+05:30")

    assert result is None


def test_compute_change_since_previous_computes_deltas(real_db):
    write_snapshot([_joined_row(oi=3000000, delta=0.51, iv=14.2)], "2026-07-20", "2026-07-20T09:15:00+05:30", spot=25010.5)
    write_snapshot([_joined_row(oi=3200000, delta=0.55, iv=14.5)], "2026-07-20", "2026-07-20T09:20:00+05:30", spot=25020.0)

    result = compute_change_since_previous("2026-07-24", 25000.0, "CE", "2026-07-20T09:20:00+05:30")

    assert result["oi_change"] == 200000
    assert result["delta_change"] == pytest.approx(0.04)
    assert result["iv_change"] == pytest.approx(0.3)
    assert result["previous_snapshot_ts"] == "2026-07-20T09:15:00+05:30"


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def test_run_market_data_cycle_success(real_db):
    config = _config(angel_atm_strike_window=10)
    instruments = _sample_instruments()
    filter_nifty_option_contracts(instruments)
    client = MagicMock()
    client.ltpData.return_value = {"status": True, "data": {"ltp": 25010.5}}
    client.optionGreek.return_value = {"status": True, "data": _sample_greeks()}
    client.getMarketData.side_effect = lambda mode, exchange_tokens: _sample_market_data_response(
        exchange_tokens["NFO"]
    )

    with (
        patch("skills.angel_client.ensure_session", return_value={"jwt_token": "j", "refresh_token": "r", "feed_token": "f"}),
        patch("skills.angel_client.build_client", return_value=client),
        patch("skills.angel_client.ensure_instruments_master", return_value=instruments),
    ):
        result = run_market_data_cycle(config, weekly_expiry="24JUL2025", monthly_expiry="24JUL2025", trading_date="2026-07-20")

    assert result["spot"] == 25010.5
    assert result["rows_written"] == 2
    assert result["pcr"] is not None
    assert len(result["chain"]) == 2
    assert {row["side"] for row in result["chain"]} == {"CE", "PE"}
    # weekly_expiry == monthly_expiry here -- sanity check never runs.
    assert result["greeks_sanity_checked"] is False


def test_run_market_data_cycle_runs_sanity_check_when_expiries_differ(real_db):
    config = _config(angel_atm_strike_window=10)
    instruments = _sample_instruments()
    client = MagicMock()
    client.ltpData.return_value = {"status": True, "data": {"ltp": 25010.5}}
    client.optionGreek.side_effect = lambda params: {
        "status": True,
        "data": _sample_greeks(theta="-8.1", iv="14.2") if params["expirydate"] == "24JUL2025" else _sample_greeks(theta="-3.5", iv="16.8"),
    }
    client.getMarketData.side_effect = lambda mode, exchange_tokens: _sample_market_data_response(exchange_tokens["NFO"])

    with (
        patch("skills.angel_client.ensure_session", return_value={"jwt_token": "j", "refresh_token": "r", "feed_token": "f"}),
        patch("skills.angel_client.build_client", return_value=client),
        patch("skills.angel_client.ensure_instruments_master", return_value=instruments),
    ):
        result = run_market_data_cycle(config, weekly_expiry="24JUL2025", monthly_expiry="28AUG2025", trading_date="2026-07-20")

    assert result["greeks_sanity_checked"] is True
    assert client.optionGreek.call_count == 2


def test_run_market_data_cycle_skips_sanity_check_when_requested(real_db):
    config = _config(angel_atm_strike_window=10)
    instruments = _sample_instruments()
    client = MagicMock()
    client.ltpData.return_value = {"status": True, "data": {"ltp": 25010.5}}
    client.optionGreek.return_value = {"status": True, "data": _sample_greeks()}
    client.getMarketData.side_effect = lambda mode, exchange_tokens: _sample_market_data_response(exchange_tokens["NFO"])

    with (
        patch("skills.angel_client.ensure_session", return_value={"jwt_token": "j", "refresh_token": "r", "feed_token": "f"}),
        patch("skills.angel_client.build_client", return_value=client),
        patch("skills.angel_client.ensure_instruments_master", return_value=instruments),
    ):
        result = run_market_data_cycle(
            config, weekly_expiry="24JUL2025", monthly_expiry="28AUG2025", trading_date="2026-07-20",
            skip_greeks_sanity_check=True,
        )

    assert result["greeks_sanity_checked"] is False
    assert client.optionGreek.call_count == 1  # only the weekly fetch, monthly skipped entirely


def test_run_market_data_cycle_no_spot_token_raises(real_db):
    config = _config()
    instruments = [c for c in _sample_instruments() if c["exch_seg"] != "NSE"]

    with (
        patch("skills.angel_client.ensure_session", return_value={"jwt_token": "j", "refresh_token": "r", "feed_token": "f"}),
        patch("skills.angel_client.build_client", return_value=MagicMock()),
        patch("skills.angel_client.ensure_instruments_master", return_value=instruments),
        pytest.raises(DataInsufficientError),
    ):
        run_market_data_cycle(config, weekly_expiry="24JUL2025", monthly_expiry="24JUL2025", trading_date="2026-07-20")


def test_run_market_data_cycle_greeks_sanity_failure_propagates(real_db):
    config = _config()
    instruments = _sample_instruments()
    client = MagicMock()
    client.ltpData.return_value = {"status": True, "data": {"ltp": 25010.5}}
    client.optionGreek.return_value = {"status": True, "data": _sample_greeks(theta="-8.1", iv="14.2")}

    with (
        patch("skills.angel_client.ensure_session", return_value={"jwt_token": "j", "refresh_token": "r", "feed_token": "f"}),
        patch("skills.angel_client.build_client", return_value=client),
        patch("skills.angel_client.ensure_instruments_master", return_value=instruments),
        pytest.raises(GreeksSanityError),
    ):
        run_market_data_cycle(
            config, weekly_expiry="24JUL2025", monthly_expiry="28AUG2025", trading_date="2026-07-20"
            )
