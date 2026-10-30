-- Every quoting decision the market maker makes (M4 step 4), including the ones
-- where it quoted nothing, with the reasons. Append-only. The main columns are for
-- reading at a glance; `detail` holds the full record: spread components and their
-- reasons, data timestamps, inventory, sizes, spread in currency and bp, and the
-- limits in force.
CREATE TABLE IF NOT EXISTS mm_quotes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instrument_id TEXT NOT NULL,
    quoted_at TEXT NOT NULL,           -- UTC
    status TEXT NOT NULL CHECK (status IN ('quoted', 'one_sided', 'none')),
    quoting_state TEXT NOT NULL,       -- normal, restricted, halted, never (fund share), unknown (no curve)
    fair_clean TEXT,                   -- per 100; NULL when nothing could be valued
    bid TEXT,                          -- clean per 100; NULL when that side isn't quoted
    ask TEXT,
    reasons TEXT NOT NULL,             -- JSON list: why nothing, or why a side is missing
    detail TEXT NOT NULL               -- JSON
);
CREATE INDEX IF NOT EXISTS mm_quotes_inst_idx ON mm_quotes(instrument_id, id);
