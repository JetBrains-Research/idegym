SET lock_timeout = '2s';
SET statement_timeout = '30s';

DROP INDEX CONCURRENTLY IF EXISTS public.ix_async_operations_in_progress_started;
