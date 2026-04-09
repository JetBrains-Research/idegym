-- Create tables if they don't exist

-- Create clients table
CREATE TABLE IF NOT EXISTS clients (
    id UUID PRIMARY KEY,
    name VARCHAR,
    namespace VARCHAR,
    created_at BIGINT,
    last_heartbeat_time BIGINT,
    availability VARCHAR,
    nodes_count BIGINT DEFAULT 0
);

-- Create index on name
CREATE INDEX IF NOT EXISTS ix_clients_name ON clients (name);

-- Create servers table
CREATE TABLE IF NOT EXISTS servers (
    id BIGSERIAL PRIMARY KEY,
    client_id UUID NOT NULL REFERENCES clients(id),
    client_name VARCHAR,
    server_name VARCHAR,
    generated_name VARCHAR,
    namespace VARCHAR,
    created_at BIGINT,
    last_heartbeat_time BIGINT,
    availability VARCHAR,
    image_tag VARCHAR,
    container_runtime VARCHAR,
    cpu FLOAT DEFAULT 0.0,
    ram FLOAT DEFAULT 0.0,
    run_as_root BOOLEAN DEFAULT FALSE NOT NULL,
    server_kind VARCHAR NOT NULL DEFAULT 'idegym',
    service_port INTEGER NOT NULL DEFAULT 80
);

-- Create index on generated_name
CREATE UNIQUE INDEX IF NOT EXISTS ix_servers_generated_name ON servers (generated_name);

-- Create resource_limit_rules table
CREATE TABLE IF NOT EXISTS resource_limit_rules (
    id BIGSERIAL PRIMARY KEY,
    client_name_regex VARCHAR NOT NULL,
    pods_limit INTEGER NOT NULL,
    cpu_limit FLOAT NOT NULL,
    ram_limit FLOAT NOT NULL,
    used_cpu FLOAT DEFAULT 0.0 NOT NULL,
    used_ram FLOAT DEFAULT 0.0 NOT NULL,
    current_pods INTEGER DEFAULT 0 NOT NULL,
    priority INTEGER DEFAULT 0 NOT NULL
);

-- Create index on client_name_regex
CREATE UNIQUE INDEX IF NOT EXISTS ix_resource_limit_rules_client_name_regex ON resource_limit_rules (client_name_regex);

-- Create job_statuses table
CREATE TABLE IF NOT EXISTS job_statuses (
    id BIGSERIAL PRIMARY KEY,
    job_name VARCHAR NOT NULL,
    details TEXT,
    tag VARCHAR NOT NULL,
    request_id VARCHAR,
    status VARCHAR,
    created_at BIGINT,
    updated_at BIGINT
);

-- Create index on job_name
CREATE UNIQUE INDEX IF NOT EXISTS ix_job_statuses_job_name ON job_statuses (job_name);

-- Create async_operations table
CREATE TABLE IF NOT EXISTS async_operations (
    id BIGSERIAL PRIMARY KEY,
    request_type VARCHAR NOT NULL,
    status VARCHAR,
    request TEXT,
    result TEXT,
    client_id UUID REFERENCES clients(id),
    server_id BIGINT REFERENCES servers(id),
    orchestrator_pod VARCHAR,
    scheduled_at BIGINT,
    started_at BIGINT,
    finished_at BIGINT
);

-- Create alembic_version table if it doesn't exist
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);
