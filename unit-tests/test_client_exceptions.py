"""Typed HTTP failures raised by the client.

Two things are pinned here: the mapping from status code to exception type, which is what a
retry policy branches on, and the fact that the change stayed backwards compatible — the
messages and the ``RuntimeError`` base are what existing callers already depend on.
"""

from typing import Optional
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
    IdeGYMConnectionError,
    IdeGYMHTTPError,
    IdeGYMNotFoundError,
    IdeGYMSandboxError,
    IdeGYMServerError,
    IdeGYMTimeoutError,
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
    assert type(http_error("failed", status_code=status_code)) is expected


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
    assert isinstance(caught.value.__cause__, httpx.ReadTimeout)


def test_a_timeout_is_still_a_builtin_timeout_error() -> None:
    error = IdeGYMTimeoutError("slow", status_code=504)

    assert isinstance(error, TimeoutError)
    assert isinstance(error, IdeGYMHTTPError)
    assert isinstance(error, RuntimeError)
    assert (str(error), error.status_code) == ("slow", 504)


async def test_make_request_chains_the_status_error() -> None:
    utils = _utils(lambda request: httpx.Response(500, text="boom"))

    with pytest.raises(IdeGYMServerError) as caught:
        await utils.make_request("GET", "/api/idegym-servers")

    assert isinstance(caught.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.parametrize("transport_error", [httpx.ConnectError, httpx.RemoteProtocolError, httpx.ReadError])
async def test_make_request_types_a_transport_failure(transport_error) -> None:
    def fail(request):
        raise transport_error("connection lost", request=request)

    utils = _utils(fail)

    with pytest.raises(IdeGYMConnectionError) as caught:
        await utils.make_request("POST", "/api/idegym-servers")

    assert isinstance(caught.value, IdeGYMHTTPError)
    assert not isinstance(caught.value, IdeGYMTimeoutError)
    assert (caught.value.status_code, caught.value.method, caught.value.url) == (None, "POST", "/api/idegym-servers")
    assert isinstance(caught.value.__cause__, transport_error)


async def test_polling_deadline_raises_a_typed_timeout() -> None:
    from idegym.client.operations.utils import PollingConfig

    utils = _utils(
        lambda request: httpx.Response(
            200, json={"id": 1, "request_type": "x", "status": "IN_PROGRESS", "scheduled_at": 0}
        )
    )

    with pytest.raises(IdeGYMTimeoutError, match="did not finish within") as caught:
        await utils.wait_for_async_operation_to_end(
            operation_id=1,
            # model_construct, since the field is whole seconds and the test should not take one.
            polling_config=PollingConfig.model_construct(
                initial_delay_in_sec=0.0,
                poll_interval_in_sec=0.01,
                wait_timeout_in_sec=0.1,
                factor_for_exponential_wait=1.5,
                max_delay_for_exponential_wait_in_sec=1.0,
            ),
        )

    assert caught.value.status_code is None
    assert isinstance(caught.value, TimeoutError)


async def test_a_request_timeout_while_polling_is_not_rewrapped() -> None:
    def time_out(request):
        raise httpx.ReadTimeout("read timed out", request=request)

    utils = _utils(time_out)

    with pytest.raises(IdeGYMTimeoutError, match="Request timed out"):
        await utils.wait_for_async_operation_to_end(operation_id=1)


async def test_start_server_deadline_raises_a_typed_timeout(mocker) -> None:
    operations = _server_operations(mocker, None)
    # The first reading starts the clock, the second is already past the deadline.
    mocker.patch("idegym.client.operations.servers.time", mocker.MagicMock(time=mocker.MagicMock(side_effect=[0, 1e6])))

    with pytest.raises(IdeGYMTimeoutError, match="Server start timed out"):
        await operations.start_server(image_tag="registry.test/env:latest", client_id=uuid4())


async def test_a_gone_sandbox_is_distinguishable_from_a_busy_control_plane() -> None:
    gone = _utils(lambda request: httpx.Response(410, text="pod unreachable"))
    busy = _utils(lambda request: httpx.Response(429, text="quota"))

    with pytest.raises(IdeGYMNotFoundError):
        await gone.make_request("GET", "/api/idegym-servers/1/capabilities")
    with pytest.raises(IdeGYMBusyError):
        await busy.make_request("POST", "/api/idegym-servers")


def _forwarding(mocker, terminal_result) -> ForwardingOperations:
    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda client_id: client_id
    utils.make_request = mocker.AsyncMock(return_value={"async_operation_id": 5})
    utils.parse_response.side_effect = lambda response_raw, model_class: model_class.model_validate(response_raw)
    utils.wait_for_async_operation_to_end = mocker.AsyncMock(return_value=terminal_result)
    return ForwardingOperations(utils=utils)


async def test_forwarding_failure_carries_the_forwarded_status_and_body(mocker) -> None:
    operations = _forwarding(mocker, ErrorResponse(status_code=404, body='{"detail":"Path not found"}'))

    with pytest.raises(IdeGYMSandboxError) as caught:
        await operations.forward_request("POST", 9, "tools/bash", client_id="c")

    # A live sandbox answering 404 is not "the sandbox is gone".
    assert not isinstance(caught.value, IdeGYMNotFoundError)
    assert isinstance(caught.value, IdeGYMHTTPError)
    assert caught.value.status_code == 404
    assert caught.value.body == '{"detail":"Path not found"}'
    assert "Failed to forward request POST" in str(caught.value)


@pytest.mark.parametrize(
    ("status_code", "body", "expected"),
    [
        (410, "Failed to forward request: unable to connect to http://srv", IdeGYMNotFoundError),
        (499, "Failed to forward request: client disconnected", IdeGYMCancelledError),
        (500, "Failed to forward request to http://srv: ReadTimeout", IdeGYMServerError),
        (500, '{"detail":"boom"}', IdeGYMSandboxError),
        (422, '{"detail":"invalid"}', IdeGYMSandboxError),
    ],
)
async def test_forwarding_keeps_orchestrator_statuses_on_the_normal_mapping(mocker, status_code, body, expected):
    operations = _forwarding(mocker, ErrorResponse(status_code=status_code, body=body))

    with pytest.raises(IdeGYMHTTPError) as caught:
        await operations.forward_request("POST", 9, "tools/bash", client_id="c")

    assert type(caught.value) is expected
    assert caught.value.status_code == status_code


async def test_start_server_failure_is_typed(mocker) -> None:
    from idegym.client.client import IdeGYMClient

    client = IdeGYMClient.__new__(IdeGYMClient)
    client._utils = mocker.MagicMock(current_client_id="c")
    client.server = mocker.MagicMock()
    client.server.start_server = mocker.AsyncMock(return_value=ErrorResponse(status_code=503, body="no capacity"))

    with pytest.raises(IdeGYMBusyError) as caught:
        await client.start_server(image_tag="registry.test/env:latest", server_name="srv")

    assert caught.value.status_code == 503
    assert caught.value.body == "no capacity"
    assert str(caught.value).startswith("Starting server srv failed: ")


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


# --------------------------------------------------------------------------------------
# with_server cleanup
# --------------------------------------------------------------------------------------


def _client_with_server(mocker, cleanup_error: Optional[Exception]):
    from idegym.client.client import IdeGYMClient

    client = IdeGYMClient.__new__(IdeGYMClient)
    client.start_server = mocker.AsyncMock(return_value=mocker.MagicMock(server_id=7))
    client.stop_server = mocker.AsyncMock(side_effect=cleanup_error)
    client.finish_server = mocker.AsyncMock(side_effect=cleanup_error)
    return client


@pytest.mark.parametrize("close_action", ["STOP", "FINISH"])
async def test_with_server_keeps_the_body_exception_when_cleanup_also_fails(mocker, close_action) -> None:
    from idegym.client.client import ServerCloseAction

    client = _client_with_server(mocker, http_error("already gone", status_code=404))

    async def fail_inside_the_server() -> None:
        async with client.with_server(
            image_tag="registry.test/env:latest", close_action=ServerCloseAction(close_action)
        ):
            raise ValueError("body failed")

    with pytest.raises(ValueError, match="body failed"):
        await fail_inside_the_server()

    cleanup = client.stop_server if close_action == "STOP" else client.finish_server
    cleanup.assert_awaited_once()


async def test_with_server_raises_a_cleanup_failure_after_a_successful_body(mocker) -> None:
    from idegym.client.client import ServerCloseAction

    client = _client_with_server(mocker, http_error("delete failed", status_code=500))

    with pytest.raises(IdeGYMServerError, match="delete failed"):
        async with client.with_server(image_tag="registry.test/env:latest", close_action=ServerCloseAction.STOP):
            pass


async def test_with_server_cleans_up_after_a_successful_body(mocker) -> None:
    client = _client_with_server(mocker, None)

    async with client.with_server(image_tag="registry.test/env:latest") as server:
        assert server.server_id == 7

    client.finish_server.assert_awaited_once_with(server)


# --------------------------------------------------------------------------------------
# Quota exhaustion and registration failures
# --------------------------------------------------------------------------------------


def _start_server_operations(mocker, terminal_results):
    from idegym.client.operations.servers import ServerOperations

    client_id = uuid4()
    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda value: value
    utils.validate_namespace.side_effect = lambda namespace: namespace or "idegym"
    utils.make_request = mocker.AsyncMock(
        return_value={"namespace": "idegym", "client_id": str(client_id), "operation_id": 3}
    )
    utils.parse_response.side_effect = lambda response_raw, model_class: model_class.model_validate(response_raw)
    utils.wait_for_async_operation_to_end = mocker.AsyncMock(side_effect=terminal_results)
    return ServerOperations(utils=utils, project=mocker.MagicMock()), client_id


async def test_start_server_raises_busy_when_the_quota_stays_exhausted(mocker) -> None:
    sleep = mocker.patch("idegym.client.operations.servers.sleep", new=mocker.AsyncMock())
    quota = ErrorResponse(status_code=429, body="quota exhausted")
    operations, client_id = _start_server_operations(mocker, [quota])

    with pytest.raises(IdeGYMBusyError) as caught:
        await operations.start_server(
            image_tag="registry.test/env:latest",
            client_id=client_id,
            server_start_wait_timeout_in_seconds=10,
            # Longer than the client's whole deadline (the timeout plus its grace period).
            retry_delay_in_seconds=100,
        )

    assert (caught.value.status_code, caught.value.body) == (429, "quota exhausted")
    assert "still rate-limited after 1 attempt(s)" in str(caught.value)
    sleep.assert_not_awaited()


async def test_start_server_retries_a_429_while_the_wait_allows(mocker) -> None:
    from idegym.api.orchestrator.servers import StartServerResponse

    mocker.patch("idegym.client.operations.servers.sleep", new=mocker.AsyncMock())
    started = StartServerResponse(namespace="idegym", client_id=uuid4(), server_id=5)
    operations, client_id = _start_server_operations(mocker, [ErrorResponse(status_code=429, body="quota"), started])

    response = await operations.start_server(
        image_tag="registry.test/env:latest", client_id=client_id, retry_delay_in_seconds=1
    )

    assert response is started


async def test_a_failed_registration_raises_the_typed_error(mocker) -> None:
    from idegym.client.client import IdeGYMClient

    client = IdeGYMClient.__new__(IdeGYMClient)
    client._http_client = mocker.MagicMock(is_closed=False)
    client._owns_http_client = False
    client._heartbeat_task = None
    client._utils = mocker.MagicMock(current_namespace="idegym")
    client.name, client.nodes_count = "run", 0
    client._register_client = mocker.AsyncMock(return_value=ErrorResponse(status_code=403, body="namespace denied"))

    with pytest.raises(IdeGYMAuthError) as caught:
        await client.__aenter__()

    assert (caught.value.status_code, caught.value.body) == (403, "namespace denied")
    assert "Failed to register client" in str(caught.value)


async def test_snapshot_server_raises_instead_of_returning_the_failure(mocker) -> None:
    operations = _server_operations(mocker, ErrorResponse(status_code=500, body="snapshot failed"))
    operations._utils.make_request.return_value = {"server_id": 7, "server_name": "srv", "operation_id": 3}

    with pytest.raises(IdeGYMServerError, match="Snapshotting server 7 failed"):
        await operations.snapshot_server(server_id=7, client_id=uuid4())


async def test_stop_client_raises_instead_of_returning_the_failure(mocker) -> None:
    from idegym.client.operations.clients import ClientOperations

    utils = mocker.MagicMock()
    utils.validate_client_id.side_effect = lambda client_id: client_id
    utils.validate_namespace.side_effect = lambda namespace: namespace or "idegym"
    utils.make_request = mocker.AsyncMock(return_value={"operation_id": 3})
    utils.parse_response.side_effect = lambda response_raw, model_class: model_class.model_validate(response_raw)
    utils.wait_for_async_operation_to_end = mocker.AsyncMock(
        return_value=ErrorResponse(status_code=500, body="could not delete pods")
    )
    client_id = uuid4()

    with pytest.raises(IdeGYMServerError, match=f"Stopping client {client_id} failed"):
        await ClientOperations(utils=utils).stop_client(client_id=client_id)


def _registered_client(mocker, stop_error: Optional[Exception]):
    from idegym.client.client import IdeGYMClient

    client = IdeGYMClient.__new__(IdeGYMClient)
    client._heartbeat_task = None
    client._http_client = mocker.MagicMock(aclose=mocker.AsyncMock())
    client._owns_http_client = True
    client._otel_config = mocker.MagicMock()
    client._utils = mocker.MagicMock(current_client_id=uuid4())
    client._stop_client = mocker.AsyncMock(side_effect=stop_error)
    mocker.patch("idegym.client.client.uninstrument")
    return client


async def test_a_failed_deregistration_is_raised_when_nothing_else_is(mocker) -> None:
    client = _registered_client(mocker, http_error("could not delete pods", status_code=500))

    with pytest.raises(IdeGYMServerError, match="could not delete pods"):
        await client.__aexit__(None, None, None)

    client._http_client.aclose.assert_awaited_once()


async def test_a_failed_deregistration_does_not_mask_the_body_exception(mocker) -> None:
    client = _registered_client(mocker, http_error("could not delete pods", status_code=500))
    body_error = ValueError("body failed")

    # __aexit__ returning normally lets `async with` re-raise the body's exception.
    assert not await client.__aexit__(ValueError, body_error, None)

    client._stop_client.assert_awaited_once()
    client._http_client.aclose.assert_awaited_once()
