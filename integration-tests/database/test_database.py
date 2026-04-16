"""Integration tests for the orchestrator database access layer.

Runs against a real PostgreSQL instance (via testcontainers) so that
PostgreSQL-specific SQL (regex ``~`` operator, FOR UPDATE SKIP LOCKED, etc.)
is exercised faithfully.
"""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.status import Status
from idegym.orchestrator.database.database import (
    check_resources_and_save_server,
    create_client,
    create_resource_limit_rule,
    delete_old_async_operations,
    find_matching_finished_server,
    find_matching_resource_limit_rule,
    get_alive_clients,
    get_async_operation,
    get_client,
    get_client_by_name,
    get_clients_by_status,
    get_finished_clients,
    get_finished_idegym_servers,
    get_idegym_server,
    get_idegym_server_by_generated_name,
    get_idegym_servers_by_client_id,
    get_idegym_servers_by_status,
    get_job_status,
    get_job_status_by_id,
    get_resource_limit_rule,
    get_running_idegym_servers,
    mark_stale_async_operations_as_finished,
    need_to_release_nodes,
    need_to_spin_up_nodes,
    save_async_operation,
    save_idegym_server,
    save_job_status,
    subtract_resources_from_rule,
    update_async_operation,
    update_client_heartbeat,
    update_idegym_server_heartbeat,
    update_idegym_server_owner,
    update_job_status,
    update_resource_limit_rule,
)
from idegym.orchestrator.database.models import Client, IdeGYMServer, ResourceLimitRule
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IMAGE = "registry.example.com/server:latest"
_RUNTIME = "gvisor"
_NAMESPACE = "idegym"


async def _make_client(db: AsyncSession, name: str = "test-client", nodes: int = 0) -> Client:
    return await create_client(db, name=name, nodes_count=nodes, namespace=_NAMESPACE)


async def _make_rule(
    db: AsyncSession,
    regex: str = ".*",
    pods: int = 10,
    cpu: float = 20.0,
    ram: float = 40.0,
    priority: int = 0,
) -> ResourceLimitRule:
    return await create_resource_limit_rule(
        db, client_name_regex=regex, pods_limit=pods, cpu_limit=cpu, ram_limit=ram, priority=priority
    )


async def _make_server(
    db: AsyncSession,
    client: Client,
    server_name: str = "my-server",
    image_tag: str = _IMAGE,
    container_runtime: str = _RUNTIME,
    cpu: float = 1.0,
    ram: float = 2.0,
    availability: str = AvailabilityStatus.ALIVE,
) -> IdeGYMServer:
    server = await save_idegym_server(
        db,
        client_id=client.id,
        client_name=client.name,
        server_name=server_name,
        namespace=_NAMESPACE,
        image_tag=image_tag,
        container_runtime=container_runtime,
        cpu=cpu,
        ram=ram,
    )
    if availability != AvailabilityStatus.ALIVE:
        server.availability = availability
        await db.commit()
    return server


# ===========================================================================
# Client CRUD
# ===========================================================================


async def test_create_and_get_client(db: AsyncSession):
    client = await _make_client(db, "alpha")
    assert client.id is not None
    assert client.name == "alpha"
    assert client.availability == AvailabilityStatus.ALIVE
    assert client.namespace == _NAMESPACE

    fetched = await get_client(db, client.id)
    assert fetched is not None
    assert fetched.id == client.id
    assert fetched.name == "alpha"


async def test_get_client_returns_none_for_unknown_id(db: AsyncSession):
    result = await get_client(db, uuid4())
    assert result is None


async def test_get_client_by_name(db: AsyncSession):
    await _make_client(db, "bravo")
    client = await get_client_by_name(db, "bravo")
    assert client is not None
    assert client.name == "bravo"


async def test_get_client_by_name_returns_none_when_missing(db: AsyncSession):
    assert await get_client_by_name(db, "nonexistent") is None


async def test_get_alive_clients(db: AsyncSession):
    c1 = await _make_client(db, "c1")
    c2 = await _make_client(db, "c2")
    c2.availability = AvailabilityStatus.FINISHED
    await db.commit()

    alive = await get_alive_clients(db)
    alive_ids = {c.id for c in alive}
    assert c1.id in alive_ids
    assert c2.id not in alive_ids


async def test_get_finished_clients(db: AsyncSession):
    c1 = await _make_client(db, "c1")
    c2 = await _make_client(db, "c2")
    c1.availability = AvailabilityStatus.FINISHED
    await db.commit()

    finished = await get_finished_clients(db)
    finished_ids = {c.id for c in finished}
    assert c1.id in finished_ids
    assert c2.id not in finished_ids


async def test_get_clients_by_status(db: AsyncSession):
    c1 = await _make_client(db, "c1")
    c2 = await _make_client(db, "c2")
    c2.availability = AvailabilityStatus.STOPPED
    await db.commit()

    results = await get_clients_by_status(db, {AvailabilityStatus.ALIVE, AvailabilityStatus.STOPPED})
    result_ids = {c.id for c in results}
    assert c1.id in result_ids
    assert c2.id in result_ids


async def test_update_client_heartbeat(db: AsyncSession):
    client = await _make_client(db, "heartbeat-client")
    old_ts = client.last_heartbeat_time

    updated = await update_client_heartbeat(db, client.id, AvailabilityStatus.ALIVE)
    assert updated is not None
    assert updated.last_heartbeat_time >= old_ts
    assert updated.availability == AvailabilityStatus.ALIVE


async def test_update_client_heartbeat_ignores_terminal_status(db: AsyncSession):
    client = await _make_client(db, "terminal-client")
    client.availability = AvailabilityStatus.KILLED
    await db.commit()
    old_ts = client.last_heartbeat_time

    updated = await update_client_heartbeat(db, client.id, AvailabilityStatus.ALIVE)
    assert updated is not None
    # Status and timestamp must remain unchanged for terminal clients
    assert updated.availability == AvailabilityStatus.KILLED
    assert updated.last_heartbeat_time == old_ts


async def test_update_client_heartbeat_returns_none_for_unknown(db: AsyncSession):
    result = await update_client_heartbeat(db, uuid4())
    assert result is None


# ===========================================================================
# Node management
# ===========================================================================


async def test_need_to_spin_up_nodes_single_client(db: AsyncSession):
    """Only client with nodes: spin up is required."""
    client = await _make_client(db, "solo", nodes=3)
    assert await need_to_spin_up_nodes(db, client.id) is True


async def test_need_to_spin_up_nodes_another_client_covers(db: AsyncSession):
    """Another ALIVE client with equal nodes: no spin-up needed."""
    c1 = await _make_client(db, "shared", nodes=2)
    c2 = await _make_client(db, "shared", nodes=2)
    # c1 already running; c2 is a second registration — no spin-up for c2
    assert await need_to_spin_up_nodes(db, c2.id) is False
    _ = c1  # suppress unused warning


async def test_need_to_spin_up_nodes_zero_nodes(db: AsyncSession):
    """Clients with nodes_count=0 never need spin-up."""
    client = await _make_client(db, "zero")
    assert await need_to_spin_up_nodes(db, client.id) is False


async def test_need_to_release_nodes_last_client(db: AsyncSession):
    """When the only remaining client is removed, release to 0."""
    client = await _make_client(db, "last", nodes=5)
    result = await need_to_release_nodes(db, client.id)
    assert result is not None
    assert result.name == "last"
    assert result.nodes == 0


async def test_need_to_release_nodes_higher_count_exists(db: AsyncSession):
    """Another client has equal/higher nodes: no release needed (nodes=-1)."""
    c1 = await _make_client(db, "same", nodes=3)
    c2 = await _make_client(db, "same", nodes=3)
    result = await need_to_release_nodes(db, c1.id)
    assert result is not None
    assert result.nodes == -1
    _ = c2


async def test_need_to_release_nodes_zero_nodes(db: AsyncSession):
    """Clients with nodes=0 return None — nothing to release."""
    client = await _make_client(db, "zero")
    assert await need_to_release_nodes(db, client.id) is None


# ===========================================================================
# Server CRUD
# ===========================================================================


async def test_save_idegym_server_generated_name(db: AsyncSession):
    client = await _make_client(db)
    server = await save_idegym_server(
        db,
        client_id=client.id,
        client_name=client.name,
        server_name="my-server",
        namespace=_NAMESPACE,
        image_tag=_IMAGE,
        container_runtime=_RUNTIME,
        cpu=2.0,
        ram=4.0,
    )
    assert server.id is not None
    assert server.generated_name == f"my-server-{server.id}"
    assert server.cpu == 2.0
    assert server.ram == 4.0
    assert server.availability == AvailabilityStatus.ALIVE


async def test_get_idegym_server(db: AsyncSession):
    client = await _make_client(db)
    server = await _make_server(db, client)

    fetched = await get_idegym_server(db, server.id)
    assert fetched is not None
    assert fetched.id == server.id
    assert fetched.client_id == client.id


async def test_get_idegym_server_returns_none(db: AsyncSession):
    assert await get_idegym_server(db, 99999) is None


async def test_get_idegym_server_by_generated_name(db: AsyncSession):
    client = await _make_client(db)
    server = await _make_server(db, client, server_name="lookup-server")

    fetched = await get_idegym_server_by_generated_name(db, server.generated_name)
    assert fetched is not None
    assert fetched.id == server.id


async def test_get_idegym_servers_by_client_id(db: AsyncSession):
    c1 = await _make_client(db, "owner")
    c2 = await _make_client(db, "other")
    s1 = await _make_server(db, c1, "s1")
    s2 = await _make_server(db, c1, "s2")
    await _make_server(db, c2, "s3")

    servers = await get_idegym_servers_by_client_id(db, c1.id)
    server_ids = {s.id for s in servers}
    assert s1.id in server_ids
    assert s2.id in server_ids
    assert len(servers) == 2


async def test_get_running_idegym_servers(db: AsyncSession):
    client = await _make_client(db)
    alive = await _make_server(db, client, "alive-srv", availability=AvailabilityStatus.ALIVE)
    reused = await _make_server(db, client, "reused-srv", availability=AvailabilityStatus.REUSED)
    finished = await _make_server(db, client, "done-srv", availability=AvailabilityStatus.FINISHED)

    running = await get_running_idegym_servers(db)
    running_ids = {s.id for s in running}
    assert alive.id in running_ids
    assert reused.id in running_ids
    assert finished.id not in running_ids


async def test_get_finished_idegym_servers(db: AsyncSession):
    client = await _make_client(db)
    finished = await _make_server(db, client, "done", availability=AvailabilityStatus.FINISHED)
    alive = await _make_server(db, client, "live")

    result = await get_finished_idegym_servers(db)
    result_ids = {s.id for s in result}
    assert finished.id in result_ids
    assert alive.id not in result_ids


async def test_get_idegym_servers_by_status(db: AsyncSession):
    client = await _make_client(db)
    stopped = await _make_server(db, client, "stopped", availability=AvailabilityStatus.STOPPED)
    alive = await _make_server(db, client, "alive")

    results = await get_idegym_servers_by_status(db, {AvailabilityStatus.STOPPED})
    result_ids = {s.id for s in results}
    assert stopped.id in result_ids
    assert alive.id not in result_ids


async def test_update_idegym_server_heartbeat(db: AsyncSession):
    client = await _make_client(db)
    server = await _make_server(db, client)
    old_ts = server.last_heartbeat_time

    updated = await update_idegym_server_heartbeat(db, server.id, AvailabilityStatus.ALIVE)
    assert updated is not None
    assert updated.last_heartbeat_time >= old_ts
    assert updated.availability == AvailabilityStatus.ALIVE


async def test_update_idegym_server_heartbeat_ignores_terminal(db: AsyncSession):
    client = await _make_client(db)
    server = await _make_server(db, client, availability=AvailabilityStatus.KILLED)
    old_ts = server.last_heartbeat_time

    updated = await update_idegym_server_heartbeat(db, server.id, AvailabilityStatus.ALIVE)
    assert updated is not None
    assert updated.availability == AvailabilityStatus.KILLED
    assert updated.last_heartbeat_time == old_ts


async def test_update_idegym_server_heartbeat_releases_resources(db: AsyncSession):
    """Transitioning to a terminal non-FINISHED state decrements the resource rule counters."""
    await _make_rule(db, ".*", pods=10, cpu=20.0, ram=40.0, priority=-1)
    client = await _make_client(db, "heartbeat-res-client")
    server = await check_resources_and_save_server(
        db, client.id, client.name, "res-server", _NAMESPACE, cpu_request=4.0, ram_request=8.0
    )
    assert server is not None

    rule_before = await find_matching_resource_limit_rule(db, client.name)
    assert rule_before is not None
    assert rule_before.used_cpu == 4.0
    assert rule_before.current_pods == 1

    await update_idegym_server_heartbeat(db, server.id, AvailabilityStatus.STOPPED)

    rule_after = await find_matching_resource_limit_rule(db, client.name)
    assert rule_after is not None
    assert rule_after.used_cpu == 0.0
    assert rule_after.used_ram == 0.0
    assert rule_after.current_pods == 0


async def test_update_idegym_server_heartbeat_returns_none_for_unknown(db: AsyncSession):
    assert await update_idegym_server_heartbeat(db, 99999) is None


async def test_update_idegym_server_owner(db: AsyncSession):
    old_owner = await _make_client(db, "old-owner")
    new_owner = await _make_client(db, "new-owner")
    server = await _make_server(db, old_owner)

    updated = await update_idegym_server_owner(db, server.id, new_owner.id)
    assert updated is not None
    assert updated.client_id == new_owner.id


async def test_update_idegym_server_owner_returns_none_for_unknown(db: AsyncSession):
    client = await _make_client(db)
    assert await update_idegym_server_owner(db, 99999, client.id) is None


# ===========================================================================
# Resource limit rules
# ===========================================================================


async def test_create_and_get_resource_limit_rule(db: AsyncSession):
    rule = await _make_rule(db, "test-.*", pods=5, cpu=10.0, ram=20.0, priority=1)
    assert rule.id is not None
    assert rule.client_name_regex == "test-.*"
    assert rule.pods_limit == 5
    assert rule.used_cpu == 0.0

    fetched = await get_resource_limit_rule(db, rule.id)
    assert fetched is not None
    assert fetched.id == rule.id


async def test_find_matching_resource_limit_rule_default_catchall(db: AsyncSession):
    """The default '.*' rule matches any client name."""
    await _make_rule(db, ".*", priority=-1)
    result = await find_matching_resource_limit_rule(db, "anything-goes")
    assert result is not None
    assert result.client_name_regex == ".*"


async def test_find_matching_resource_limit_rule_priority(db: AsyncSession):
    """A more specific higher-priority rule wins over the catchall."""
    await _make_rule(db, ".*", pods=50, priority=-1)
    specific = await _make_rule(db, "team-.*", pods=5, priority=10)

    result = await find_matching_resource_limit_rule(db, "team-alpha")
    assert result is not None
    assert result.id == specific.id


async def test_find_matching_resource_limit_rule_no_match(db: AsyncSession):
    """Returns None when no rule matches."""
    await _make_rule(db, "specific-prefix-.*", priority=0)
    result = await find_matching_resource_limit_rule(db, "other-client")
    assert result is None


async def test_update_resource_limit_rule(db: AsyncSession):
    rule = await _make_rule(db, ".*", pods=10, cpu=20.0, ram=40.0, priority=0)
    updated = await update_resource_limit_rule(db, rule.id, pods_limit=20, cpu_limit=50.0, ram_limit=100.0, priority=5)
    assert updated is not None
    assert updated.pods_limit == 20
    assert updated.cpu_limit == 50.0
    assert updated.priority == 5


async def test_subtract_resources_from_rule(db: AsyncSession):
    rule = await _make_rule(db, ".*", pods=10, cpu=20.0, ram=40.0, priority=-1)
    rule.used_cpu = 8.0
    rule.used_ram = 16.0
    rule.current_pods = 2
    await db.commit()

    client = await _make_client(db)
    await subtract_resources_from_rule(db, client.name, cpu_amount=4.0, ram_amount=8.0)
    await db.commit()

    refreshed = await get_resource_limit_rule(db, rule.id)
    assert refreshed is not None
    assert refreshed.used_cpu == pytest.approx(4.0)
    assert refreshed.used_ram == pytest.approx(8.0)
    assert refreshed.current_pods == 1


async def test_subtract_resources_does_not_go_below_zero(db: AsyncSession):
    rule = await _make_rule(db, ".*", pods=10, cpu=20.0, ram=40.0, priority=-1)
    rule.used_cpu = 1.0
    rule.used_ram = 1.0
    rule.current_pods = 0
    await db.commit()

    client = await _make_client(db)
    await subtract_resources_from_rule(db, client.name, cpu_amount=100.0, ram_amount=100.0)
    await db.commit()

    refreshed = await get_resource_limit_rule(db, rule.id)
    assert refreshed is not None
    assert refreshed.used_cpu == 0.0
    assert refreshed.used_ram == 0.0
    assert refreshed.current_pods == 0


# ===========================================================================
# Atomic resource check + server creation
# ===========================================================================


async def test_check_resources_and_save_server_success(db: AsyncSession):
    await _make_rule(db, ".*", pods=5, cpu=10.0, ram=20.0, priority=-1)
    client = await _make_client(db)

    server = await check_resources_and_save_server(
        db, client.id, client.name, "new-server", _NAMESPACE, cpu_request=2.0, ram_request=4.0, image_tag=_IMAGE
    )
    assert server is not None
    assert server.cpu == 2.0
    assert server.ram == 4.0
    assert server.generated_name == f"new-server-{server.id}"

    rule = await find_matching_resource_limit_rule(db, client.name)
    assert rule is not None
    assert rule.used_cpu == pytest.approx(2.0)
    assert rule.used_ram == pytest.approx(4.0)
    assert rule.current_pods == 1


async def test_check_resources_and_save_server_cpu_exceeded(db: AsyncSession):
    await _make_rule(db, ".*", pods=5, cpu=1.0, ram=20.0, priority=-1)
    client = await _make_client(db)

    server = await check_resources_and_save_server(
        db, client.id, client.name, "over-cpu", _NAMESPACE, cpu_request=2.0, ram_request=1.0
    )
    assert server is None


async def test_check_resources_and_save_server_ram_exceeded(db: AsyncSession):
    await _make_rule(db, ".*", pods=5, cpu=10.0, ram=1.0, priority=-1)
    client = await _make_client(db)

    server = await check_resources_and_save_server(
        db, client.id, client.name, "over-ram", _NAMESPACE, cpu_request=1.0, ram_request=2.0
    )
    assert server is None


async def test_check_resources_and_save_server_pods_exceeded(db: AsyncSession):
    await _make_rule(db, ".*", pods=1, cpu=10.0, ram=20.0, priority=-1)
    client = await _make_client(db)

    first = await check_resources_and_save_server(
        db, client.id, client.name, "pod1", _NAMESPACE, cpu_request=1.0, ram_request=1.0
    )
    assert first is not None

    second = await check_resources_and_save_server(
        db, client.id, client.name, "pod2", _NAMESPACE, cpu_request=1.0, ram_request=1.0
    )
    assert second is None


async def test_check_resources_no_matching_rule(db: AsyncSession):
    """No rule at all → server is not created."""
    client = await _make_client(db)
    server = await check_resources_and_save_server(
        db, client.id, client.name, "no-rule", _NAMESPACE, cpu_request=1.0, ram_request=1.0
    )
    assert server is None


# ===========================================================================
# Server reuse (find_matching_finished_server)
# ===========================================================================


async def test_find_matching_finished_server(db: AsyncSession):
    client = await _make_client(db)
    finished = await _make_server(db, client, availability=AvailabilityStatus.FINISHED)

    result = await find_matching_finished_server(
        db,
        client_name=client.name,
        server_name=None,
        image_tag=_IMAGE,
        container_runtime=_RUNTIME,
        run_as_root=False,
        server_kind="idegym",
    )
    assert result.server is not None
    assert result.server.id == finished.id
    assert result.blocked_by_fifo is False

    # Server should have been marked REUSED
    assert result.server.availability == AvailabilityStatus.REUSED


async def test_find_matching_finished_server_no_match(db: AsyncSession):
    client = await _make_client(db)
    await _make_server(db, client)  # ALIVE, not FINISHED

    result = await find_matching_finished_server(
        db,
        client_name=client.name,
        server_name=None,
        image_tag=_IMAGE,
        container_runtime=_RUNTIME,
        run_as_root=False,
        server_kind="idegym",
    )
    assert result.server is None
    assert result.blocked_by_fifo is False


async def test_find_matching_finished_server_wrong_image(db: AsyncSession):
    client = await _make_client(db)
    await _make_server(db, client, image_tag="other:tag", availability=AvailabilityStatus.FINISHED)

    result = await find_matching_finished_server(
        db,
        client_name=client.name,
        server_name=None,
        image_tag=_IMAGE,
        container_runtime=_RUNTIME,
        run_as_root=False,
        server_kind="idegym",
    )
    assert result.server is None


async def test_find_matching_finished_server_fifo_blocked(db: AsyncSession):
    """When a SCHEDULED START_SERVER op exists and FIFO is enabled, reuse is blocked."""
    client = await _make_client(db)
    await _make_server(db, client, availability=AvailabilityStatus.FINISHED)

    # Schedule an older start-server operation for the same parameters
    request_payload = json.dumps(
        {
            "image_tag": _IMAGE,
            "runtime_class_name": _RUNTIME,
            "run_as_root": False,
            "server_kind": "idegym",
            "server_name": "my-server",
        }
    )
    op = await save_async_operation(db, AsyncOperationType.START_SERVER, client_id=client.id)
    # Back-date the scheduled_at so it appears older than "now"
    op.scheduled_at = 1  # epoch 1 ms — definitely in the past
    op.request = request_payload
    await db.commit()

    result = await find_matching_finished_server(
        db,
        client_name=client.name,
        server_name="my-server",
        image_tag=_IMAGE,
        container_runtime=_RUNTIME,
        run_as_root=False,
        server_kind="idegym",
        enable_fifo_check=True,
    )
    assert result.server is None
    assert result.blocked_by_fifo is True


# ===========================================================================
# Async operations
# ===========================================================================


async def test_save_and_get_async_operation(db: AsyncSession):
    client = await _make_client(db)
    op = await save_async_operation(db, AsyncOperationType.START_SERVER, client_id=client.id)

    assert op.id is not None
    assert op.status == AsyncOperationStatus.SCHEDULED
    assert op.request_type == AsyncOperationType.START_SERVER

    fetched = await get_async_operation(db, op.id)
    assert fetched is not None
    assert fetched.id == op.id


async def test_update_async_operation_to_in_progress(db: AsyncSession):
    client = await _make_client(db)
    op = await save_async_operation(db, AsyncOperationType.STOP_CLIENT, client_id=client.id)
    assert op.started_at is None

    updated = await update_async_operation(db, op.id, AsyncOperationStatus.IN_PROGRESS, orchestrator_pod="pod-abc")
    assert updated is not None
    assert updated.status == AsyncOperationStatus.IN_PROGRESS
    assert updated.started_at is not None
    assert updated.orchestrator_pod == "pod-abc"


async def test_update_async_operation_to_succeeded_sets_finished_at(db: AsyncSession):
    client = await _make_client(db)
    op = await save_async_operation(db, AsyncOperationType.STOP_SERVER, client_id=client.id)

    await update_async_operation(db, op.id, AsyncOperationStatus.IN_PROGRESS)
    done = await update_async_operation(db, op.id, AsyncOperationStatus.SUCCEEDED, result={"ok": True})
    assert done is not None
    assert done.status == AsyncOperationStatus.SUCCEEDED
    assert done.finished_at is not None


async def test_update_async_operation_returns_none_for_unknown(db: AsyncSession):
    result = await update_async_operation(db, 99999, AsyncOperationStatus.SUCCEEDED)
    assert result is None


async def test_delete_old_async_operations(db: AsyncSession):
    client = await _make_client(db)
    op = await save_async_operation(db, AsyncOperationType.FORWARD_REQUEST, client_id=client.id)

    # Mark it started long ago
    old_ts = 1_000_000  # epoch 1000s in ms
    op.started_at = old_ts
    await db.commit()

    current_time = 10_000_000  # 10000s in ms
    max_age = timedelta(seconds=1)  # 1 second → anything started before 9999s should be deleted

    deleted = await delete_old_async_operations(db, current_time=current_time, max_age=max_age)
    assert deleted == 1

    fetched = await get_async_operation(db, op.id)
    assert fetched is None


async def test_delete_old_async_operations_leaves_recent_ones(db: AsyncSession):
    client = await _make_client(db)
    op = await save_async_operation(db, AsyncOperationType.FORWARD_REQUEST, client_id=client.id)

    now = 10_000_000
    op.started_at = now - 500  # started 500 ms ago — within the 1-second window
    await db.commit()

    deleted = await delete_old_async_operations(db, current_time=now, max_age=timedelta(seconds=1))
    assert deleted == 0

    fetched = await get_async_operation(db, op.id)
    assert fetched is not None


async def test_mark_stale_async_operations_as_finished(db: AsyncSession):
    client = await _make_client(db)
    op = await save_async_operation(db, AsyncOperationType.START_SERVER, client_id=client.id)
    op.status = AsyncOperationStatus.IN_PROGRESS
    op.started_at = 1_000  # very old start time (1 second in ms)
    await db.commit()

    current_time = 1_000_000  # 1000s in ms
    stale_threshold = timedelta(seconds=1)  # ops started more than 1s ago

    updated = await mark_stale_async_operations_as_finished(
        db, current_time=current_time, stale_inprogress=stale_threshold
    )
    assert updated == 1

    fetched = await get_async_operation(db, op.id)
    assert fetched is not None
    assert fetched.status == AsyncOperationStatus.FINISHED_BY_WATCHER
    assert fetched.finished_at == current_time


async def test_mark_stale_does_not_touch_recent_in_progress(db: AsyncSession):
    client = await _make_client(db)
    op = await save_async_operation(db, AsyncOperationType.START_SERVER, client_id=client.id)
    now = 1_000_000
    op.status = AsyncOperationStatus.IN_PROGRESS
    op.started_at = now - 100  # only 100 ms ago — not stale
    await db.commit()

    updated = await mark_stale_async_operations_as_finished(db, current_time=now, stale_inprogress=timedelta(seconds=1))
    assert updated == 0


# ===========================================================================
# Job statuses
# ===========================================================================


async def test_save_and_get_job_status(db: AsyncSession):
    job = await save_job_status(db, job_name="build-123", tag="v1.0", status=Status.IN_PROGRESS)
    assert job.id is not None
    assert job.job_name == "build-123"
    assert job.status == Status.IN_PROGRESS

    fetched = await get_job_status(db, "build-123")
    assert fetched is not None
    assert fetched.id == job.id


async def test_get_job_status_by_id(db: AsyncSession):
    job = await save_job_status(db, job_name="build-by-id", tag="v2.0")

    fetched = await get_job_status_by_id(db, job.id)
    assert fetched is not None
    assert fetched.job_name == "build-by-id"


async def test_get_job_status_returns_none_for_unknown(db: AsyncSession):
    assert await get_job_status(db, "nonexistent-job") is None


async def test_save_job_status_upserts_existing(db: AsyncSession):
    """Second save_job_status call for the same job_name updates the existing record."""
    first = await save_job_status(db, job_name="kaniko-job", tag="v1", status=Status.IN_PROGRESS)
    second = await save_job_status(db, job_name="kaniko-job", tag="v1", status=Status.SUCCESS)

    assert first.id == second.id
    assert second.status == Status.SUCCESS


async def test_update_job_status(db: AsyncSession):
    await save_job_status(db, job_name="update-me", tag="v0")
    updated = await update_job_status(db, job_name="update-me", status=Status.SUCCESS, tag="v1", details="done")

    assert updated is not None
    assert updated.tag == "v1"
    assert updated.status == Status.SUCCESS
    assert updated.details == "done"


async def test_update_job_status_returns_none_for_unknown(db: AsyncSession):
    result = await update_job_status(db, "ghost-job", status=Status.FAILURE, tag="none")
    assert result is None


async def test_save_job_status_with_request_id(db: AsyncSession):
    job = await save_job_status(db, job_name="req-job", tag="v1", request_id="req-abc")
    assert job.request_id == "req-abc"

    fetched = await get_job_status(db, "req-job")
    assert fetched is not None
    assert fetched.request_id == "req-abc"
