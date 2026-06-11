-- Track the GKE-generated PodSnapshot resource name so a specific snapshot can be restored by tag
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS pod_snapshot_name VARCHAR;
