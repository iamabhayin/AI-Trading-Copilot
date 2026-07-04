"""Indicator Engine — Section 3.2.

Agent/Model: Code (pandas-ta). Deterministic, auditable — no LLM involved.

Computes RSI, MACD, EMA crossovers, Bollinger Bands, ADX, volume anomalies,
and support/resistance from OHLCV candles. Chart timeframe is configurable
(e.g. `1h`, `4h`, `1d`, `1wk`) and passed in as a parameter — same skill
code, different interval, no separate skill needed. Default recommendation
is `1d` for swing-trade analysis, with `1h` as an opt-in secondary view.
Outputs structured indicator values per ticker, tagged with the timeframe
used.
"""

# TODO: Phase 1 — data fetch + indicator engine (plain Python, console output)

import pandas as pd
import pandas_ta as ta

REQUIRED_COLUMNS = {"open", "high", "low", "close", "volume"}

VOLUME_ANOMALY_MULTIPLIER = 2.0
EMA_FAST = 20
EMA_SLOW = 50
SUPPORT_RESISTANCE_WINDOW = 20


def compute_indicators(df: pd.DataFrame, timeframe: str = "1d") -> pd.DataFrame:
    """Append RSI, MACD, EMA crossover, Bollinger Bands, ADX, volume anomaly,
    and rolling support/resistance columns to an OHLCV DataFrame.

    `df` must have (case-insensitive) open/high/low/close/volume columns.
    The returned DataFrame is tagged with `timeframe` via `.attrs`.
    """
    missing = REQUIRED_COLUMNS - {c.lower() for c in df.columns}
    if missing:
        raise ValueError(f"OHLCV dataframe missing required columns: {sorted(missing)}")

    out = df.copy()
    out.columns = [c.lower() for c in out.columns]

    out["rsi_14"] = ta.rsi(out["close"], length=14)

    macd = ta.macd(out["close"])
    out = out.join(macd)

    out["ema_20"] = ta.ema(out["close"], length=EMA_FAST)
    out["ema_50"] = ta.ema(out["close"], length=EMA_SLOW)
    out["ema_crossover_bullish"] = out["ema_20"] > out["ema_50"]

    bbands = ta.bbands(out["close"], length=20)
    out = out.join(bbands)

    adx = ta.adx(out["high"], out["low"], out["close"], length=14)
    out["adx_14"] = adx["ADX_14"]

    out["volume_sma_20"] = out["volume"].rolling(20).mean()
    out["volume_anomaly"] = out["volume"] > (VOLUME_ANOMALY_MULTIPLIER * out["volume_sma_20"])

    out["support"] = out["low"].rolling(SUPPORT_RESISTANCE_WINDOW).min()
    out["resistance"] = out["high"].rolling(SUPPORT_RESISTANCE_WINDOW).max()

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
        "macd": _safe_float(latest.get("MACD_12_26_9")),
        "macd_signal": _safe_float(latest.get("MACDs_12_26_9")),
        "ema_20": _safe_float(latest.get("ema_20")),
        "ema_50": _safe_float(latest.get("ema_50")),
        "ema_crossover_bullish": bool(latest.get("ema_crossover_bullish")),
        "bb_upper": _safe_float(latest.get("BBU_20_2.0_2.0")),
        "bb_lower": _safe_float(latest.get("BBL_20_2.0_2.0")),
        "adx_14": _safe_float(latest.get("adx_14")),
        "volume_anomaly": bool(latest.get("volume_anomaly")),
        "support": _safe_float(latest.get("support")),
        "resistance": _safe_float(latest.get("resistance")),
    }


def _safe_float(value) -> float | None:
    return None if pd.isna(value) else float(value)


if __name__ == "__main__":
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from config.startup import StartupService

    settings = StartupService().start()
    tickers = settings.watchlist or ["AAPL"]
    timeframe = settings.default_timeframe or "1d"

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
