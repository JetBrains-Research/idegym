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
        "keepalive_until": None,
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
    assert status.keepalive_until is None


async def test_status_reports_an_active_keepalive_hold(stub_orchestrator) -> None:
    stub_orchestrator(_record(keepalive_until=1_900_000))

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    assert status.keepalive_until == 1_900_000


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


def test_only_alive_and_reused_servers_are_usable() -> None:
    """``FINISHED`` is neither terminal nor usable: the server exists but has been handed back."""
    assert {status for status in AvailabilityStatus if status.is_usable} == {
        AvailabilityStatus.ALIVE,
        AvailabilityStatus.REUSED,
    }
    assert not any(status.is_usable and status.is_terminal for status in AvailabilityStatus)


def test_status_response_is_the_list_row_plus_the_pod_view() -> None:
    from idegym.api.orchestrator.servers import ServerStatusResponse, ServerSummary

    extra = set(ServerStatusResponse.model_fields) - set(ServerSummary.model_fields)

    assert issubclass(ServerStatusResponse, ServerSummary)
    assert extra == {"idle_seconds", "pod_phase", "pod_ready"}


async def test_a_crashed_server_reports_its_reason_instead_of_raising(stub_orchestrator) -> None:
    """`validate_server` would 410 here; a status endpoint has to answer."""
    _, pods = stub_orchestrator(_record(availability=AvailabilityStatus.CRASHED, details="OOMKilled"))

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    assert status.availability == AvailabilityStatus.CRASHED
    assert status.details == "OOMKilled"
    assert (status.pod_phase, status.pod_ready) == (None, None)
    pods.assert_not_awaited()


async def test_a_live_server_without_a_pod_reports_it_not_ready(stub_orchestrator) -> None:
    stub_orchestrator(_record(), pod=(None, False))

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    assert (status.pod_phase, status.pod_ready) == (None, False)


@pytest.mark.parametrize(
    "availability",
    [status for status in AvailabilityStatus if status.is_terminal],
)
async def test_a_terminal_server_is_not_looked_up_in_kubernetes(stub_orchestrator, availability) -> None:
    _, pods = stub_orchestrator(_record(availability=availability))

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    pods.assert_not_awaited()
    assert status.availability == availability
    assert status.pod_ready is None


@pytest.mark.parametrize("error", [TimeoutError("list_pods timed out"), RuntimeError("403 Forbidden")])
async def test_a_kubernetes_failure_still_reports_the_record(stub_orchestrator, mocker, error) -> None:
    """RBAC, an API timeout or a deleted namespace must not hide the recorded availability."""
    stub_orchestrator(_record(availability=AvailabilityStatus.FINISHED, details="handed back"))
    mocker.patch.object(server_router, "pod_phase_and_readiness", mocker.AsyncMock(side_effect=error))
    warning = mocker.patch.object(server_router.logger, "warning")

    status = await server_router.get_server_status(server_id=7, client_id=uuid4())

    assert (status.availability, status.details) == (AvailabilityStatus.FINISHED, "handed back")
    assert (status.pod_phase, status.pod_ready) == (None, None)
    assert warning.call_args.kwargs["server"] == "my-server-abc123"


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
# validate_server, the strict counterpart
# --------------------------------------------------------------------------------------


@pytest.fixture
def validating(mocker):
    """Stand in for the session ``@with_db_session`` opens and for the ownership lookup."""
    from contextlib import asynccontextmanager

    from idegym.orchestrator.database import helpers

    @asynccontextmanager
    async def session():
        yield mocker.MagicMock()

    def configure(**owned):
        mocker.patch.object(helpers, "get_db_session", session)
        mocker.patch.object(helpers, "_load_owned_server", mocker.AsyncMock(**owned))
        return helpers

    return configure


@pytest.mark.parametrize("availability", [AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED])
async def test_validate_server_accepts_a_usable_server(validating, availability) -> None:
    record = _record(availability=availability)
    helpers = validating(return_value=record)

    assert await helpers.validate_server(client_id=uuid4(), server_id=7) is record


async def test_validate_server_rejects_an_unusable_server_with_its_reason(validating) -> None:
    from fastapi import HTTPException

    helpers = validating(return_value=_record(availability=AvailabilityStatus.CRASHED, details="OOMKilled"))

    with pytest.raises(HTTPException) as caught:
        await helpers.validate_server(client_id=uuid4(), server_id=7)

    assert caught.value.status_code == 410
    assert caught.value.detail.endswith("(status: CRASHED): OOMKilled")


async def test_validate_server_shares_the_ownership_check(validating) -> None:
    from fastapi import HTTPException

    helpers = validating(side_effect=HTTPException(status_code=404))

    with pytest.raises(HTTPException) as caught:
        await helpers.validate_server(client_id=uuid4(), server_id=7)

    assert caught.value.status_code == 404


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
