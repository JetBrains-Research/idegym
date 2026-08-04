# Database Migrations (Alembic)

This document describes how database migrations are organized and operated for the Orchestrator service.
The project uses Alembic and aims to support an SQL‑first workflow.

Location of migration-related files:
- Alembic configuration: orchestrator/src/idegym/orchestrator/alembic.ini
- Alembic env: orchestrator/src/idegym/orchestrator/migrations/env.py
- Migrations: orchestrator/src/idegym/orchestrator/migrations/versions/


## Overview

- Migrations are managed by Alembic.
- The application runs migrations automatically on startup and protects against concurrent execution using a PostgreSQL advisory lock.
- Revision IDs are simple sequential integers.
- Each migration can be implemented using plain SQL files (preferred) and a thin Python wrapper that executes the corresponding SQL files for upgrade and downgrade.


## How migrations are applied at runtime

On service startup, the Orchestrator calls MigrationManager, which:
- Acquires a PostgreSQL advisory lock (id 42239) to avoid concurrent migrations across multiple instances.
- Runs alembic upgrade heads against the configured database.
- Releases the lock when done.

Relevant code:
- orchestrator/src/idegym/orchestrator/database/database.py (init_db)
- orchestrator/src/idegym/orchestrator/migration_manager.py (advisory lock + alembic upgrade logic)

Startup also checks the revision the release declares (`database.schemaRevision` in the chart,
`IDEGYM_DATABASE_SCHEMA_REVISION` in the pod) against the head in the image, and refuses to
start when they disagree. A migration must therefore bump that chart value in the same change;
a unit test enforces it.


## How migrations are applied for a rollback

Startup only ever migrates forward. Moving the schema to an exact revision — the older one a
release being rolled back to declares — goes through the same MigrationManager under the same
advisory lock, driven from a CLI in the orchestrator image:

```
python -m idegym.orchestrator.db_cli schema current
python -m idegym.orchestrator.db_cli schema head
python -m idegym.orchestrator.db_cli migrate --target 002 --allow-downgrade
```

Run it only with every writer stopped, and prefer `scripts/rollback.py`, which sequences the
whole rollback. Both are documented in website/docs/reference/database_rollback.md.

Two rules follow from downgrades being executable rather than decorative:
- **Never touch `alembic_version`.** Alembic creates it and updates it in the same transaction
  as the migration; a migration that drops or creates it breaks its own downgrade.
- **Every revision ships a `<rev>_down.sql`.** The downgrade path is validated against those
  files before anything runs, so a missing one makes the revision non-revertible — the release
  can then only be rolled back by restoring a backup.

Every up/down pair is exercised against real PostgreSQL, with data seeded at each revision, in
integration-tests/database/test_migration_roundtrip.py.


## Directory structure and naming rules

- All migrations live under orchestrator/src/idegym/orchestrator/migrations/versions.
- Revision files must follow a strict sequential scheme:
  - Revision IDs: 001, 002, 003, …
  - Filenames: <rev>_<slug>.py, where <rev> is the 3‑digit revision id (e.g., 002_add_something.py)
  - This is enforced by:
    - orchestrator/src/idegym/orchestrator/alembic.ini: file_template = %(rev)s_%(slug)s
- SQL‑first pattern: for each revision, place two SQL files next to the Python file:
  - <rev>_up.sql — DDL/DML to apply during upgrade
  - <rev>_down.sql — DDL/DML to apply during downgrade
- The Python migration module should execute those SQL files. See 001_initial_schema.py for a reference implementation.
- Python script is generated automatically based on script.py.mako template

Example files for rev 001:
- orchestrator/src/idegym/orchestrator/migrations/versions/001_initial_schema.py
- orchestrator/src/idegym/orchestrator/migrations/versions/001_up.sql
- orchestrator/src/idegym/orchestrator/migrations/versions/001_down.sql


## Creating a new migration

1) Ensure you have a clean working tree and that you’re up to date with main to minimize merge conflicts around sequential revision IDs.

2) Create a new, empty revision:
   - From the project root:
     - `uv run alembic -c orchestrator/src/idegym/orchestrator/alembic.ini revision -m "my beautiful migration" --rev-id=002`
   - This will create a new file orchestrator/src/idegym/orchestrator/migrations/versions/002_my_beautiful_migration.py

3) Add SQL files next to the created Python file:
   - Create orchestrator/src/idegym/orchestrator/migrations/versions/002_up.sql
   - Create orchestrator/src/idegym/orchestrator/migrations/versions/002_down.sql
   - Write the necessary DDL/DML statements. Prefer idempotent operations when practical (e.g., CREATE INDEX IF NOT EXISTS) to ease replays in non‑prod.

4) Update the generated Python file to execute your SQL files if needed:
   - Use 001_initial_schema.py as a reference. It defines a small helper to read and split SQL statements and calls it from upgrade() and downgrade().

5) Commit the three files together (<rev>_<slug>.py, <rev>_up.sql, <rev>_down.sql).


## Limitations and gotchas

- Sequential IDs and branch merges:
  - If two branches both create “002”, a merge will produce a duplicate ID.
  - Resolve by renumbering one branch’s revision to the next free number and adjusting filenames and the revision value in the Python file.

- SQL splitting:
  - The helper that executes SQL files removes lines starting with -- and splits statements by semicolons.
  - Avoid placing semicolons inside functions or procedures (not common for our simple DDL/DML).
  - If needed, implement a more robust splitter for complex scripts.

- Advisory lock:
  - If migrations are “skipped” at startup, it likely means another instance held the lock.
  - Check logs across instances. The lock ID is 42239.


## Examples

Create a new empty revision and wire SQL files:

1) Create revision:
   - `uv run alembic -c orchestrator/src/idegym/orchestrator/alembic.ini revision -m "add user audit table" --rev-id=002`

2) Suppose Alembic created 002_add_user_audit_table.py. Add:
   - orchestrator/src/idegym/orchestrator/migrations/versions/002_up.sql
   - orchestrator/src/idegym/orchestrator/migrations/versions/002_down.sql

3) Implement 002_up.sql / 002_down.sql and make the Python file call them (already autogenerated).
