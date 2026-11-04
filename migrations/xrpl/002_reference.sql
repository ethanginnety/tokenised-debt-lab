-- The prices the bot quotes from (M8, first testnet run): the book without the bot's own offers and
-- expired ones, and the AMM with its fee. best_bid, best_ask and mid stay the raw book, for the record.
-- ref_reasons is NULL on rows from before this, whose reference was never worked out.
ALTER TABLE xrpl_snapshots ADD COLUMN ref_bid TEXT;
ALTER TABLE xrpl_snapshots ADD COLUMN ref_ask TEXT;
ALTER TABLE xrpl_snapshots ADD COLUMN ref_mid TEXT;
ALTER TABLE xrpl_snapshots ADD COLUMN ref_reasons TEXT;
ALTER TABLE xrpl_snapshots ADD COLUMN excluded_account TEXT;
ALTER TABLE xrpl_snapshots ADD COLUMN own_left_out INTEGER;
ALTER TABLE xrpl_snapshots ADD COLUMN expired_left_out INTEGER;
