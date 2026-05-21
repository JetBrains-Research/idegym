-- snapshot_prepare_requests must come first: snapshot_jobs references it
CREATE TABLE IF NOT EXISTS snapshot_prepare_requests (
    id UUID PRIMARY KEY,
    total_requested INTEGER NOT NULL,
    succeeded INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    created_at BIGINT,
    updated_at BIGINT
);

CREATE TABLE IF NOT EXISTS snapshots (
    id BIGSERIAL PRIMARY KEY,
    snapshot_name VARCHAR NOT NULL,
    request_hash VARCHAR NOT NULL,
    namespace VARCHAR NOT NULL,
    image_tag VARCHAR NOT NULL,
    server_name VARCHAR NOT NULL,
    runtime_class_name VARCHAR,
    run_as_root BOOLEAN NOT NULL DEFAULT FALSE,
    server_kind VARCHAR NOT NULL,
    created_at BIGINT,
    updated_at BIGINT
);

CREATE INDEX IF NOT EXISTS ix_snapshots_request_hash ON snapshots (request_hash);

CREATE TABLE IF NOT EXISTS snapshot_jobs (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR NOT NULL,
    status VARCHAR,
    request_hash VARCHAR NOT NULL,
    request TEXT NOT NULL,
    snapshot_id BIGINT REFERENCES snapshots(id),
    prepare_request_id UUID REFERENCES snapshot_prepare_requests(id),
    details TEXT,
    created_at BIGINT,
    updated_at BIGINT
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_snapshot_jobs_job_id ON snapshot_jobs (job_id);
CREATE INDEX IF NOT EXISTS ix_snapshot_jobs_request_hash ON snapshot_jobs (request_hash);
