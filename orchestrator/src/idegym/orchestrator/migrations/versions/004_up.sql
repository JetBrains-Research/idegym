-- Persist the server's GKE snapshot group id (the idegym.jetbrains.com/snapshot-id pod label) so snapshots of a restored server report the correct group
ALTER TABLE servers ADD COLUMN IF NOT EXISTS snapshot_id VARCHAR;
