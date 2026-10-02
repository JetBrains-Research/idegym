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


async def test_an_unknown_client_is_a_404_from_the_listing(mocker) -> None:
    from fastapi import HTTPException

    mocker.patch.object(
        server_router, "list_client_servers", mocker.AsyncMock(side_effect=HTTPException(status_code=404))
    )

    with pytest.raises(HTTPException) as caught:
        await server_router.list_servers(client_id=uuid4())

    assert caught.value.status_code == 404


# --------------------------------------------------------------------------------------
# The database helper
# --------------------------------------------------------------------------------------


@pytest.fixture
def owned_rows(mocker):
    """Stand in for the session ``@with_db_session`` would open, the client lookup and the query."""

    @asynccontextmanager
    async def session():
        yield mocker.MagicMock()

    def configure(*rows, client=True):
        mocker.patch.object(helpers, "get_db_session", session)
        lookup = mocker.patch.object(helpers, "get_client", mocker.AsyncMock(return_value=client or None))
        query = mocker.patch.object(
            helpers, "list_idegym_servers_by_client_id", mocker.AsyncMock(return_value=list(rows))
        )
        return lookup, query

    return configure


@pytest.mark.parametrize("include_terminal", [True, False])
async def test_helper_filters_and_orders_in_the_query(owned_rows, include_terminal) -> None:
    lookup, query = owned_rows(_row(2), _row(1))
    client_id = uuid4()

    servers = await helpers.list_client_servers(client_id=client_id, include_terminal=include_terminal)

    assert [server.id for server in servers] == [2, 1]
    # The client check and the listing share the one session the decorator opened.
    assert lookup.await_args.args[0] is query.await_args.args[0]
    assert query.await_args.args[1:] == (client_id,)
    assert query.await_args.kwargs == {"include_terminal": include_terminal}


async def test_helper_rejects_an_unknown_client_without_querying(owned_rows) -> None:
    from fastapi import HTTPException

    _, query = owned_rows(client=False)

    with pytest.raises(HTTPException) as caught:
        await helpers.list_client_servers(client_id=uuid4(), include_terminal=False)

    assert caught.value.status_code == 404
    query.assert_not_awaited()


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
