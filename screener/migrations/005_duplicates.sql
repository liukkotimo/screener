-- Listings of the same company on several exchanges: all but one are deactivated by `screener dedupe`.

ALTER TABLE item ADD COLUMN duplicate_of INTEGER REFERENCES item(item_id) ON DELETE SET NULL;  -- the listing kept instead
