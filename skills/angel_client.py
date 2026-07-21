"""Angel One SmartAPI client (Phase 15) — the options data source.

SmartAPI supports officially automatable, unattended login — client code
+ PIN + TOTP — which matters since this runs on cron with no human in
the loop each morning. DATA ONLY: this module never places an order.

Method signatures/exceptions below were verified against the installed
`smartapi-python==1.5.5` package directly (not guessed). Response FIELD
NAMES for optionGreek/getMarketData/generateSession follow Angel One's
publicly documented SmartAPI Market Data & Login APIs; the instruments-
master JSON's column conventions (`strike` stored *100, `exch_seg`,
`instrumenttype`) follow the widely-documented public scrip-master file
shape. Neither has been exercised against a live account from this
environment — verify field names against one real response before the
live dry run.

Known SmartAPI bug this module designs around: forum reports of the
Option Greeks endpoint occasionally returning monthly-expiry Greeks when
a weekly expiry is requested. `assert_greeks_sanity()` is a mandatory
runtime check for this, not optional (see GreeksSanityError).

Rate limits: Angel One doesn't publish an official number for
optionGreek specifically (getMarketData/quote is documented at 1
request/second; combined limits across endpoints are tighter still) —
verified empirically instead: two manual live test runs a few minutes
apart were enough to trip "Access denied because of exceeding access
rate" on optionGreek. `_call_with_backoff()` retries that specific
condition with exponential backoff rather than failing the whole cycle
on the first hit.
"""

import json
import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pyotp
import requests
from SmartApi import SmartConnect
from SmartApi.smartExceptions import PermissionException, SmartAPIException, TokenException

from config.options_config import OptionsConfig
from db.database import write_option_chain_snapshot_rows
from skills.options_data_fetch import DataInsufficientError

IST = ZoneInfo("Asia/Kolkata")

INSTRUMENTS_MASTER_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
MAX_TOKENS_PER_MARKET_DATA_CALL = 50
RATE_LIMIT_MAX_RETRIES = 3
RATE_LIMIT_BASE_DELAY_SECONDS = 2.0
_RATE_LIMIT_MESSAGE_MARKERS = ("exceeding access rate", "access rate limit")


class AngelAuthError(Exception):
    """Raised on login/session failure — designed to be caught by the
    calling skill and routed to the existing #monitoring notify path. A
    silent auth failure is indistinguishable from a quiet market day, so
    this is always raised loudly, never swallowed."""


class GreeksSanityError(DataInsufficientError):
    """Raised when Angel One's optionGreek endpoint appears to be
    serving the same Greeks for two different expiries — the known
    SmartAPI weekly/monthly bug. Never let this data feed the rulebook."""


def _safe_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _public_ip() -> str | None:
    """Best-effort diagnostic aid for forbidden/auth errors: Angel One
    whitelists a static IP per app, so a rotated home/ISP IP is the most
    likely cause of a previously-working setup suddenly failing."""
    try:
        resp = requests.get("https://ifconfig.me/ip", timeout=5)
        resp.raise_for_status()
        return resp.text.strip()
    except requests.RequestException:
        return None


def _forbidden_error_message(exc: Exception) -> str:
    ip = _public_ip()
    ip_note = f" Current public IP: {ip}." if ip else " Could not determine current public IP."
    return (
        f"Angel One rejected the request as forbidden ({exc}). Angel One whitelists a static "
        f"IP per registered app — a rotated home/ISP IP is the most likely cause if this "
        f"previously worked.{ip_note}"
    )


def _is_rate_limit_error(exc: SmartAPIException) -> bool:
    """The SDK has no typed RateLimitException — a rate-limit rejection
    surfaces as a generic DataException, since Angel returns a plain-text
    (non-JSON) body the SDK's own JSON parsing then wraps into a
    DataException. Detect it by message content instead."""
    message = str(exc).lower()
    return any(marker in message for marker in _RATE_LIMIT_MESSAGE_MARKERS)


def _call_with_backoff(func, *args, max_retries: int = RATE_LIMIT_MAX_RETRIES, sleep_fn=time.sleep, **kwargs):
    """Calls a SmartConnect SDK method, retrying with exponential backoff
    only when Angel One rate-limited the request (see
    _is_rate_limit_error). Any other SmartAPIException — or a rate limit
    still in effect after `max_retries` — propagates immediately; this is
    a targeted fix for a specific, empirically-observed failure mode, not
    a general-purpose retry-everything wrapper."""
    last_exc = None
    for attempt in range(max_retries):
        try:
            return func(*args, **kwargs)
        except SmartAPIException as exc:
            if not _is_rate_limit_error(exc):
                raise
            last_exc = exc
            sleep_fn(RATE_LIMIT_BASE_DELAY_SECONDS * (2**attempt))
    raise last_exc


# --------------------------------------------------------------------------
# Auth — login once per run, reuse within the run and across the trading day
# --------------------------------------------------------------------------


def _load_cached_session(cache_path: str) -> dict | None:
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cached_session(cache_path: str, session: dict) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session))


def _session_is_fresh(cached: dict, now: datetime | None = None) -> bool:
    """Angel One sessions are treated as valid for the trading day. A
    session obtained on a different IST calendar date than today is
    stale — never trust a token across a day boundary."""
    if not cached.get("jwt_token") or not cached.get("obtained_ts"):
        return False
    obtained_date = datetime.fromisoformat(cached["obtained_ts"]).date()
    current = (now or datetime.now(IST)).date()
    return obtained_date == current


def login(config: OptionsConfig) -> dict:
    """Run the Angel One unattended login sequence and return a fresh
    session dict, caching it to `config.angel_token_cache_path`.

    Never call this inside a polling loop — Angel One rate-limits the
    login endpoint aggressively. Callers should go through
    `ensure_session()`, which only logs in when no same-day cached
    session exists.
    """
    if not (config.angel_api_key and config.angel_client_code and config.angel_pin and config.angel_totp_secret):
        raise AngelAuthError("ANGEL_API_KEY / ANGEL_CLIENT_CODE / ANGEL_PIN / ANGEL_TOTP_SECRET must all be set")

    smart = SmartConnect(api_key=config.angel_api_key)
    totp_code = pyotp.TOTP(config.angel_totp_secret).now()

    try:
        result = smart.generateSession(config.angel_client_code, config.angel_pin, totp_code)
    except (TokenException, PermissionException) as exc:
        raise AngelAuthError(_forbidden_error_message(exc)) from exc
    except SmartAPIException as exc:
        raise AngelAuthError(f"Angel One login failed: {exc}") from exc

    if not result or not result.get("status"):
        message = (result or {}).get("message", "unknown error")
        raise AngelAuthError(f"Angel One login rejected: {message}")

    data = result["data"]
    session = {
        "jwt_token": _strip_bearer_prefix(data["jwtToken"]),
        "refresh_token": data["refreshToken"],
        "feed_token": data["feedToken"],
        "client_code": data.get("clientcode", config.angel_client_code),
        "obtained_ts": datetime.now(IST).isoformat(),
    }
    _save_cached_session(config.angel_token_cache_path, session)
    return session


def _strip_bearer_prefix(token: str) -> str:
    """SmartConnect.generateSession() sets the client's own access_token
    to the raw JWT internally, but mutates the *returned* dict's
    data['jwtToken'] to "Bearer " + jwtToken before handing it back
    (verified against a live response — see smartConnect.py's
    generateSession source). Cache/pass around the raw token, since
    build_client() -> SmartConnect._request() always prepends "Bearer "
    itself; caching the prefixed value produces a malformed double-Bearer
    Authorization header on every subsequent authenticated call."""
    prefix = "Bearer "
    return token[len(prefix) :] if token.startswith(prefix) else token


def ensure_session(config: OptionsConfig) -> dict:
    """Return a cached same-day session if one exists, else log in.
    Safe to call multiple times per run — subsequent calls within the
    same trading day hit the fresh-cache path, satisfying the
    login-once-per-run policy without extra in-process state."""
    cached = _load_cached_session(config.angel_token_cache_path)
    if cached and _session_is_fresh(cached):
        return cached
    return login(config)


def build_client(config: OptionsConfig, session: dict) -> SmartConnect:
    """Construct an authenticated SmartConnect client from a session
    dict returned by ensure_session()/login()."""
    return SmartConnect(
        api_key=config.angel_api_key,
        access_token=session["jwt_token"],
        refresh_token=session["refresh_token"],
        feed_token=session["feed_token"],
    )


# --------------------------------------------------------------------------
# Instruments master (never hardcode expiry/lot size/token — rulebook Rule 18)
# --------------------------------------------------------------------------


def fetch_instruments_master() -> list[dict]:
    """The scrip master is a real ~35MB JSON file — verified live to take
    ~165s to download on this connection, well past a naive short timeout.
    (connect_timeout, read_timeout) — read_timeout is generous since the
    observed failure mode was mid-stream stalls, not a dead connection."""
    resp = requests.get(INSTRUMENTS_MASTER_URL, timeout=(10, 240))
    resp.raise_for_status()
    return resp.json()


def _load_cached_instruments(cache_path: str) -> dict | None:
    path = Path(cache_path)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cached_instruments(cache_path: str, instruments: list[dict], fetched_date: str) -> None:
    path = Path(cache_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"fetched_date": fetched_date, "instruments": instruments}))


def ensure_instruments_master(config: OptionsConfig, today: date | None = None) -> list[dict]:
    """Daily-cached instruments master. Refuses to serve a cache older
    than the current trading day — if a fresh fetch fails and no
    same-day cache exists, this fails loudly rather than silently
    serving stale instrument metadata (lot size / symboltoken changes
    can and do happen)."""
    current_date = (today or datetime.now(IST).date()).isoformat()
    cached = _load_cached_instruments(config.angel_instruments_cache_path)
    if cached and cached.get("fetched_date") == current_date:
        return cached["instruments"]

    try:
        instruments = fetch_instruments_master()
    except requests.RequestException as exc:
        raise DataInsufficientError(
            f"Angel One instruments master fetch failed and no same-day cache exists: {exc}"
        ) from exc

    _save_cached_instruments(config.angel_instruments_cache_path, instruments, current_date)
    return instruments


def filter_nifty_option_contracts(instruments: list[dict]) -> list[dict]:
    return [
        row
        for row in instruments
        if row.get("exch_seg") == "NFO" and row.get("name") == "NIFTY" and row.get("instrumenttype") == "OPTIDX"
    ]


def resolve_nifty_spot_token(instruments: list[dict]) -> dict | None:
    """Resolve the NIFTY 50 spot index token from the NSE segment —
    never hardcoded, per rulebook Rule 18."""
    for row in instruments:
        if row.get("exch_seg") == "NSE" and row.get("symbol") == "Nifty 50":
            return row
    return None


def _normalize_strike(raw_strike) -> float:
    """Angel One's instruments-master JSON stores strike price *100
    (e.g. 25000 as "2500000.000000") — normalize back to a real strike."""
    return float(raw_strike) / 100


def _parse_master_expiry(expiry: str) -> date:
    return datetime.strptime(expiry, "%d%b%Y").date()


def format_expiry_readable(expiry: str) -> str:
    """Angel's DDMMMYYYY expiry (e.g. '21JUL2026') as a human-readable
    'day Month' string (e.g. '21 July') for alert messages -- alerts show
    this alongside the raw expiry, never in place of it, since 'day Month'
    alone is ambiguous across a year boundary."""
    parsed = _parse_master_expiry(expiry)
    return f"{parsed.day} {parsed.strftime('%B')}"


def list_expiries(contracts: list[dict]) -> list[str]:
    """Sorted unique expiry strings (e.g. '24JUL2025') from a filtered
    NIFTY OPTIDX contract list."""
    return sorted({c["expiry"] for c in contracts if c.get("expiry")}, key=_parse_master_expiry)


def select_weekly_and_monthly_expiry(available_expiries: list[str], today: date) -> tuple[str, str]:
    """Nearest weekly + current monthly, derived purely from listed
    expiry dates (Angel's contract data has no separate weekly/monthly
    flag): "current monthly" = the last expiry within the nearest
    expiry's calendar month. Required for the mandatory weekly-vs-
    monthly Greeks sanity check (assert_greeks_sanity). Raises ValueError
    if there are no future expiries — the caller should already have
    verified `available_expiries` is non-empty and current."""
    future = sorted((e for e in available_expiries if _parse_master_expiry(e) >= today), key=_parse_master_expiry)
    if not future:
        raise ValueError("No future expiries available to select from")

    nearest_weekly = future[0]
    nearest_month = _parse_master_expiry(nearest_weekly).month
    same_month = [e for e in future if _parse_master_expiry(e).month == nearest_month]
    current_monthly = same_month[-1] if same_month else nearest_weekly
    return nearest_weekly, current_monthly


def resolve_symboltoken(contracts: list[dict], expiry: str, strike: float, side: str) -> str | None:
    for contract in contracts:
        if (
            contract.get("expiry") == expiry
            and _normalize_strike(contract.get("strike", -1)) == strike
            and contract.get("symbol", "").endswith(side)
        ):
            return contract.get("token")
    return None


def select_atm_strikes(contracts: list[dict], expiry: str, spot: float, window: int) -> list[float]:
    """ATM ± `window` listed strikes for the given expiry, from actually
    listed strikes — never assume fixed 50-point spacing (rulebook
    Section 6)."""
    strikes = sorted({_normalize_strike(c["strike"]) for c in contracts if c.get("expiry") == expiry})
    if not strikes:
        return []
    atm_index = min(range(len(strikes)), key=lambda i: abs(strikes[i] - spot))
    lo = max(0, atm_index - window)
    hi = min(len(strikes), atm_index + window + 1)
    return strikes[lo:hi]


# --------------------------------------------------------------------------
# Data fetch — Greeks + market data, joined; PCR; weekly/monthly sanity check
# --------------------------------------------------------------------------


def fetch_nifty_spot(client: SmartConnect, spot_token_row: dict) -> float:
    try:
        resp = _call_with_backoff(
            client.ltpData, spot_token_row["exch_seg"], spot_token_row["symbol"], spot_token_row["token"]
        )
    except (TokenException, PermissionException) as exc:
        raise AngelAuthError(_forbidden_error_message(exc)) from exc
    except SmartAPIException as exc:
        raise DataInsufficientError(f"Angel One NIFTY spot fetch failed: {exc}") from exc

    if not resp or not resp.get("status") or not resp.get("data"):
        raise DataInsufficientError(f"Angel One NIFTY spot fetch returned no data: {resp}")

    ltp = _safe_float(resp["data"].get("ltp"))
    if ltp is None:
        raise DataInsufficientError("Angel One NIFTY spot fetch returned no usable ltp")
    return ltp


def fetch_option_greeks(client: SmartConnect, expiry: str) -> list[dict]:
    """Raw per-strike Greeks for one expiry. `expiry` must be Angel's
    DDMMMYYYY format (e.g. '24JUL2025')."""
    try:
        resp = _call_with_backoff(client.optionGreek, {"name": "NIFTY", "expirydate": expiry})
    except (TokenException, PermissionException) as exc:
        raise AngelAuthError(_forbidden_error_message(exc)) from exc
    except SmartAPIException as exc:
        raise DataInsufficientError(f"Angel One optionGreek fetch failed for {expiry}: {exc}") from exc

    if not resp or not resp.get("status") or not resp.get("data"):
        raise DataInsufficientError(f"Angel One optionGreek returned no data for {expiry}: {resp}")
    return resp["data"]


def fetch_market_data(client: SmartConnect, tokens: list[str]) -> dict[str, dict]:
    """FULL-mode quote data for up to 50 NFO tokens, keyed by symbolToken."""
    if len(tokens) > MAX_TOKENS_PER_MARKET_DATA_CALL:
        raise ValueError(f"Angel One getMarketData accepts at most {MAX_TOKENS_PER_MARKET_DATA_CALL} tokens per call")

    try:
        resp = _call_with_backoff(client.getMarketData, "FULL", {"NFO": tokens})
    except (TokenException, PermissionException) as exc:
        raise AngelAuthError(_forbidden_error_message(exc)) from exc
    except SmartAPIException as exc:
        raise DataInsufficientError(f"Angel One getMarketData fetch failed: {exc}") from exc

    if not resp or not resp.get("status"):
        raise DataInsufficientError(f"Angel One getMarketData returned an error: {resp}")

    fetched = (resp.get("data") or {}).get("fetched") or []
    return {row["symbolToken"]: row for row in fetched}


def fetch_market_data_batched(client: SmartConnect, tokens: list[str]) -> dict[str, dict]:
    """fetch_market_data(), chunked to respect the 50-tokens-per-call cap."""
    market_by_token: dict[str, dict] = {}
    for start in range(0, len(tokens), MAX_TOKENS_PER_MARKET_DATA_CALL):
        batch = tokens[start : start + MAX_TOKENS_PER_MARKET_DATA_CALL]
        market_by_token.update(fetch_market_data(client, batch))
    return market_by_token


def join_greeks_and_market_data(
    greeks: list[dict], market_by_token: dict[str, dict], contracts: list[dict], expiry: str
) -> list[dict]:
    """Join optionGreek rows (keyed by strike + optionType) with
    getMarketData rows (keyed by symbolToken) via the resolved
    (expiry, strike, side) -> symboltoken mapping."""
    joined = []
    for greek in greeks:
        strike = _safe_float(greek.get("strikePrice"))
        side = greek.get("optionType")
        if strike is None or side is None:
            continue
        token = resolve_symboltoken(contracts, expiry, strike, side)
        market = market_by_token.get(token) if token else None
        depth = (market or {}).get("depth") or {}
        best_bid = (depth.get("buy") or [{}])[0]
        best_ask = (depth.get("sell") or [{}])[0]

        joined.append(
            {
                "expiry": expiry,
                "strike": strike,
                "side": side,
                "delta": _safe_float(greek.get("delta")),
                "gamma": _safe_float(greek.get("gamma")),
                "theta": _safe_float(greek.get("theta")),
                "vega": _safe_float(greek.get("vega")),
                "iv": _safe_float(greek.get("impliedVolatility")),
                "ltp": _safe_float((market or {}).get("ltp")),
                "oi": _safe_float((market or {}).get("opnInterest")),
                "volume": _safe_float((market or {}).get("tradeVolume")),
                "bid": _safe_float(best_bid.get("price")),
                "bid_qty": _safe_float(best_bid.get("quantity")),
                "ask": _safe_float(best_ask.get("price")),
                "ask_qty": _safe_float(best_ask.get("quantity")),
            }
        )
    return joined


def compute_pcr(joined_rows: list[dict]) -> float | None:
    """Total PE OI / total CE OI, computed deterministically from the
    joined chain — never trust Angel's own putCallRatio() endpoint,
    since its strike universe isn't guaranteed to match ours (rulebook
    Section 29)."""
    total_pe_oi = sum(row["oi"] or 0 for row in joined_rows if row["side"] == "PE")
    total_ce_oi = sum(row["oi"] or 0 for row in joined_rows if row["side"] == "CE")
    if total_ce_oi == 0:
        return None
    return total_pe_oi / total_ce_oi


def _find_atm_greek(greeks: list[dict], atm_strike: float) -> dict | None:
    for row in greeks:
        strike = _safe_float(row.get("strikePrice"))
        if strike is not None and abs(strike - atm_strike) < 1e-6:
            return row
    return None


def assert_greeks_sanity(
    weekly_greeks: list[dict], monthly_greeks: list[dict], atm_strike: float, tolerance: float = 1e-6
) -> None:
    """Compare ATM theta/IV between the nearest weekly and current
    monthly expiry Greeks fetches — different time-to-expiry should
    produce different theta/IV in virtually every real market state. If
    they're suspiciously identical, Angel is very likely serving monthly
    Greeks for the weekly request (the known SmartAPI bug). Raises
    GreeksSanityError rather than letting this data reach the rulebook.
    """
    weekly_atm = _find_atm_greek(weekly_greeks, atm_strike)
    monthly_atm = _find_atm_greek(monthly_greeks, atm_strike)
    if weekly_atm is None or monthly_atm is None:
        raise DataInsufficientError("Could not locate ATM Greeks for the weekly/monthly sanity check")

    weekly_theta = _safe_float(weekly_atm.get("theta"))
    monthly_theta = _safe_float(monthly_atm.get("theta"))
    weekly_iv = _safe_float(weekly_atm.get("impliedVolatility"))
    monthly_iv = _safe_float(monthly_atm.get("impliedVolatility"))

    theta_matches = (
        weekly_theta is not None and monthly_theta is not None and abs(weekly_theta - monthly_theta) < tolerance
    )
    iv_matches = weekly_iv is not None and monthly_iv is not None and abs(weekly_iv - monthly_iv) < tolerance

    if theta_matches and iv_matches:
        raise GreeksSanityError(
            "Angel One optionGreek returned identical ATM theta/IV for the weekly and monthly "
            "expiry — this matches the known SmartAPI bug where monthly Greeks bleed into weekly "
            "requests. Refusing to use this data."
        )


# --------------------------------------------------------------------------
# Snapshot writer + change-in-OI/delta/IV (query-based, no duplicated state)
# --------------------------------------------------------------------------


def write_snapshot(joined_rows: list[dict], trading_date: str, snapshot_ts: str, spot: float) -> int:
    """Batch-insert one fetched+joined Angel One chain into
    option_chain_snapshots via the shared db.database helper."""
    rows = [
        (
            snapshot_ts,
            trading_date,
            row["expiry"],
            row["strike"],
            row["side"],
            row["ltp"],
            row["volume"],
            row["oi"],
            row["bid"],
            row["bid_qty"],
            row["ask"],
            row["ask_qty"],
            row["iv"],
            row["delta"],
            row["gamma"],
            row["theta"],
            row["vega"],
            spot,
        )
        for row in joined_rows
    ]
    if not rows:
        raise DataInsufficientError("No joined Angel One rows to write")
    return write_option_chain_snapshot_rows(rows)


def compute_change_since_previous(
    expiry_date: str, strike: float, side: str, current_snapshot_ts: str
) -> dict | None:
    """Change-in-OI/delta/IV vs. the immediately preceding snapshot for
    this (expiry, strike, side) — computed via query against the full
    snapshot history, never stored as a duplicated prev_oi column (Task 5
    design decision — see module docstring / Task 1 handoff note).
    Returns None if there is no prior snapshot to compare against.
    """
    from db.database import get_connection

    with get_connection() as conn:
        rows = conn.execute(
            """SELECT snapshot_ts, oi, delta, iv FROM option_chain_snapshots
               WHERE expiry_date = ? AND strike = ? AND side = ? AND snapshot_ts <= ?
               ORDER BY snapshot_ts DESC LIMIT 2""",
            (expiry_date, strike, side, current_snapshot_ts),
        ).fetchall()

    if len(rows) < 2:
        return None

    current, previous = rows[0], rows[1]
    return {
        "oi_change": _delta(current["oi"], previous["oi"]),
        "delta_change": _delta(current["delta"], previous["delta"]),
        "iv_change": _delta(current["iv"], previous["iv"]),
        "previous_snapshot_ts": previous["snapshot_ts"],
    }


def _delta(current, previous):
    return (current - previous) if current is not None and previous is not None else None


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------


def run_market_data_cycle(
    config: OptionsConfig,
    weekly_expiry: str,
    monthly_expiry: str,
    trading_date: str | None = None,
    skip_greeks_sanity_check: bool = False,
) -> dict:
    """One full Angel One data-layer cycle for the nearest weekly expiry:
    ensure a session, refresh the instruments master, resolve spot + ATM
    window, run the weekly-vs-monthly Greeks sanity check (unless
    `skip_greeks_sanity_check` — the check guards against a persistent
    SmartAPI bug, not a per-cycle condition, so it only needs to run once
    per trading day; re-running it every 1-min WATCH-mode cycle doubles
    the optionGreek call volume for no benefit and was empirically
    hitting Angel One's rate limit), fetch + join Greeks and market data,
    and write the snapshot. Raises GreeksSanityError or
    DataInsufficientError rather than ever handing suspect/incomplete
    data to the analytics layer.

    Returns `"greeks_sanity_checked": True` when the check actually ran
    and passed this cycle, so the caller can persist that and skip it on
    subsequent cycles the same trading day.
    """
    session = ensure_session(config)
    client = build_client(config, session)

    instruments = ensure_instruments_master(config)
    contracts = filter_nifty_option_contracts(instruments)

    spot_row = resolve_nifty_spot_token(instruments)
    if spot_row is None:
        raise DataInsufficientError("Could not resolve the NIFTY spot index token from the instruments master")
    spot = fetch_nifty_spot(client, spot_row)

    strikes = select_atm_strikes(contracts, weekly_expiry, spot, config.angel_atm_strike_window)
    if not strikes:
        raise DataInsufficientError(f"No listed strikes found for expiry {weekly_expiry}")
    atm_strike = min(strikes, key=lambda s: abs(s - spot))

    weekly_greeks = fetch_option_greeks(client, weekly_expiry)
    greeks_sanity_checked = False
    if not skip_greeks_sanity_check and monthly_expiry != weekly_expiry:
        monthly_greeks = fetch_option_greeks(client, monthly_expiry)
        assert_greeks_sanity(weekly_greeks, monthly_greeks, atm_strike)
        greeks_sanity_checked = True

    strike_set = set(strikes)
    tokens = [
        token
        for strike in strikes
        for side in ("CE", "PE")
        if (token := resolve_symboltoken(contracts, weekly_expiry, strike, side))
    ]
    market_by_token = fetch_market_data_batched(client, tokens)

    greeks_in_window = [g for g in weekly_greeks if _safe_float(g.get("strikePrice")) in strike_set]
    joined = join_greeks_and_market_data(greeks_in_window, market_by_token, contracts, weekly_expiry)

    snapshot_ts = datetime.now(IST).isoformat()
    resolved_trading_date = trading_date or datetime.now(IST).date().isoformat()
    written = write_snapshot(joined, resolved_trading_date, snapshot_ts, spot)

    return {
        "snapshot_ts": snapshot_ts,
        "trading_date": resolved_trading_date,
        "expiry": weekly_expiry,
        "spot": spot,
        "atm_strike": atm_strike,
        "rows_written": written,
        "pcr": compute_pcr(joined),
        "chain": joined,
        "greeks_sanity_checked": greeks_sanity_checked,
    }
