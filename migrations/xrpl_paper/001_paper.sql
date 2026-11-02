-- RLUSD/XRP paper trading (M7 steps 5-6). PAPER: nothing here is an order on any ledger.
-- Its own database (data/xrpl_paper.sqlite3 by default), never a bond database and never the
-- monitor's: the paper bot only reads the monitor's database.
-- Amounts and prices are decimal strings. Times are UTC ISO 8601. Prices are RLUSD per XRP.

CREATE TABLE paper_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO paper_meta(key, value) VALUES ('role', 'xrpl-paper');

-- One row per start of the paper bot.
CREATE TABLE paper_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    stop_reason TEXT
);

CREATE TABLE paper_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    at TEXT NOT NULL,
    run_id INTEGER NOT NULL
);

-- Bot state and limits: append-only, the latest row is in force (as M4's mm_ tables).
CREATE TABLE paper_state_changes (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('running', 'paused', 'killed')),
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE paper_limit_changes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT NOT NULL
);

-- Two paper accounts on the same quotes, one per fill rule (decision E).
CREATE TABLE paper_accounts (
    name TEXT PRIMARY KEY,
    rule TEXT NOT NULL CHECK (rule IN ('conservative', 'optimistic')),
    opened_at TEXT NOT NULL,
    ledger_index INTEGER NOT NULL,
    mid TEXT NOT NULL,
    usd_per_eur TEXT NOT NULL,
    start_eur TEXT NOT NULL
);

-- Every change to an account's balances, append-only: balances are the sums.
-- mid: the book mid when it happened, for the P&L split.
CREATE TABLE paper_journal (
    id INTEGER PRIMARY KEY,
    account TEXT NOT NULL REFERENCES paper_accounts(name),
    at TEXT NOT NULL,
    ledger_index INTEGER,
    kind TEXT NOT NULL CHECK (kind IN ('open', 'fill', 'fee')),
    xrp TEXT NOT NULL,
    rlusd TEXT NOT NULL,
    mid TEXT NOT NULL,
    ref TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    UNIQUE (account, kind, ref)
);

-- Every quoting decision, quoted or not, with its reasons and the parts of the half-spread.
CREATE TABLE paper_decisions (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    ledger_index INTEGER,
    quoted INTEGER NOT NULL,
    reasons TEXT NOT NULL,             -- JSON list
    fair TEXT,
    components TEXT NOT NULL,          -- JSON: base, vol, age, fee, inventory skew (bp) and inputs
    bid_price TEXT, ask_price TEXT, bid_size TEXT, ask_size TEXT
);

CREATE TABLE paper_orders (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL REFERENCES paper_accounts(name),
    decision_id INTEGER REFERENCES paper_decisions(id),
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),   -- the bot's side, in XRP
    price TEXT NOT NULL,
    size TEXT NOT NULL,                -- XRP
    filled TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL CHECK (status IN ('open', 'filled', 'cancelled')),
    placed_at TEXT NOT NULL,
    placed_ledger INTEGER NOT NULL,    -- fills only from trades in later ledgers
    closed_at TEXT,
    closed_ledger INTEGER,             -- cancel decided after seeing this ledger: still in force for its trades
    reason TEXT
);
CREATE INDEX paper_orders_open ON paper_orders(status, account);

-- Paper fills: one per order per ledger, from the real trades that ledger (decision E).
CREATE TABLE paper_fills (
    id TEXT PRIMARY KEY,
    account TEXT NOT NULL REFERENCES paper_accounts(name),
    order_id TEXT NOT NULL REFERENCES paper_orders(id),
    ledger_index INTEGER NOT NULL,
    at TEXT NOT NULL,
    side TEXT NOT NULL,
    price TEXT NOT NULL,
    xrp TEXT NOT NULL,
    rlusd TEXT NOT NULL,
    mid TEXT NOT NULL,
    trades TEXT NOT NULL               -- JSON: the real trades that filled it
);

-- Each account's value at the first tick of each UTC day, for the daily loss limit.
CREATE TABLE paper_day_open (
    account TEXT NOT NULL REFERENCES paper_accounts(name),
    day TEXT NOT NULL,
    at TEXT NOT NULL,
    mid TEXT NOT NULL,
    value_rlusd TEXT NOT NULL,
    PRIMARY KEY (account, day)
);

-- How far the bot has read the monitor's trades.
CREATE TABLE paper_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ledger_index INTEGER NOT NULL
);

-- Audit: placed, refused, cancelled, state and limit changes, safety cancels.
CREATE TABLE paper_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL               -- JSON
);
