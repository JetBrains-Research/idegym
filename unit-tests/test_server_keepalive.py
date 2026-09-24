"""The explicit keepalive: the endpoint, the watcher's respect for it, and the client call.

The behaviour that matters is negative — a held server must survive a cleanup pass it would
otherwise have failed — so most of these drive ``cleanup_servers`` directly.
"""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.servers import KeepaliveServerRequest
from idegym.api.type import Duration
from idegym.orchestrator.router import server as server_router
from idegym.watcher import cleanup as watcher_cleanup
from pydantic import ValidationError

NOW = 10_000_000
INACTIVE_TIMEOUT = Duration(minutes=10)
FINISHED_TIMEOUT = Duration(minutes=5)


def _stale_server(**overrides) -> SimpleNamespace:
    """A server whose last request finished an hour ago — well past every timeout."""
    record = {
        "id": 7,
        "generated_name": "srv-abc123",
        "namespace": "idegym",
        "availability": AvailabilityStatus.ALIVE,
        "last_heartbeat_time": NOW - 60 * 60 * 1000,
        "keepalive_until": None,
    }
    record.update(overrides)
    return SimpleNamespace(**record)


@pytest.fixture
def cleanup_pass(mocker):
    """Run one ``cleanup_servers`` pass over the given servers and report what was deleted."""

    async def run(*servers):
        mocker.patch.object(watcher_cleanup, "get_idegym_servers_by_status", mocker.AsyncMock(return_value=servers))
        deleted = mocker.patch.object(watcher_cleanup, "clean_up_server", mocker.AsyncMock())
        mocker.patch.object(watcher_cleanup, "update_idegym_server_heartbeat", mocker.AsyncMock())

        await watcher_cleanup.cleanup_servers(
            db=mocker.MagicMock(),
            current_time=NOW,
            inactive_timeout=INACTIVE_TIMEOUT,
            finished_timeout=FINISHED_TIMEOUT,
        )
        return [call.kwargs["name"] for call in deleted.await_args_list]

    return run


# --------------------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------------------


async def test_an_idle_server_without_a_hold_is_still_reaped(cleanup_pass) -> None:
    assert await cleanup_pass(_stale_server()) == ["srv-abc123"]


async def test_a_held_server_survives_the_reaper(cleanup_pass) -> None:
    assert await cleanup_pass(_stale_server(keepalive_until=NOW + 60_000)) == []


async def test_an_expired_hold_stops_protecting_the_server(cleanup_pass) -> None:
    assert await cleanup_pass(_stale_server(keepalive_until=NOW - 1)) == ["srv-abc123"]


async def test_a_hold_protects_a_finished_server_too(cleanup_pass) -> None:
    held = _stale_server(availability=AvailabilityStatus.FINISHED, keepalive_until=NOW + 60_000)

    assert await cleanup_pass(held) == []


async def test_a_hold_on_one_server_does_not_protect_another(cleanup_pass) -> None:
    held = _stale_server(id=1, generated_name="held", keepalive_until=NOW + 60_000)
    unheld = _stale_server(id=2, generated_name="unheld")

    assert await cleanup_pass(held, unheld) == ["unheld"]


# --------------------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------------------


async def test_keepalive_extends_the_window_by_the_requested_minutes(mocker) -> None:
    mocker.patch.object(server_router, "current_time_millis", return_value=NOW)
    extend = mocker.patch.object(
        server_router,
        "extend_server_keepalive",
        mocker.AsyncMock(return_value=SimpleNamespace(id=7, keepalive_until=NOW + 30 * 60_000)),
    )
    client_id = uuid4()

    response = await server_router.keepalive_server(
        KeepaliveServerRequest(client_id=client_id, server_id=7, minutes=30)
    )

    extend.assert_awaited_once_with(client_id=client_id, server_id=7, until=NOW + 30 * 60_000)
    assert response.keepalive_until == NOW + 30 * 60_000
    assert response.minutes == 30


async def test_keepalive_reports_the_window_actually_in_effect(mocker) -> None:
    """A longer hold already in place wins, and the response says so rather than echoing the ask."""
    mocker.patch.object(server_router, "current_time_millis", return_value=NOW)
    mocker.patch.object(
        server_router,
        "extend_server_keepalive",
        mocker.AsyncMock(return_value=SimpleNamespace(id=7, keepalive_until=NOW + 60 * 60_000)),
    )

    response = await server_router.keepalive_server(KeepaliveServerRequest(client_id=uuid4(), server_id=7, minutes=5))

    assert response.minutes == 60


@pytest.mark.parametrize("minutes", [0, -1, 24 * 60 + 1])
def test_keepalive_request_rejects_a_window_outside_the_allowed_range(minutes) -> None:
    with pytest.raises(ValidationError):
        KeepaliveServerRequest(client_id=uuid4(), server_id=7, minutes=minutes)


def test_keepalive_request_defaults_to_a_bounded_window() -> None:
    assert KeepaliveServerRequest(client_id=uuid4(), server_id=7).minutes == 15.0


# --------------------------------------------------------------------------------------
# The client
# --------------------------------------------------------------------------------------


async def test_client_sends_the_requested_window(mocker) -> None:
    from idegym.client.operations.servers import ServerOperations

    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda client_id: client_id
    utils.validate_namespace.side_effect = lambda namespace: namespace or "idegym"
    utils.make_request = mocker.AsyncMock(return_value={"server_id": 7, "keepalive_until": NOW, "minutes": 45.0})
    operations = ServerOperations(utils=utils, project=mocker.MagicMock())

    response = await operations.keepalive_server(server_id=7, minutes=45, client_id=uuid4())

    sent = utils.make_request.await_args.args[2]
    assert (utils.make_request.await_args.args[1], sent.minutes, sent.server_id) == (
        "/api/idegym-servers/keepalive",
        45,
        7,
    )
    assert response.minutes == 45.0
