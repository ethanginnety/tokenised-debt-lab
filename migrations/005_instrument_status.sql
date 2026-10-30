-- Where each instrument is in its life. No row means performing; a row is
-- written when that changes (matured now, defaulted with the default scenario).
CREATE TABLE IF NOT EXISTS instrument_status (
    instrument_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('performing', 'matured', 'defaulted')),
    since TEXT NOT NULL,               -- simulation date the status took effect
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL           -- UTC wall clock, for the record only
);
