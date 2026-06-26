ALTER TABLE servers ADD COLUMN IF NOT EXISTS max_restarts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS details TEXT;
-- Track the GKE-generated PodSnapshot resource name so a specific snapshot can be restored by tag
ALTER TABLE snapshots ADD COLUMN IF NOT EXISTS pod_snapshot_name VARCHAR;
-- Persist the server's GKE snapshot group id (the idegym.jetbrains.com/snapshot-id pod label) so snapshots of a restored server report the correct group
ALTER TABLE servers ADD COLUMN IF NOT EXISTS snapshot_id VARCHAR;
