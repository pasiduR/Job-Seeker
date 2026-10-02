CREATE TABLE request_pacing (
    source_key TEXT PRIMARY KEY,
    next_allowed_at TIMESTAMPTZ NOT NULL
);
