-- Drop tables in reverse order of creation (to handle foreign key constraints)

-- Drop async_operations table
DROP TABLE IF EXISTS async_operations;

-- Drop job_statuses table
DROP TABLE IF EXISTS job_statuses;

-- Drop resource_limit_rules table
DROP TABLE IF EXISTS resource_limit_rules;

-- Drop servers table
DROP TABLE IF EXISTS servers;

-- Drop clients table
DROP TABLE IF EXISTS clients;

-- alembic_version is deliberately left alone: Alembic owns it and issues the
-- DELETE that unsets revision 001 in this same transaction. Dropping it here
-- makes that statement fail and aborts the whole downgrade.
