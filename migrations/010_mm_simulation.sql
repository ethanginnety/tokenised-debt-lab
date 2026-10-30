-- Simulated market (M4 step 5). Orders can fill in part, so they track how much has filled.
ALTER TABLE mm_orders ADD COLUMN filled TEXT NOT NULL DEFAULT '0';

-- One row per simulation run. A run is resumable: `last_step` is the last step whose
-- quotes and fills were committed together, so a crash part-way is finished by running
-- the same run id again, which continues from the next step with the same random draws.
CREATE TABLE IF NOT EXISTS mm_runs (
    id TEXT PRIMARY KEY,
    seed TEXT NOT NULL,
    params TEXT NOT NULL,              -- JSON: everything that shapes the run
    start TEXT NOT NULL,               -- JSON: units, cash and marks before the first step
    started_at TEXT NOT NULL,          -- simulated clock, UTC
    last_step INTEGER NOT NULL DEFAULT -1,
    status TEXT NOT NULL CHECK (status IN ('running', 'finished')),
    summary TEXT                       -- JSON, once finished
);

-- Every simulated fill, with what the bot thought the bond was worth and what it was
-- really worth at that moment (the simulation knows both; the bot only its own).
CREATE TABLE IF NOT EXISTS mm_fills (
    id TEXT PRIMARY KEY,               -- run:step:instrument:side:client, so a replayed step books nothing twice
    run_id TEXT NOT NULL REFERENCES mm_runs(id),
    step INTEGER NOT NULL,
    order_id TEXT,                     -- NULL for an unwind against the outside market
    instrument_id TEXT NOT NULL,
    currency TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),    -- the market maker's side
    client TEXT NOT NULL CHECK (client IN ('uninformed', 'informed', 'unwind')),
    price TEXT NOT NULL,               -- clean per 100
    size TEXT NOT NULL,                -- face
    cash TEXT NOT NULL,                -- as booked in the ledger (clean + accrued, to the cent)
    bot_fair_dirty TEXT NOT NULL,      -- the bot's fair value per 100 at the fill (lagged data)
    true_fair_dirty TEXT NOT NULL,     -- the simulation's true value per 100 at the fill
    fee TEXT NOT NULL,
    txn_id TEXT NOT NULL REFERENCES ledger_txns(id),
    at TEXT NOT NULL                   -- simulated clock, UTC
);
CREATE INDEX IF NOT EXISTS mm_fills_run_idx ON mm_fills(run_id, step);
