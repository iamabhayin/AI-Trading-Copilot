"""Options data-layer support (Phase 15) — yfinance backup spot/candles/VIX,
the shared `DataInsufficientError` exception used across the Option
Trading feature, and snapshot-retention cleanup.

skills/angel_client.py is the actual options data source (Angel One
SmartAPI: chain/Greeks fetch, auth, instruments master, snapshot writes).
This module supplies what Angel One doesn't: yfinance's NIFTY candles for
price-structure analysis, a backup spot value to cross-check Angel's
fetched spot against, and India VIX.
"""

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.options_config import OptionsConfig
from db.database import get_connection
from skills.data_fetch_skill import fetch_ohlcv

IST = ZoneInfo("Asia/Kolkata")

YF_NIFTY_TICKER = "^NSEI"
YF_VIX_TICKER = "^INDIAVIX"


class DataInsufficientError(Exception):
    """Raised whenever fetched data is missing, malformed, or unusable —
    the caller must take no analytical action on the cycle (rulebook
    Section 3/42.1-42.2), never fall back to inventing or reusing stale
    data."""


# --------------------------------------------------------------------------
# yfinance — index spot backup, intraday candles, India VIX
# --------------------------------------------------------------------------


def fetch_nifty_candles(interval: str = "5m", period: str = "5d"):
    """Intraday NIFTY candles via yfinance (^NSEI), for price-structure /
    swing-high-low analysis in options_analytics.py."""
    return fetch_ohlcv(YF_NIFTY_TICKER, timeframe=interval, period=period)


def fetch_nifty_spot_backup() -> float | None:
    """Backup spot source — yfinance's latest 1-min NIFTY close. Returns
    None on any fetch failure; the caller treats Angel One's fetched spot
    as primary regardless (rulebook Section 3)."""
    try:
        candles = fetch_nifty_candles(interval="1m", period="1d")
    except Exception:
        return None
    closes = candles["close"].dropna()
    return float(closes.iloc[-1]) if not closes.empty else None


def fetch_india_vix() -> float | None:
    try:
        data = fetch_ohlcv(YF_VIX_TICKER, timeframe="1d", period="5d")
    except Exception:
        return None
    closes = data["close"].dropna()
    return float(closes.iloc[-1]) if not closes.empty else None


def check_spot_divergence(primary_spot: float | None, yfinance_spot: float | None, config: OptionsConfig) -> dict:
    """Compare the primary fetched spot (Angel One) against yfinance's
    backup spot. The primary value always wins; this only produces a
    data-quality flag for logging when the two diverge beyond
    `config.spot_divergence_threshold_pct`."""
    if not primary_spot or yfinance_spot is None:
        return {"checked": False, "diverged": False}

    diff_pct = abs(primary_spot - yfinance_spot) / primary_spot * 100
    return {
        "checked": True,
        "diverged": diff_pct > config.spot_divergence_threshold_pct,
        "primary_spot": primary_spot,
        "yfinance_spot": yfinance_spot,
        "diff_pct": diff_pct,
    }


# --------------------------------------------------------------------------
# Snapshot retention
# --------------------------------------------------------------------------


def cleanup_old_snapshots(config: OptionsConfig, today: date | None = None) -> int:
    """Delete option_chain_snapshots rows older than
    config.snapshot_retention_days trading days. Never touches
    options_positions — active/closed positions are never deleted."""
    cutoff = (today or datetime.now(IST).date()) - timedelta(days=config.snapshot_retention_days)
    with get_connection() as conn:
        cursor = conn.execute("DELETE FROM option_chain_snapshots WHERE trading_date < ?", (cutoff.isoformat(),))
        conn.commit()
        return cursor.rowcount
