from types import SimpleNamespace
from uuid import uuid4

from idegym.orchestrator.router.forwarding import forward_request_by_server_id
from starlette.datastructures import Headers
from starlette.requests import Request


async def test_forward_request_by_server_id_passes_request_headers_to_forwarding(mocker):
    client_id = uuid4()
    http_client = object()
    endpoint = mocker.patch(
        "idegym.orchestrator.router.forwarding.forward_request_to_server",
        return_value={"async_operation_id": 44},
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/forward/client/server/api/tools",
            "headers": [(b"x-test-header", b"value")],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "app": SimpleNamespace(state=SimpleNamespace(http_client=http_client)),
        }
    )

    result = await forward_request_by_server_id(request, client_id=client_id, server_id=7, path="api/tools")

    endpoint.assert_awaited_once()
    assert endpoint.await_args.kwargs["headers"] is request.headers
    assert isinstance(endpoint.await_args.kwargs["headers"], Headers)
    assert endpoint.await_args.kwargs["http_client"] is http_client
    assert result == {"async_operation_id": 44}
