"""Central config object — loads all runtime settings from .env via python-dotenv."""

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv


@dataclass
class Settings:
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

    # Analysis defaults
    default_timeframe: str = "1d"
    watchlist: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> "Settings":
        """Load settings from the process environment, reading .env first."""
        load_dotenv()
        return cls(
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
            default_timeframe=os.getenv("DEFAULT_TIMEFRAME", "1d"),
            watchlist=cls._split_watchlist(os.getenv("WATCHLIST", "")),
        )

    @staticmethod
    def _split_watchlist(raw: str) -> list[str]:
        """Turn a comma-separated WATCHLIST env var into a clean list of tickers."""
        return [ticker.strip() for ticker in raw.split(",") if ticker.strip()]
