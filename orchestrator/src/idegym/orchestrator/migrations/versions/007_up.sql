SET lock_timeout = '2s';
SET statement_timeout = '30min';
SET max_parallel_maintenance_workers = 0;

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_async_operations_in_progress_started
ON public.async_operations (started_at, id)
WHERE status = 'IN_PROGRESS';
