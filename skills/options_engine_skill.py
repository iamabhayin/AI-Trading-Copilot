"""Options Engine Skill (Phase 15 Task 7) — the cron entry point that
orchestrates one full cycle end-to-end: fetch live data -> analytics ->
rules engine (regime, proximity, state machine, confirmation) -> trade
selection -> position monitoring -> persistence -> notifications.

This is the only module in the Option Trading feature that ties the pure
modules (options_analytics.py, options_rules_engine.py,
options_trade_selector.py, options_position_monitor.py) to live I/O
(Angel One, yfinance, SQLite, Discord/Telegram) and to wall-clock time.
Every pure decision still happens in those modules; this file is glue —
fetch data, build the plain-data inputs those modules expect, call them,
act on what they return.

Dual cadence off a single cron entry (see SCHEDULING in the Phase 15
spec): meant to be invoked every 1 minute, 09:15-15:30 IST Mon-Fri.
`should_run_normal_cycle()` makes NORMAL-mode invocations a fast no-op
except on 5-minute boundaries; WATCH-mode invocations always run.
`acquire_lock`/`release_lock` guard against overlapping runs if a cycle
runs long. A separate `--eod` invocation handles end-of-day snapshot
cleanup and state reset.

Nothing here is registered on the OpenClaw Gateway yet — that's Task 8,
pending your explicit approval of the `openclaw cron add` command.
"""

import sys
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.options_config import OptionsConfig
from db.database import execute, fetch_all, fetch_one
from skills.angel_client import (
    AngelAuthError,
    build_client,
    ensure_instruments_master,
    ensure_session,
    fetch_expiry_chain,
    filter_nifty_option_contracts,
    list_expiries,
    run_market_data_cycle,
    select_next_weekly_expiry,
    select_weekly_and_monthly_expiry,
)
from skills.notify_skill import route_message
from skills.options_analytics import (
    build_market_analysis,
    compute_atr,
    detect_next_level,
    detect_zone_merge,
    years_to_expiry,
)
from skills.options_data_fetch import (
    DataInsufficientError,
    cleanup_old_snapshots,
    fetch_india_vix_with_change,
    fetch_nifty_candles,
    fetch_nifty_spot_backup,
)
from skills.options_position_monitor import (
    build_position_evidence,
    evaluate_position,
    notify_position_update,
    record_notified_state,
    should_notify,
)
from skills.options_rules_engine import (
    check_level_strike_oi_behavior,
    classify_regime,
    decide_transition,
    detect_price_trend,
    evaluate_breakout_confirmation,
    evaluate_market_rejections,
    has_crossed_level,
    score_confirmation,
    score_delta_oi,
    score_iv_greeks,
    score_oi_structure,
    score_pcr,
    score_setup,
    score_trend_pa,
    score_volume,
    summarize_confirmation_evidence,
)
from skills.options_trade_selector import (
    build_advisory_payload,
    notify_advisory,
    score_liquidity,
    score_rr,
    select_trade,
    write_advisory,
)

IST = ZoneInfo("Asia/Kolkata")


# --------------------------------------------------------------------------
# Lock (guards against overlapping cron runs)
# --------------------------------------------------------------------------


def acquire_lock(config: OptionsConfig, now: datetime) -> bool:
    """Lightweight lockfile guard. Returns True if the lock was acquired
    (safe to proceed), False if another run is still active per
    `config.lock_staleness_seconds` — a stale lock (crashed prior run)
    is treated as free."""
    lock_path = Path(config.engine_lock_path)
    if lock_path.exists():
        try:
            held_since = datetime.fromisoformat(lock_path.read_text().strip())
        except (ValueError, OSError):
            held_since = None
        if held_since and (now - held_since).total_seconds() < config.lock_staleness_seconds:
            return False
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(now.isoformat())
    return True


def release_lock(config: OptionsConfig) -> None:
    Path(config.engine_lock_path).unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Market-hours guard (defense in depth -- cron can't cleanly express the
# exact 09:15-15:30 IST window in one entry, so this makes sure a
# misfired/misconfigured cron entry can never trigger a live data fetch or
# advisory outside NSE trading hours)
# --------------------------------------------------------------------------

MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)


def is_market_hours(now: datetime) -> bool:
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


# --------------------------------------------------------------------------
# Dual cadence
# --------------------------------------------------------------------------


def should_run_normal_cycle(engine_state: dict, now: datetime, config: OptionsConfig) -> bool:
    """WATCH mode always runs. NORMAL mode only runs the full cycle on a
    5-minute boundary or if >=5 minutes have passed since the last run —
    everything else is a fast no-op, giving dual cadence off one 1-min
    cron entry."""
    if engine_state.get("mode") == "WATCH":
        return True
    last_run_ts = engine_state.get("last_run_ts")
    if not last_run_ts:
        return True
    if now.minute % 5 == 0:
        return True
    last_run = datetime.fromisoformat(last_run_ts)
    return (now - last_run).total_seconds() >= 300


# --------------------------------------------------------------------------
# Engine state persistence
# --------------------------------------------------------------------------


def get_engine_state() -> dict:
    row = fetch_one("SELECT mode, watch_level, watch_direction, watch_started_ts, last_run_ts FROM options_engine_state WHERE id = 1")
    return dict(row) if row else {"mode": "NORMAL"}


def save_engine_state(next_state: dict, last_run_ts: str) -> None:
    execute(
        """UPDATE options_engine_state
           SET mode = ?, watch_level = ?, watch_direction = ?, watch_started_ts = ?,
               last_run_ts = ?, updated_ts = datetime('now')
           WHERE id = 1""",
        (
            next_state.get("mode", "NORMAL"),
            next_state.get("watch_level"),
            next_state.get("watch_direction"),
            next_state.get("watch_started_ts"),
            last_run_ts,
        ),
    )


# --------------------------------------------------------------------------
# Historical snapshot queries (for options_analytics.annotate_change_in_oi)
# --------------------------------------------------------------------------


def _fetch_snapshot_rows(expiry_date: str, snapshot_ts: str) -> list[dict]:
    rows = fetch_all(
        """SELECT strike, side, ltp, oi, volume, bid, ask, iv, delta, gamma, theta, vega
           FROM option_chain_snapshots WHERE expiry_date = ? AND snapshot_ts = ?""",
        (expiry_date, snapshot_ts),
    )
    return [dict(row) for row in rows]


def fetch_previous_snapshot(expiry_date: str, trading_date: str, before_ts: str) -> list[dict]:
    row = fetch_one(
        "SELECT MAX(snapshot_ts) AS ts FROM option_chain_snapshots WHERE expiry_date = ? AND trading_date = ? AND snapshot_ts < ?",
        (expiry_date, trading_date, before_ts),
    )
    return _fetch_snapshot_rows(expiry_date, row["ts"]) if row and row["ts"] else []


def fetch_first_of_day_snapshot(expiry_date: str, trading_date: str) -> list[dict]:
    row = fetch_one(
        "SELECT MIN(snapshot_ts) AS ts FROM option_chain_snapshots WHERE expiry_date = ? AND trading_date = ?",
        (expiry_date, trading_date),
    )
    return _fetch_snapshot_rows(expiry_date, row["ts"]) if row and row["ts"] else []


def fetch_prior_day_last_snapshot(expiry_date: str, trading_date: str) -> list[dict]:
    day_row = fetch_one(
        "SELECT trading_date FROM option_chain_snapshots WHERE expiry_date = ? AND trading_date < ? ORDER BY trading_date DESC LIMIT 1",
        (expiry_date, trading_date),
    )
    if not day_row:
        return []
    ts_row = fetch_one(
        "SELECT MAX(snapshot_ts) AS ts FROM option_chain_snapshots WHERE expiry_date = ? AND trading_date = ?",
        (expiry_date, day_row["trading_date"]),
    )
    return _fetch_snapshot_rows(expiry_date, ts_row["ts"]) if ts_row and ts_row["ts"] else []


# --------------------------------------------------------------------------
# Live data fetch for one cycle
# --------------------------------------------------------------------------


def get_greeks_sanity_checked_date() -> str | None:
    row = fetch_one("SELECT greeks_sanity_checked_date FROM options_engine_state WHERE id = 1")
    return row["greeks_sanity_checked_date"] if row else None


def mark_greeks_sanity_checked(today: date) -> None:
    execute("UPDATE options_engine_state SET greeks_sanity_checked_date = ? WHERE id = 1", (today.isoformat(),))


def fetch_cycle_data(config: OptionsConfig, today: date) -> dict:
    """All live I/O for one cycle. Raises DataInsufficientError/
    AngelAuthError/GreeksSanityError on failure — the caller aborts the
    cycle rather than acting on partial/suspect data.

    The weekly-vs-monthly Greeks sanity check only needs to pass once per
    trading day (it guards a persistent SDK bug, not a per-cycle
    condition) — skipped here once `options_engine_state.
    greeks_sanity_checked_date` already matches today, roughly halving
    optionGreek call volume during WATCH mode's 1-min cadence.
    """
    instruments = ensure_instruments_master(config)
    contracts = filter_nifty_option_contracts(instruments)
    available_expiries = list_expiries(contracts)
    if not available_expiries:
        raise DataInsufficientError("No listed NIFTY option expiries available")

    weekly_expiry, monthly_expiry = select_weekly_and_monthly_expiry(available_expiries, today)
    skip_sanity_check = get_greeks_sanity_checked_date() == today.isoformat()
    cycle = run_market_data_cycle(
        config,
        weekly_expiry,
        monthly_expiry,
        trading_date=today.isoformat(),
        skip_greeks_sanity_check=skip_sanity_check,
    )
    if cycle.get("greeks_sanity_checked"):
        mark_greeks_sanity_checked(today)

    candles_5m = fetch_nifty_candles(interval="5m", period="5d")
    candles_1m = fetch_nifty_candles(interval="1m", period="1d")

    return {
        "spot": cycle["spot"],
        "expiry": cycle["expiry"],
        "chain": cycle["chain"],
        "contracts": contracts,
        "trading_date": cycle["trading_date"],
        "snapshot_ts": cycle["snapshot_ts"],
        "candles_5m": candles_5m,
        "candles_1m": candles_1m,
    }


# --------------------------------------------------------------------------
# Watch-mode / false-breakout notifications (rules_engine stays pure, so
# these live here — the orchestrator is where notify calls belong)
# --------------------------------------------------------------------------


def _format_labeled_levels(analysis: dict) -> list[str]:
    """Support/resistance as individually labeled lines — OI wall
    (strike-based, from the option chain) vs. price structure (swing
    high/low from candles) — instead of one unlabeled merged list that
    silently mixes round strike numbers with raw traded prices. Fallback
    rendering used only when chain/config aren't available for the full
    Part B zone-aware body (_build_rich_alert_body)."""
    # NOTE: price-structure values use :.2f, not :g -- :g's default 6
    # significant figures silently truncates the decimals on any 5-digit
    # NIFTY price (e.g. 25260.123456 -> "25260.1", dropping a real digit).
    oi_levels = analysis.get("oi_levels") or {}
    price_levels = analysis.get("price_levels") or {}
    lines = []
    for level in oi_levels.get("support") or []:
        lines.append(f"Support (OI wall): {level:g}")
    if price_levels.get("swing_low") is not None:
        lines.append(f"Support (price structure): {price_levels['swing_low']:.2f}")
    for level in oi_levels.get("resistance") or []:
        lines.append(f"Resistance (OI wall): {level:g}")
    if price_levels.get("swing_high") is not None:
        lines.append(f"Resistance (price structure): {price_levels['swing_high']:.2f}")
    return lines


def _format_alert_timestamps(data_ts: datetime | None, sent_ts: datetime | None) -> list[str]:
    """data_ts (chain snapshot time) vs sent_ts (alert delivery time) --
    lets a reader see for themselves how stale an alert's underlying data
    was by the time it arrived (alert-staleness fix, Part A1/A3)."""
    if data_ts is None and sent_ts is None:
        return []
    lines = [""]
    if data_ts is not None:
        lines.append(f"Data as of: {data_ts:%H:%M:%S} IST")
    if sent_ts is not None:
        lines.append(f"Sent at: {sent_ts:%H:%M:%S} IST")
    return lines


def _spot_refetch_warning(spot_refetch_ok: bool) -> list[str]:
    if spot_refetch_ok:
        return []
    return ["", "WARNING: spot re-check failed -- data may be stale"]


def signed_distance_pts(spot: float, level: float, direction: str) -> float:
    """Signed distance from spot to level: positive = hasn't happened yet
    (still needs to move this many points to reach the level), negative =
    already past it by this many points (Part B1 rule 1 -- the original
    incident rounded a real -7pt breach to "0.00%", actively hiding it;
    showing signed points first fixes that regardless of rounding)."""
    return level - spot if direction == "UP" else spot - level


def _format_verdict_line(verdict: str, reason: str) -> str:
    return f"VERDICT: {verdict} -- {reason}"


def _format_valid_until(valid_until: datetime | None) -> list[str]:
    if valid_until is None:
        return []
    return [f"Valid until: {valid_until:%H:%M:%S} IST (next scan)"]


def _format_delta_oi_lines(level: float, primary_side: str, chain: list[dict], previous_snapshot: list[dict] | None) -> list[str]:
    """ΔOI at the level strike, both sides, in lakhs (Part B1 rules 3-4).
    Only `primary_side` (matching the watch direction -- PE for
    support/DOWN, CE for resistance/UP) gets the plain-language writers
    tag: OI increasing = fresh writing defending the level, OI decreasing
    = writers closing (level weakening). The opposite side is shown for
    context only, no tag."""
    opposite_side = "CE" if primary_side == "PE" else "PE"
    lines = []
    for side in (primary_side, opposite_side):
        current = next((r for r in chain if r["strike"] == level and r["side"] == side), None)
        previous = next((r for r in (previous_snapshot or []) if r["strike"] == level and r["side"] == side), None)
        if not current or not previous:
            continue
        current_oi, previous_oi = current.get("oi"), previous.get("oi")
        if current_oi is None or previous_oi is None:
            continue
        change_lakhs = (current_oi - previous_oi) / 100_000
        line = f"  {level:g} {side}: {change_lakhs:+.1f}L"
        if side == primary_side:
            if change_lakhs > 0:
                line += "  -> writers DEFENDING the wall"
            elif change_lakhs < 0:
                line += "  -> writers UNWINDING"
        lines.append(line)
    return lines


def _format_confirmation_checklist(confirmation: dict | None) -> list[str]:
    """Confirmation checklist -- rulebook Sections 32/33's existing
    evaluate_breakout_confirmation() evidence fields (Part B1 rule 5).
    confirmation=None (e.g. the exact cycle a WATCH just started, before
    any confirmation cycle has run) renders every item pending.

    PCR/VIX are deliberately NOT checklist items here -- rulebook Section
    29 / Rule 22: "PCR is supporting evidence, not a signal", "never use
    PCR alone to generate a trade". They're shown elsewhere in the alert
    as context, never as a required gate, so this checklist never
    contradicts that rule."""
    items = [
        ("Price crossed the level", confirmation.get("crossed") if confirmation else None),
        ("Sustained beyond the level", confirmation.get("sustained") if confirmation else None),
        ("Volume confirms the move", confirmation.get("volume_confirmed") if confirmation else None),
        ("Level-strike OI supports the move", confirmation.get("oi_supports") if confirmation else None),
        ("Broader trend structure agrees", confirmation.get("structure_agrees") if confirmation else None),
    ]
    if confirmation and confirmation.get("retest") is not None:
        items.append(("Retest of the level held", confirmation.get("retest")))

    lines = ["CONFIRMATION CHECKLIST (all required):"]
    for label, value in items:
        if value is None:
            marker = "⏳"  # pending
        elif value:
            marker = "✅"  # met
        else:
            marker = "❌"  # failed
        lines.append(f"  {marker}  {label}")
    return lines


def _build_rich_alert_body(
    level: float,
    direction: str,
    spot: float,
    analysis: dict,
    chain: list[dict] | None,
    previous_snapshot: list[dict] | None,
    confirmation: dict | None,
    config: OptionsConfig | None,
) -> list[str]:
    """Zone/next-level/PCR+VIX/ΔOI/confirmation-checklist block shared by
    every WATCH-mode alert variant (WATCHING, BREACH_UNCONFIRMED,
    FALSE_BREAKOUT). Degrades gracefully section-by-section when
    chain/config aren't supplied, rather than forking into a separate
    "simple" template -- one rendering path, always."""
    option_side = "PE" if direction == "DOWN" else "CE"
    zone_label = "SUPPORT" if direction == "DOWN" else "RESISTANCE"
    opposite_label = "Resistance" if direction == "DOWN" else "Support"

    oi_levels = analysis.get("oi_levels") or {}
    price_levels = analysis.get("price_levels") or {}
    structure_level = price_levels.get("swing_low") if direction == "DOWN" else price_levels.get("swing_high")

    lines = [f"Regime: {analysis.get('regime')}"]

    zone = detect_zone_merge(level, structure_level, spot, config) if config is not None else None
    if zone:
        lines.append(f"{zone_label} ZONE: {zone['oi_wall']:g} (OI wall) -- {zone['width_points']:.0f} pts -- {zone['structure_level']:g} (structure)")
    else:
        lines.append(f"{zone_label}: {level:g} (OI wall)")
        if structure_level is not None:
            lines.append(f"{zone_label} (price structure): {structure_level:.2f}")

    if chain is not None and config is not None:
        next_level = detect_next_level(chain, option_side, level, direction, config)
        if next_level is not None:
            arrow_word = "below" if direction == "DOWN" else "above"
            lines.append(f"Next level {arrow_word}: {next_level:g} (next {option_side}-OI cluster)")

    opposite_key = "resistance" if direction == "DOWN" else "support"
    opposite_oi = (oi_levels.get(opposite_key) or [None])[0]
    if opposite_oi is not None:
        lines.append(f"{opposite_label}: {opposite_oi:g} (OI wall)")

    lines.append("")
    pcr = analysis.get("pcr")
    pcr_text = f"{pcr:.2f}" if pcr is not None else "n/a"
    vix_info = analysis.get("vix")
    vix_text = f"{vix_info['latest']:.1f} ({vix_info['change']:+.1f})" if vix_info else "n/a"
    lines.append(f"PCR: {pcr_text}        VIX: {vix_text}")

    if chain is not None:
        delta_oi_lines = _format_delta_oi_lines(level, option_side, chain, previous_snapshot)
        if delta_oi_lines:
            lines.append("")
            lines.append("Change in OI since last snapshot:")
            lines.extend(delta_oi_lines)

    lines.append("")
    lines.extend(_format_confirmation_checklist(confirmation))

    return lines


def format_watch_escalation_message(
    level: float,
    direction: str,
    spot: float,
    analysis: dict,
    data_ts: datetime | None = None,
    sent_ts: datetime | None = None,
    spot_refetch_ok: bool = True,
    chain: list[dict] | None = None,
    previous_snapshot: list[dict] | None = None,
    confirmation: dict | None = None,
    config: OptionsConfig | None = None,
    valid_until: datetime | None = None,
) -> str:
    side = "resistance" if direction == "UP" else "support"
    outcome = "bullish breakout -> CE" if direction == "UP" else "bearish breakdown -> PE"
    distance_pts = signed_distance_pts(spot, level, direction)
    distance_pct = distance_pts / level * 100
    lines = [
        f"WATCHING NIFTY {side.upper()}",
        "",
        f"Spot: {spot:,.1f} ({distance_pts:+.1f} pts / {distance_pct:+.2f}%)",
        f"Level: {level:g} {side}",
        f"Direction if confirmed: {outcome}",
        "",
        *_build_rich_alert_body(level, direction, spot, analysis, chain, previous_snapshot, confirmation, config),
        *_spot_refetch_warning(spot_refetch_ok),
        "",
        _format_verdict_line("WATCH", "level in proximity, monitoring for confirmation"),
        *_format_valid_until(valid_until),
        *_format_alert_timestamps(data_ts, sent_ts),
    ]
    return "\n".join(lines)


def notify_watch_escalation(
    level: float,
    direction: str,
    spot: float,
    analysis: dict,
    data_ts: datetime | None = None,
    sent_ts: datetime | None = None,
    spot_refetch_ok: bool = True,
    chain: list[dict] | None = None,
    previous_snapshot: list[dict] | None = None,
    confirmation: dict | None = None,
    config: OptionsConfig | None = None,
    valid_until: datetime | None = None,
) -> None:
    text = format_watch_escalation_message(
        level, direction, spot, analysis, data_ts, sent_ts, spot_refetch_ok,
        chain, previous_snapshot, confirmation, config, valid_until,
    )
    route_message("option_trading", text, send_telegram=False, telegram_parse_mode=None)


def format_breach_unconfirmed_message(
    level: float,
    direction: str,
    spot: float,
    analysis: dict,
    data_ts: datetime | None = None,
    sent_ts: datetime | None = None,
    spot_refetch_ok: bool = True,
    chain: list[dict] | None = None,
    previous_snapshot: list[dict] | None = None,
    confirmation: dict | None = None,
    config: OptionsConfig | None = None,
    valid_until: datetime | None = None,
) -> str:
    """Sent instead of the plain WATCHING alert when a fresh spot
    re-check (Part A1) shows the level was already crossed by send time
    (Part A2) -- a "WATCHING" message for a level the market has already
    moved through is wrong by definition."""
    side = "resistance" if direction == "UP" else "support"
    outcome = "bullish breakout -> CE" if direction == "UP" else "bearish breakdown -> PE"
    distance_pts = signed_distance_pts(spot, level, direction)
    distance_pct = distance_pts / level * 100
    lines = [
        f"LEVEL BREACHED -- AWAITING CONFIRMATION ({side.upper()})",
        "",
        f"Spot: {spot:,.1f} already past {level:g} {side} ({distance_pts:+.1f} pts / {distance_pct:+.2f}%)",
        f"Direction if confirmed: {outcome}",
        "",
        *_build_rich_alert_body(level, direction, spot, analysis, chain, previous_snapshot, confirmation, config),
        *_spot_refetch_warning(spot_refetch_ok),
        "",
        _format_verdict_line("BREACH-UNCONFIRMED", "level crossed, confirmation pending"),
        *_format_valid_until(valid_until),
        *_format_alert_timestamps(data_ts, sent_ts),
    ]
    return "\n".join(lines)


def notify_breach_unconfirmed(
    level: float,
    direction: str,
    spot: float,
    analysis: dict,
    data_ts: datetime | None = None,
    sent_ts: datetime | None = None,
    spot_refetch_ok: bool = True,
    chain: list[dict] | None = None,
    previous_snapshot: list[dict] | None = None,
    confirmation: dict | None = None,
    config: OptionsConfig | None = None,
    valid_until: datetime | None = None,
) -> None:
    text = format_breach_unconfirmed_message(
        level, direction, spot, analysis, data_ts, sent_ts, spot_refetch_ok,
        chain, previous_snapshot, confirmation, config, valid_until,
    )
    route_message("option_trading", text, send_telegram=False, telegram_parse_mode=None)


def format_false_breakout_message(
    level: float,
    direction: str,
    confirmation: dict,
    valid_until: datetime | None = None,
) -> str:
    side = "resistance" if direction == "UP" else "support"
    kind = "BREAKOUT" if direction == "UP" else "BREAKDOWN"
    lines = [
        f"FALSE {kind} at {level:g} {side}",
        f"OI classification at the level strike: {confirmation.get('oi_classification')}",
        "",
        *_format_confirmation_checklist(confirmation),
        "",
        _format_verdict_line("FALSE BREAKOUT", "fresh writing detected against the move -- avoiding this trade"),
        *_format_valid_until(valid_until),
    ]
    return "\n".join(lines)


def notify_false_breakout(level: float, direction: str, confirmation: dict, valid_until: datetime | None = None) -> None:
    text = format_false_breakout_message(level, direction, confirmation, valid_until)
    route_message("option_trading", text, send_telegram=False, telegram_parse_mode=None)


# --------------------------------------------------------------------------
# Alert staleness fix -- fresh-spot re-check + valid_until (Part A1/A2/A3)
# --------------------------------------------------------------------------


def refetch_spot_for_alert(cycle_spot: float) -> tuple[float, bool]:
    """Best-effort fresh spot check immediately before alert delivery
    (Part A1) -- yfinance, single call, not a full Angel One re-fetch.
    Falls back to the cycle's own (possibly stale) spot on failure;
    callers must render the re-check-failed warning when the second
    element is False."""
    fresh = fetch_nifty_spot_backup()
    if fresh is None:
        return cycle_spot, False
    return fresh, True


def compute_valid_until(now: datetime, next_mode: str) -> datetime:
    """Next scheduled scan time for the mode being entered -- WATCH mode
    runs 1-min cadence, NORMAL mode 5-min (Part A3)."""
    minutes = 1 if next_mode == "WATCH" else 5
    return now + timedelta(minutes=minutes)


def record_alert_sent(alert_type: str, direction: str | None, level: float | None, data_ts: str | None, valid_until: datetime) -> None:
    execute(
        "INSERT INTO options_alerts_sent (sent_ts, alert_type, direction, level, data_ts, valid_until) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(IST).isoformat(), alert_type, direction, level, data_ts, valid_until.isoformat()),
    )


def _handle_escalation(
    next_state: dict, cycle_spot: float, analysis: dict, cycle_data: dict, now: datetime,
    previous_snapshot: list[dict], config: OptionsConfig,
) -> None:
    """Rulebook A1/A2 staleness fix: re-check spot immediately before
    choosing which alert to send. The cycle's own spot was fetched at the
    top of fetch_cycle_data() -- potentially minutes ago on a cache-miss
    cycle (verified live: ~165s alone for the instruments master on a
    cold cache) -- so it may already be stale by the time an alert is
    about to go out. No confirmation evidence exists yet for this exact
    cycle (mode was still NORMAL going in), so the checklist renders as
    all-pending -- correct, since nothing has been evaluated yet."""
    level = next_state["watch_level"]
    direction = next_state["watch_direction"]

    fresh_spot, refetch_ok = refetch_spot_for_alert(cycle_spot)
    sent_ts = datetime.now(IST)
    data_ts = datetime.fromisoformat(cycle_data["snapshot_ts"])
    valid_until = compute_valid_until(now, "WATCH")
    analysis["vix"] = fetch_india_vix_with_change()

    if has_crossed_level(fresh_spot, level, direction):
        notify_breach_unconfirmed(
            level, direction, fresh_spot, analysis, data_ts, sent_ts, refetch_ok,
            cycle_data["chain"], previous_snapshot, None, config, valid_until,
        )
        alert_type = "BREACH_UNCONFIRMED"
    else:
        notify_watch_escalation(
            level, direction, fresh_spot, analysis, data_ts, sent_ts, refetch_ok,
            cycle_data["chain"], previous_snapshot, None, config, valid_until,
        )
        alert_type = "WATCHING"

    record_alert_sent(alert_type, direction, level, data_ts.isoformat(), valid_until)


def _handle_false_breakout(engine_state: dict, confirmation: dict, now: datetime) -> None:
    valid_until = compute_valid_until(now, "NORMAL")
    notify_false_breakout(engine_state["watch_level"], engine_state["watch_direction"], confirmation, valid_until)
    record_alert_sent("FALSE_BREAKOUT", engine_state["watch_direction"], engine_state["watch_level"], None, valid_until)


# --------------------------------------------------------------------------
# Position monitor pass
# --------------------------------------------------------------------------


def run_position_monitor_pass(analysis: dict, spot: float, config: OptionsConfig, today: date) -> None:
    """Re-evaluates every active options_positions row against its
    original thesis, notifying only on a state change."""
    import json

    positions = fetch_all("SELECT * FROM options_positions WHERE status = 'active'")
    for row in positions:
        position = dict(row)
        thesis = json.loads(position["thesis_json"])
        side = position["side"]
        current_row = next((r for r in analysis["chain"] if r["strike"] == position["strike"] and r["side"] == side), None)
        if current_row is None:
            # Contract outside this cycle's fetched window -- e.g. a
            # position opened off _fetch_next_weekly_chain_fallback()'s
            # next-weekly chain while the primary cycle still fetches the
            # (about-to-expire) nearest weekly. Self-heals within ~1-2
            # trading days once that weekly expires and this expiry
            # becomes the new primary; skip rather than guess in the
            # meantime.
            continue

        opposite_side = "PE" if side == "CE" else "CE"
        opposite_classification = check_level_strike_oi_behavior(
            analysis["chain"], analysis.get("previous_snapshot"), position["strike"], opposite_side, config
        )
        # expiry_date is stored in Angel's DDMMMYYYY format, not ISO.
        expiry_date = datetime.strptime(position["expiry_date"], "%d%b%Y").date()
        days_to_expiry = (expiry_date - today).days

        evidence = build_position_evidence(
            thesis=thesis,
            current_spot=spot,
            current_regime=analysis.get("regime"),
            opposite_side_oi_classification=opposite_classification,
            entry_iv=thesis.get("iv"),
            current_iv=current_row.get("iv"),
            days_to_expiry=days_to_expiry,
            current_rr=None,
            config=config,
        )
        decision = evaluate_position(evidence)

        if should_notify(position, decision):
            notify_position_update(position, decision, current_row.get("ltp"), spot)
            record_notified_state(position["id"], decision["action"])


# --------------------------------------------------------------------------
# Main cycle
# --------------------------------------------------------------------------


def run_cycle(config: OptionsConfig, now: datetime | None = None) -> dict:
    """One full engine cycle. Returns a small status dict describing what
    happened, for logging/testing. Never raises for expected failure
    modes (data unavailable, auth failure, greeks sanity failure) —
    those are caught and routed to #monitoring."""
    now = now or datetime.now(IST)
    today = now.date()

    if not acquire_lock(config, now):
        return {"status": "locked"}

    try:
        if not is_market_hours(now):
            return {"status": "outside_market_hours"}

        engine_state = get_engine_state()
        if not should_run_normal_cycle(engine_state, now, config):
            return {"status": "skipped_cadence"}

        try:
            cycle_data = fetch_cycle_data(config, today)
        except (DataInsufficientError, AngelAuthError) as exc:
            route_message("monitoring", f"Option Trading engine: data cycle failed -- {exc}", send_telegram=False, telegram_parse_mode=None)
            return {"status": "data_error", "error": str(exc)}

        expiry, chain, spot = cycle_data["expiry"], cycle_data["chain"], cycle_data["spot"]
        previous_snapshot = fetch_previous_snapshot(expiry, cycle_data["trading_date"], cycle_data["snapshot_ts"])
        first_of_day = fetch_first_of_day_snapshot(expiry, cycle_data["trading_date"])
        prior_day_last = fetch_prior_day_last_snapshot(expiry, cycle_data["trading_date"])

        atr = compute_atr(cycle_data["candles_5m"])
        price_trend = detect_price_trend(cycle_data["candles_5m"])

        analysis = build_market_analysis(
            chain=chain,
            spot=spot,
            config=config,
            candles=cycle_data["candles_5m"],
            previous_snapshot=previous_snapshot,
            first_of_day_snapshot=first_of_day,
            prior_day_last_snapshot=prior_day_last,
            time_to_expiry_years=years_to_expiry(expiry, now),
        )
        support = (analysis["support_resistance"].get("support") or [None])[0]
        resistance = (analysis["support_resistance"].get("resistance") or [None])[0]
        regime = classify_regime(spot, support, resistance, price_trend)
        analysis["regime"] = regime
        analysis["previous_snapshot"] = previous_snapshot

        confirmation = None
        if engine_state.get("mode") == "WATCH":
            watch_level = engine_state["watch_level"]
            direction = engine_state["watch_direction"]
            option_side = "CE" if direction == "UP" else "PE"
            confirmation = evaluate_breakout_confirmation(
                candles=cycle_data["candles_1m"],
                level=watch_level,
                direction=direction,
                level_strike=watch_level,
                option_side=option_side,
                chain=chain,
                previous_snapshot=previous_snapshot,
                volume_result=analysis["volume"],
                regime=regime,
                config=config,
            )

        transition = decide_transition(
            engine_state, spot, analysis["support_resistance"], now, config, atr=atr, confirmation=confirmation
        )
        save_engine_state(transition["next_state"], now.isoformat())

        action = transition["action"]
        if action == "ESCALATE":
            _handle_escalation(transition["next_state"], spot, analysis, cycle_data, now, previous_snapshot, config)
        elif action == "FALSE_BREAKOUT":
            _handle_false_breakout(engine_state, confirmation, now)
        elif action == "CONFIRMED":
            _handle_confirmed(engine_state, chain, previous_snapshot, cycle_data, analysis, confirmation, spot, atr, regime, now, config, today)

        run_position_monitor_pass(analysis, spot, config, today)

        return {"status": "ok", "action": action, "regime": regime}
    finally:
        release_lock(config)


def _fetch_next_weekly_chain_fallback(cycle_data: dict, spot: float, config: OptionsConfig, today: date) -> list[dict] | None:
    """Rulebook Section 34 fallback: only called when the primary weekly
    expiry's chain didn't have enough runway for a new trade
    (select_trade's own "insufficient time to expiry" rejection, e.g. the
    weekly expires today or tomorrow). One extra live Greeks + market-data
    fetch for the next available expiry, on demand -- not fetched every
    cycle, to stay well inside Angel One's empirically-observed rate limit
    (see angel_client.py's module docstring). Returns None (falls through
    to the original NO_TRADE) if there's no later expiry listed or the
    fallback fetch itself fails.

    Known limitation: a position opened off this fallback chain uses a
    different expiry than the cycle's primary fetch, so
    run_position_monitor_pass()'s per-cycle chain lookup won't find it
    until the primary weekly actually expires and the next weekly becomes
    the new primary (at most ~1-2 trading days) -- monitoring resumes
    automatically at that point, this isn't a permanent blind spot.
    """
    contracts = cycle_data["contracts"]
    available_expiries = list_expiries(contracts)
    next_expiry = select_next_weekly_expiry(available_expiries, cycle_data["expiry"], today)
    if next_expiry is None:
        return None
    try:
        session = ensure_session(config)
        client = build_client(config, session)
        return fetch_expiry_chain(client, contracts, next_expiry, spot, config.angel_atm_strike_window)
    except (AngelAuthError, DataInsufficientError) as exc:
        route_message(
            "monitoring", f"Option Trading engine: next-weekly fallback fetch failed -- {exc}",
            send_telegram=False, telegram_parse_mode=None,
        )
        return None


def _handle_confirmed(engine_state, chain, previous_snapshot, cycle_data, analysis, confirmation, spot, atr, regime, now, config, today):
    """Runs the Trade Selector on a confirmed breakout/breakdown and
    persists + (maybe) notifies the resulting advisory."""
    direction = engine_state["watch_direction"]
    level = engine_state["watch_level"]
    option_side = "CE" if direction == "UP" else "PE"

    market_rejections = evaluate_market_rejections(
        {"breakout_unconfirmed": direction == "UP" and not confirmation.get("confirmed"),
         "breakdown_unconfirmed": direction == "DOWN" and not confirmation.get("confirmed")},
        config,
    )

    result = select_trade(
        direction=direction,
        confirmation_level=level,
        option_side=option_side,
        expiry_chain=chain,
        contracts=cycle_data["contracts"],
        entry_spot=spot,
        support_resistance=analysis["support_resistance"],
        atr=atr,
        capital=config.capital,
        config=config,
        today=today,
    )

    if result["action"] == "NO_TRADE" and result.get("reasons") == ["insufficient time to expiry"]:
        fallback_chain = _fetch_next_weekly_chain_fallback(cycle_data, spot, config, today)
        if fallback_chain:
            result = select_trade(
                direction=direction,
                confirmation_level=level,
                option_side=option_side,
                expiry_chain=fallback_chain,
                contracts=cycle_data["contracts"],
                entry_spot=spot,
                support_resistance=analysis["support_resistance"],
                atr=atr,
                capital=config.capital,
                config=config,
                today=today,
            )

    # NOTE: if `result` came from the fallback chain (a different expiry
    # than `chain`/`previous_snapshot`), these lookups legitimately find
    # nothing -- oi_change/previous_oi fall through to None below, same
    # as any other selected_strike outside the primary window. Fetching a
    # second expiry's OI history here isn't worth the extra live call for
    # what's already a rare fallback path.
    selected_strike = result.get("trade", {}).get("strike") if result.get("trade") else None
    current_oi_row = next((r for r in chain if r["strike"] == selected_strike and r["side"] == option_side), None) if selected_strike else None
    previous_oi_row = next((r for r in previous_snapshot if r["strike"] == selected_strike and r["side"] == option_side), None) if selected_strike else None
    oi_change = (
        current_oi_row["oi"] - previous_oi_row["oi"]
        if current_oi_row and previous_oi_row and current_oi_row.get("oi") is not None and previous_oi_row.get("oi") is not None
        else None
    )
    previous_oi = previous_oi_row["oi"] if previous_oi_row else None
    oi_classification = confirmation.get("oi_classification")

    sub_scores = {
        "trend_pa": score_trend_pa(regime),
        "confirmation": score_confirmation(confirmation),
        "oi_structure": score_oi_structure(oi_classification),
        "delta_oi": score_delta_oi(oi_change, previous_oi),
        "volume": score_volume(analysis["volume"]),
        "iv_greeks": score_iv_greeks(analysis.get("greeks_crosscheck", [])),
        "pcr": score_pcr(analysis.get("pcr")),
        "rr": score_rr(result.get("trade", {}).get("risk_reward") if result.get("trade") else None, config),
        "liquidity": score_liquidity(result.get("trade", {}).get("spread_pct") if result.get("trade") else None, config),
    }
    score_result = score_setup(sub_scores, config)
    score = score_result["score"]

    if market_rejections and result["action"] != "NO_TRADE":
        result = {"action": "NO_TRADE", "reasons": market_rejections, "trade": result.get("trade")}
    elif score < config.min_score and result["action"] != "NO_TRADE":
        result = {"action": "NO_TRADE", "reasons": [f"score {score} below minimum {config.min_score}"], "trade": result.get("trade")}

    why = summarize_confirmation_evidence(confirmation) if result["action"] != "NO_TRADE" else None
    payload = build_advisory_payload(
        result, now.isoformat(), spot, regime, analysis["support_resistance"], analysis.get("pcr"), score, why=why
    )
    write_advisory(payload, score, {"confirmation": confirmation, "sub_scores": sub_scores, "market_rejections": market_rejections})

    if result["action"] in ("BUY_CE_CANDIDATE", "BUY_PE_CANDIDATE"):
        notify_advisory(payload)


# --------------------------------------------------------------------------
# Session start / end heartbeat
# --------------------------------------------------------------------------
#
# Deliberately does NOT depend on Angel One/live data -- the whole point is
# to be a heartbeat that proves the cron infrastructure itself is alive,
# independent of whether the data source is having problems (that's
# already its own #monitoring alert path via fetch_cycle_data's error
# handling). Discord-only (send_telegram=False), matching this project's
# existing convention for passive, non-actionable digests.


def format_session_start_message(today: date) -> str:
    return "\n".join(
        [
            "NIFTY Options Engine -- SESSION START",
            f"Date: {today.isoformat()}",
            "Market hours: 09:15-15:30 IST",
            "Engine cron is alive and beginning today's cycles.",
        ]
    )


def notify_session_start(config: OptionsConfig, today: date | None = None) -> None:
    text = format_session_start_message(today or datetime.now(IST).date())
    route_message("option_trading", text, send_telegram=False, telegram_parse_mode=None)


def format_session_end_message(today: date) -> dict:
    advisory_rows = fetch_all(
        "SELECT action, COUNT(*) AS cnt FROM options_advisories WHERE created_ts LIKE ? GROUP BY action",
        (f"{today.isoformat()}%",),
    )
    advisory_counts = {row["action"]: row["cnt"] for row in advisory_rows}
    opened_today = fetch_one("SELECT COUNT(*) AS cnt FROM options_positions WHERE date(opened_ts) = date('now')")["cnt"]
    closed_today = fetch_one(
        "SELECT COUNT(*) AS cnt FROM options_positions WHERE status = 'closed' AND date(closed_ts) = date('now')"
    )["cnt"]
    still_active = fetch_one("SELECT COUNT(*) AS cnt FROM options_positions WHERE status = 'active'")["cnt"]
    engine_state = get_engine_state()

    lines = [
        "NIFTY Options Engine -- SESSION END",
        f"Date: {today.isoformat()}",
        f"Advisories today: {sum(advisory_counts.values())} "
        f"(BUY_CE {advisory_counts.get('BUY_CE_CANDIDATE', 0)}, "
        f"BUY_PE {advisory_counts.get('BUY_PE_CANDIDATE', 0)}, "
        f"NO_TRADE {advisory_counts.get('NO_TRADE', 0)}, "
        f"WAIT {advisory_counts.get('WAIT', 0)})",
        f"Positions opened today: {opened_today}",
        f"Positions closed today: {closed_today}",
        f"Still active: {still_active}",
        f"Engine mode at close: {engine_state.get('mode')}",
    ]
    return {"text": "\n".join(lines), "advisory_counts": advisory_counts}


def notify_session_end(config: OptionsConfig, today: date | None = None) -> None:
    formatted = format_session_end_message(today or datetime.now(IST).date())
    route_message("option_trading", formatted["text"], send_telegram=False, telegram_parse_mode=None)


# --------------------------------------------------------------------------
# End of day
# --------------------------------------------------------------------------


def run_end_of_day(config: OptionsConfig, today: date | None = None) -> dict:
    """Snapshot retention cleanup + reset engine state to NORMAL.
    `greeks_sanity_checked_date` also resets — a new trading day needs
    its own fresh sanity check."""
    deleted = cleanup_old_snapshots(config, today)
    execute(
        "UPDATE options_engine_state SET mode = 'NORMAL', watch_level = NULL, watch_direction = NULL, "
        "watch_started_ts = NULL, greeks_sanity_checked_date = NULL, updated_ts = datetime('now') WHERE id = 1"
    )
    return {"status": "ok", "snapshots_deleted": deleted}


# --------------------------------------------------------------------------
# CLI entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    from config.startup import StartupService

    args = argv if argv is not None else sys.argv[1:]
    StartupService().start()
    config = OptionsConfig.load()

    if "--session-start" in args:
        notify_session_start(config)
        print("Session-start notification sent")
        return

    if "--session-end" in args:
        notify_session_end(config)
        print("Session-end notification sent")
        return

    if "--eod" in args:
        result = run_end_of_day(config)
        print(f"End-of-day cleanup: {result}")
        return

    result = run_cycle(config)
    print(f"Cycle result: {result}")


if __name__ == "__main__":
    main()
