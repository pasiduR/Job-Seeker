CREATE TABLE sources (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL CHECK (
        type IN ('job_board', 'ats_board', 'career_page', 'rss', 'email_alert')
    ),
    url TEXT NOT NULL,
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (type, url)
);

CREATE TABLE search_filters (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    roles TEXT[] NOT NULL DEFAULT '{}',
    locations TEXT[] NOT NULL DEFAULT '{}',
    remote BOOLEAN,
    exclude_keywords TEXT[] NOT NULL DEFAULT '{}',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE subscriptions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    search_filter_id BIGINT NOT NULL REFERENCES search_filters(id) ON DELETE CASCADE,
    polling_interval_minutes INTEGER NOT NULL DEFAULT 10
        CHECK (polling_interval_minutes > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_polled_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (source_id, search_filter_id)
);

CREATE TABLE schedules (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    cron_expression TEXT NOT NULL,
    pipeline_step TEXT,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    last_enqueued_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE jobs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id BIGINT REFERENCES sources(id) ON DELETE SET NULL,
    source_job_id TEXT,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    url TEXT NOT NULL,
    description TEXT NOT NULL,
    location TEXT,
    remote BOOLEAN,
    posted_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'found' CHECK (
        status IN (
            'found', 'scored', 'tailored', 'filled', 'approved', 'submitted',
            'skipped', 'failed', 'needs_manual'
        )
    ),
    score SMALLINT CHECK (score BETWEEN 1 AND 10),
    score_details JSONB,
    failure_reason TEXT,
    needs_manual_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX jobs_url_unique ON jobs (url);
CREATE UNIQUE INDEX jobs_company_title_unique ON jobs (lower(company), lower(title));
CREATE UNIQUE INDEX jobs_source_job_unique
    ON jobs (source_id, source_job_id)
    WHERE source_job_id IS NOT NULL;

CREATE TABLE cv_versions (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id BIGINT REFERENCES jobs(id) ON DELETE CASCADE,
    is_base BOOLEAN NOT NULL DEFAULT FALSE,
    tex TEXT NOT NULL,
    pdf_path TEXT NOT NULL,
    diff_from_base TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CHECK ((is_base AND job_id IS NULL) OR (NOT is_base AND job_id IS NOT NULL))
);

CREATE UNIQUE INDEX one_base_cv ON cv_versions (is_base) WHERE is_base;
CREATE UNIQUE INDEX one_tailored_cv_per_job ON cv_versions (job_id) WHERE job_id IS NOT NULL;

CREATE TABLE applications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id BIGINT NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
    cv_version_id BIGINT NOT NULL REFERENCES cv_versions(id) ON DELETE RESTRICT,
    external_reference TEXT,
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE skills_to_learn (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    skill TEXT NOT NULL,
    estimated_days INTEGER NOT NULL CHECK (estimated_days >= 0),
    learning_plan TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, skill)
);

CREATE TABLE profile (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    data JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE queue_jobs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    run_id UUID NOT NULL,
    job_id BIGINT REFERENCES jobs(id) ON DELETE CASCADE,
    task TEXT NOT NULL,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_at TIMESTAMPTZ,
    locked_by TEXT,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX queue_jobs_claimable
    ON queue_jobs (available_at, id)
    WHERE status = 'queued';

CREATE TABLE notifications (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id BIGINT REFERENCES jobs(id) ON DELETE CASCADE,
    channel TEXT NOT NULL,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'sent', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    sent_at TIMESTAMPTZ,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE run_logs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id UUID NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'schedule', 'event')),
    step TEXT,
    job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
    status TEXT NOT NULL,
    duration_ms INTEGER CHECK (duration_ms >= 0),
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX run_logs_run_id_index ON run_logs (run_id, created_at);

CREATE TABLE settings (
    key TEXT PRIMARY KEY,
    value JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

INSERT INTO settings (key, value) VALUES
    ('score_threshold', '7'::jsonb),
    ('max_skill_days', '7'::jsonb),
    ('max_added_skills', '3'::jsonb),
    ('skill_placement', '"currently_learning"'::jsonb),
    ('auto_submit', 'false'::jsonb),
    ('batch_daily_cap', '10'::jsonb),
    ('fast_lane_daily_cap', '5'::jsonb)
ON CONFLICT (key) DO NOTHING;

CREATE TABLE llm_calls (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
    step TEXT NOT NULL,
    prompt_version TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER CHECK (input_tokens >= 0),
    output_tokens INTEGER CHECK (output_tokens >= 0),
    cost_usd NUMERIC(12, 6) CHECK (cost_usd >= 0),
    latency_ms INTEGER NOT NULL CHECK (latency_ms >= 0),
    valid_output BOOLEAN NOT NULL,
    error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE form_traces (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    sequence_number INTEGER NOT NULL CHECK (sequence_number > 0),
    tool TEXT NOT NULL CHECK (
        tool IN ('extract_fields', 'fill_field', 'upload_file', 'click_next', 'screenshot')
    ),
    input JSONB NOT NULL DEFAULT '{}'::jsonb,
    result JSONB NOT NULL DEFAULT '{}'::jsonb,
    screenshot_path TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, sequence_number)
);
