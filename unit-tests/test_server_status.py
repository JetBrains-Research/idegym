"""The per-server status endpoint and its client wrapper.

The properties worth pinning are the ones that make it usable as a liveness probe: it answers
for a dead server instead of raising, and it does not count as activity.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.orchestrator.router import server as server_router


def _record(**overrides) -> SimpleNamespace:
    record = {
        "id": 7,
        "server_name": "my-server",
        "generated_name": "my-server-abc123",
        "namespace": "idegym",
        "availability": AvailabilityStatus.ALIVE,
        "image_tag": "registry.test/env:latest",
        "created_at": 1_000_000,
        "last_heartbeat_time": 1_060_000,
        "details": None,
    }
    record.update(overrides)
    return SimpleNamespace(**record)


@pytest.fixture
def stub_orchestrator(mocker):
    """Patch the two things the handler reaches out to, and freeze the clock."""

    def configure(record, pod=("Running", True)):
        owned = mocker.patch.object(server_router, "get_owned_server", mocker.AsyncMock(return_value=record))
        pods = mocker.patch.object(server_router, "pod_phase_and_readiness", mocker.AsyncMock(return_value=pod))
        mocker.patch.object(server_router, "current_time_millis", return_value=1_120_000)
        return owned, pods

    return configure


async def test_status_reports_the_record_the_pod_and_the_idle_time(stub_orchestrator) -> None:
    client_id = uuid4()
    owned, pods = stub_orchestrator(_record())

    status = await server_router.get_server_status(server_id=7, client_id=client_id)

    owned.assert_awaited_once_with(client_id=client_id, server_id=7)
    pods.assert_awaited_once_with("app=my-server-abc123", "idegym")
    assert status.availability == AvailabilityStatus.ALIVE
    assert status.usable is True
    assert status.pod_phase == "Running"
    assert status.pod_ready is True
    assert status.last_activity_at == 1_060_000
    assert status.idle_seconds == 60.0


@pytest.mark.parametrize(
    ("availability", "usable"),
    [
        (AvailabilityStatus.ALIVE, True),
        (AvailabilityStatus.REUSED, True),
        (AvailabilityStatus.FINISHED, False),
        (AvailabilityStatus.CRASHED, False),
        (AvailabilityStatus.KILLED, False),
    ],
)
async def test_usable_tracks_the_states_that_accept_requests(stub_orchestrator, availability, usable) -> None:
    stub_orchestrator(_record(availability=availability))

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    assert status.usable is usable


async def test_a_crashed_server_reports_its_reason_instead_of_raising(stub_orchestrator) -> None:
    """`validate_server` would 410 here; a status endpoint has to answer."""
    stub_orchestrator(_record(availability=AvailabilityStatus.CRASHED, details="OOMKilled"), pod=(None, False))

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    assert status.availability == AvailabilityStatus.CRASHED
    assert status.details == "OOMKilled"
    assert status.pod_phase is None
    assert status.pod_ready is False


async def test_reading_status_does_not_record_activity(stub_orchestrator, mocker) -> None:
    stub_orchestrator(_record())
    update = mocker.patch.object(server_router, "update_server_status", mocker.AsyncMock())

    await server_router.get_server_status(server_id=7, client_id=uuid4())

    update.assert_not_awaited()


async def test_idle_seconds_never_goes_negative_on_clock_skew(stub_orchestrator) -> None:
    stub_orchestrator(_record(last_heartbeat_time=9_000_000))

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    assert status.idle_seconds == 0


async def test_client_wrapper_passes_the_client_id_and_parses_the_response(mocker) -> None:
    from idegym.client.operations.servers import ServerOperations

    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda client_id: client_id
    utils.make_request = mocker.AsyncMock(
        return_value={
            "server_id": 7,
            "generated_name": "my-server-abc123",
            "namespace": "idegym",
            "availability": "ALIVE",
            "usable": True,
            "created_at": 1,
            "last_activity_at": 2,
            "idle_seconds": 0.5,
            "pod_ready": True,
        }
    )
    operations = ServerOperations(utils=utils, project=mocker.MagicMock())
    client_id = uuid4()

    status = await operations.get_server_status(server_id=7, client_id=client_id)

    utils.make_request.assert_awaited_once_with(
        "GET", "/api/idegym-servers/7/status", params={"client_id": str(client_id)}
    )
    assert status.usable is True


# --------------------------------------------------------------------------------------
# Pod phase lookup
# --------------------------------------------------------------------------------------


def _pod(phase, *, ready=True, terminating=False, containers=1):
    return SimpleNamespace(
        metadata=SimpleNamespace(name="pod", deletion_timestamp=object() if terminating else None),
        status=SimpleNamespace(
            phase=phase,
            container_statuses=[SimpleNamespace(ready=ready) for _ in range(containers)],
        ),
    )


@pytest.mark.parametrize(
    ("pods", "expected"),
    [
        ([], (None, False)),
        ([_pod("Running")], ("Running", True)),
        ([_pod("Running", ready=False)], ("Running", False)),
        ([_pod("Pending")], ("Pending", False)),
        ([_pod("Running", containers=0)], ("Running", False)),
    ],
)
async def test_pod_phase_and_readiness_summarises_one_pod(mocker, pods, expected) -> None:
    from idegym.backend.utils import kubernetes_client

    mocker.patch.object(kubernetes_client, "list_pods", mocker.AsyncMock(return_value=pods))

    assert await kubernetes_client.pod_phase_and_readiness("app=x", "idegym") == expected


async def test_pod_phase_and_readiness_ignores_a_pod_on_its_way_out(mocker) -> None:
    from idegym.backend.utils import kubernetes_client

    mocker.patch.object(
        kubernetes_client,
        "list_pods",
        mocker.AsyncMock(return_value=[_pod("Running", terminating=True), _pod("Pending")]),
    )

    assert await kubernetes_client.pod_phase_and_readiness("app=x", "idegym") == ("Pending", False)
