-- What this database is for: 'main' (the app's normal database) or
-- 'scenario' (a throwaway copy for lifecycle demos). Set once, never changed.
CREATE TABLE IF NOT EXISTS db_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Everyone who can own something: test holders and simulated issuers.
CREATE TABLE IF NOT EXISTS parties (
    id TEXT PRIMARY KEY,               -- e.g. alder, issuer:SHORT-USD-2029
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('holder', 'issuer')),
    description TEXT NOT NULL DEFAULT '',
    payments_blocked INTEGER NOT NULL DEFAULT 0,  -- 1 = compliance hold on payments out to this holder
    created_at TEXT NOT NULL
);

-- Double-entry journal. A transaction is a set of entries whose amounts sum
-- to zero for each asset, so nothing is created or lost. Never updated.
CREATE TABLE IF NOT EXISTS ledger_txns (
    id TEXT PRIMARY KEY,               -- deterministic, e.g. issue:SHORT-USD-2029
    kind TEXT NOT NULL,                -- issue, transfer, coupon, ...
    effective_date TEXT NOT NULL,      -- the date it counts from (balances as of a date use this)
    fingerprint TEXT NOT NULL,         -- hash of the contents: same id + different contents is refused
    memo TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL           -- UTC wall clock, for the record only
);
CREATE INDEX IF NOT EXISTS ledger_txns_date_idx ON ledger_txns(effective_date);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    txn_id TEXT NOT NULL REFERENCES ledger_txns(id),
    owner TEXT NOT NULL REFERENCES parties(id),
    asset TEXT NOT NULL,               -- a currency (USD, GBP) or an instrument id (units)
    amount TEXT NOT NULL               -- Decimal as text, summed in Python, never with SQL SUM (float)
);
CREATE INDEX IF NOT EXISTS ledger_entries_owner_idx ON ledger_entries(owner, asset);
CREATE INDEX IF NOT EXISTS ledger_entries_txn_idx ON ledger_entries(txn_id);
