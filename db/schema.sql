-- AI Trading Copilot — SQLite schema (see architecture doc Section 5)
-- Single-file SQLite, no server, no ORM.

PRAGMA foreign_keys = ON;

-- Every recommendation ever made by the Signal Skill.
CREATE TABLE IF NOT EXISTS suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker      TEXT NOT NULL,
    action      TEXT NOT NULL CHECK (action IN ('BUY', 'SELL', 'HOLD')),
    entry_price REAL,
    stop_loss   REAL,
    target      REAL,
    confidence  REAL,
    rationale   TEXT,
    timeframe   TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- What you actually bought/sold, parsed by the Position Tracker skill.
CREATE TABLE IF NOT EXISTS positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    suggestion_id INTEGER REFERENCES suggestions (id),
    ticker        TEXT NOT NULL,
    qty           REAL NOT NULL,
    entry_price   REAL NOT NULL,
    stop_loss     REAL,
    target        REAL,
    status        TEXT NOT NULL CHECK (status IN ('active', 'closed')) DEFAULT 'active',
    entry_time    TEXT NOT NULL DEFAULT (datetime('now')),
    exit_time     TEXT,
    exit_price    REAL
);

-- Chat history per ticker, powers Chat Skill follow-up Q&A.
CREATE TABLE IF NOT EXISTS conversations (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker    TEXT,
    role      TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    message   TEXT NOT NULL,
    timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Dedup tracking so the Monitor Skill doesn't spam repeat alerts.
CREATE TABLE IF NOT EXISTS alerts_sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    position_id INTEGER REFERENCES positions (id),
    alert_type  TEXT NOT NULL CHECK (alert_type IN ('stop_loss', 'target', 'risk_judgment', 'news')),
    message     TEXT,
    sent_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_suggestions_ticker ON suggestions (ticker);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions (status);
CREATE INDEX IF NOT EXISTS idx_conversations_ticker ON conversations (ticker);
CREATE INDEX IF NOT EXISTS idx_alerts_sent_position ON alerts_sent (position_id);
