-- Default scenario. instrument_status gains 'grace_period' (a coupon missed,
-- default not yet declared). SQLite can't change a CHECK, so the table is rebuilt.
CREATE TABLE instrument_status_new (
    instrument_id TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (status IN ('performing', 'grace_period', 'defaulted', 'matured')),
    since TEXT NOT NULL,               -- simulation date the status took effect
    reason TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL           -- UTC wall clock, for the record only
);
INSERT INTO instrument_status_new SELECT instrument_id, status, since, reason, updated_at FROM instrument_status;
DROP TABLE instrument_status;
ALTER TABLE instrument_status_new RENAME TO instrument_status;

-- Entitlements gain 'written_off': a missed coupon whose claim was settled by
-- the default recovery (the recovery is a share of face + missed coupons).
CREATE TABLE entitlements_new (
    payment_id TEXT NOT NULL REFERENCES payment_events(id),
    holder TEXT NOT NULL REFERENCES parties(id),
    units TEXT NOT NULL,
    amount_due TEXT NOT NULL,
    amount_paid TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL CHECK (status IN ('due', 'paid', 'unpaid', 'written_off')),
    reason TEXT NOT NULL DEFAULT '',
    txn_id TEXT REFERENCES ledger_txns(id),
    paid_at TEXT,
    PRIMARY KEY (payment_id, holder)
);
INSERT INTO entitlements_new SELECT payment_id, holder, units, amount_due, amount_paid, status, reason, txn_id, paid_at
    FROM entitlements;
DROP TABLE entitlements;
ALTER TABLE entitlements_new RENAME TO entitlements;

-- One row per bond whose default was triggered, with the terms fixed at that moment.
CREATE TABLE IF NOT EXISTS defaults (
    instrument_id TEXT PRIMARY KEY,
    triggered_on TEXT NOT NULL,        -- simulation date of the trigger
    missed_payment_id TEXT NOT NULL,   -- the first coupon after the trigger
    missed_pay_date TEXT NOT NULL,
    missed_scheduled_date TEXT NOT NULL,
    missed_per_100 TEXT NOT NULL,      -- the missed coupon per 100 face
    grace_days INTEGER NOT NULL,
    distressed_spread_bp TEXT NOT NULL,
    recovery_rate TEXT NOT NULL,       -- fraction of the claim (face + missed coupons)
    recovery_delay_months INTEGER NOT NULL,
    declared_on TEXT,                  -- set when the grace period ends unpaid
    recovery_date TEXT,                -- declared_on + delay (before the weekend roll)
    recovery_paid_on TEXT,             -- set once every recovery entitlement is paid or recorded unpaid
    created_at TEXT NOT NULL
);
