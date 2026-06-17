ALTER TABLE servers DROP COLUMN IF EXISTS snapshot_id;
ALTER TABLE snapshots DROP COLUMN IF EXISTS pod_snapshot_name;
