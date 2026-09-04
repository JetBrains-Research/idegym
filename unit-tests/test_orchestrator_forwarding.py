import asyncio
from types import SimpleNamespace
from uuid import uuid4

from fastapi import Response
from idegym.api.orchestrator.operations import ForwardRequestResponse
from idegym.orchestrator.router import forwarding
from idegym.orchestrator.router.forwarding import (
    _MAX_FORWARD_WAIT_SECONDS,
    _parse_wait_seconds,
    forward_request_by_server_id,
    forward_request_to_server,
)
from starlette.datastructures import Headers
from starlette.requests import Request


def _http_request(query_string: bytes = b"", *, http_client: object | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/forward/client/server/api/tools",
            "query_string": query_string,
            "headers": [(b"x-test-header", b"value")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "app": SimpleNamespace(state=SimpleNamespace(http_client=http_client)),
        }
    )


async def test_forward_request_by_server_id_passes_request_headers_to_forwarding(mocker):
    client_id = uuid4()
    http_client = object()
    endpoint = mocker.patch(
        "idegym.orchestrator.router.forwarding.forward_request_to_server",
        return_value={"async_operation_id": 44},
    )
    request = _http_request(http_client=http_client)
    response = Response()

    result = await forward_request_by_server_id(request, response, client_id=client_id, server_id=7, path="api/tools")

    endpoint.assert_awaited_once()
    assert endpoint.await_args.kwargs["headers"] is request.headers
    assert isinstance(endpoint.await_args.kwargs["headers"], Headers)
    assert endpoint.await_args.kwargs["http_client"] is http_client
    # No ?wait_seconds on this request → the fast path is off (0.0), and the
    # injected Response is threaded through so the handler can flip 202 → 200.
    assert endpoint.await_args.kwargs["wait_seconds"] == 0.0
    assert endpoint.await_args.kwargs["response"] is response
    assert result == {"async_operation_id": 44}


def test_parse_wait_seconds_variants():
    def req(qs: bytes) -> Request:
        return Request({"type": "http", "method": "POST", "path": "/", "query_string": qs, "headers": []})

    assert _parse_wait_seconds(req(b"")) == 0.0  # absent
    assert _parse_wait_seconds(req(b"wait_seconds=25")) == 25.0
    assert _parse_wait_seconds(req(b"wait_seconds=0")) == 0.0  # non-positive → off
    assert _parse_wait_seconds(req(b"wait_seconds=-3")) == 0.0
    assert _parse_wait_seconds(req(b"wait_seconds=abc")) == 0.0  # unparseable → off
    assert _parse_wait_seconds(req(b"wait_seconds=99999")) == _MAX_FORWARD_WAIT_SECONDS  # clamped


def _patch_forward_deps(mocker):
    """validate_server + create_async_operation stubbed to async no-ops."""

    async def fake_validate_server(**_):
        return SimpleNamespace(generated_name="srv", namespace="ns", service_port=80)

    async def fake_create_async_operation(**_):
        return 99

    mocker.patch.object(forwarding, "validate_server", new=fake_validate_server)
    mocker.patch.object(forwarding, "create_async_operation", new=fake_create_async_operation)


async def test_forward_blocking_returns_inline_result_and_sets_200(mocker):
    """With wait_seconds > 0 and a fast task, the handler returns the inline
    ForwardRequestResponse and flips the response status to 200."""
    _patch_forward_deps(mocker)
    inline = ForwardRequestResponse(status_code=200, headers={}, body='{"exit_code": 0}')

    async def fake_task(**_):
        return inline

    mocker.patch.object(forwarding, "_task_forward_request", new=fake_task)

    response = Response()
    result = await forward_request_to_server(
        client_id=uuid4(),
        server_id=7,
        path="api/tools/idegym_bash_only/bash",
        method="POST",
        headers=Headers({}),
        body="{}",
        http_client=object(),
        wait_seconds=25.0,
        response=response,
    )
    assert result is inline
    assert response.status_code == 200


async def test_forward_blocking_falls_back_to_ticket_when_slow(mocker):
    """A tool that outruns the blocking window yields the 202-ticket shape; the
    detached task keeps running (never cancelled) to write its DB row."""
    _patch_forward_deps(mocker)
    released = asyncio.Event()
    ran_to_completion = asyncio.Event()

    async def slow_task(**_):
        await released.wait()
        ran_to_completion.set()
        return ForwardRequestResponse(status_code=200, headers={}, body="{}")

    mocker.patch.object(forwarding, "_task_forward_request", new=slow_task)

    response = Response()
    result = await forward_request_to_server(
        client_id=uuid4(),
        server_id=7,
        path="api/tools/idegym_bash_only/bash",
        method="POST",
        headers=Headers({}),
        body="{}",
        http_client=object(),
        wait_seconds=0.05,  # far shorter than the (blocked) task
        response=response,
    )
    # outran the window → ticket, not the inline result
    assert result.async_operation_id == 99
    assert result.status_code is None

    # The task must NOT have been cancelled by the timeout — release it and
    # confirm it completes (so the DB row the poll fallback reads still lands).
    released.set()
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
    assert ran_to_completion.is_set()


async def test_forward_no_wait_seconds_is_fire_and_forget_ticket(mocker):
    """wait_seconds <= 0 preserves the original behavior: return the 202 ticket
    immediately without awaiting the task."""
    _patch_forward_deps(mocker)
    started = asyncio.Event()

    async def bg_task(**_):
        started.set()
        return ForwardRequestResponse(status_code=200, headers={}, body="{}")

    mocker.patch.object(forwarding, "_task_forward_request", new=bg_task)

    result = await forward_request_to_server(
        client_id=uuid4(),
        server_id=7,
        path="api/tools/idegym_bash_only/bash",
        method="POST",
        headers=Headers({}),
        body="{}",
        http_client=object(),
        wait_seconds=0.0,
    )
    assert result.async_operation_id == 99
    assert result.status_code is None
    # drain the detached background task
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    await asyncio.gather(*pending, return_exceptions=True)
