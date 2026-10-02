-- One row per job with the latest form-fill result; form_traces holds its steps.
CREATE TABLE form_fills (
    job_id BIGINT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    cv_version_id BIGINT NOT NULL REFERENCES cv_versions(id) ON DELETE RESTRICT,
    outcome TEXT NOT NULL CHECK (outcome IN ('filled', 'needs_manual')),
    reason TEXT NOT NULL,
    form_url TEXT NOT NULL,
    answers JSONB NOT NULL DEFAULT '[]'::jsonb,
    screenshot_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
