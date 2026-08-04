"""Up/down round-trip coverage for every Alembic revision against real PostgreSQL.

The rest of this suite builds the schema with ``Base.metadata.create_all``, so the
migrations themselves were never executed by any test. They are now the mechanism a
release rollback depends on, which makes an untested downgrade a deployment hazard rather
than a latent one.

Each test gets a throwaway database inside the shared container: Alembic owns the whole
schema here, including dropping it, which cannot share a database with the ORM-created one
the other modules truncate.
"""

from collections.abc import AsyncIterator, Iterator
from typing import Any
from uuid import uuid4

import pytest
from idegym.api.exceptions import MigrationError
from idegym.orchestrator.database.models import Base
from idegym.orchestrator.migration_manager import BASE_REVISION, MigrationDirection, MigrationManager
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

CLIENT_ID = "11111111-1111-1111-1111-111111111111"
PREPARE_REQUEST_ID = "22222222-2222-2222-2222-222222222222"

# Tables each revision adds, checked as the schema is walked up and down.
TABLES_BY_REVISION = {
    "001": {"clients", "servers", "resource_limit_rules", "job_statuses", "async_operations"},
    "002": {"snapshot_prepare_requests", "snapshots", "snapshot_jobs"},
    "003": set(),
}

# Columns revision 003 adds, and whose data its downgrade deliberately discards.
COLUMNS_ADDED_BY_003 = {
    "servers": {"details", "max_restarts", "snapshot_id"},
    "snapshots": {"pod_snapshot_name"},
}


@pytest.fixture
def migration_db_url(db_url: str) -> Iterator[str]:
    """Create a database only this test uses, and drop it afterwards."""
    database = f"migrations_{uuid4().hex[:12]}"
    admin_url = db_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://", 1)
    # CREATE/DROP DATABASE cannot run inside a transaction.
    admin_engine = create_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{database}"'))
        try:
            yield db_url.rsplit("/", 1)[0] + f"/{database}"
        finally:
            with admin_engine.connect() as conn:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
    finally:
        admin_engine.dispose()


@pytest.fixture
async def manager(migration_db_url: str) -> AsyncIterator[MigrationManager]:
    engine = create_async_engine(migration_db_url, pool_size=1, max_overflow=0)
    try:
        yield MigrationManager(engine=engine, db_url=migration_db_url)
    finally:
        await engine.dispose()


async def table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_table_names()))


async def column_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        columns = await conn.run_sync(lambda sync_conn: inspect(sync_conn).get_columns(table))
    return {column["name"] for column in columns}


async def execute(engine: AsyncEngine, statement: str, **parameters: Any) -> None:
    async with engine.begin() as conn:
        await conn.execute(text(statement), parameters)


async def scalar(engine: AsyncEngine, statement: str) -> Any:
    async with engine.connect() as conn:
        return (await conn.execute(text(statement))).scalar()


async def seed_revision_001(engine: AsyncEngine) -> None:
    await execute(
        engine,
        "INSERT INTO clients (id, name, namespace, created_at, last_heartbeat_time, availability, nodes_count)"
        " VALUES (:id, 'round-trip', 'idegym', 1, 1, 'AVAILABLE', 1)",
        id=CLIENT_ID,
    )
    await execute(
        engine,
        "INSERT INTO servers (client_id, client_name, server_name, generated_name, namespace, created_at,"
        " last_heartbeat_time, availability, image_tag, cpu, ram)"
        " VALUES (:client_id, 'round-trip', 'srv', 'srv-abc', 'idegym', 1, 1, 'AVAILABLE', 'tag', 1.0, 2.0)",
        client_id=CLIENT_ID,
    )


async def seed_revision_002(engine: AsyncEngine) -> None:
    await execute(
        engine,
        "INSERT INTO snapshot_prepare_requests (id, total_requested, succeeded, failed, created_at, updated_at)"
        " VALUES (:id, 1, 0, 0, 1, 1)",
        id=PREPARE_REQUEST_ID,
    )
    await execute(
        engine,
        "INSERT INTO snapshots (snapshot_name, request_hash, namespace, image_tag, server_name, server_kind,"
        " created_at, updated_at) VALUES ('snap', 'hash', 'idegym', 'tag', 'srv', 'idegym', 1, 1)",
    )
    await execute(
        engine,
        "INSERT INTO snapshot_jobs (job_id, status, request_hash, request, snapshot_id, prepare_request_id,"
        " created_at, updated_at)"
        " VALUES ('job', 'SUCCESS', 'hash', '{}', (SELECT id FROM snapshots LIMIT 1), :prepare_request_id, 1, 1)",
        prepare_request_id=PREPARE_REQUEST_ID,
    )


async def seed_revision_003(engine: AsyncEngine) -> None:
    await execute(
        engine,
        "UPDATE servers SET details = 'crashed once', max_restarts = 3, snapshot_id = 'group-1'",
    )
    await execute(engine, "UPDATE snapshots SET pod_snapshot_name = 'pod-snapshot-1'")


SEEDS = {"001": seed_revision_001, "002": seed_revision_002, "003": seed_revision_003}


async def test_round_trip_through_every_revision(manager: MigrationManager):
    """Walk base -> head one revision at a time with data in place, then walk back down.

    Seeding at each revision is what makes the downgrades meaningful: an empty schema
    reverts cleanly even when the SQL would fail on a populated one.
    """
    engine = manager.engine
    chain = manager.revision_chain()
    assert chain == ["001", "002", "003"], "update this test's per-revision expectations"

    expected_tables: set[str] = set()
    for revision in chain:
        plan = await manager.migrate_to(revision)
        assert plan.direction is MigrationDirection.UPGRADE
        assert await manager.get_current_revision() == revision

        expected_tables |= TABLES_BY_REVISION[revision]
        assert expected_tables <= await table_names(engine)
        await SEEDS[revision](engine)

    for table, columns in COLUMNS_ADDED_BY_003.items():
        assert columns <= await column_names(engine, table)

    for revision, previous in zip(reversed(chain), [*reversed(chain[:-1]), BASE_REVISION], strict=True):
        plan = await manager.migrate_to(previous, allow_downgrade=True)
        assert plan.direction is MigrationDirection.DOWNGRADE
        assert plan.revisions == (revision,)
        assert await manager.get_current_revision() == (None if previous == BASE_REVISION else previous)

        expected_tables -= TABLES_BY_REVISION[revision]
        assert expected_tables == await table_names(engine) - {"alembic_version"}

    # Back up to head: the schema returns, the data does not — dropping a table is the
    # documented cost of downgrading past the revision that created it.
    await manager.migrate_to("heads")
    assert await manager.get_current_revision() == chain[-1]
    assert TABLES_BY_REVISION["001"] | TABLES_BY_REVISION["002"] <= await table_names(engine)


async def test_downgrading_only_the_last_revision_preserves_rows(manager: MigrationManager):
    """003 -> 002 -> 003 keeps every row, and empties exactly the columns 003 added."""
    engine = manager.engine
    await manager.migrate_to("heads")
    await seed_revision_001(engine)
    await seed_revision_002(engine)
    await seed_revision_003(engine)

    await manager.migrate_to("002", allow_downgrade=True)
    for table, columns in COLUMNS_ADDED_BY_003.items():
        assert not columns & await column_names(engine, table)
    assert await scalar(engine, "SELECT count(*) FROM servers") == 1
    assert await scalar(engine, "SELECT count(*) FROM snapshots") == 1

    await manager.migrate_to("003")
    assert await scalar(engine, "SELECT count(*) FROM servers") == 1
    assert await scalar(engine, "SELECT generated_name FROM servers") == "srv-abc"
    assert await scalar(engine, "SELECT details FROM servers") is None
    assert await scalar(engine, "SELECT max_restarts FROM servers") == 0
    assert await scalar(engine, "SELECT pod_snapshot_name FROM snapshots") is None


async def test_downgrade_to_base_leaves_alembic_bookkeeping_intact(manager: MigrationManager):
    """A migration must not drop ``alembic_version``.

    Alembic deletes the revision row in the same transaction, so dropping the table there
    aborts the whole downgrade — which is why this used to be impossible rather than lossy.
    """
    await manager.migrate_to("heads")
    await seed_revision_001(manager.engine)

    await manager.migrate_to(BASE_REVISION, allow_downgrade=True)

    assert await table_names(manager.engine) == {"alembic_version"}
    assert await manager.get_current_revision() is None


async def test_head_schema_matches_the_orm_models(manager: MigrationManager):
    """Every ORM table and column exists at head, in the database the migrations built.

    A column added to ``models.py`` but not to a migration (or the reverse) otherwise only
    surfaces at runtime, because the rest of the suite creates its schema from the models.
    """
    await manager.migrate_to("heads")

    migrated = await table_names(manager.engine)
    assert set(Base.metadata.tables) <= migrated

    for name, table in Base.metadata.tables.items():
        assert {column.name for column in table.columns} == await column_names(manager.engine, name), name


async def test_downgrade_needs_explicit_approval(manager: MigrationManager):
    await manager.migrate_to("heads")

    with pytest.raises(MigrationError, match="downgrade"):
        await manager.migrate_to("002")

    assert await manager.get_current_revision() == "003"
    assert COLUMNS_ADDED_BY_003["servers"] <= await column_names(manager.engine, "servers")


async def test_unknown_target_is_rejected_before_touching_the_schema(manager: MigrationManager):
    await manager.migrate_to("heads")

    with pytest.raises(MigrationError, match="not one of the migrations in this image"):
        await manager.migrate_to("999", allow_downgrade=True)

    assert await manager.get_current_revision() == "003"


async def test_revision_this_image_does_not_contain_is_rejected(manager: MigrationManager):
    """The failure mode of rolling back with the wrong (older) image.

    Alembic cannot traverse a revision it has no script for, so the error has to name the
    situation rather than surface as a missing-revision KeyError.
    """
    await manager.migrate_to("heads")
    await execute(manager.engine, "UPDATE alembic_version SET version_num = '004'")

    with pytest.raises(MigrationError, match="use the image that introduced it"):
        await manager.migrate_to("003", allow_downgrade=True)
