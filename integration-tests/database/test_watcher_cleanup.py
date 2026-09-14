"""Integration tests for the watcher cleanup loop against a real PostgreSQL instance.

The cleanup functions live in :mod:`idegym.watcher.cleanup` (extracted from the orchestrator).
They are exercised directly against a testcontainers PostgreSQL database; the Kubernetes-facing
helpers they import are mocked in the ``idegym.watcher.cleanup`` namespace so the tests stay
purely at the database layer.
"""

import time
from uuid import uuid4

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.status import Status
from idegym.api.type import Duration
from idegym.orchestrator.database.models import AsyncOperation, Client, IdeGYMServer, JobStatusRecord
from idegym.watcher.cleanup import (
    check_orphaned_builds,
    cleanup_clients,
    cleanup_requests,
    cleanup_servers,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

DAY_MS = 24 * 60 * 60 * 1000


@pytest.fixture
def mock_k8s(mocker):
    """Mock every Kubernetes / node helper cleanup reaches, directly or through a build context."""
    return {
        "clean_up_server": mocker.patch(
            "idegym.watcher.cleanup.clean_up_server", new=mocker.AsyncMock(return_value=None)
        ),
        "are_any_pods_alive": mocker.patch(
            "idegym.watcher.cleanup.are_any_pods_alive", new=mocker.AsyncMock(return_value=False)
        ),
        # Reached via get_build_status' kaniko branch, so patched where that branch resolves it.
        "get_job_status": mocker.patch(
            "idegym.backend.utils.image_builder.status.get_job_status",
            new=mocker.AsyncMock(return_value=Status.SUCCESS),
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


async def test_cleanup_requests_deletes_old_and_marks_stale(db: AsyncSession, mock_k8s):
    now = int(time.time() * 1000)
    client = await _make_client(db, last_heartbeat_time=now)

    old_op = AsyncOperation(
        request_type=AsyncOperationType.START_SERVER,
        status=AsyncOperationStatus.SUCCEEDED,
        client_id=client.id,
        started_at=now - 15 * DAY_MS,  # older than max_age (14d) -> deleted
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
        max_age=Duration(days=14),
        stale_inprogress=Duration(hours=24),
    )

    db.expire_all()
    assert (await db.execute(select(AsyncOperation).where(AsyncOperation.id == old_id))).scalar_one_or_none() is None
    reloaded_stale = await _reload(db, AsyncOperation, stale_id)
    assert reloaded_stale.status == AsyncOperationStatus.FINISHED_BY_WATCHER


async def test_check_orphaned_builds_reconciles_status(db: AsyncSession, mock_k8s):
    job_name = f"kaniko-build-{uuid4().hex[:8]}"
    job = JobStatusRecord(
        job_name=job_name,
        build_context=f"kaniko://original-namespace/{job_name}",
        tag="example:latest",
        status=Status.IN_PROGRESS,
    )
    db.add(job)
    await db.commit()
    job_id = job.id

    # Kubernetes reports the job already finished successfully.
    await check_orphaned_builds(db)

    reloaded = await _reload(db, JobStatusRecord, job_id)
    assert reloaded.status == Status.SUCCESS
    # The namespace comes from the build context, not from the watcher's own deployment.
    mock_k8s["get_job_status"].assert_awaited_once_with(job_name, "original-namespace")


@pytest.mark.parametrize("backend_status", [Status.IN_PROGRESS, Status.SUCCESS, Status.FAILURE])
async def test_reconcile_cloud_build_after_restart(db, mock_k8s, mocker, backend_status):
    context = f"cloudbuild_gke://project/us-central1/{uuid4()}"
    job = JobStatusRecord(
        job_name=context.rsplit("/", 1)[1], build_context=context, tag="example:latest", status=Status.IN_PROGRESS
    )
    db.add(job)
    await db.commit()
    job_id = job.id
    status = mocker.patch("idegym.watcher.cleanup.get_build_status", return_value=backend_status)
    await check_orphaned_builds(db)
    assert (await _reload(db, JobStatusRecord, job_id)).status == backend_status
    status.assert_awaited_once_with(context)
    mock_k8s["get_job_status"].assert_not_awaited()


async def test_build_without_a_context_is_left_to_the_orchestrator(db, mock_k8s, mocker):
    """A record predating the column names no backend, so the watcher cannot reconcile it."""
    job = JobStatusRecord(job_name=str(uuid4()), tag="example:latest", status=Status.IN_PROGRESS)
    db.add(job)
    await db.commit()
    job_id = job.id
    status = mocker.patch("idegym.watcher.cleanup.get_build_status")
    await check_orphaned_builds(db)
    assert (await _reload(db, JobStatusRecord, job_id)).status == Status.IN_PROGRESS
    status.assert_not_awaited()
    mock_k8s["get_job_status"].assert_not_awaited()


async def test_status_query_error_leaves_build_in_progress(db, mocker):
    job = JobStatusRecord(
        job_name=str(uuid4()),
        build_context="cloudbuild_gke://project/region/id",
        tag="example:latest",
        status=Status.IN_PROGRESS,
    )
    db.add(job)
    await db.commit()
    job_id = job.id
    mocker.patch("idegym.watcher.cleanup.get_build_status", side_effect=ConnectionError("unavailable"))
    await check_orphaned_builds(db)
    assert (await _reload(db, JobStatusRecord, job_id)).status == Status.IN_PROGRESS
