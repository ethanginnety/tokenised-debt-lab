-- RLUSD/XRP on the XRP Ledger TESTNET (M8). Its own database, never the monitor's, the paper
-- bot's or a bond database. No key, seed or secret is ever stored here.
-- Amounts and prices are decimal strings. Times are UTC ISO 8601. Prices are RLUSD per XRP.

CREATE TABLE live_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO live_meta(key, value) VALUES ('role', 'xrpl-live');
INSERT INTO live_meta(key, value) VALUES ('network', 'testnet');

CREATE TABLE live_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    stop_reason TEXT
);

CREATE TABLE live_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    at TEXT NOT NULL,
    run_id INTEGER NOT NULL
);

-- Bot state and limits: append-only, the latest row is in force.
CREATE TABLE live_state_changes (
    id INTEGER PRIMARY KEY,
    state TEXT NOT NULL CHECK (state IN ('running', 'paused', 'killed')),
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE live_limit_changes (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    value TEXT NOT NULL,
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT NOT NULL
);

-- The one account, from its first validated read.
CREATE TABLE live_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    address TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    ledger_index INTEGER NOT NULL,
    mid TEXT NOT NULL,
    usd_per_eur TEXT NOT NULL,
    aside_xrp TEXT NOT NULL DEFAULT '0',     -- on the ledger, over the budget: the bot doesn't use it
    aside_rlusd TEXT NOT NULL DEFAULT '0'
);

-- Every change to the budget's balances, from validated ledgers, append-only.
-- external: moved by something other than the bot's own trading (a faucet, a wallet payment).
CREATE TABLE live_journal (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    ledger_index INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('open', 'maker_fill', 'taker_fill', 'fee', 'external', 'rounding')),
    xrp TEXT NOT NULL,
    rlusd TEXT NOT NULL,
    mid TEXT NOT NULL,
    ref TEXT NOT NULL,
    memo TEXT NOT NULL DEFAULT '',
    UNIQUE (kind, ref)
);

-- Written, and committed, BEFORE the transaction is sent: a crash can't lose track of one.
-- pending: sent or maybe sent, outcome unknown until validated or its last ledger has passed.
CREATE TABLE live_txs (
    hash TEXT PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('OfferCreate', 'OfferCancel')),
    sequence INTEGER NOT NULL,
    last_ledger INTEGER NOT NULL,
    offer_sequence INTEGER,
    order_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('pending', 'applied', 'failed', 'not_applied')),
    preliminary TEXT,
    result TEXT,
    ledger_index INTEGER,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE INDEX live_txs_status ON live_txs(status);

-- pending: not yet seen on a validated ledger. open: resting. The ledger decides the rest.
CREATE TABLE live_orders (
    id TEXT PRIMARY KEY,
    decision_id INTEGER,
    paper_order_id TEXT,               -- the paper order on the same quote, for the comparison
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    price TEXT NOT NULL,
    size TEXT NOT NULL,                -- XRP
    sequence INTEGER NOT NULL,         -- the OfferCreate's Sequence: the offer's identity on the ledger
    expiration INTEGER NOT NULL,       -- Ripple-epoch seconds, on a 10 s boundary
    tx_hash TEXT NOT NULL,
    replaces INTEGER,                  -- the offer this replaced, in one transaction
    filled_xrp TEXT NOT NULL DEFAULT '0',
    filled_rlusd TEXT NOT NULL DEFAULT '0',
    took TEXT NOT NULL DEFAULT '0',    -- XRP of it that took liquidity on arrival: should stay 0
    status TEXT NOT NULL CHECK (status IN ('pending', 'open', 'filled', 'cancelled', 'expired', 'failed')),
    placed_at TEXT NOT NULL,
    placed_ledger INTEGER NOT NULL,
    applied_ledger INTEGER,
    closed_at TEXT,
    closed_ledger INTEGER,
    reason TEXT
);
CREATE INDEX live_orders_status ON live_orders(status);

-- One per order per transaction that filled it.
CREATE TABLE live_fills (
    id TEXT PRIMARY KEY,
    order_id TEXT,
    ledger_index INTEGER NOT NULL,
    at TEXT NOT NULL,
    tx_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('maker', 'taker')),
    side TEXT NOT NULL,
    xrp TEXT NOT NULL,
    rlusd TEXT NOT NULL,
    mid TEXT NOT NULL
);

CREATE TABLE live_day_open (
    day TEXT PRIMARY KEY,
    at TEXT NOT NULL,
    mid TEXT NOT NULL,
    value_rlusd TEXT NOT NULL
);

-- How far the bot has read the account's validated transactions.
CREATE TABLE live_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    ledger_index INTEGER NOT NULL
);

CREATE TABLE live_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL               -- JSON
);
