"""Option Trading (NIFTY Options Engine) config — Phase 15.

Self-contained, mirrors config/settings.py's Config/ConfigLoader split
(Pydantic-validated, every setting defaults to a safe value, no required
vars at load time) but kept as its own module rather than folded into the
main Config: this feature is fully independent of the equity pipeline and
its threshold surface is large enough to deserve its own file, per the
Phase 15 spec.

Every threshold used by the analytics/rules/trade-selector modules lives
here, not hardcoded in those modules — see options-rulebook.md for the
rule semantics each threshold gates.
"""

import os

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError


class OptionsConfigError(RuntimeError):
    """Raised when Option Trading configuration fails validation."""


class OptionsConfig(BaseModel):
    # --- Angel One SmartAPI auth (options data source; data-only, never
    # used for order placement). Unattended login via client code + PIN +
    # TOTP so the engine can log in on cron with no human in the loop. ---
    angel_api_key: str = ""
    angel_client_code: str = ""
    angel_pin: str = ""
    angel_totp_secret: str = ""
    angel_token_cache_path: str = "db/.angel_token.json"
    angel_instruments_cache_path: str = "db/.angel_instruments.json"
    # getMarketData is token-based (<=50 tokens/call), so strike selection
    # happens at fetch time here -- distinct from strike_window_primary/
    # extended below, which are the analytics layer's post-fetch filter.
    angel_atm_strike_window: int = 10

    # --- Regime/proximity detection ---
    proximity_threshold_pct: float = 0.20
    proximity_mode: str = "pct"  # 'pct' | 'atr'
    atr_multiplier: float = 0.5
    deescalate_threshold_pct: float = 0.40

    # --- Watch-mode state machine ---
    sustain_minutes: int = 5
    watch_timeout_minutes: int = 45

    # --- Analytics ---
    volume_confirm_multiplier: float = 1.5
    boundary_min_share: float = 0.20
    strike_window_primary: int = 5
    strike_window_extended: int = 10
    greeks_crosscheck_enabled: bool = True
    # Minimum |% move| in premium/OI to count as a real signal rather than
    # noise, when classifying LONG_BUILDUP/SHORT_BUILDUP/SHORT_COVERING/
    # LONG_UNWINDING (rulebook Section 16).
    classification_min_move_pct: float = 2.0
    # Risk-free rate fed into py_vollib's Black-Scholes Greeks cross-check.
    risk_free_rate: float = 0.065
    # |recomputed - broker| delta divergence above this logs a warning —
    # informational only, never blocks (rulebook implementation notes).
    greeks_divergence_threshold: float = 0.15

    # --- Scoring (rulebook Section 43 weights) ---
    min_score: int = 70
    score_weight_trend_pa: int = 20
    score_weight_confirmation: int = 20
    score_weight_oi_structure: int = 15
    score_weight_delta_oi: int = 10
    score_weight_volume: int = 10
    score_weight_rr: int = 10
    score_weight_iv_greeks: int = 5
    score_weight_pcr: int = 5
    score_weight_liquidity: int = 5

    # --- Trade selection ---
    min_rr: float = 2.0
    capital: float = 0.0
    risk_pct: float = 0.01
    delta_band_min: float = 0.50
    delta_band_max: float = 0.70
    max_spread_pct: float = 2.0
    min_oi: int = 0
    min_volume: int = 0
    holding_fit_multiplier: float = 2.0
    theta_danger_days: int = 3
    iv_event_calendar_path: str = "config/options_event_calendar.json"
    # Entry zone = watched level -> level +/- this % (rulebook Section 37:
    # "use an entry zone, not a single price").
    entry_zone_buffer_pct: float = 0.15
    # Underlying invalidation = watched level, pulled back by this % against
    # the breakout direction (rulebook Section 38's "loss of breakout/
    # retest structure").
    invalidation_buffer_pct: float = 0.30
    # Option premium SL is floored here so a delta-mapped SL never implies
    # a negative or unrealistically-near-zero premium.
    min_option_premium: float = 0.05
    # T1 = entry + (next_level - entry) * this ratio; T2 = next_level.
    target_split_ratio: float = 0.5

    # --- Data layer ---
    snapshot_retention_days: int = 10
    data_staleness_seconds: int = 120
    # Angel One's fetched spot is always primary; this only gates a
    # data-quality warning when yfinance's backup spot diverges further
    # than this from it.
    spot_divergence_threshold_pct: float = 0.5
    # Comma-separated in env; parsed to a list. Empty = derive dynamically
    # from the live expiry list each cycle (nearest weekly + next weekly +
    # current monthly) rather than a fixed hardcoded set.
    expiries_to_fetch: list[str] = []

    # --- Engine orchestration (Task 7) ---
    # Lightweight lockfile guarding against overlapping cron runs.
    engine_lock_path: str = "db/.options_engine.lock"
    lock_staleness_seconds: int = 300

    @classmethod
    def load(cls) -> "OptionsConfig":
        return OptionsConfigLoader().load()

    @staticmethod
    def _split_csv(raw: str) -> list[str]:
        return [item.strip() for item in raw.split(",") if item.strip()]

    @staticmethod
    def _parse_bool(raw: str) -> bool:
        return raw.strip().lower() in ("1", "true", "yes")


def _env(name: str, default: str) -> str:
    """os.getenv(name, default), but also falls back to `default` when the
    var is *set but blank* — the documented .env.example convention for
    every OPTIONS_*/ANGEL_* threshold is "leave blank to use the default",
    and plain os.getenv only falls back on a genuinely missing key."""
    return os.getenv(name) or default


class OptionsConfigLoader:
    """Reads environment variables (via .env) into a validated OptionsConfig.

    No required vars at load time — Angel One credentials are checked at
    the point of use in the data-fetch layer, not here, matching the
    project's existing "each skill checks what it needs" convention.
    """

    def load(self) -> OptionsConfig:
        load_dotenv()

        try:
            return OptionsConfig(
                angel_api_key=_env("ANGEL_API_KEY", ""),
                angel_client_code=_env("ANGEL_CLIENT_CODE", ""),
                angel_pin=_env("ANGEL_PIN", ""),
                angel_totp_secret=_env("ANGEL_TOTP_SECRET", ""),
                angel_token_cache_path=_env("ANGEL_TOKEN_CACHE_PATH", "db/.angel_token.json"),
                angel_instruments_cache_path=_env("ANGEL_INSTRUMENTS_CACHE_PATH", "db/.angel_instruments.json"),
                angel_atm_strike_window=int(_env("ANGEL_ATM_STRIKE_WINDOW", "10")),
                proximity_threshold_pct=float(_env("OPTIONS_PROXIMITY_THRESHOLD_PCT", "0.20")),
                proximity_mode=_env("OPTIONS_PROXIMITY_MODE", "pct"),
                atr_multiplier=float(_env("OPTIONS_ATR_MULTIPLIER", "0.5")),
                deescalate_threshold_pct=float(_env("OPTIONS_DEESCALATE_THRESHOLD_PCT", "0.40")),
                sustain_minutes=int(_env("OPTIONS_SUSTAIN_MINUTES", "5")),
                watch_timeout_minutes=int(_env("OPTIONS_WATCH_TIMEOUT_MINUTES", "45")),
                volume_confirm_multiplier=float(_env("OPTIONS_VOLUME_CONFIRM_MULTIPLIER", "1.5")),
                boundary_min_share=float(_env("OPTIONS_BOUNDARY_MIN_SHARE", "0.20")),
                strike_window_primary=int(_env("OPTIONS_STRIKE_WINDOW_PRIMARY", "5")),
                strike_window_extended=int(_env("OPTIONS_STRIKE_WINDOW_EXTENDED", "10")),
                greeks_crosscheck_enabled=OptionsConfig._parse_bool(
                    _env("OPTIONS_GREEKS_CROSSCHECK_ENABLED", "true")
                ),
                classification_min_move_pct=float(_env("OPTIONS_CLASSIFICATION_MIN_MOVE_PCT", "2.0")),
                risk_free_rate=float(_env("OPTIONS_RISK_FREE_RATE", "0.065")),
                greeks_divergence_threshold=float(_env("OPTIONS_GREEKS_DIVERGENCE_THRESHOLD", "0.15")),
                min_score=int(_env("OPTIONS_MIN_SCORE", "70")),
                score_weight_trend_pa=int(_env("OPTIONS_SCORE_WEIGHT_TREND_PA", "20")),
                score_weight_confirmation=int(_env("OPTIONS_SCORE_WEIGHT_CONFIRMATION", "20")),
                score_weight_oi_structure=int(_env("OPTIONS_SCORE_WEIGHT_OI_STRUCTURE", "15")),
                score_weight_delta_oi=int(_env("OPTIONS_SCORE_WEIGHT_DELTA_OI", "10")),
                score_weight_volume=int(_env("OPTIONS_SCORE_WEIGHT_VOLUME", "10")),
                score_weight_rr=int(_env("OPTIONS_SCORE_WEIGHT_RR", "10")),
                score_weight_iv_greeks=int(_env("OPTIONS_SCORE_WEIGHT_IV_GREEKS", "5")),
                score_weight_pcr=int(_env("OPTIONS_SCORE_WEIGHT_PCR", "5")),
                score_weight_liquidity=int(_env("OPTIONS_SCORE_WEIGHT_LIQUIDITY", "5")),
                min_rr=float(_env("OPTIONS_MIN_RR", "2.0")),
                capital=float(_env("OPTIONS_CAPITAL", "0")),
                risk_pct=float(_env("OPTIONS_RISK_PCT", "0.01")),
                delta_band_min=float(_env("OPTIONS_DELTA_BAND_MIN", "0.50")),
                delta_band_max=float(_env("OPTIONS_DELTA_BAND_MAX", "0.70")),
                max_spread_pct=float(_env("OPTIONS_MAX_SPREAD_PCT", "2.0")),
                min_oi=int(_env("OPTIONS_MIN_OI", "0")),
                min_volume=int(_env("OPTIONS_MIN_VOLUME", "0")),
                holding_fit_multiplier=float(_env("OPTIONS_HOLDING_FIT_MULTIPLIER", "2.0")),
                theta_danger_days=int(_env("OPTIONS_THETA_DANGER_DAYS", "3")),
                iv_event_calendar_path=_env(
                    "OPTIONS_IV_EVENT_CALENDAR_PATH", "config/options_event_calendar.json"
                ),
                entry_zone_buffer_pct=float(_env("OPTIONS_ENTRY_ZONE_BUFFER_PCT", "0.15")),
                invalidation_buffer_pct=float(_env("OPTIONS_INVALIDATION_BUFFER_PCT", "0.30")),
                min_option_premium=float(_env("OPTIONS_MIN_OPTION_PREMIUM", "0.05")),
                target_split_ratio=float(_env("OPTIONS_TARGET_SPLIT_RATIO", "0.5")),
                snapshot_retention_days=int(_env("OPTIONS_SNAPSHOT_RETENTION_DAYS", "10")),
                data_staleness_seconds=int(_env("OPTIONS_DATA_STALENESS_SECONDS", "120")),
                spot_divergence_threshold_pct=float(_env("OPTIONS_SPOT_DIVERGENCE_THRESHOLD_PCT", "0.5")),
                expiries_to_fetch=OptionsConfig._split_csv(_env("OPTIONS_EXPIRIES_TO_FETCH", "")),
                engine_lock_path=_env("OPTIONS_ENGINE_LOCK_PATH", "db/.options_engine.lock"),
                lock_staleness_seconds=int(_env("OPTIONS_LOCK_STALENESS_SECONDS", "300")),
            )
        except (ValidationError, ValueError) as exc:
            raise OptionsConfigError(f"Invalid Option Trading configuration: {exc}") from exc
