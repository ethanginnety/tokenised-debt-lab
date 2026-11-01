-- RLUSD/XRP monitor (M7 step 3). Its own database (data/xrpl.sqlite3 by default), never a bond database.
-- Amounts and prices are stored as decimal strings. Times are UTC ISO 8601.

CREATE TABLE xrpl_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT INTO xrpl_meta(key, value) VALUES ('role', 'xrpl-monitor');

-- One row per start of the monitor.
CREATE TABLE xrpl_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    stopped_at TEXT,
    stop_reason TEXT
);

-- The book (and the AMM beside it) at one validated ledger.
-- kind 'top': a shallow read every ledger (best prices, spread);
-- kind 'deep': a deeper read every N ledgers (depth bands).
CREATE TABLE xrpl_snapshots (
    ledger_index INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES xrpl_runs(id),
    endpoint TEXT NOT NULL,
    close_time TEXT NOT NULL,          -- the ledger's close time
    received_at TEXT NOT NULL,         -- when the read finished here
    kind TEXT NOT NULL CHECK (kind IN ('top', 'deep')),
    valid INTEGER NOT NULL,
    reasons TEXT NOT NULL,             -- JSON list; empty when valid
    best_bid TEXT, best_ask TEXT, mid TEXT, spread_bp TEXT,
    bid_offers INTEGER NOT NULL, ask_offers INTEGER NOT NULL, dropped INTEGER NOT NULL,
    bids_complete INTEGER NOT NULL, asks_complete INTEGER NOT NULL,
    depth_json TEXT NOT NULL,          -- [{band_bp, bid_rlusd, ask_rlusd, bid_xrp, ask_xrp, complete}]
    levels_json TEXT,                  -- top levels each side; thinned after a few days
    amm_xrp TEXT, amm_rlusd TEXT, amm_fee_pct TEXT
);
CREATE INDEX xrpl_snapshots_received ON xrpl_snapshots(received_at);

-- Trades on the pair, derived from validated transaction metadata: book offers consumed,
-- and swaps through the AMM pool. One row per consumed offer or per swap.
CREATE TABLE xrpl_trades (
    tx_hash TEXT NOT NULL,
    node TEXT NOT NULL,                -- the consumed offer's ledger index, or 'amm'
    ledger_index INTEGER NOT NULL,
    venue TEXT NOT NULL CHECK (venue IN ('book', 'amm')),
    taker_side TEXT NOT NULL CHECK (taker_side IN ('buy_xrp', 'sell_xrp')),
    price TEXT NOT NULL,               -- RLUSD per XRP, average for this row
    xrp TEXT NOT NULL,
    rlusd TEXT NOT NULL,
    maker TEXT,                        -- offer owner (book); the AMM account (amm)
    pool_price_before TEXT,            -- AMM only: pool price before and after, fee excluded
    pool_price_after TEXT,
    tx_type TEXT NOT NULL,
    received_at TEXT NOT NULL,
    PRIMARY KEY (tx_hash, node)
);
CREATE INDEX xrpl_trades_ledger ON xrpl_trades(ledger_index);

-- Times the data wasn't fresh, with the reason. At most one open (ended_at NULL) at a time.
CREATE TABLE xrpl_outages (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL CHECK (kind IN ('not_running', 'disconnected', 'stale', 'missed_ledgers')),
    started_at TEXT NOT NULL,
    ended_at TEXT,
    detail TEXT NOT NULL
);

-- Things worth knowing that aren't outages: failovers, read failures, issuer changes, FX failures.
CREATE TABLE xrpl_events (
    id INTEGER PRIMARY KEY,
    at TEXT NOT NULL,
    kind TEXT NOT NULL,
    detail TEXT NOT NULL
);

-- Last sign of life, so time the monitor wasn't running at all shows as a gap on the next start.
CREATE TABLE xrpl_heartbeat (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    at TEXT NOT NULL,
    run_id INTEGER NOT NULL,
    ledger_index INTEGER
);

-- The RLUSD issuer's settings, read at a validated ledger, at start and then every so often.
CREATE TABLE xrpl_issuer_checks (
    ledger_index INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    changed INTEGER NOT NULL           -- 1 if different from the previous check
);

-- Network reserves and fee, read from a validated ledger.
CREATE TABLE xrpl_network (
    ledger_index INTEGER PRIMARY KEY,
    checked_at TEXT NOT NULL,
    reserve_base_xrp TEXT NOT NULL,
    reserve_inc_xrp TEXT NOT NULL,
    base_fee_drops TEXT NOT NULL,
    current_fee_drops TEXT NOT NULL
);

-- ECB reference rates (decision D): USD per EUR, one per observation date.
CREATE TABLE fx_rates (
    pair TEXT NOT NULL,
    obs_date TEXT NOT NULL,
    rate TEXT NOT NULL,
    source TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (pair, obs_date)
);
