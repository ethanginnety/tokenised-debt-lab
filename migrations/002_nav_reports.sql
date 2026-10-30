-- Reported NAVs for fund shares (instrument C). Append-only, like curves:
-- each report is kept so its age and history can be shown.
CREATE TABLE IF NOT EXISTS nav_reports (
    id TEXT PRIMARY KEY,               -- fund_id:as_of:reported_at
    fund_id TEXT NOT NULL,
    nav_per_share TEXT NOT NULL,       -- Decimal as text
    as_of TEXT NOT NULL,               -- date the NAV values the portfolio at
    reported_at TEXT NOT NULL,         -- when it was published, UTC ISO-8601
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS nav_fund_idx ON nav_reports(fund_id, reported_at);
