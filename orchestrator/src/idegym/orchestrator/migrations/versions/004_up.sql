-- Epoch milliseconds until which an explicit keepalive holds the server against the inactivity reaper
ALTER TABLE servers ADD COLUMN IF NOT EXISTS keepalive_until BIGINT;
