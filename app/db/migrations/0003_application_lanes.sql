-- Batch and fast-lane applications have separate daily caps.
ALTER TABLE applications
    ADD COLUMN lane TEXT NOT NULL DEFAULT 'batch' CHECK (lane IN ('batch', 'fast_lane')),
    ADD COLUMN screenshot_path TEXT;

CREATE INDEX applications_lane_submitted_at ON applications (lane, submitted_at);
