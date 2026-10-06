"""Check watcher cleanup against PostgreSQL with Kubernetes helpers mocked."""

import asyncio
import time
from uuid import uuid4

import pytest
from idegym.api.config import SQLAlchemyConfig, WatcherConfig
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.status import Status
from idegym.api.type import Duration
from idegym.orchestrator.database import database
from idegym.orchestrator.database.models import AsyncOperation, Client, IdeGYMServer, JobStatusRecord
from idegym.watcher import cleanup
from idegym.watcher.cleanup import (
    check_orphaned_kaniko_jobs,
    cleanup_clients,
    cleanup_requests,
    cleanup_servers,
)
from idegym.watcher.operation_retention import retain_operations_once
from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

DAY_MS = 24 * 60 * 60 * 1000


@pytest.fixture
def mock_k8s(mocker):
    """Mock every Kubernetes / node helper the cleanup module imports."""
    return {
        "clean_up_server": mocker.patch(
            "idegym.watcher.cleanup.clean_up_server", new=mocker.AsyncMock(return_value=None)
        ),
        "is_server_pod_alive": mocker.patch(
            "idegym.watcher.cleanup.is_server_pod_alive", new=mocker.AsyncMock(return_value=False)
        ),
        "get_job_status": mocker.patch(
            "idegym.watcher.cleanup.get_job_status", new=mocker.AsyncMock(return_value=Status.SUCCESS)
        ),
        "change_number_of_spun_nodes": mocker.patch(
            "idegym.watcher.cleanup.change_number_of_spun_nodes", new=mocker.AsyncMock(return_value=False)
        ),
    }


async def _make_client(db: AsyncSession, *, last_heartbeat_time: int, availability=AvailabilityStatus.ALIVE) -> Client:
    client = Client(
        id=uuid4(),
        name="watcher-test",
        namespace="idegym",
        last_heartbeat_time=last_heartbeat_time,
        availability=availability,
        nodes_count=0,
    )
    db.add(client)
    await db.commit()
    return client


async def _reload(db: AsyncSession, model, ident):
    db.expire_all()
    result = await db.execute(select(model).where(model.id == ident))
    return result.scalar_one()


async def test_cleanup_servers_marks_inactive_server_killed(db: AsyncSession, mock_k8s):
    now = int(time.time() * 1000)
    client = await _make_client(db, last_heartbeat_time=now)
    server = IdeGYMServer(
        client_id=client.id,
        client_name=client.name,
        server_name="srv",
        generated_name=f"srv-{uuid4().hex[:8]}",
        namespace="idegym",
        last_heartbeat_time=now - 30 * 60 * 1000,  # 30 minutes ago
        availability=AvailabilityStatus.ALIVE,
    )
    db.add(server)
    await db.commit()
    server_id = server.id

    await cleanup_servers(
        db,
        current_time=now,
        inactive_timeout=Duration(minutes=10),
        finished_timeout=Duration(minutes=5),
    )

    reloaded = await _reload(db, IdeGYMServer, server_id)
    assert reloaded.availability == AvailabilityStatus.KILLED
    mock_k8s["clean_up_server"].assert_awaited_once()


async def test_cleanup_clients_marks_inactive_client_killed(db: AsyncSession, mock_k8s):
    now = int(time.time() * 1000)
    client = await _make_client(db, last_heartbeat_time=now - 30 * 60 * 1000)

    await cleanup_clients(db, current_time=now, inactive_timeout=Duration(minutes=10))

    reloaded = await _reload(db, Client, client.id)
    assert reloaded.availability == AvailabilityStatus.KILLED
    mock_k8s["change_number_of_spun_nodes"].assert_awaited_once()


async def test_cleanup_requests_marks_stale_without_deleting_history(db: AsyncSession, mock_k8s):
    now = int(time.time() * 1000)
    client = await _make_client(db, last_heartbeat_time=now)

    old_op = AsyncOperation(
        request_type=AsyncOperationType.START_SERVER,
        status=AsyncOperationStatus.SUCCEEDED,
        client_id=client.id,
        started_at=now - 15 * DAY_MS,
        finished_at=now - 15 * DAY_MS,
    )
    stale_op = AsyncOperation(
        request_type=AsyncOperationType.START_SERVER,
        status=AsyncOperationStatus.IN_PROGRESS,
        client_id=client.id,
        started_at=now - 25 * 60 * 60 * 1000,  # 25h ago, older than stale (24h) -> finished by watcher
    )
    db.add_all([old_op, stale_op])
    await db.commit()
    old_id, stale_id = old_op.id, stale_op.id

    await cleanup_requests(
        db,
        now,
        stale_inprogress=Duration(hours=24),
    )

    db.expire_all()
    assert (
        await db.execute(select(AsyncOperation).where(AsyncOperation.id == old_id))
    ).scalar_one_or_none() is not None
    reloaded_stale = await _reload(db, AsyncOperation, stale_id)
    assert reloaded_stale.status == AsyncOperationStatus.FINISHED_BY_WATCHER


async def test_check_orphaned_kaniko_jobs_reconciles_status(db: AsyncSession, mock_k8s):
    job = JobStatusRecord(
        job_name=f"kaniko-{uuid4().hex[:8]}",
        tag="example:latest",
        status=Status.IN_PROGRESS,
    )
    db.add(job)
    await db.commit()
    job_id = job.id

    # Kubernetes reports the job already finished successfully.
    await check_orphaned_kaniko_jobs(db, namespace="idegym")

    reloaded = await _reload(db, JobStatusRecord, job_id)
    assert reloaded.status == Status.SUCCESS
    mock_k8s["get_job_status"].assert_awaited()


async def test_stale_cleanup_is_bounded_and_oldest_first(db):
    now = 30 * DAY_MS
    stale = [
        AsyncOperation(request_type="FORWARD_REQUEST", status="IN_PROGRESS", started_at=now - 2 * DAY_MS + n)
        for n in range(503)
    ]
    protected = [
        AsyncOperation(request_type="START_SERVER", status=status, started_at=started)
        for status, started in [
            ("SUCCEEDED", 1),
            ("FAILED", 1),
            ("CANCELLED", 1),
            ("SCHEDULED", 1),
            ("IN_PROGRESS", now - DAY_MS),
            ("IN_PROGRESS", now),
            ("IN_PROGRESS", None),
        ]
    ]
    db.add_all([*stale, *protected])
    await db.commit()
    first_ids = {row.id for row in stale[:500]}
    protected_ids = {row.id: row.status for row in protected}
    assert await database.mark_stale_async_operations_as_finished(db, now, Duration(hours=24)) == 500
    assert not db.in_transaction()
    db.expire_all()
    finished = (await db.execute(select(AsyncOperation).where(AsyncOperation.finished_at == now))).scalars().all()
    assert {row.id for row in finished} == first_ids
    assert all(row.status == "FINISHED_BY_WATCHER" for row in finished)
    assert await database.mark_stale_async_operations_as_finished(db, now, Duration(hours=24)) == 3
    assert await database.mark_stale_async_operations_as_finished(db, now, Duration(hours=24)) == 0
    assert not db.in_transaction()
    db.expire_all()
    for row_id, status in protected_ids.items():
        assert (await db.get(AsyncOperation, row_id)).status == status


async def test_stale_cleanup_skips_concurrent_completion(db, db_url):
    now = 30 * DAY_MS
    rows = [AsyncOperation(request_type="FORWARD_REQUEST", status="IN_PROGRESS", started_at=1) for _ in range(2)]
    db.add_all(rows)
    await db.commit()
    locked_id, other_id = [row.id for row in rows]
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE async_operations SET status='SUCCEEDED', finished_at=:now WHERE id=:id"),
                {"now": now, "id": locked_id},
            )
            assert await database.mark_stale_async_operations_as_finished(db, now, Duration(hours=24)) == 1
        assert await database.mark_stale_async_operations_as_finished(db, now, Duration(hours=24)) == 0
        db.expire_all()
        assert (await db.get(AsyncOperation, locked_id)).status == "SUCCEEDED"
        assert (await db.get(AsyncOperation, other_id)).status == "FINISHED_BY_WATCHER"
    finally:
        await engine.dispose()


async def test_stale_cleanup_lock_timeout_rolls_back_and_recovers(db, db_url, mocker):
    mocker.patch.object(database.logger, "exception")
    db.add(AsyncOperation(request_type="FORWARD_REQUEST", status="IN_PROGRESS", started_at=1))
    await db.commit()
    original_timeout = await db.scalar(text("SHOW statement_timeout"))
    await db.commit()
    engine = create_async_engine(db_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("LOCK TABLE async_operations IN ACCESS EXCLUSIVE MODE"))
            async with asyncio.timeout(3):
                assert await database.mark_stale_async_operations_as_finished(db, 30 * DAY_MS, Duration(hours=24)) == 0
            assert not db.in_transaction()
            assert await db.scalar(text("SELECT 1")) == 1
            assert await db.scalar(text("SHOW statement_timeout")) == original_timeout
        assert await database.mark_stale_async_operations_as_finished(db, 30 * DAY_MS, Duration(hours=24)) == 1
    finally:
        await engine.dispose()


async def test_empty_stale_batch_preserves_advisory_lock_and_resets_timeouts(db, db_url):
    engine = create_async_engine(db_url)
    try:
        async with engine.connect() as connection, AsyncSession(bind=connection) as session:
            pid = await session.scalar(text("SELECT pg_backend_pid()"))
            original_timeout = await session.scalar(text("SHOW statement_timeout"))
            assert await database.acquire_advisory_lock(session, cleanup.CLEANUP_ADVISORY_LOCK_ID)
            assert await database.mark_stale_async_operations_as_finished(session, 30 * DAY_MS, Duration(hours=24)) == 0
            assert not session.in_transaction()
            assert await session.scalar(text("SHOW statement_timeout")) == original_timeout
            assert await session.scalar(text("SELECT pg_backend_pid()")) == pid
            assert not await database.acquire_advisory_lock(db, cleanup.CLEANUP_ADVISORY_LOCK_ID)
            assert await database.release_advisory_lock(session, cleanup.CLEANUP_ADVISORY_LOCK_ID)
    finally:
        await engine.dispose()


async def test_stale_cleanup_partial_index_supports_generic_plans(db, mocker):
    execute = mocker.spy(db, "execute")
    assert await database.mark_stale_async_operations_as_finished(db, 30 * DAY_MS, Duration(hours=24)) == 0
    query = next(call.args[0] for call in execute.call_args_list if "WITH candidates" in str(call.args[0]))
    compiled = query.compile(dialect=PGDialect_asyncpg())
    arguments = ", ".join(
        compiled.render_literal_value(compiled.params[name], compiled.binds[name].type) for name in compiled.positiontup
    )
    await db.execute(text("SET LOCAL plan_cache_mode = force_generic_plan"))
    await db.execute(text("SET LOCAL enable_seqscan = off"))
    await db.execute(text(f"PREPARE stale_cleanup_plan AS {compiled}"))
    try:
        plan = (await db.execute(text(f"EXPLAIN EXECUTE stale_cleanup_plan({arguments})"))).scalars().all()
        assert "ix_async_operations_in_progress_started" in "\n".join(plan)
    finally:
        await db.execute(text("DEALLOCATE stale_cleanup_plan"))


@pytest.mark.parametrize("failure", [None, "connect", "database", "unlock"])
async def test_cleanup_releases_pinned_lock_with_concurrent_retention(db, db_url, mocker, failure):
    engine = database.create_db_engine(db_url, SQLAlchemyConfig(pool_size=2, max_overflow=2))
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    mocker.patch.object(database, "SessionFactory", factory)
    config = WatcherConfig()
    backend_pids = []
    unlock_results = []
    cleanup_engine = engine
    attempts = 1
    if failure == "connect":
        cleanup_engine = mocker.Mock(wraps=engine)
        cleanup_engine.connect.side_effect = [ConnectionRefusedError("database unavailable"), engine.connect()]
        attempts = 2

    async def perform(session, *args, **kwargs):
        backend_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        await retain_operations_once(config)
        await session.commit()
        backend_pids.append(await session.scalar(text("SELECT pg_backend_pid()")))
        if failure == "database":
            await session.execute(text("SELECT 1 / 0"))

    async def release(session, lock_id):
        released = False if failure == "unlock" else await database.release_advisory_lock(session, lock_id)
        unlock_results.append(released)
        return released

    mocker.patch.object(cleanup, "perform_cleanup_operations", perform)
    mocker.patch.object(cleanup, "release_advisory_lock", release)
    # Stop after one complete cleanup pass without waiting for its interval.
    mocker.patch.object(
        cleanup, "asyncio", sleep=mocker.AsyncMock(side_effect=[None] * attempts + [asyncio.CancelledError])
    )

    try:
        with pytest.raises(asyncio.CancelledError):
            await cleanup.cleanup_inactive_pods(config, cleanup_engine)
        assert len(backend_pids) == 2
        assert backend_pids[0] == backend_pids[1]
        assert unlock_results == [failure != "unlock"]
        assert await database.acquire_advisory_lock(db, cleanup.CLEANUP_ADVISORY_LOCK_ID)
        assert await database.release_advisory_lock(db, cleanup.CLEANUP_ADVISORY_LOCK_ID)
    finally:
        await engine.dispose()
