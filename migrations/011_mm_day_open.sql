-- The market maker's position and marks at the start of each day (M4 step 6), so the
-- daily-loss limit can work out the day's P&L from the ledger instead of being told it.
-- Written the first time a day's P&L is needed; afterwards only a late opening mark is added.
CREATE TABLE IF NOT EXISTS mm_day_open (
    day TEXT PRIMARY KEY,              -- UTC calendar day of the clock (the simulated clock in a simulation)
    opened_at TEXT NOT NULL,           -- that clock, UTC
    last_entry_id INTEGER NOT NULL,    -- ledger entries after this one happened during the day
    position TEXT NOT NULL,            -- JSON: asset -> units or cash held by the market maker
    marks TEXT NOT NULL                -- JSON: instrument -> {currency, dirty per 100, late?}
);

-- Each configured instrument's currency, so a holding that can't be valued today still
-- counts against the right currency (and only that one). Written by seed_book.
CREATE TABLE IF NOT EXISTS instrument_currencies (
    instrument_id TEXT PRIMARY KEY,
    currency TEXT NOT NULL
);
