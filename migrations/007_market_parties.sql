-- foreign-keys: off
-- Market maker (M4 step 1). Parties gain three roles: the market maker, the
-- simulated market (the counterparty to every client order) and the funding
-- account that brings cash into the simulation. SQLite can't change a CHECK and
-- other tables reference parties, so the table is rebuilt with foreign keys off;
-- the runner checks every reference before committing.
CREATE TABLE parties_new (
    id TEXT PRIMARY KEY,               -- e.g. alder, issuer:SHORT-USD-2029, market-maker
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('holder', 'issuer', 'market_maker', 'market', 'funding')),
    description TEXT NOT NULL DEFAULT '',
    payments_blocked INTEGER NOT NULL DEFAULT 0,  -- 1 = compliance hold on payments out to this holder
    created_at TEXT NOT NULL
);
INSERT INTO parties_new SELECT id, name, role, description, payments_blocked, created_at FROM parties;
DROP TABLE parties;
ALTER TABLE parties_new RENAME TO parties;

-- Each bond's final record date, from its schedule and record-date rule. Units
-- can't move after it except to be redeemed (fixes risk R15). Written when the
-- ledger is seeded, from config/instruments.json.
CREATE TABLE IF NOT EXISTS bond_terms (
    instrument_id TEXT PRIMARY KEY,
    final_record_date TEXT NOT NULL,
    updated_at TEXT NOT NULL           -- UTC wall clock, for the record only
);
