"""Tests for skills/angel_order_client.py — Phase 17 order placement.

No live network/API calls: the SmartConnect client is always a MagicMock.
"""

from unittest.mock import MagicMock

import pytest
from SmartApi.smartExceptions import (
    InputException,
    OrderException,
    PermissionException,
    SmartAPIException,
    TokenException,
)

from skills.angel_order_client import OrderPlacementError, get_order_status, place_market_order


def test_place_market_order_rejects_invalid_transactiontype():
    client = MagicMock()
    with pytest.raises(ValueError):
        place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="SHORT", quantity=75)


def test_place_market_order_builds_intraday_market_orderparams():
    client = MagicMock()
    client.placeOrderFullResponse.return_value = {"status": True, "data": {"orderid": "AO1"}}

    place_market_order(client, tradingsymbol="NIFTY24JUL2525300CE", symboltoken="45526", transactiontype="BUY", quantity=75)

    orderparams = client.placeOrderFullResponse.call_args[0][0]
    assert orderparams["ordertype"] == "MARKET"
    assert orderparams["producttype"] == "INTRADAY"
    assert orderparams["transactiontype"] == "BUY"
    assert orderparams["exchange"] == "NFO"
    assert orderparams["quantity"] == "75"
    assert orderparams["tradingsymbol"] == "NIFTY24JUL2525300CE"
    assert orderparams["symboltoken"] == "45526"


def test_place_market_order_success():
    client = MagicMock()
    client.placeOrderFullResponse.return_value = {"status": True, "data": {"orderid": "AO1"}}

    result = place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)

    assert result == {"ok": True, "order_id": "AO1", "raw": {"status": True, "data": {"orderid": "AO1"}}, "error": None}


def test_place_market_order_status_false_returns_not_ok():
    client = MagicMock()
    client.placeOrderFullResponse.return_value = {"status": False, "message": "insufficient margin"}

    result = place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)

    assert result["ok"] is False
    assert result["order_id"] is None
    assert result["error"] == "insufficient margin"


def test_place_market_order_missing_orderid_returns_not_ok():
    client = MagicMock()
    client.placeOrderFullResponse.return_value = {"status": True, "data": {}}

    result = place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)

    assert result["ok"] is False


def test_place_market_order_empty_response_returns_not_ok():
    client = MagicMock()
    client.placeOrderFullResponse.return_value = None

    result = place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)

    assert result["ok"] is False
    assert "empty response" in result["error"]


def test_place_market_order_order_exception_returns_not_ok_not_raised():
    """OrderException/InputException are broker-side rejections (e.g. bad
    quantity, insufficient margin) -- these must be reported back to the
    caller, never silently retried (see module docstring)."""
    client = MagicMock()
    client.placeOrderFullResponse.side_effect = OrderException("margin exceeded")

    result = place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)

    assert result["ok"] is False
    assert "margin exceeded" in result["error"]


def test_place_market_order_input_exception_returns_not_ok():
    client = MagicMock()
    client.placeOrderFullResponse.side_effect = InputException("bad quantity")

    result = place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)

    assert result["ok"] is False


def test_place_market_order_token_exception_raises_order_placement_error():
    client = MagicMock()
    client.placeOrderFullResponse.side_effect = TokenException("session expired")

    with pytest.raises(OrderPlacementError):
        place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)


def test_place_market_order_permission_exception_raises_order_placement_error():
    client = MagicMock()
    client.placeOrderFullResponse.side_effect = PermissionException("not permitted")

    with pytest.raises(OrderPlacementError):
        place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)


def test_place_market_order_generic_smartapi_exception_raises_order_placement_error():
    client = MagicMock()
    client.placeOrderFullResponse.side_effect = SmartAPIException("network blip")

    with pytest.raises(OrderPlacementError):
        place_market_order(client, tradingsymbol="X", symboltoken="1", transactiontype="BUY", quantity=75)


def test_get_order_status_finds_matching_order():
    client = MagicMock()
    client.orderBook.return_value = {
        "status": True,
        "data": [{"orderid": "AO1", "averageprice": "121.5"}, {"orderid": "AO2", "averageprice": "80.0"}],
    }

    status = get_order_status(client, "AO2")

    assert status == {"orderid": "AO2", "averageprice": "80.0"}


def test_get_order_status_returns_none_when_not_found():
    client = MagicMock()
    client.orderBook.return_value = {"status": True, "data": [{"orderid": "AO1", "averageprice": "121.5"}]}

    assert get_order_status(client, "AO999") is None


def test_get_order_status_returns_none_on_failed_book_fetch():
    client = MagicMock()
    client.orderBook.return_value = {"status": False}

    assert get_order_status(client, "AO1") is None


def test_get_order_status_raises_on_smartapi_exception():
    client = MagicMock()
    client.orderBook.side_effect = SmartAPIException("boom")

    with pytest.raises(OrderPlacementError):
        get_order_status(client, "AO1")
