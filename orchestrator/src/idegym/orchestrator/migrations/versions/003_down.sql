ALTER TABLE servers DROP COLUMN IF EXISTS details;
ALTER TABLE servers DROP COLUMN IF EXISTS max_restarts;
ALTER TABLE servers DROP COLUMN IF EXISTS snapshot_id;
ALTER TABLE snapshots DROP COLUMN IF EXISTS pod_snapshot_name;
