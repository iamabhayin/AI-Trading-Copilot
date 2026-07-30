"""Angel One SmartAPI order-placement client (Phase 17 auto-trading).

Deliberate split from `angel_client.py`, whose docstring promises "DATA
ONLY: this module never places an order" — that boundary stays true, and
this module is the only place in the repo that calls `placeOrder`/
`placeOrderFullResponse`.

Safety note this module is built around: order placement is NEVER retried
on failure/timeout, unlike `angel_client.py`'s `_call_with_backoff` for
market-data reads. A data-fetch retry is harmless; a blind retry on an
order call risks a duplicate fill if the first attempt actually succeeded
broker-side but the response was lost. A failed call here always means
"treat the trade as not placed and let the caller decide" — never
auto-retry.

Only `options_auto_trader.py` calls this module, and only with
transactiontype="BUY" to open a position or "SELL" to close one it already
holds — there is no code path here that can open a short position.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from SmartApi import SmartConnect
from SmartApi.smartExceptions import (
    InputException,
    OrderException,
    PermissionException,
    SmartAPIException,
    TokenException,
)


class OrderPlacementError(Exception):
    """Raised when an order call fails outright (network/SDK error) rather
    than returning a normal rejected-order response. Callers must treat
    this as "unknown whether the order reached the broker" and never
    retry automatically."""


def place_market_order(
    client: SmartConnect,
    *,
    tradingsymbol: str,
    symboltoken: str,
    transactiontype: str,
    quantity: int,
    exchange: str = "NFO",
) -> dict:
    """Places a single MARKET/INTRADAY order and returns a normalized
    result. `producttype=INTRADAY` is deliberate: it makes AngelOne itself
    auto-square-off the position near session close as a second backstop,
    on top of the engine's own 15:15 forced-exit pass. MARKET (not LIMIT)
    is deliberate too — a limit stop-loss order can fail to fill in a fast
    market, which would blow through the "loss capped to premium paid"
    guarantee.

    Never retried — see module docstring.
    """
    if transactiontype not in ("BUY", "SELL"):
        raise ValueError(f"transactiontype must be BUY or SELL, got {transactiontype!r}")

    orderparams = {
        "variety": "NORMAL",
        "tradingsymbol": tradingsymbol,
        "symboltoken": symboltoken,
        "transactiontype": transactiontype,
        "exchange": exchange,
        "ordertype": "MARKET",
        "producttype": "INTRADAY",
        "duration": "DAY",
        "quantity": str(quantity),
    }

    try:
        response = client.placeOrderFullResponse(orderparams)
    except (TokenException, PermissionException) as exc:
        raise OrderPlacementError(f"AngelOne auth/permission error placing order: {exc}") from exc
    except (OrderException, InputException) as exc:
        return {"ok": False, "order_id": None, "raw": None, "error": str(exc)}
    except SmartAPIException as exc:
        raise OrderPlacementError(f"AngelOne order call failed: {exc}") from exc

    if not response:
        return {"ok": False, "order_id": None, "raw": response, "error": "empty response from placeOrderFullResponse"}

    status_ok = bool(response.get("status"))
    order_id = (response.get("data") or {}).get("orderid") if isinstance(response.get("data"), dict) else None
    if not status_ok or not order_id:
        return {
            "ok": False,
            "order_id": order_id,
            "raw": response,
            "error": response.get("message") or "order not confirmed by AngelOne",
        }

    return {"ok": True, "order_id": order_id, "raw": response, "error": None}


def get_order_status(client: SmartConnect, order_id: str) -> dict | None:
    """Looks up a single order's current status/fill details from the
    order book. Returns None if the order id isn't found (order book is
    the source of truth for whether/at what price a MARKET order actually
    filled)."""
    try:
        book = client.orderBook()
    except SmartAPIException as exc:
        raise OrderPlacementError(f"AngelOne order book fetch failed: {exc}") from exc

    if not book or not book.get("status"):
        return None

    for order in book.get("data") or []:
        if order.get("orderid") == order_id:
            return order
    return None
