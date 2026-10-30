-- Market maker controls (M4 step 3). Limits and the bot's state are append-only:
-- the one in force is the latest row, so every change is kept with who made it
-- and why, and a restart reads exactly what was in force before it.
CREATE TABLE IF NOT EXISTS mm_limit_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,                -- e.g. max_inventory
    scope TEXT NOT NULL,               -- an instrument id, a currency, or 'all'
    value TEXT NOT NULL,               -- Decimal as text
    changed_at TEXT NOT NULL,          -- UTC
    changed_by TEXT NOT NULL,          -- 'config' for the starting values, else who set it
    reason TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS mm_limit_changes_idx ON mm_limit_changes(name, scope, id);

CREATE TABLE IF NOT EXISTS mm_state_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    state TEXT NOT NULL CHECK (state IN ('running', 'paused', 'killed')),
    changed_at TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    reason TEXT NOT NULL
);

-- Orders the market maker has placed (its quotes, one row per side). Open orders
-- count against the limits; pausing, a daily-loss breach and the kill switch cancel them.
CREATE TABLE IF NOT EXISTS mm_orders (
    id TEXT PRIMARY KEY,               -- deterministic, so placing it again does nothing
    instrument_id TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    price TEXT NOT NULL,               -- clean, per 100 face
    size TEXT NOT NULL,                -- face
    status TEXT NOT NULL CHECK (status IN ('open', 'cancelled', 'filled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT ''    -- why it was cancelled
);
CREATE INDEX IF NOT EXISTS mm_orders_status_idx ON mm_orders(status, instrument_id);
