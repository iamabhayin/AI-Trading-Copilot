"""Options Auto-Trader (Phase 17) — turns a fully-gated BUY_CE_CANDIDATE/
BUY_PE_CANDIDATE advisory into a real (or simulated) AngelOne order.

BUY CE/PE only, never SELL to open (see angel_order_client.py's module
docstring for where that invariant is enforced). Loss is capped to the
premium paid by construction (a long option's max loss is always the
premium paid — no separate mechanism needed for that), but a MARKET SL
exit can still fail to fill; `auto_trading_squareoff_time` and
`force_close_stale_positions` are the backstops against a position
surviving past this session.

Reuses everything the Options Engine already computed rather than
re-deriving it: `option_stop`/`target_1` are the trade-selector's own
structure-derived exit levels (rulebook 14-step framework), `contracts`
is the already-fetched instruments-master filter, `analysis["chain"]` is
the already-fetched premium data for the position-monitor pass to read
from.

Design mirrors options_position_entry.py/options_position_monitor.py:
pure guard/decision functions first, DB/network-touching functions at the
bottom. Every entry/exit notification goes to the `monitoring` Discord
channel (the user's explicit choice, distinct from the advisory-only
`option_trading` channel `notify_advisory`/`notify_position_update` use).
"""

import sys
import uuid
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.options_config import OptionsConfig
from db.database import execute, fetch_all, fetch_one
from skills.angel_client import (
    AngelAuthError,
    build_client,
    ensure_session,
    fetch_market_data,
    format_expiry_readable,
)
from skills.angel_order_client import OrderPlacementError, get_order_status, place_market_order
from skills.notify_skill import route_message
from skills.options_data_fetch import DataInsufficientError

_EXIT_REASON_LABELS = {
    "TARGET_HIT": "target hit",
    "SL_HIT": "stop-loss hit",
    "FORCED_SQUAREOFF": "forced same-day square-off",
}


def _safe_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Pure guards / decisions
# --------------------------------------------------------------------------


def is_automation_enabled(config: OptionsConfig) -> bool:
    return config.auto_trading_enabled


def count_trades_today(today: date) -> int:
    row = fetch_one("SELECT COUNT(*) AS cnt FROM auto_options_positions WHERE trade_date = ?", (today.isoformat(),))
    return row["cnt"] if row else 0


def has_open_position() -> dict | None:
    row = fetch_one("SELECT * FROM auto_options_positions WHERE status = 'open' LIMIT 1")
    return dict(row) if row else None


def can_open_new_trade(config: OptionsConfig, today: date) -> bool:
    """Master entry gate: automation on, no position already open (trades
    are sequential -- never concurrent), and the daily cap not yet hit."""
    if not is_automation_enabled(config):
        return False
    if has_open_position() is not None:
        return False
    return count_trades_today(today) < config.auto_trading_max_trades_per_day


def compute_lots(entry_premium: float, lot_size: int, budget: float) -> int:
    """floor(budget / cost of 1 lot) -- the budget is a hard cap, never
    rounded up to afford a lot it can't actually cover."""
    if not entry_premium or not lot_size:
        return 0
    cost_per_lot = entry_premium * lot_size
    if cost_per_lot <= 0:
        return 0
    return int(budget // cost_per_lot)


def resolve_contract_row(contracts: list[dict], expiry: str, strike: float, side: str) -> dict | None:
    """Same matching rule as angel_client.resolve_symboltoken, but returns
    the full contract row -- order placement needs both `symbol`
    (tradingsymbol) and `token` (symboltoken), not just the token."""
    for contract in contracts:
        if (
            contract.get("expiry") == expiry
            and float(contract.get("strike", -1)) / 100 == strike
            and contract.get("symbol", "").endswith(side)
        ):
            return contract
    return None


def decide_exit(position: dict, current_premium: float, now: datetime, config: OptionsConfig) -> str | None:
    """Priority: SL (risk-first) > target > forced same-day square-off.
    Only one of SL/target can ever be true at once for a long option
    (option_stop < entry_premium < target_1 by construction), so the
    order between those two never matters in practice."""
    if current_premium <= position["option_stop"]:
        return "SL_HIT"
    if current_premium >= position["target_1"]:
        return "TARGET_HIT"
    squareoff_time = datetime.strptime(config.auto_trading_squareoff_time, "%H:%M").time()
    if now.time() >= squareoff_time:
        return "FORCED_SQUAREOFF"
    return None


# --------------------------------------------------------------------------
# Discord message formatting -- deterministic, no LLM (same rationale as
# options_trade_selector._template_advisory_message: a dropped number in a
# financial message is not a cosmetic issue).
# --------------------------------------------------------------------------


def format_entry_message(position: dict, trades_today: int, max_trades: int) -> str:
    dry_run_tag = " [DRY RUN]" if position["dry_run"] else ""
    lots, lot_size = position["qty_lots"], position["lot_size"]
    lines = [
        f"\U0001F916 AUTO-TRADE ENTRY{dry_run_tag}",
        f"BUY {position['contract']}",
        f"Qty: {lots} lot(s) ({lots * lot_size} qty)",
        f"Entry price: Rs {position['entry_premium']:,.2f}",
        f"Market trend: {position.get('trend_label') or 'n/a'}",
        f"Stop-loss: Rs {position['option_stop']:,.2f}  |  Target: Rs {position['target_1']:,.2f}",
        f"Order ID: {position['entry_order_id']}",
        f"Trade {trades_today}/{max_trades} today",
    ]
    return "\n".join(lines)


def format_exit_message(position: dict, trades_today: int, max_trades: int) -> str:
    dry_run_tag = " [DRY RUN]" if position["dry_run"] else ""
    reason_label = _EXIT_REASON_LABELS.get(position["exit_reason"], position["exit_reason"])
    entry_premium, exit_premium = position["entry_premium"], position["exit_premium"]
    pnl = position["realized_pnl"]
    pnl_pct = (exit_premium - entry_premium) / entry_premium * 100 if entry_premium else None
    lines = [
        f"\U0001F916 AUTO-TRADE EXIT{dry_run_tag} -- {reason_label.upper()}",
        position["contract"],
        f"Entry: Rs {entry_premium:,.2f} -> Exit: Rs {exit_premium:,.2f}",
        f"P&L: {'+' if pnl >= 0 else ''}Rs {pnl:,.2f}" + (f" ({pnl_pct:+.1f}%)" if pnl_pct is not None else ""),
        f"Trade {trades_today}/{max_trades} today"
        + (" -- automation done for today" if trades_today >= max_trades else " complete"),
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Entry
# --------------------------------------------------------------------------


def maybe_execute_entry(payload: dict, contracts: list[dict], config: OptionsConfig, today: date) -> None:
    """Called right after write_advisory() for a confirmed BUY_CE/PE
    candidate. A no-op (no Discord message) whenever automation can't or
    shouldn't act -- the advisory alert itself already posted, so a skip
    here is silent by design, not a failure."""
    if not can_open_new_trade(config, today):
        return

    entry_premium, lot_size = payload.get("entry_premium"), payload.get("lot_size")
    if not entry_premium or not lot_size:
        return

    lots = compute_lots(entry_premium, lot_size, config.auto_trading_budget_per_trade)
    if lots < 1:
        return

    contract_row = resolve_contract_row(contracts, payload["expiry"], payload["strike"], payload["side"])
    if contract_row is None:
        return

    tradingsymbol, symboltoken = contract_row["symbol"], contract_row["token"]
    quantity = lots * lot_size

    if config.auto_trading_dry_run:
        dry_run, order_id, fill_premium = True, f"DRYRUN-{uuid.uuid4()}", entry_premium
    else:
        try:
            session = ensure_session(config)
            client = build_client(config, session)
            result = place_market_order(
                client, tradingsymbol=tradingsymbol, symboltoken=symboltoken,
                transactiontype="BUY", quantity=quantity,
            )
        except (AngelAuthError, OrderPlacementError) as exc:
            route_message("monitoring", f"AUTO-TRADE ENTRY FAILED: {exc}", telegram_parse_mode=None)
            return

        if not result["ok"]:
            route_message("monitoring", f"AUTO-TRADE ENTRY REJECTED: {result['error']}", telegram_parse_mode=None)
            return

        dry_run, order_id = False, result["order_id"]
        order_status = get_order_status(client, order_id)
        fill_premium = _safe_float(order_status.get("averageprice")) if order_status else None
        fill_premium = fill_premium or entry_premium

    contract = f"NIFTY {payload['strike']:g} {payload['side']} {format_expiry_readable(payload['expiry'])}"
    execute(
        """
        INSERT INTO auto_options_positions
            (status, trade_date, contract, tradingsymbol, symboltoken, expiry_date, strike, side,
             qty_lots, lot_size, dry_run, entry_order_id, entry_premium, entry_spot,
             option_stop, target_1, trend_label)
        VALUES ('open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            today.isoformat(), contract, tradingsymbol, symboltoken, payload["expiry"], payload["strike"], payload["side"],
            lots, lot_size, int(dry_run), order_id, fill_premium, payload.get("spot"),
            payload["option_stop"], payload["target_1"], payload.get("market_regime"),
        ),
    )

    position = has_open_position()
    route_message(
        "monitoring", format_entry_message(position, count_trades_today(today), config.auto_trading_max_trades_per_day),
        telegram_parse_mode=None,
    )


# --------------------------------------------------------------------------
# Exit
# --------------------------------------------------------------------------


def _current_premium_from_chain(analysis: dict, position: dict) -> float | None:
    row = next(
        (r for r in analysis.get("chain", []) if r["strike"] == position["strike"] and r["side"] == position["side"]),
        None,
    )
    return row.get("ltp") if row else None


def _fetch_current_premium_live(config: OptionsConfig, symboltoken: str) -> float | None:
    """Standalone LTP fetch for when a position's strike falls outside
    this cycle's already-fetched chain window -- rare (see
    run_position_monitor_pass's identical edge case), but a real-money
    exit check should not silently skip a cycle just because of that."""
    try:
        session = ensure_session(config)
        client = build_client(config, session)
        market_data = fetch_market_data(client, [symboltoken])
    except (AngelAuthError, DataInsufficientError):
        return None
    row = market_data.get(symboltoken)
    return _safe_float(row.get("ltp")) if row else None


def _execute_exit(position: dict, exit_reason: str, current_premium: float | None, config: OptionsConfig, today: date) -> None:
    quantity = position["qty_lots"] * position["lot_size"]

    if position["dry_run"]:
        order_id = f"DRYRUN-{uuid.uuid4()}"
        exit_premium = current_premium if current_premium is not None else position["entry_premium"]
    else:
        try:
            session = ensure_session(config)
            client = build_client(config, session)
            result = place_market_order(
                client, tradingsymbol=position["tradingsymbol"], symboltoken=position["symboltoken"],
                transactiontype="SELL", quantity=quantity,
            )
        except (AngelAuthError, OrderPlacementError) as exc:
            route_message("monitoring", f"AUTO-TRADE EXIT FAILED for {position['contract']}: {exc}", telegram_parse_mode=None)
            return

        if not result["ok"]:
            route_message(
                "monitoring", f"AUTO-TRADE EXIT REJECTED for {position['contract']}: {result['error']}",
                telegram_parse_mode=None,
            )
            return

        order_id = result["order_id"]
        order_status = get_order_status(client, order_id)
        exit_premium = _safe_float(order_status.get("averageprice")) if order_status else None
        exit_premium = exit_premium or current_premium or position["entry_premium"]

    realized_pnl = (exit_premium - position["entry_premium"]) * quantity

    execute(
        """
        UPDATE auto_options_positions
        SET status = 'closed', exit_order_id = ?, exit_premium = ?, exit_reason = ?,
            realized_pnl = ?, closed_ts = datetime('now')
        WHERE id = ?
        """,
        (order_id, exit_premium, exit_reason, realized_pnl, position["id"]),
    )

    closed_position = fetch_one("SELECT * FROM auto_options_positions WHERE id = ?", (position["id"],))
    route_message(
        "monitoring",
        format_exit_message(dict(closed_position), count_trades_today(today), config.auto_trading_max_trades_per_day),
        telegram_parse_mode=None,
    )


def run_auto_exit_pass(analysis: dict, config: OptionsConfig, now: datetime, today: date) -> None:
    """Called every cycle alongside run_position_monitor_pass. At most one
    open automated position exists at a time by construction
    (can_open_new_trade blocks a second entry)."""
    if not is_automation_enabled(config):
        return

    position = has_open_position()
    if position is None:
        return

    current_premium = _current_premium_from_chain(analysis, position)
    if current_premium is None:
        current_premium = _fetch_current_premium_live(config, position["symboltoken"])
    if current_premium is None:
        route_message(
            "monitoring",
            f"AUTO-TRADE WARNING: could not fetch current premium for open position {position['contract']} this cycle.",
            telegram_parse_mode=None,
        )
        return

    exit_reason = decide_exit(position, current_premium, now, config)
    if exit_reason is None:
        return

    _execute_exit(position, exit_reason, current_premium, config, today)


def force_close_stale_positions(config: OptionsConfig, today: date) -> None:
    """EOD safety net (run_end_of_day) on top of the 15:15 in-cycle pass
    and AngelOne's own INTRADAY auto-square-off: closes anything still
    'open', in case both of those didn't fire for any reason.

    Deliberately does NOT restrict to trade_date == today: has_open_position()
    has no date filter either (status='open' only), so a position that
    somehow survived a prior day's close would otherwise sit 'open' in the
    DB forever -- silently blocking every future can_open_new_trade() check
    (at most one open position at a time, by construction) with no trade
    ever executing again. Automation being intraday-only depends on this
    never leaving a stale row behind, regardless of which day opened it."""
    if not is_automation_enabled(config):
        return

    position = has_open_position()
    if position is None:
        return

    if position["trade_date"] != today.isoformat():
        route_message(
            "monitoring",
            f"AUTO-TRADE WARNING: force-closing {position['contract']} left open since "
            f"{position['trade_date']} -- both that day's squareoff and EOD backstop must have failed.",
            telegram_parse_mode=None,
        )

    current_premium = _fetch_current_premium_live(config, position["symboltoken"])
    _execute_exit(position, "FORCED_SQUAREOFF", current_premium, config, today)
