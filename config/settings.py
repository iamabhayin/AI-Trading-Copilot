"""Central config — Pydantic-validated, loads from .env.

`Settings` is kept as a backward-compatible alias for `Config` so existing
skill code (`from config.settings import Settings`, `Settings.load()`)
does not need to change.
"""

import os

from dotenv import load_dotenv
from pydantic import BaseModel, Field, ValidationError

# No vars are required at load time — every setting defaults to "" / a safe
# default, and each skill checks (or gracefully degrades on) the specific
# ones it actually needs at the point of use, not at Config.load() time.
# Kept as an explicit, empty tuple rather than removed so a future var can
# be added here deliberately without re-deriving this design decision.
REQUIRED_ENV_VARS: tuple[str, ...] = ()


class ConfigError(RuntimeError):
    """Raised when configuration is missing (per REQUIRED_ENV_VARS) or fails validation."""


class Config(BaseModel):
    # Claude / Anthropic
    anthropic_api_key: str = ""

    # Broker API
    broker_api_key: str = ""
    broker_api_secret: str = ""
    broker_access_token: str = ""

    # Market data / news providers
    alpha_vantage_api_key: str = ""
    newsapi_key: str = ""
    finnhub_api_key: str = ""

    # Telegram
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # Discord
    discord_bot_token: str = ""
    discord_guild_id: str = ""
    discord_channel_market_news: str = ""
    discord_channel_signals: str = ""
    discord_channel_monitoring: str = ""
    discord_channel_chat: str = ""
    discord_channel_closed_trades: str = ""

    # Ollama
    ollama_host: str = ""
    ollama_model: str = ""

    # Storage
    sqlite_db_path: str = "db/trading_copilot.db"

    # Logging
    log_level: str = "INFO"

    # Analysis defaults
    default_timeframe: str = "1d"
    watchlist: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls) -> "Config":
        """Load settings from the process environment, reading .env first.

        Raises ConfigError if a required var is missing or a value fails
        validation — see ConfigLoader for the actual loading logic.
        """
        return ConfigLoader().load()

    @staticmethod
    def _split_watchlist(raw: str) -> list[str]:
        """Turn a comma-separated WATCHLIST env var into a clean list of tickers."""
        return [ticker.strip() for ticker in raw.split(",") if ticker.strip()]


class ConfigLoader:
    """Reads environment variables (via .env) into a validated Config.

    Fails loudly — raises ConfigError naming every missing required var,
    or wrapping a Pydantic validation failure — rather than silently
    falling back to defaults for anything in REQUIRED_ENV_VARS.
    """

    def load(self) -> Config:
        load_dotenv()

        missing = [name for name in REQUIRED_ENV_VARS if not os.getenv(name)]
        if missing:
            raise ConfigError(
                f"Missing required environment variable(s): {', '.join(missing)}. "
                "Set them in .env (see .env.example)."
            )

        try:
            return Config(
                anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
                broker_api_key=os.getenv("BROKER_API_KEY", ""),
                broker_api_secret=os.getenv("BROKER_API_SECRET", ""),
                broker_access_token=os.getenv("BROKER_ACCESS_TOKEN", ""),
                alpha_vantage_api_key=os.getenv("ALPHA_VANTAGE_API_KEY", ""),
                newsapi_key=os.getenv("NEWSAPI_KEY", ""),
                finnhub_api_key=os.getenv("FINNHUB_API_KEY", ""),
                telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
                telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
                discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
                discord_guild_id=os.getenv("DISCORD_GUILD_ID", ""),
                discord_channel_market_news=os.getenv("DISCORD_CHANNEL_MARKET_NEWS", ""),
                discord_channel_signals=os.getenv("DISCORD_CHANNEL_SIGNALS", ""),
                discord_channel_monitoring=os.getenv("DISCORD_CHANNEL_MONITORING", ""),
                discord_channel_chat=os.getenv("DISCORD_CHANNEL_CHAT", ""),
                discord_channel_closed_trades=os.getenv("DISCORD_CHANNEL_CLOSED_TRADES", ""),
                ollama_host=os.getenv("OLLAMA_HOST", ""),
                ollama_model=os.getenv("OLLAMA_MODEL", ""),
                sqlite_db_path=os.getenv("SQLITE_DB_PATH", "db/trading_copilot.db"),
                log_level=os.getenv("LOG_LEVEL", "INFO"),
                default_timeframe=os.getenv("DEFAULT_TIMEFRAME", "1d"),
                watchlist=Config._split_watchlist(os.getenv("WATCHLIST", "")),
            )
        except ValidationError as exc:
            raise ConfigError(f"Invalid configuration: {exc}") from exc


# Backward-compatible alias — every skill imports `Settings` and calls
# `Settings.load()`; this keeps that working unchanged.
Settings = Config
