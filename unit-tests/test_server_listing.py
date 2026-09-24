"""Listing the servers a client owns.

The point of the endpoint is recovery after a crash, so the cases that matter are the ones a
crashed client would hit: terminal rows, ordering, and scoping to one registration.
"""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from uuid import uuid4

import pytest
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.orchestrator.database import helpers
from idegym.orchestrator.router import server as server_router


def _row(server_id, availability=AvailabilityStatus.ALIVE, created_at=0, **overrides):
    record = {
        "id": server_id,
        "server_name": f"srv-{server_id}",
        "generated_name": f"srv-{server_id}-abc",
        "namespace": "idegym",
        "availability": availability,
        "image_tag": "registry.test/env:latest",
        "created_at": created_at,
        "last_heartbeat_time": created_at,
        "keepalive_until": None,
        "details": None,
    }
    record.update(overrides)
    return SimpleNamespace(**record)


@pytest.fixture
def listed(mocker):
    def configure(*rows):
        mocker.patch.object(server_router, "validate_client", mocker.AsyncMock())
        return mocker.patch.object(server_router, "list_client_servers", mocker.AsyncMock(return_value=list(rows)))

    return configure


async def test_listing_maps_every_row_and_marks_usability(listed) -> None:
    client_id = uuid4()
    query = listed(_row(1), _row(2, availability=AvailabilityStatus.FINISHED))

    response = await server_router.list_servers(client_id=client_id)

    query.assert_awaited_once_with(client_id=client_id, include_terminal=False)
    assert response.client_id == client_id
    assert [(s.server_id, s.usable) for s in response.servers] == [(1, True), (2, False)]


async def test_listing_reports_a_terminal_servers_reason(listed) -> None:
    listed(_row(1, availability=AvailabilityStatus.CRASHED, details="OOMKilled"))

    response = await server_router.list_servers(client_id=uuid4(), include_terminal=True)

    assert response.servers[0].details == "OOMKilled"
    assert response.servers[0].usable is False


async def test_include_terminal_is_passed_through(listed) -> None:
    query = listed()

    await server_router.list_servers(client_id=uuid4(), include_terminal=True)

    assert query.await_args.kwargs["include_terminal"] is True


async def test_an_unknown_client_is_rejected_before_listing(mocker) -> None:
    from fastapi import HTTPException

    mocker.patch.object(server_router, "validate_client", mocker.AsyncMock(side_effect=HTTPException(status_code=404)))
    query = mocker.patch.object(server_router, "list_client_servers", mocker.AsyncMock())

    with pytest.raises(HTTPException):
        await server_router.list_servers(client_id=uuid4())

    query.assert_not_awaited()


# --------------------------------------------------------------------------------------
# The database helper
# --------------------------------------------------------------------------------------


@pytest.fixture
def owned_rows(mocker):
    """Feed rows to the helper, standing in for the session ``@with_db_session`` would open."""

    @asynccontextmanager
    async def session():
        yield mocker.MagicMock()

    def configure(*rows):
        mocker.patch.object(helpers, "get_db_session", session)
        mocker.patch.object(helpers, "get_idegym_servers_by_client_id", mocker.AsyncMock(return_value=list(rows)))

    return configure


async def test_helper_hides_terminal_servers_by_default(owned_rows) -> None:
    owned_rows(_row(1), _row(2, availability=AvailabilityStatus.KILLED))

    servers = await helpers.list_client_servers(client_id=uuid4(), include_terminal=False)

    assert [server.id for server in servers] == [1]


async def test_helper_returns_terminal_servers_when_asked(owned_rows) -> None:
    owned_rows(_row(1), _row(2, availability=AvailabilityStatus.KILLED))

    servers = await helpers.list_client_servers(client_id=uuid4(), include_terminal=True)

    assert {server.id for server in servers} == {1, 2}


async def test_helper_returns_newest_first(owned_rows) -> None:
    owned_rows(_row(1, created_at=100), _row(2, created_at=300), _row(3, created_at=200))

    servers = await helpers.list_client_servers(client_id=uuid4(), include_terminal=True)

    assert [server.id for server in servers] == [2, 3, 1]


async def test_client_wrapper_returns_the_rows_and_sends_the_filter(mocker) -> None:
    from idegym.client.operations.servers import ServerOperations

    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda client_id: client_id
    client_id = uuid4()
    utils.make_request = mocker.AsyncMock(
        return_value={
            "client_id": str(client_id),
            "servers": [
                {
                    "server_id": 1,
                    "generated_name": "srv-1-abc",
                    "namespace": "idegym",
                    "availability": "ALIVE",
                    "usable": True,
                    "created_at": 1,
                    "last_activity_at": 1,
                }
            ],
        }
    )
    operations = ServerOperations(utils=utils, project=mocker.MagicMock())

    response = await operations.list_servers(client_id=client_id, include_terminal=True)

    utils.make_request.assert_awaited_once_with(
        "GET", "/api/idegym-servers", params={"client_id": str(client_id), "include_terminal": True}
    )
    assert [server.server_id for server in response.servers] == [1]
