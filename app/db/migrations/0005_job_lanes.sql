ALTER TABLE jobs ADD COLUMN application_lane TEXT NOT NULL DEFAULT 'batch'
    CHECK (application_lane IN ('batch', 'fast_lane'));
