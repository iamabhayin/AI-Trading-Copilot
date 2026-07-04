"""Startup Service — sequences config load, logging init, and SQLite
schema verification in one place, so every skill starts the same way
instead of each duplicating its own bootstrap logic.
"""

import sqlite3
import sys
from pathlib import Path

from loguru import logger

from config.settings import Config, ConfigError
from db.database import init_db

LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss} | {level: <8} | {extra[component]}:{extra[event]} | {message}"
)


class StartupError(RuntimeError):
    """Raised when any startup step fails, with the underlying cause
    attached (`raise StartupError(...) from exc`).
    """


class StartupService:
    """Runs the startup sequence: load config -> init logging -> verify/
    initialize the DB schema -> log a startup-complete event.
    """

    def start(self) -> Config:
        """Run the full startup sequence. Returns the loaded Config."""
        config = self._load_config()
        self._init_logging(config)
        self._init_database()
        logger.bind(component="startup", event="startup_complete").info("Startup complete")
        return config

    def _load_config(self) -> Config:
        try:
            return Config.load()
        except ConfigError as exc:
            raise StartupError("Failed to load configuration") from exc

    def _init_logging(self, config: Config) -> None:
        try:
            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            logger.configure(extra={"component": "-", "event": "-"})
            logger.remove()
            logger.add(sys.stderr, level=config.log_level, format=LOG_FORMAT)
            logger.add(
                LOGS_DIR / "app.log",
                level=config.log_level,
                format=LOG_FORMAT,
                rotation="1 day",
                retention="14 days",
            )
        except OSError as exc:
            raise StartupError("Failed to initialize logging") from exc

    def _init_database(self) -> None:
        try:
            init_db()  # CREATE TABLE IF NOT EXISTS — safe to re-run, never touches existing rows
        except (sqlite3.Error, OSError) as exc:
            raise StartupError("Failed to verify/initialize the SQLite schema") from exc
        logger.bind(component="startup", event="db_ready").info("Database schema verified")
