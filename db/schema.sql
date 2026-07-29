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

-- ===========================================================================
-- Option Trading (NIFTY Options Engine) — Phase 15. See docs/options-rulebook.md
-- for rule semantics; these tables are the deterministic engine's data layer.
-- ===========================================================================

-- Full fetched option chain, every cycle, every strike in the fetched
-- window — not just the analysis window, so change-in-OI history survives
-- ATM re-centering as spot moves across cycles.
CREATE TABLE IF NOT EXISTS option_chain_snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_ts  TEXT NOT NULL,
    trading_date TEXT NOT NULL,
    expiry_date  TEXT NOT NULL,
    strike       REAL NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('CE', 'PE')),
    ltp          REAL,
    volume       INTEGER,
    oi           INTEGER,
    bid          REAL,
    bid_qty      INTEGER,
    ask          REAL,
    ask_qty      INTEGER,
    iv           REAL,
    delta        REAL,
    gamma        REAL,
    theta        REAL,
    vega         REAL,
    spot         REAL NOT NULL,
    UNIQUE (snapshot_ts, expiry_date, strike, side)
);

-- Single-row dual-cadence state for the rules engine (NORMAL vs WATCH mode).
-- id is pinned to 1 by the CHECK constraint; seeded once below.
CREATE TABLE IF NOT EXISTS options_engine_state (
    id               INTEGER PRIMARY KEY CHECK (id = 1),
    mode             TEXT NOT NULL DEFAULT 'NORMAL' CHECK (mode IN ('NORMAL', 'WATCH')),
    watch_level      REAL,
    watch_direction  TEXT CHECK (watch_direction IN ('UP', 'DOWN')),
    watch_started_ts TEXT,
    last_run_ts      TEXT,
    -- Trading date (YYYY-MM-DD) the weekly-vs-monthly Greeks sanity check
    -- last passed on. The check guards against a persistent SmartAPI bug,
    -- so once it passes for a trading day it doesn't need re-running
    -- every single cycle -- see skills/angel_client.py's
    -- assert_greeks_sanity() and the Task 7 rate-limit fix.
    greeks_sanity_checked_date TEXT,
    updated_ts       TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO options_engine_state (id, mode, updated_ts) VALUES (1, 'NORMAL', datetime('now'));

-- Every advisory the rules engine + trade selector ever produced, whether
-- notified or not (routine WAIT/NO_TRADE cycles stay SQLite-only).
CREATE TABLE IF NOT EXISTS options_advisories (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_ts     TEXT NOT NULL,
    action         TEXT NOT NULL CHECK (action IN ('BUY_CE_CANDIDATE', 'BUY_PE_CANDIDATE', 'WAIT', 'NO_TRADE')),
    payload_json   TEXT NOT NULL,
    score          INTEGER,
    rules_log_json TEXT,
    notified       INTEGER NOT NULL DEFAULT 0
);

-- Open/closed option positions. Never deleted once closed — same
-- never-delete-a-position rule as the equity `positions` table above.
CREATE TABLE IF NOT EXISTS options_positions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    status        TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'closed')),
    contract      TEXT NOT NULL,
    expiry_date   TEXT NOT NULL,
    strike        REAL NOT NULL,
    side          TEXT NOT NULL CHECK (side IN ('CE', 'PE')),
    qty_lots      INTEGER NOT NULL,
    lot_size      INTEGER NOT NULL,
    entry_premium REAL NOT NULL,
    entry_spot    REAL,
    thesis_json   TEXT NOT NULL,
    opened_ts     TEXT NOT NULL DEFAULT (datetime('now')),
    closed_ts     TEXT,
    exit_premium  REAL,
    -- Last position-monitor decision actually notified for this position
    -- (e.g. 'HOLD', 'EXIT_TARGET') -- dedup bookkeeping only, never the
    -- thesis itself (thesis_json is immutable, rulebook Rule 33).
    last_notified_state TEXT
);

-- Every WATCH-mode notification the engine sends (escalation/watching,
-- breach-unconfirmed, false breakout) -- separate from options_advisories
-- (which only persists CONFIRMED-stage trade decisions). Exists for
-- staleness tracking: valid_until is the next scheduled scan time, so a
-- reader (or a future dedup/cleanup pass) can tell whether an alert is
-- still current without re-deriving cadence rules.
CREATE TABLE IF NOT EXISTS options_alerts_sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    sent_ts     TEXT NOT NULL,
    alert_type  TEXT NOT NULL CHECK (alert_type IN ('WATCHING', 'BREACH_UNCONFIRMED', 'FALSE_BREAKOUT')),
    direction   TEXT CHECK (direction IN ('UP', 'DOWN')),
    level       REAL,
    data_ts     TEXT,
    valid_until TEXT NOT NULL
);

-- Broker-executed automated trades (Phase 17 auto-trading). Deliberately
-- separate from options_positions above: that table is populated by a human
-- replying to a Discord alert (options_position_entry.py) and has no notion
-- of a broker order id, symboltoken, or dry-run simulation. Keeping this
-- table distinct means the manual advisory/confirmation flow is untouched.
CREATE TABLE IF NOT EXISTS auto_options_positions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    status            TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    trade_date        TEXT NOT NULL,
    contract          TEXT NOT NULL,
    tradingsymbol     TEXT NOT NULL,
    symboltoken       TEXT NOT NULL,
    expiry_date       TEXT NOT NULL,
    strike            REAL NOT NULL,
    side              TEXT NOT NULL CHECK (side IN ('CE', 'PE')),
    qty_lots          INTEGER NOT NULL,
    lot_size          INTEGER NOT NULL,
    dry_run           INTEGER NOT NULL DEFAULT 1,
    entry_order_id    TEXT,
    entry_premium     REAL,
    entry_spot        REAL,
    option_stop       REAL NOT NULL,
    target_1          REAL NOT NULL,
    trend_label       TEXT,
    opened_ts         TEXT NOT NULL DEFAULT (datetime('now')),
    exit_order_id     TEXT,
    exit_premium      REAL,
    exit_reason       TEXT CHECK (exit_reason IN ('TARGET_HIT', 'SL_HIT', 'FORCED_SQUAREOFF')),
    realized_pnl      REAL,
    closed_ts         TEXT
);

CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_lookup ON option_chain_snapshots (trading_date, expiry_date, strike, side);
CREATE INDEX IF NOT EXISTS idx_option_chain_snapshots_ts ON option_chain_snapshots (snapshot_ts);
CREATE INDEX IF NOT EXISTS idx_options_advisories_created ON options_advisories (created_ts);
CREATE INDEX IF NOT EXISTS idx_options_positions_status ON options_positions (status);
CREATE INDEX IF NOT EXISTS idx_options_alerts_sent_ts ON options_alerts_sent (sent_ts);
CREATE INDEX IF NOT EXISTS idx_auto_options_positions_trade_date ON auto_options_positions (trade_date, status);
