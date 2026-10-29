-- Audit log: append-only record of everything material the system does.
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,          -- UTC ISO-8601
    kind TEXT NOT NULL,                -- e.g. app_started, curve_loaded, valuation
    entity TEXT NOT NULL DEFAULT '',   -- instrument id, currency, ...
    mode TEXT NOT NULL,                -- simulation | paper | live
    payload TEXT NOT NULL,             -- JSON
    idempotency_key TEXT UNIQUE        -- same key twice = recorded once
);
CREATE INDEX IF NOT EXISTS events_kind_idx ON events(kind, id);

-- Every curve ever loaded, so the last valid one survives outages/restarts.
CREATE TABLE IF NOT EXISTS curve_snapshots (
    id TEXT PRIMARY KEY,               -- currency:source:observation_date
    currency TEXT NOT NULL,
    kind TEXT NOT NULL,
    observation_date TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS curve_ccy_idx ON curve_snapshots(currency, observation_date);
