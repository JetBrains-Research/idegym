"""Client-side deadlines around starting a server and polling an async operation.

Two failures are pinned here. The client used to give up at the same moment the orchestrator
did, so the orchestrator's diagnosis of a slow start never reached the caller; and a long poll
backoff could put the last poll well before the deadline, so an operation that finished in that
gap was reported as timed out.
"""

import asyncio
from uuid import uuid4

import httpx
import pytest
from idegym.api.orchestrator.servers import ServerActionResponse, StartServerResponse
from idegym.client.exceptions import IdeGYMTimeoutError
from idegym.client.operations import utils as utils_module
from idegym.client.operations.servers import ServerOperations, _client_start_deadline
from idegym.client.operations.utils import HTTPUtils, PollingConfig


def _status(status: str, result=None) -> dict:
    return {"id": 1, "request_type": "x", "status": status, "result": result, "scheduled_at": 0}


def _polling_utils(statuses: list[dict]) -> tuple[HTTPUtils, list[httpx.Request]]:
    polls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        polls.append(request)
        return httpx.Response(200, json=statuses[min(len(polls), len(statuses)) - 1])

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://idegym.test")
    return HTTPUtils(http_client=client, current_namespace="idegym", current_client_id=None), polls


def _polling(wait_timeout: float, poll_interval: float) -> PollingConfig:
    # model_construct, since the field is whole seconds and these tests should not take one.
    return PollingConfig.model_construct(
        initial_delay_in_sec=0.0,
        wait_timeout_in_sec=wait_timeout,
        poll_interval_in_sec=poll_interval,
        factor_for_exponential_wait=1.5,
        max_delay_for_exponential_wait_in_sec=120.0,
    )


# --------------------------------------------------------------------------------------
# Polling
# --------------------------------------------------------------------------------------


async def test_a_backoff_longer_than_the_deadline_still_polls_at_the_deadline() -> None:
    """The next scheduled poll lies far past the deadline; the one at the deadline sees success."""
    utils, polls = _polling_utils([_status("IN_PROGRESS"), _status("SUCCEEDED", result="done")])

    result = await utils.wait_for_async_operation_to_end(operation_id=1, polling_config=_polling(0.2, 3600))

    assert result == "done"
    assert len(polls) == 2


async def test_the_final_poll_at_the_deadline_is_the_last() -> None:
    utils, polls = _polling_utils([_status("IN_PROGRESS")])

    with pytest.raises(IdeGYMTimeoutError, match="did not finish within"):
        await utils.wait_for_async_operation_to_end(operation_id=1, polling_config=_polling(0.2, 3600))

    assert len(polls) == 2


async def test_a_hanging_poll_is_bounded_past_the_deadline(mocker) -> None:
    mocker.patch.object(utils_module, "_FINAL_POLL_ALLOWANCE_IN_SEC", 0.1)

    async def hang(request):
        await asyncio.sleep(3600)

    client = httpx.AsyncClient(transport=httpx.MockTransport(hang), base_url="http://idegym.test")
    utils = HTTPUtils(http_client=client, current_namespace="idegym", current_client_id=None)

    with pytest.raises(IdeGYMTimeoutError, match="did not finish within"):
        await utils.wait_for_async_operation_to_end(operation_id=1, polling_config=_polling(0.1, 0.01))


# --------------------------------------------------------------------------------------
# Start and restart deadlines
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("server_timeout", "client_deadline"), [(300, 360), (30, 90), (1000, 1100)])
def test_the_client_waits_a_grace_period_past_the_server_timeout(server_timeout, client_deadline) -> None:
    assert _client_start_deadline(server_timeout) == client_deadline


def _server_operations(mocker, make_request_result: dict, terminal_results: list):
    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda value: value
    utils.validate_namespace.side_effect = lambda namespace: namespace or "idegym"
    utils.make_request = mocker.AsyncMock(return_value=make_request_result)
    utils.parse_response.side_effect = lambda response_raw, model_class: model_class.model_validate(response_raw)
    utils.wait_for_async_operation_to_end = mocker.AsyncMock(side_effect=terminal_results)
    return ServerOperations(utils=utils, project=mocker.MagicMock()), utils


async def test_start_server_sends_the_timeout_but_waits_past_it(mocker) -> None:
    client_id = uuid4()
    started = StartServerResponse(namespace="idegym", client_id=client_id, server_id=5)
    operations, utils = _server_operations(
        mocker, {"namespace": "idegym", "client_id": str(client_id), "operation_id": 3}, [started]
    )

    await operations.start_server(
        image_tag="registry.test/env:latest", client_id=client_id, server_start_wait_timeout_in_seconds=300
    )

    request = utils.make_request.call_args.args[2]
    assert request.server_start_wait_timeout_in_seconds == 300
    assert utils.make_request.call_args.kwargs["request_timeout"] == 360
    assert utils.wait_for_async_operation_to_end.call_args.kwargs["polling_config"].wait_timeout_in_sec == 360


async def test_start_server_keeps_retrying_a_429_through_the_grace_period(mocker) -> None:
    from idegym.api.orchestrator.servers import ErrorResponse

    sleep = mocker.patch("idegym.client.operations.servers.sleep", new=mocker.AsyncMock())
    client_id = uuid4()
    started = StartServerResponse(namespace="idegym", client_id=client_id, server_id=5)
    operations, _ = _server_operations(
        mocker,
        {"namespace": "idegym", "client_id": str(client_id), "operation_id": 3},
        [ErrorResponse(status_code=429, body="quota"), started],
    )

    # A 15s retry does not fit in the 10s the server is given, but does fit in the client's deadline.
    response = await operations.start_server(
        image_tag="registry.test/env:latest",
        client_id=client_id,
        server_start_wait_timeout_in_seconds=10,
        retry_delay_in_seconds=15,
    )

    assert response is started
    sleep.assert_awaited_once_with(15)


async def test_restart_server_sends_the_timeout_but_waits_past_it(mocker) -> None:
    success = ServerActionResponse(server_name="srv", message="restarted")
    operations, utils = _server_operations(
        mocker, {"server_name": "srv", "message": "ok", "operation_id": 3}, [success]
    )

    await operations.restart_server(server_id=7, client_id=uuid4(), server_start_wait_timeout_in_seconds=300)

    request = utils.make_request.call_args.args[2]
    assert request.server_start_wait_timeout_in_seconds == 300
    assert utils.make_request.call_args.kwargs["request_timeout"] == 360
    assert utils.wait_for_async_operation_to_end.call_args.kwargs["polling_config"].wait_timeout_in_sec == 360
