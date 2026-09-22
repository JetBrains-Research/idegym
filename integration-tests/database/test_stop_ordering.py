"""Integration tests for the stop tasks' ordering against a real PostgreSQL instance.

``_task_stop_server`` and ``_task_stop_client`` run with the real database helpers (the module-global
``SessionFactory`` is pointed at the test database); only the Kubernetes calls are mocked in the router
modules.
"""

import pytest
from idegym.api.exceptions import ResourceDeletionFailedException
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.orchestrator.servers import AliveServerInfo
from idegym.orchestrator.database import database as database_module
from idegym.orchestrator.database.database import (
    check_resources_and_save_server,
    create_client,
    create_resource_limit_rule,
    save_async_operation,
)
from idegym.orchestrator.database.models import AsyncOperation, Client, IdeGYMServer, ResourceLimitRule
from idegym.orchestrator.router import client as client_router
from idegym.orchestrator.router import server as server_router
from idegym.orchestrator.util import decorators
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

pytestmark = pytest.mark.integration

_NAMESPACE = "idegym"


@pytest.fixture
async def session_factory(db: AsyncSession, db_url: str, mocker):
    """Point the orchestrator's ``with_db_session`` helpers at the test database."""
    engine = create_async_engine(db_url, pool_size=3, max_overflow=2)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False, autoflush=False)
    mocker.patch.object(database_module, "SessionFactory", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _setup(db: AsyncSession) -> tuple[ResourceLimitRule, Client, IdeGYMServer, int]:
    rule = await create_resource_limit_rule(db, ".*", pods_limit=10, cpu_limit=20.0, ram_limit=40.0, priority=-1)
    client = await create_client(db, "stop-client")
    server = await check_resources_and_save_server(
        db, client.id, client.name, "srv", _NAMESPACE, cpu_request=2.0, ram_request=4.0
    )
    assert server is not None
    operation = await save_async_operation(db, AsyncOperationType.STOP_SERVER, client_id=client.id, server_id=server.id)
    return rule, client, server, operation.id


async def _reload(db: AsyncSession, model, ident):
    """Re-read a row, overwriting the copy cached in the session with the database state."""
    query = select(model).where(model.id == ident).execution_options(populate_existing=True)
    return (await db.execute(query)).scalar_one()


async def test_stop_server_deletes_pod_before_writing_and_survives_a_failed_in_progress_write(
    db: AsyncSession, session_factory, mocker
):
    rule, _, server, operation_id = await _setup(db)
    order: list[str] = []

    async def fake_clean_up(name, namespace):
        order.append(f"k8s:{name}")

    real_update_operation_status = server_router.update_operation_status

    async def flaky_update_operation_status(**kwargs):
        if kwargs["async_operation_status"] == AsyncOperationStatus.IN_PROGRESS:
            raise TimeoutError("QueuePool limit of size 3 overflow 1 reached")
        return await real_update_operation_status(**kwargs)

    real_update_server_status = server_router.update_server_status

    async def recording_update_server_status(**kwargs):
        order.append(f"db:{kwargs['availability_status']}")
        return await real_update_server_status(**kwargs)

    mocker.patch.object(server_router, "clean_up_server", fake_clean_up)
    mocker.patch.object(server_router, "update_operation_status", flaky_update_operation_status)
    mocker.patch.object(server_router, "update_server_status", recording_update_server_status)

    await server_router._task_stop_server(
        server_id=server.id,
        server_generated_name=server.generated_name,
        namespace=_NAMESPACE,
        async_operation_id=operation_id,
    )

    assert order == [f"k8s:{server.generated_name}", f"db:{AvailabilityStatus.STOPPED}"]
    assert (await _reload(db, IdeGYMServer, server.id)).availability == AvailabilityStatus.STOPPED
    assert (await _reload(db, AsyncOperation, operation_id)).status == AsyncOperationStatus.SUCCEEDED
    reloaded_rule = await _reload(db, ResourceLimitRule, rule.id)
    assert (reloaded_rule.used_cpu, reloaded_rule.used_ram, reloaded_rule.current_pods) == (0.0, 0.0, 0)


async def test_stop_server_records_deletion_failed_only_when_kubernetes_fails(
    db: AsyncSession, session_factory, mocker
):
    rule, _, server, operation_id = await _setup(db)
    clean_up = mocker.patch.object(
        server_router, "clean_up_server", mocker.AsyncMock(side_effect=ResourceDeletionFailedException("boom"))
    )

    await server_router._task_stop_server(
        server_id=server.id,
        server_generated_name=server.generated_name,
        namespace=_NAMESPACE,
        async_operation_id=operation_id,
    )

    clean_up.assert_awaited_once_with(name=server.generated_name, namespace=_NAMESPACE)
    assert (await _reload(db, IdeGYMServer, server.id)).availability == AvailabilityStatus.DELETION_FAILED
    operation = await _reload(db, AsyncOperation, operation_id)
    assert operation.status == AsyncOperationStatus.FAILED
    assert "boom" in operation.result
    # Entering DELETION_FAILED releases the quota; the watcher finalizes the row later without touching it.
    reloaded_rule = await _reload(db, ResourceLimitRule, rule.id)
    assert (reloaded_rule.used_cpu, reloaded_rule.used_ram, reloaded_rule.current_pods) == (0.0, 0.0, 0)


async def test_stop_server_leaves_row_alive_when_no_status_write_succeeds(db: AsyncSession, session_factory, mocker):
    _, _, server, operation_id = await _setup(db)
    clean_up = mocker.patch.object(server_router, "clean_up_server", mocker.AsyncMock())
    unavailable = mocker.AsyncMock(side_effect=TimeoutError("QueuePool limit of size 3 overflow 1 reached"))
    mocker.patch.object(server_router, "update_server_status", unavailable)
    mocker.patch.object(decorators, "update_server_status", unavailable)

    with pytest.raises(TimeoutError):
        await server_router._task_stop_server(
            server_id=server.id,
            server_generated_name=server.generated_name,
            namespace=_NAMESPACE,
            async_operation_id=operation_id,
        )

    clean_up.assert_awaited_once_with(name=server.generated_name, namespace=_NAMESPACE)
    # No terminal status was written, so the row stays ALIVE for the watcher's inactivity timeout.
    assert (await _reload(db, IdeGYMServer, server.id)).availability == AvailabilityStatus.ALIVE


async def test_stop_client_deletes_each_pod_before_marking_it_stopped(db: AsyncSession, session_factory, mocker):
    _, client, server, _ = await _setup(db)
    operation = await save_async_operation(db, AsyncOperationType.STOP_CLIENT, client_id=client.id)
    order: list[str] = []

    async def fake_clean_up(name, namespace):
        order.append(f"k8s:{name}")

    real_update_server_status = client_router.update_server_status

    async def recording_update_server_status(**kwargs):
        order.append(f"db:{kwargs['availability_status']}")
        return await real_update_server_status(**kwargs)

    mocker.patch.object(client_router, "clean_up_server", fake_clean_up)
    mocker.patch.object(client_router, "update_server_status", recording_update_server_status)
    mocker.patch.object(client_router, "change_number_of_spun_nodes", mocker.AsyncMock(return_value=False))

    await client_router._task_stop_client(
        servers_info=[AliveServerInfo(id=server.id, generated_name=server.generated_name)],
        client_id=client.id,
        namespace=_NAMESPACE,
        async_operation_id=operation.id,
    )

    assert order == [f"k8s:{server.generated_name}", f"db:{AvailabilityStatus.STOPPED}"]
    assert (await _reload(db, IdeGYMServer, server.id)).availability == AvailabilityStatus.STOPPED
    assert (await _reload(db, Client, client.id)).availability == AvailabilityStatus.STOPPED
    assert (await _reload(db, AsyncOperation, operation.id)).status == AsyncOperationStatus.SUCCEEDED
