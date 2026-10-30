-- Scheduled payments (coupons now; principal and default in later steps).
-- One row per instrument, kind and payment date. Entitlements are fixed when
-- the event is scheduled, from holdings at the end of the record date.
CREATE TABLE IF NOT EXISTS payment_events (
    id TEXT PRIMARY KEY,               -- e.g. coupon:SHORT-USD-2029:2026-08-17
    instrument_id TEXT NOT NULL,
    kind TEXT NOT NULL,                -- coupon (principal arrives with maturity)
    pay_date TEXT NOT NULL,
    record_date TEXT NOT NULL,         -- holders at the end of this day are paid
    accrual_start TEXT NOT NULL,
    accrual_end TEXT NOT NULL,
    currency TEXT NOT NULL,
    amount_per_100 TEXT NOT NULL,      -- cash per 100 face, Decimal as text
    status TEXT NOT NULL CHECK (status IN ('scheduled', 'in_progress', 'paid', 'partly_paid', 'missed')),
    total_due TEXT NOT NULL,           -- sum of holder amounts, each rounded to cents
    total_paid TEXT NOT NULL DEFAULT '0',
    rounding_residue TEXT NOT NULL,    -- exact total minus total_due (rounding to cents)
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (instrument_id, kind, pay_date)
);
CREATE INDEX IF NOT EXISTS payment_events_instrument_idx ON payment_events(instrument_id, record_date);

-- What each holder is owed for one payment, and whether it was paid.
CREATE TABLE IF NOT EXISTS entitlements (
    payment_id TEXT NOT NULL REFERENCES payment_events(id),
    holder TEXT NOT NULL REFERENCES parties(id),
    units TEXT NOT NULL,               -- face held at the end of the record date
    amount_due TEXT NOT NULL,          -- rounded to cents (banker's rounding)
    amount_paid TEXT NOT NULL DEFAULT '0',
    status TEXT NOT NULL CHECK (status IN ('due', 'paid', 'unpaid')),
    reason TEXT NOT NULL DEFAULT '',   -- why an amount is unpaid, e.g. payments blocked
    txn_id TEXT REFERENCES ledger_txns(id),  -- the ledger transaction that paid it
    paid_at TEXT,                      -- UTC wall clock, for the record only
    PRIMARY KEY (payment_id, holder)
);
