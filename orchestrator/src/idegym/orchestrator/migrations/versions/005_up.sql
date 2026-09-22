-- Partial index over the statuses that hold resource quota; the watcher's cleanup, crash detection
-- and usage recount select only these rows out of a table dominated by terminal ones.
CREATE INDEX IF NOT EXISTS ix_servers_live ON servers (availability) WHERE availability IN ('ALIVE', 'FINISHED', 'REUSED');
