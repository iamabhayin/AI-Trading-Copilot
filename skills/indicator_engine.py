"""Indicator Engine — Section 3.2.

Agent/Model: Code (pandas-ta). Deterministic, auditable — no LLM involved.

Computes exactly three indicators from OHLCV candles: RSI, a 14-day/50-day
EMA pair, and Bollinger Bands. Chart timeframe is configurable (e.g. `1h`,
`1d`) and passed in as a parameter — same skill code, different interval, no
separate skill needed. Default is `1h`, since this is a swing/positional
system trading off hourly candles. "14-day"/"50-day" mean calendar trading
days, not candle counts — `TRADING_HOURS_PER_DAY` converts between the two
for the EMA pair. Outputs structured indicator values per ticker, tagged
with the timeframe used.
"""

# TODO: Phase 1 — data fetch + indicator engine (plain Python, console output)

import pandas as pd
import pandas_ta as ta

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}

# NSE trading hours per day — basis for converting a calendar-day EMA
# lookback (e.g. "14-day") into an hourly-candle period count (e.g. ~88).
TRADING_HOURS_PER_DAY = 6.25

# Candles per trading day, by timeframe.
CANDLES_PER_TRADING_DAY = {"1h": TRADING_HOURS_PER_DAY, "1d": 1}

EMA_FAST_DAYS = 14
EMA_SLOW_DAYS = 50


def _period_for_days(days: int, timeframe: str) -> int:
    """Convert a calendar-day lookback into a candle-count period for `timeframe`."""
    candles_per_day = CANDLES_PER_TRADING_DAY.get(timeframe, 1)
    return round(days * candles_per_day)


def compute_indicators(df: pd.DataFrame, timeframe: str = "1h") -> pd.DataFrame:
    """Append RSI, EMA(14-day)/EMA(50-day), and Bollinger Bands columns to
    an OHLCV DataFrame.

    `df` must have (case-insensitive) open/high/low/close/volume columns.
    The EMA periods are calendar-day based and converted to a candle count
    for `timeframe` (e.g. ~88/~312 candles on `1h`). The returned DataFrame
    is tagged with `timeframe` via `.attrs`.
    """
    missing = REQUIRED_COLUMNS - {c.lower() for c in df.columns}
    if missing:
        raise ValueError(f"OHLCV dataframe missing required columns: {sorted(missing)}")

    out = df.copy()
    out.columns = [c.lower() for c in out.columns]

    out["rsi_14"] = ta.rsi(out["close"], length=14)

    out["ema_14d"] = ta.ema(out["close"], length=_period_for_days(EMA_FAST_DAYS, timeframe))
    out["ema_50d"] = ta.ema(out["close"], length=_period_for_days(EMA_SLOW_DAYS, timeframe))

    bbands = ta.bbands(out["close"], length=20)
    out = out.join(bbands)

    out.attrs["timeframe"] = timeframe
    return out


def summarize_latest(df: pd.DataFrame) -> dict:
    """Flatten the most recent row of an indicator DataFrame into a plain
    dict — the structured shape the Signal Skill (Phase 2) consumes.
    """
    latest = df.iloc[-1]
    return {
        "timeframe": df.attrs.get("timeframe"),
        "close": float(latest["close"]),
        "rsi_14": _safe_float(latest.get("rsi_14")),
        "ema_14d": _safe_float(latest.get("ema_14d")),
        "ema_50d": _safe_float(latest.get("ema_50d")),
        "bb_upper": _safe_float(latest.get("BBU_20_2.0_2.0")),
        "bb_lower": _safe_float(latest.get("BBL_20_2.0_2.0")),
    }


def summarize_recent(df: pd.DataFrame, n: int = 10) -> list[dict]:
    """Flatten the last `n` rows of an indicator DataFrame into a list of
    plain dicts (oldest to newest) — the history window the Signal Skill
    checks EMA cross and Bollinger Band squeeze setups against, since
    `summarize_latest()` only exposes a single current-value snapshot.
    """
    timeframe = df.attrs.get("timeframe")
    recent = df.iloc[-n:]
    return [
        {
            "timeframe": timeframe,
            "close": _safe_float(row.get("close")),
            "ema_14d": _safe_float(row.get("ema_14d")),
            "ema_50d": _safe_float(row.get("ema_50d")),
            "bb_upper": _safe_float(row.get("BBU_20_2.0_2.0")),
            "bb_lower": _safe_float(row.get("BBL_20_2.0_2.0")),
        }
        for _, row in recent.iterrows()
    ]


def _safe_float(value) -> float | None:
    return None if pd.isna(value) else float(value)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from config.startup import StartupService

    settings = StartupService().start()
    tickers = settings.watchlist or ["AAPL"]
    timeframe = settings.default_timeframe or "1h"

    for ticker in tickers:
        print(f"--- {ticker} ({timeframe}) ---")
        try:
            from skills.data_fetch_skill import fetch_ohlcv

            ohlcv = fetch_ohlcv(ticker, timeframe=timeframe)
        except Exception as exc:  # network/API unavailable — fall back to synthetic data
            print(f"  live fetch failed ({exc}); using synthetic data for a console smoke test")
            import numpy as np

            rng = np.random.default_rng(0)
            n = 120
            close = 100 + np.cumsum(rng.normal(0, 1, n))
            idx = pd.date_range(end=pd.Timestamp.today(), periods=n, freq="D")
            ohlcv = pd.DataFrame(
                {
                    "open": close + rng.normal(0, 0.5, n),
                    "high": close + rng.uniform(0, 2, n),
                    "low": close - rng.uniform(0, 2, n),
                    "close": close,
                    "volume": rng.integers(1000, 5000, n),
                },
                index=idx,
            )

        indicators = compute_indicators(ohlcv, timeframe=timeframe)
        summary = summarize_latest(indicators)
        for key, value in summary.items():
            print(f"  {key}: {value}")
