-- Sandboxes run as plain Pods addressed by IP on the container port.
-- pod_manifest keeps the submitted Pod manifest so a restart can recreate the pod.
ALTER TABLE servers ADD COLUMN IF NOT EXISTS container_port INTEGER NOT NULL DEFAULT 8000;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS pod_ip VARCHAR;
ALTER TABLE servers ADD COLUMN IF NOT EXISTS pod_manifest JSONB;
