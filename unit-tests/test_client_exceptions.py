"""Typed HTTP failures raised by the client.

Two things are pinned here: the mapping from status code to exception type, which is what a
retry policy branches on, and the fact that the change stayed backwards compatible — the
messages and the ``RuntimeError`` base are what existing callers already depend on.
"""

from uuid import uuid4

import httpx
import pytest
from idegym.api.exceptions import IdeGYMException
from idegym.api.orchestrator.servers import ErrorResponse
from idegym.client.exceptions import (
    IdeGYMAuthError,
    IdeGYMBadRequestError,
    IdeGYMBusyError,
    IdeGYMCancelledError,
    IdeGYMHTTPError,
    IdeGYMNotFoundError,
    IdeGYMServerError,
    IdeGYMTimeoutError,
    error_class_for_status,
    http_error,
)
from idegym.client.operations.forwarding import ForwardingOperations
from idegym.client.operations.utils import HTTPUtils


def _utils(handler) -> HTTPUtils:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(transport=transport, base_url="http://idegym.test")
    return HTTPUtils(http_client=client, current_namespace="idegym", current_client_id=None)


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (400, IdeGYMBadRequestError),
        (422, IdeGYMBadRequestError),
        (401, IdeGYMAuthError),
        (403, IdeGYMAuthError),
        (404, IdeGYMNotFoundError),
        (410, IdeGYMNotFoundError),
        (408, IdeGYMTimeoutError),
        (504, IdeGYMTimeoutError),
        (429, IdeGYMBusyError),
        (503, IdeGYMBusyError),
        (499, IdeGYMCancelledError),
        (500, IdeGYMServerError),
        (502, IdeGYMServerError),
        (418, IdeGYMBadRequestError),
        (None, IdeGYMHTTPError),
    ],
)
def test_status_code_selects_the_exception_type(status_code, expected) -> None:
    assert error_class_for_status(status_code) is expected


def test_every_typed_error_stays_catchable_as_before() -> None:
    error = http_error("boom", status_code=404, body="gone", method="GET", url="/api/x")

    assert isinstance(error, IdeGYMNotFoundError)
    assert isinstance(error, IdeGYMHTTPError)
    assert isinstance(error, IdeGYMException)
    assert isinstance(error, RuntimeError)
    assert (error.status_code, error.body, error.method, error.url) == (404, "gone", "GET", "/api/x")
    assert str(error) == "boom"


async def test_make_request_raises_the_typed_error_with_the_original_message() -> None:
    utils = _utils(lambda request: httpx.Response(429, text="slow down"))

    with pytest.raises(IdeGYMBusyError) as caught:
        await utils.make_request("GET", "/api/idegym-servers")

    assert caught.value.status_code == 429
    assert caught.value.body == "slow down"
    assert caught.value.url == "/api/idegym-servers"
    assert "Request failed: url=/api/idegym-servers status=429" in str(caught.value)


async def test_make_request_reports_a_client_side_timeout_as_a_timeout() -> None:
    def time_out(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    utils = _utils(time_out)

    with pytest.raises(IdeGYMTimeoutError) as caught:
        await utils.make_request("GET", "/api/idegym-servers")

    assert caught.value.status_code is None


async def test_a_gone_sandbox_is_distinguishable_from_a_busy_control_plane() -> None:
    gone = _utils(lambda request: httpx.Response(410, text="pod unreachable"))
    busy = _utils(lambda request: httpx.Response(429, text="quota"))

    with pytest.raises(IdeGYMNotFoundError):
        await gone.make_request("GET", "/api/idegym-servers/1/capabilities")
    with pytest.raises(IdeGYMBusyError):
        await busy.make_request("POST", "/api/idegym-servers")


async def test_forwarding_failure_carries_the_forwarded_status_and_body(mocker) -> None:
    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda client_id: client_id
    utils.make_request = mocker.AsyncMock(return_value={"async_operation_id": 5})
    utils.parse_response.side_effect = lambda response_raw, model_class: model_class.model_validate(response_raw)
    utils.wait_for_async_operation_to_end = mocker.AsyncMock(
        return_value=ErrorResponse(status_code=404, body="server 9 not found")
    )
    operations = ForwardingOperations(utils=utils)

    with pytest.raises(IdeGYMNotFoundError) as caught:
        await operations.forward_request("POST", 9, "tools/bash", client_id="c")

    assert caught.value.status_code == 404
    assert caught.value.body == "server 9 not found"
    assert "Failed to forward request POST" in str(caught.value)


async def test_start_server_failure_is_typed(mocker) -> None:
    from idegym.client.client import IdeGYMClient

    client = IdeGYMClient.__new__(IdeGYMClient)
    client._utils = mocker.MagicMock(current_client_id="c")
    client.server = mocker.MagicMock()
    client.server.start_server = mocker.AsyncMock(return_value=ErrorResponse(status_code=503, body="no capacity"))

    with pytest.raises(IdeGYMBusyError) as caught:
        await client.start_server(image_tag="registry.test/env:latest")

    assert caught.value.status_code == 503


# --------------------------------------------------------------------------------------
# Operations that used to report failure by returning it
# --------------------------------------------------------------------------------------


def _server_operations(mocker, terminal_result):
    from idegym.client.operations.servers import ServerOperations

    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda client_id: client_id
    utils.validate_namespace.side_effect = lambda namespace: namespace or "idegym"
    utils.make_request = mocker.AsyncMock(return_value={"server_name": "srv", "message": "ok", "operation_id": 3})
    utils.parse_response.side_effect = lambda response_raw, model_class: model_class.model_validate(response_raw)
    utils.wait_for_async_operation_to_end = mocker.AsyncMock(return_value=terminal_result)
    return ServerOperations(utils=utils, project=mocker.MagicMock())


async def test_stop_server_raises_instead_of_returning_the_failure(mocker) -> None:
    operations = _server_operations(mocker, ErrorResponse(status_code=500, body="delete failed"))

    with pytest.raises(IdeGYMServerError) as caught:
        await operations.stop_server(server_id=7, client_id=uuid4())

    assert caught.value.status_code == 500
    assert "Stopping server 7 failed" in str(caught.value)


async def test_restart_server_raises_instead_of_returning_the_failure(mocker) -> None:
    operations = _server_operations(mocker, ErrorResponse(status_code=410, body="pod gone"))

    with pytest.raises(IdeGYMNotFoundError):
        await operations.restart_server(server_id=7, client_id=uuid4())


async def test_a_successful_stop_still_returns_the_action_response(mocker) -> None:
    from idegym.api.orchestrator.servers import ServerActionResponse

    success = ServerActionResponse(server_name="srv", message="Successfully stopped")
    operations = _server_operations(mocker, success)

    assert await operations.stop_server(server_id=7, client_id=uuid4()) is success


def test_raise_for_error_response_passes_a_success_through() -> None:
    from idegym.client.exceptions import raise_for_error_response

    value = object()

    assert raise_for_error_response(value, "Doing a thing") is value
