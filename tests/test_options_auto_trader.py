"""Tests for skills/options_auto_trader.py — Phase 17 auto-trading.

No live network/API calls: SmartConnect (ensure_session/build_client/
place_market_order/get_order_status/fetch_market_data) and Discord/Telegram
(route_message) are always mocked. DB-touching functions run against a real
tmp-path SQLite database, mirroring test_options_position_monitor.py's
`real_db` fixture.
"""

from datetime import date, datetime
from unittest.mock import MagicMock, patch

import pytest

from config.options_config import OptionsConfig
from skills.options_auto_trader import (
    can_open_new_trade,
    compute_lots,
    count_trades_today,
    decide_exit,
    force_close_stale_positions,
    format_entry_message,
    format_exit_message,
    has_open_position,
    is_automation_enabled,
    maybe_execute_entry,
    resolve_contract_row,
    run_auto_exit_pass,
)

TODAY = date(2026, 7, 29)


def _config(**overrides) -> OptionsConfig:
    base = {
        "auto_trading_enabled": True,
        "auto_trading_dry_run": True,
        "auto_trading_budget_per_trade": 15000.0,
        "auto_trading_max_trades_per_day": 2,
        "auto_trading_squareoff_time": "15:15",
    }
    base.update(overrides)
    return OptionsConfig(**base)


def _payload(**overrides) -> dict:
    base = {
        "expiry": "24JUL2025",
        "strike": 25300.0,
        "side": "CE",
        "entry_premium": 120.0,
        "option_stop": 95.0,
        "target_1": 155.0,
        "lot_size": 75,
        "spot": 25320.0,
        "market_regime": "Bullish breakout, confirmed close above 25,275",
    }
    base.update(overrides)
    return base


def _contracts() -> list[dict]:
    return [
        {"token": "45526", "symbol": "NIFTY24JUL2525300CE", "expiry": "24JUL2025", "strike": "2530000.000000"},
        {"token": "45527", "symbol": "NIFTY24JUL2525300PE", "expiry": "24JUL2025", "strike": "2530000.000000"},
    ]


@pytest.fixture
def real_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    mock_settings = MagicMock(sqlite_db_path=db_path)
    monkeypatch.setattr("config.settings.Settings.load", lambda: mock_settings)

    from db.database import init_db

    init_db()
    return db_path


def _insert_position(status="open", trade_date=TODAY.isoformat(), dry_run=1, **overrides):
    from db.database import execute

    row = {
        "status": status,
        "trade_date": trade_date,
        "contract": "NIFTY 25300 CE 24 July",
        "tradingsymbol": "NIFTY24JUL2525300CE",
        "symboltoken": "45526",
        "expiry_date": "24JUL2025",
        "strike": 25300.0,
        "side": "CE",
        "qty_lots": 1,
        "lot_size": 75,
        "dry_run": dry_run,
        "entry_order_id": "DRYRUN-1",
        "entry_premium": 120.0,
        "entry_spot": 25320.0,
        "option_stop": 95.0,
        "target_1": 155.0,
        "trend_label": "Bullish",
    }
    row.update(overrides)
    return execute(
        """
        INSERT INTO auto_options_positions
            (status, trade_date, contract, tradingsymbol, symboltoken, expiry_date, strike, side,
             qty_lots, lot_size, dry_run, entry_order_id, entry_premium, entry_spot,
             option_stop, target_1, trend_label)
        VALUES (:status, :trade_date, :contract, :tradingsymbol, :symboltoken, :expiry_date, :strike, :side,
                :qty_lots, :lot_size, :dry_run, :entry_order_id, :entry_premium, :entry_spot,
                :option_stop, :target_1, :trend_label)
        """,
        row,
    )


# --------------------------------------------------------------------------
# Pure guards / decisions
# --------------------------------------------------------------------------


def test_compute_lots_basic():
    assert compute_lots(entry_premium=120.0, lot_size=75, budget=15000.0) == 1  # 120*75=9000, 2 lots=18000>budget


def test_compute_lots_two_lots_fit():
    assert compute_lots(entry_premium=90.0, lot_size=75, budget=15000.0) == 2  # 90*75=6750, 2 lots=13500<=15000


def test_compute_lots_budget_too_small_for_one_lot():
    assert compute_lots(entry_premium=500.0, lot_size=75, budget=1000.0) == 0


def test_compute_lots_zero_premium_never_divides_by_zero():
    assert compute_lots(entry_premium=0.0, lot_size=75, budget=15000.0) == 0


def test_resolve_contract_row_match():
    row = resolve_contract_row(_contracts(), "24JUL2025", 25300.0, "CE")
    assert row is not None
    assert row["token"] == "45526"
    assert row["symbol"] == "NIFTY24JUL2525300CE"


def test_resolve_contract_row_no_match():
    assert resolve_contract_row(_contracts(), "24JUL2025", 25400.0, "CE") is None


def test_decide_exit_sl_hit():
    position = {"option_stop": 95.0, "target_1": 155.0}
    now = datetime(2026, 7, 29, 11, 0)
    assert decide_exit(position, current_premium=90.0, now=now, config=_config()) == "SL_HIT"


def test_decide_exit_target_hit():
    position = {"option_stop": 95.0, "target_1": 155.0}
    now = datetime(2026, 7, 29, 11, 0)
    assert decide_exit(position, current_premium=160.0, now=now, config=_config()) == "TARGET_HIT"


def test_decide_exit_forced_squareoff_after_cutoff():
    position = {"option_stop": 95.0, "target_1": 155.0}
    now = datetime(2026, 7, 29, 15, 20)
    assert decide_exit(position, current_premium=130.0, now=now, config=_config()) == "FORCED_SQUAREOFF"


def test_decide_exit_none_when_no_condition_met():
    position = {"option_stop": 95.0, "target_1": 155.0}
    now = datetime(2026, 7, 29, 11, 0)
    assert decide_exit(position, current_premium=130.0, now=now, config=_config()) is None


def test_is_automation_enabled():
    assert is_automation_enabled(_config(auto_trading_enabled=True)) is True
    assert is_automation_enabled(_config(auto_trading_enabled=False)) is False


# --------------------------------------------------------------------------
# DB-backed guards
# --------------------------------------------------------------------------


def test_can_open_new_trade_true_when_clear(real_db):
    assert can_open_new_trade(_config(), TODAY) is True


def test_can_open_new_trade_false_when_automation_disabled(real_db):
    assert can_open_new_trade(_config(auto_trading_enabled=False), TODAY) is False


def test_can_open_new_trade_false_when_position_open(real_db):
    _insert_position(status="open")
    assert can_open_new_trade(_config(), TODAY) is False


def test_can_open_new_trade_false_when_daily_cap_hit(real_db):
    _insert_position(status="closed")
    _insert_position(status="closed", entry_order_id="DRYRUN-2")
    assert can_open_new_trade(_config(auto_trading_max_trades_per_day=2), TODAY) is False


def test_count_trades_today_scoped_by_date(real_db):
    _insert_position(status="closed", trade_date=TODAY.isoformat())
    _insert_position(status="closed", trade_date="2026-07-28", entry_order_id="DRYRUN-2")
    assert count_trades_today(TODAY) == 1


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------


def test_maybe_execute_entry_dry_run_opens_position_and_notifies(real_db):
    with patch("skills.options_auto_trader.route_message") as mock_route:
        maybe_execute_entry(_payload(), _contracts(), _config(), TODAY)

    position = has_open_position()
    assert position is not None
    assert position["dry_run"] == 1
    assert position["entry_order_id"].startswith("DRYRUN-")
    assert position["entry_premium"] == 120.0
    assert position["qty_lots"] == 1
    assert position["tradingsymbol"] == "NIFTY24JUL2525300CE"

    mock_route.assert_called_once()
    args, kwargs = mock_route.call_args
    assert args[0] == "monitoring"
    assert "AUTO-TRADE ENTRY" in args[1]
    assert kwargs.get("telegram_parse_mode") is None


def test_maybe_execute_entry_noop_when_automation_disabled(real_db):
    with patch("skills.options_auto_trader.route_message") as mock_route:
        maybe_execute_entry(_payload(), _contracts(), _config(auto_trading_enabled=False), TODAY)

    assert has_open_position() is None
    mock_route.assert_not_called()


def test_maybe_execute_entry_noop_when_budget_too_small(real_db):
    with patch("skills.options_auto_trader.route_message") as mock_route:
        maybe_execute_entry(_payload(), _contracts(), _config(auto_trading_budget_per_trade=10.0), TODAY)

    assert has_open_position() is None
    mock_route.assert_not_called()


def test_maybe_execute_entry_noop_when_contract_not_found(real_db):
    payload = _payload(strike=99999.0)
    with patch("skills.options_auto_trader.route_message") as mock_route:
        maybe_execute_entry(payload, _contracts(), _config(), TODAY)

    assert has_open_position() is None
    mock_route.assert_not_called()


def test_maybe_execute_entry_noop_when_position_already_open(real_db):
    _insert_position(status="open")
    with patch("skills.options_auto_trader.route_message") as mock_route:
        maybe_execute_entry(_payload(), _contracts(), _config(), TODAY)

    # Still exactly one open position -- no second entry.
    rows_open = [1]  # sanity placeholder, real assertion below
    from db.database import fetch_all

    assert len(fetch_all("SELECT id FROM auto_options_positions WHERE status = 'open'")) == 1
    mock_route.assert_not_called()


def test_maybe_execute_entry_live_places_buy_order_only(real_db):
    mock_client = MagicMock()
    order_result = {"ok": True, "order_id": "AO123", "raw": {}, "error": None}
    with (
        patch("skills.options_auto_trader.route_message"),
        patch("skills.options_auto_trader.ensure_session", return_value={"jwt_token": "j"}),
        patch("skills.options_auto_trader.build_client", return_value=mock_client),
        patch("skills.options_auto_trader.place_market_order", return_value=order_result) as mock_place,
        patch("skills.options_auto_trader.get_order_status", return_value={"averageprice": "121.5"}),
    ):
        maybe_execute_entry(_payload(), _contracts(), _config(auto_trading_dry_run=False), TODAY)

    mock_place.assert_called_once()
    _, kwargs = mock_place.call_args
    assert kwargs["transactiontype"] == "BUY"

    position = has_open_position()
    assert position["dry_run"] == 0
    assert position["entry_order_id"] == "AO123"
    assert position["entry_premium"] == 121.5


# --------------------------------------------------------------------------
# Exit
# --------------------------------------------------------------------------


def _analysis_with_ltp(strike, side, ltp):
    return {"chain": [{"strike": strike, "side": side, "ltp": ltp}]}


def test_run_auto_exit_pass_target_hit_closes_dry_run_position(real_db):
    _insert_position(status="open")
    now = datetime(2026, 7, 29, 11, 0)
    with patch("skills.options_auto_trader.route_message") as mock_route:
        run_auto_exit_pass(_analysis_with_ltp(25300.0, "CE", 160.0), _config(), now, TODAY)

    from db.database import fetch_one

    row = fetch_one("SELECT * FROM auto_options_positions WHERE status = 'closed'")
    assert row is not None
    assert row["exit_reason"] == "TARGET_HIT"
    assert row["exit_premium"] == 160.0
    assert row["realized_pnl"] == (160.0 - 120.0) * 1 * 75
    assert row["exit_order_id"].startswith("DRYRUN-")
    mock_route.assert_called_once()


def test_run_auto_exit_pass_noop_when_no_open_position(real_db):
    now = datetime(2026, 7, 29, 11, 0)
    with patch("skills.options_auto_trader.route_message") as mock_route:
        run_auto_exit_pass(_analysis_with_ltp(25300.0, "CE", 200.0), _config(), now, TODAY)
    mock_route.assert_not_called()


def test_run_auto_exit_pass_noop_when_automation_disabled(real_db):
    _insert_position(status="open")
    now = datetime(2026, 7, 29, 11, 0)
    with patch("skills.options_auto_trader.route_message") as mock_route:
        run_auto_exit_pass(_analysis_with_ltp(25300.0, "CE", 200.0), _config(auto_trading_enabled=False), now, TODAY)
    mock_route.assert_not_called()
    assert has_open_position() is not None


def test_run_auto_exit_pass_no_condition_met_leaves_position_open(real_db):
    _insert_position(status="open")
    now = datetime(2026, 7, 29, 11, 0)
    with patch("skills.options_auto_trader.route_message") as mock_route:
        run_auto_exit_pass(_analysis_with_ltp(25300.0, "CE", 130.0), _config(), now, TODAY)
    mock_route.assert_not_called()
    assert has_open_position() is not None


def test_run_auto_exit_pass_live_places_sell_order_only(real_db):
    _insert_position(status="open", dry_run=0)
    now = datetime(2026, 7, 29, 11, 0)
    mock_client = MagicMock()
    order_result = {"ok": True, "order_id": "AO456", "raw": {}, "error": None}
    with (
        patch("skills.options_auto_trader.route_message"),
        patch("skills.options_auto_trader.ensure_session", return_value={"jwt_token": "j"}),
        patch("skills.options_auto_trader.build_client", return_value=mock_client),
        patch("skills.options_auto_trader.place_market_order", return_value=order_result) as mock_place,
        patch("skills.options_auto_trader.get_order_status", return_value={"averageprice": "158.0"}),
    ):
        run_auto_exit_pass(_analysis_with_ltp(25300.0, "CE", 160.0), _config(), now, TODAY)

    mock_place.assert_called_once()
    _, kwargs = mock_place.call_args
    assert kwargs["transactiontype"] == "SELL"

    from db.database import fetch_one

    row = fetch_one("SELECT * FROM auto_options_positions WHERE status = 'closed'")
    assert row["exit_order_id"] == "AO456"
    assert row["exit_premium"] == 158.0


def test_force_close_stale_positions_closes_open_position(real_db):
    _insert_position(status="open")
    with (
        patch("skills.options_auto_trader.route_message") as mock_route,
        patch("skills.options_auto_trader._fetch_current_premium_live", return_value=140.0),
    ):
        force_close_stale_positions(_config(), TODAY)

    from db.database import fetch_one

    row = fetch_one("SELECT * FROM auto_options_positions WHERE status = 'closed'")
    assert row is not None
    assert row["exit_reason"] == "FORCED_SQUAREOFF"
    assert row["exit_premium"] == 140.0
    mock_route.assert_called_once()


def test_force_close_stale_positions_closes_and_warns_when_from_prior_day(real_db):
    """Regression: force_close_stale_positions used to no-op on a position
    whose trade_date != today, leaving it 'open' forever and permanently
    blocking can_open_new_trade() (has_open_position() has no date filter).
    It must close ANY open position, and flag the abnormal case loudly."""
    yesterday = date(2026, 7, 28)
    _insert_position(status="open", trade_date=yesterday.isoformat())
    with (
        patch("skills.options_auto_trader.route_message") as mock_route,
        patch("skills.options_auto_trader._fetch_current_premium_live", return_value=140.0),
    ):
        force_close_stale_positions(_config(), TODAY)

    from db.database import fetch_one

    row = fetch_one("SELECT * FROM auto_options_positions WHERE status = 'closed'")
    assert row is not None
    assert row["exit_reason"] == "FORCED_SQUAREOFF"
    assert has_open_position() is None
    assert mock_route.call_count == 2  # stale-position warning + the normal exit notification
    warning_text = mock_route.call_args_list[0].args[1]
    assert "WARNING" in warning_text and yesterday.isoformat() in warning_text


def test_force_close_stale_positions_noop_when_none_open(real_db):
    with patch("skills.options_auto_trader.route_message") as mock_route:
        force_close_stale_positions(_config(), TODAY)
    mock_route.assert_not_called()


def test_force_close_stale_positions_noop_when_automation_disabled(real_db):
    _insert_position(status="open")
    with patch("skills.options_auto_trader.route_message") as mock_route:
        force_close_stale_positions(_config(auto_trading_enabled=False), TODAY)
    mock_route.assert_not_called()
    assert has_open_position() is not None


# --------------------------------------------------------------------------
# Message formatting
# --------------------------------------------------------------------------


def test_format_entry_message_contains_key_fields():
    position = {
        "dry_run": 1, "contract": "NIFTY 25300 CE 24 July", "qty_lots": 3, "lot_size": 75,
        "entry_premium": 120.5, "trend_label": "Bullish breakout", "option_stop": 95.0,
        "target_1": 155.0, "entry_order_id": "DRYRUN-abc",
    }
    text = format_entry_message(position, trades_today=1, max_trades=2)
    assert "[DRY RUN]" in text
    assert "NIFTY 25300 CE 24 July" in text
    assert "3 lot(s) (225 qty)" in text
    assert "Rs 120.50" in text
    assert "Bullish breakout" in text
    assert "Trade 1/2 today" in text


def test_format_exit_message_contains_pnl():
    position = {
        "dry_run": 1, "contract": "NIFTY 25300 CE 24 July", "exit_reason": "TARGET_HIT",
        "entry_premium": 120.0, "exit_premium": 155.0, "realized_pnl": 2625.0,
    }
    text = format_exit_message(position, trades_today=1, max_trades=2)
    assert "TARGET HIT" in text
    assert "+Rs 2,625.00" in text
    assert "Trade 1/2 today complete" in text


def test_format_exit_message_marks_automation_done_when_cap_hit():
    position = {
        "dry_run": 1, "contract": "NIFTY 25300 CE 24 July", "exit_reason": "SL_HIT",
        "entry_premium": 120.0, "exit_premium": 95.0, "realized_pnl": -1875.0,
    }
    text = format_exit_message(position, trades_today=2, max_trades=2)
    assert "automation done for today" in text


# --------------------------------------------------------------------------
# Safety invariant: only SELL for an already-open position, never to open
# --------------------------------------------------------------------------


def test_entry_never_sends_sell(real_db):
    """maybe_execute_entry only ever calls place_market_order with BUY."""
    mock_client = MagicMock()
    order_result = {"ok": True, "order_id": "AO789", "raw": {}, "error": None}
    calls = []

    def _record(*args, **kwargs):
        calls.append(kwargs.get("transactiontype"))
        return order_result

    with (
        patch("skills.options_auto_trader.route_message"),
        patch("skills.options_auto_trader.ensure_session", return_value={"jwt_token": "j"}),
        patch("skills.options_auto_trader.build_client", return_value=mock_client),
        patch("skills.options_auto_trader.place_market_order", side_effect=_record),
        patch("skills.options_auto_trader.get_order_status", return_value={"averageprice": "121.5"}),
    ):
        maybe_execute_entry(_payload(), _contracts(), _config(auto_trading_dry_run=False), TODAY)

    assert calls == ["BUY"]
