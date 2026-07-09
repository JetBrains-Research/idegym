"""Integration tests for the watcher crash detector against a real PostgreSQL instance.

``detect_crashed_servers`` (:mod:`idegym.watcher.crash_detector`) is exercised against a
testcontainers PostgreSQL database; the Kubernetes-facing helpers it imports (``list_pods`` and
``clean_up_server``) are mocked in the ``idegym.watcher.crash_detector`` namespace so the tests
stay purely at the database layer.
"""

import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.orchestrator.database.models import Client, IdeGYMServer
from idegym.watcher.crash_detector import detect_crashed_servers
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration


def _pod(generated_name: str, *, restart_count=0, terminated=None, phase="Running", reason=None, message=None):
    last_state = SimpleNamespace(terminated=terminated)
    container = SimpleNamespace(
        restart_count=restart_count,
        state=SimpleNamespace(terminated=None, waiting=None, running=None),
        last_state=last_state,
    )
    return SimpleNamespace(
        metadata=SimpleNamespace(
            deletion_timestamp=None,
            name=generated_name,
            labels={"app": generated_name, "app.kubernetes.io/part-of": "idegym"},
        ),
        status=SimpleNamespace(phase=phase, reason=reason, message=message, container_statuses=[container]),
    )


async def _make_alive_server(db: AsyncSession, *, max_restarts: int) -> IdeGYMServer:
    client = Client(id=uuid4(), name="crash-test", namespace="idegym", last_heartbeat_time=int(time.time() * 1000))
    db.add(client)
    await db.commit()

    server = IdeGYMServer(
        client_id=client.id,
        client_name=client.name,
        server_name="srv",
        generated_name=f"srv-{uuid4().hex[:8]}",
        namespace="idegym",
        last_heartbeat_time=int(time.time() * 1000),
        availability=AvailabilityStatus.ALIVE,
        max_restarts=max_restarts,
    )
    db.add(server)
    await db.commit()
    return server


async def _reload(db: AsyncSession, server_id: int) -> IdeGYMServer:
    db.expire_all()
    return (await db.execute(select(IdeGYMServer).where(IdeGYMServer.id == server_id))).scalar_one()


async def test_oom_crash_marks_server_crashed_and_tears_down(db: AsyncSession, mocker):
    server = await _make_alive_server(db, max_restarts=0)
    pod = _pod(
        server.generated_name,
        restart_count=1,
        terminated=SimpleNamespace(reason="OOMKilled", exit_code=137, signal=None, message=None),
    )
    mocker.patch("idegym.watcher.crash_detector.list_pods", new=mocker.AsyncMock(return_value=[pod]))
    clean_up = mocker.patch("idegym.watcher.crash_detector.clean_up_server", new=mocker.AsyncMock())

    await detect_crashed_servers(db)

    reloaded = await _reload(db, server.id)
    assert reloaded.availability == AvailabilityStatus.CRASHED
    assert reloaded.details and "OOMKilled" in reloaded.details
    clean_up.assert_awaited_once_with(name=server.generated_name, namespace="idegym")


async def test_restart_within_budget_stays_alive(db: AsyncSession, mocker):
    server = await _make_alive_server(db, max_restarts=3)
    pod = _pod(
        server.generated_name,
        restart_count=1,
        terminated=SimpleNamespace(reason="Error", exit_code=1, signal=None, message=None),
    )
    mocker.patch("idegym.watcher.crash_detector.list_pods", new=mocker.AsyncMock(return_value=[pod]))
    clean_up = mocker.patch("idegym.watcher.crash_detector.clean_up_server", new=mocker.AsyncMock())

    await detect_crashed_servers(db)

    reloaded = await _reload(db, server.id)
    assert reloaded.availability == AvailabilityStatus.ALIVE
    assert reloaded.details is None
    clean_up.assert_not_awaited()


async def test_crash_with_teardown_failure_marks_deletion_failed(db: AsyncSession, mocker):
    server = await _make_alive_server(db, max_restarts=0)
    pod = _pod(
        server.generated_name,
        restart_count=2,
        terminated=SimpleNamespace(reason="OOMKilled", exit_code=137, signal=None, message=None),
    )
    mocker.patch("idegym.watcher.crash_detector.list_pods", new=mocker.AsyncMock(return_value=[pod]))
    mocker.patch(
        "idegym.watcher.crash_detector.clean_up_server",
        new=mocker.AsyncMock(side_effect=RuntimeError("deletion failed")),
    )

    await detect_crashed_servers(db)

    reloaded = await _reload(db, server.id)
    assert reloaded.availability == AvailabilityStatus.DELETION_FAILED
    assert reloaded.details and "OOMKilled" in reloaded.details


async def test_evicted_pod_marks_server_crashed(db: AsyncSession, mocker):
    server = await _make_alive_server(db, max_restarts=10)
    pod = _pod(
        server.generated_name,
        restart_count=0,
        phase="Failed",
        reason="Evicted",
        message="The node was low on resource: ephemeral-storage.",
    )
    mocker.patch("idegym.watcher.crash_detector.list_pods", new=mocker.AsyncMock(return_value=[pod]))
    mocker.patch("idegym.watcher.crash_detector.clean_up_server", new=mocker.AsyncMock())

    await detect_crashed_servers(db)

    reloaded = await _reload(db, server.id)
    assert reloaded.availability == AvailabilityStatus.CRASHED
    assert reloaded.details and "ephemeral-storage" in reloaded.details
