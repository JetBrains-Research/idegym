import asyncio
from asyncio import CancelledError
from os import environ as env
from typing import Optional
from uuid import UUID

import websockets
from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect, status
from httpx import AsyncClient, ConnectError
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import (
    AsyncOperationStatus,
    AsyncOperationType,
    ForwardRequestPayload,
    ForwardRequestResponse,
)
from idegym.orchestrator.database.helpers import (
    create_async_operation,
    update_operation_status,
    update_operation_with_error,
    update_server_status,
    validate_server,
)
from idegym.orchestrator.util.decorators import handle_general_exceptions, handle_websocket_exceptions
from idegym.orchestrator.util.errors import format_error
from idegym.utils.decorators import executes_operation_in_background
from idegym.utils.logging import get_logger
from starlette.websockets import WebSocketState
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State

router = APIRouter()
logger = get_logger(__name__)


@executes_operation_in_background
@router.api_route(
    "/api/forward/{client_id}/{server_id}/{path:path}",
    methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
    status_code=status.HTTP_202_ACCEPTED,
)
@handle_general_exceptions(error_message="Failed to forward request to IdeGYM server")
async def forward_request_by_server_id(request: Request, client_id: UUID, server_id: int, path: str):
    logger.info(
        f"Received forwarding request: {request.method} {request.url} "
        f"to IdeGYM server ID {server_id} for client {client_id} with path: {path}"
    )
    server = await validate_server(client_id=client_id, server_id=server_id)
    request_content = await parse_request_body(request)
    forward_payload = construct_forwarding_payload(
        path=path,
        request=request,
        request_content=request_content,
        generated_name=server.generated_name,
        namespace=server.namespace,
        server_id=server_id,
        service_port=server.service_port,
    )
    async_operation_id = await create_async_operation(
        async_operation_type=AsyncOperationType.FORWARD_REQUEST,
        client_id=client_id,
        server_id=server_id,
        request=forward_payload,
    )
    asyncio.create_task(
        _task_forward_request(
            http_client=request.app.state.http_client,
            forward_payload=forward_payload,
            async_operation_id=async_operation_id,
        )
    )
    return ForwardRequestResponse(async_operation_id=async_operation_id)


def construct_forwarding_payload(
    path: str,
    request: Request,
    request_content: str,
    generated_name: str,
    namespace: Optional[str],
    server_id: int,
    service_port: int = 80,
):
    target_url = f"http://{_build_server_host(generated_name, namespace)}:{service_port}/{path}"
    headers = request.headers.mutablecopy()
    del headers["Host"]
    del headers["Authorization"]
    return ForwardRequestPayload(
        method=request.method,
        path=path,
        headers=dict(headers),
        body=request_content,
        target_url=target_url,
        server_id=server_id,
    )


async def parse_request_body(request: Request) -> str:
    if request.method not in ["POST", "PUT", "PATCH"]:
        return ""

    body = await request.body()
    logger.info(f"Request body size: {len(body)} bytes")

    try:
        return body.decode("utf-8")
    except UnicodeDecodeError:
        return str(body)


async def _task_forward_request(
    http_client: AsyncClient, forward_payload: ForwardRequestPayload, async_operation_id: int
):
    need_to_update_server_heartbeat = False
    try:
        await update_operation_status(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.IN_PROGRESS,
            orchestrator_pod=env.get("__POD_NAME"),
        )
        status_code, headers, response_text = await forward_request_internally(
            forward_payload=forward_payload, http_client=http_client
        )

        if status_code >= 400:
            if status_code < 500:  # 4xx means the server processed the request; update heartbeat
                need_to_update_server_heartbeat = True

            await update_operation_with_error(
                async_operation_id=async_operation_id,
                status_code=status_code,
                body=response_text,
            )
        else:
            need_to_update_server_heartbeat = True
            await update_operation_status(
                async_operation_id=async_operation_id,
                async_operation_status=AsyncOperationStatus.SUCCEEDED,
                result=ForwardRequestResponse(status_code=status_code, headers=headers, body=response_text),
            )

    except ConnectError as ce:
        message = f"Failed to forward request: unable to connect to {forward_payload.target_url}"
        logger.warning(message)
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            status_code=status.HTTP_410_GONE,
            body=format_error(message=message, exception=ce),
        )

    except CancelledError:
        message = f"Failed to forward request: client disconnected while streaming from {forward_payload.target_url}"
        logger.warning(message)
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.CANCELLED,
            status_code=499,  # non-standard: client closed the connection
            body=message,
        )

    except Exception as e:
        message = f"Failed to forward request to {forward_payload.target_url}"
        logger.exception(message)
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            body=format_error(message=message, exception=e),
        )

    if need_to_update_server_heartbeat:
        await update_server_heartbeat_on_call(forward_payload.path, forward_payload.server_id)


async def forward_request_internally(
    forward_payload: ForwardRequestPayload, http_client: AsyncClient
) -> tuple[int, dict[str, str], str]:
    logger.info(f"Forwarding to: {forward_payload.target_url}")

    async with http_client.stream(
        method=forward_payload.method,
        url=forward_payload.target_url,
        headers=forward_payload.headers,
        content=forward_payload.body,
    ) as response:
        logger.info(f"Received response from {forward_payload.target_url} with status code: {response.status_code}")
        response_content = await response.aread()
        response_text = response_content.decode(encoding="utf-8", errors="replace")

    return response.status_code, dict(response.headers), response_text


async def update_server_heartbeat_on_call(path: str, server_id: int):
    if path.startswith("api/tools") or path.startswith("api/rewards"):
        logger.info(f"Updating heartbeat for server ID {server_id} on tool or reward call")
        await update_server_status(server_id=server_id, availability_status=AvailabilityStatus.ALIVE)


@handle_websocket_exceptions(error_message="Failed to forward WebSocket to IdeGYM server")
@router.websocket("/api/ws-forward/{client_id}/{server_id}/ws")
async def forward_websocket(websocket: WebSocket, client_id: UUID, server_id: int):
    logger.info(f"Received WebSocket forwarding request for server ID {server_id} for client {client_id}")
    server = await validate_server(client_id=client_id, server_id=server_id)
    target_host = _build_server_host(server.generated_name, server.namespace)
    target_url = f"ws://{target_host}:{server.service_port}/ws"
    await websocket.accept()
    await update_server_status(server_id=server_id, availability_status=AvailabilityStatus.ALIVE)

    async with websockets.connect(target_url) as upstream:

        async def relay_client_to_upstream():
            try:
                async for message in websocket.iter_text():
                    await upstream.send(message)
            except (WebSocketDisconnect, ConnectionClosed):
                pass
            finally:
                if upstream.state != State.CLOSED:
                    await upstream.close()

        async def relay_upstream_to_client():
            try:
                async for message in upstream:
                    await websocket.send_text(message if isinstance(message, str) else message.decode())
                    await update_server_status(server_id=server_id, availability_status=AvailabilityStatus.ALIVE)
            except (WebSocketDisconnect, ConnectionClosed):
                pass

        client_task = asyncio.create_task(relay_client_to_upstream())
        upstream_task = asyncio.create_task(relay_upstream_to_client())
        done, pending = await asyncio.wait(
            [client_task, upstream_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        for task in done:
            if task.cancelled():
                continue
            exception = task.exception()
            if exception and not isinstance(exception, ConnectionClosed):
                raise exception

    # Explicitly close the downstream WebSocket after the relay loop ends.
    # Uvicorn does not send a close frame automatically when the handler returns,
    # so without this the client would hang indefinitely instead of receiving
    # the connection-closed notification.
    #
    # We only send a close frame when the client is still connected
    # (client_state != DISCONNECTED). If the client disconnected first,
    # client_state is already DISCONNECTED and trying to send would produce
    # spurious errors. application_state guards against an accidental double-send.
    if (
        websocket.client_state != WebSocketState.DISCONNECTED
        and websocket.application_state != WebSocketState.DISCONNECTED
    ):
        await websocket.close(code=1000)


def _build_server_host(generated_name: str, namespace: Optional[str]) -> str:
    """
    Build a DNS name for a server Service in Kubernetes.
    Using namespace-qualified service names avoids cross-namespace lookup failures.
    """
    if namespace:
        return f"{generated_name}.{namespace}.svc"
    return generated_name
