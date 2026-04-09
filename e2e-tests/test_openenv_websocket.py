import asyncio
from urllib.parse import urlsplit, urlunsplit

import pytest
import websockets
from idegym.api.orchestrator.servers import ServerKind, ServerReuseStrategy
from idegym.client.client import ServerCloseAction
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.idegym_utils import create_http_client
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK
from websockets.protocol import State


def _ws_url_from_openenv_url(openenv_url: str) -> str:
    parsed_url = urlsplit(openenv_url.rstrip("/"))
    assert parsed_url.scheme in {"http", "https"}
    ws_scheme = "wss" if parsed_url.scheme == "https" else "ws"
    return urlunsplit((ws_scheme, parsed_url.netloc, f"{parsed_url.path}/ws", "", ""))


@pytest.mark.asyncio
async def test_openenv_websocket_forwarding_commands(websocket_test_image, test_id):
    """
    Start an OpenEnv-like server, connect via websocket forwarding endpoint,
    send several commands, validate responses, and stop server on exit.
    """
    async with create_http_client(
        name=f"openenv-ws-{test_id}",
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=websocket_test_image,
            server_name=f"openenv-ws-{test_id}",
            runtime_class_name="gvisor",
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.STOP,
            server_kind=ServerKind.OPENENV,
        ) as server:
            ws_url = _ws_url_from_openenv_url(server.openenv_url)

            async with websockets.connect(ws_url) as socket:
                await socket.send("status")
                assert await socket.recv() == "ready"

                await socket.send("ping")
                assert await socket.recv() == "pong"

                await socket.send("echo hello-openenv")
                assert await socket.recv() == "hello-openenv"

                await socket.send("add 7 35")
                assert await socket.recv() == "42"

                await socket.send("close")
                assert await socket.recv() == "bye"


@pytest.mark.asyncio
async def test_openenv_websocket_error_processing_and_closure(websocket_test_image, test_id):
    """
    Validate OpenEnv websocket error responses and graceful close propagation.
    """
    async with create_http_client(
        name=f"openenv-ws-errors-{test_id}",
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=websocket_test_image,
            server_name=f"openenv-ws-errors-{test_id}",
            runtime_class_name="gvisor",
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.STOP,
            server_kind=ServerKind.OPENENV,
        ) as server:
            ws_url = _ws_url_from_openenv_url(server.openenv_url)

            async with websockets.connect(ws_url) as socket:
                await socket.send("add 1 nope")
                assert await socket.recv() == "error: add requires two integers"

                await socket.send("add 1")
                assert await socket.recv() == "error: bad add syntax"

                await socket.send("unsupported")
                assert await socket.recv() == "error: unknown command"

                await socket.send("ping")
                assert await socket.recv() == "pong"

                await socket.send("close")
                assert await socket.recv() == "bye"

                with pytest.raises(ConnectionClosedOK):
                    await asyncio.wait_for(socket.recv(), timeout=15)


@pytest.mark.asyncio
async def test_openenv_websocket_non_graceful_server_closure(websocket_test_image, test_id):
    """
    Simulate a non-graceful server-side closure and verify the websocket ends up closed.
    """
    async with create_http_client(
        name=f"openenv-ws-server-abort-{test_id}",
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=websocket_test_image,
            server_name=f"openenv-ws-server-abort-{test_id}",
            runtime_class_name="gvisor",
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.STOP,
            server_kind=ServerKind.OPENENV,
        ) as server:
            ws_url = _ws_url_from_openenv_url(server.openenv_url)

            async with websockets.connect(ws_url) as socket:
                await socket.send("status")
                assert await socket.recv() == "ready"

                await socket.send("crash")
                with pytest.raises(ConnectionClosed):
                    await asyncio.wait_for(socket.recv(), timeout=15)

                await asyncio.wait_for(socket.wait_closed(), timeout=15)
                assert socket.state == State.CLOSED
                assert socket.close_code is not None


@pytest.mark.asyncio
async def test_openenv_websocket_non_graceful_client_closure(websocket_test_image, test_id):
    """
    Simulate a non-graceful client-side closure (transport abort) and verify closure state.
    """
    async with create_http_client(
        name=f"openenv-ws-client-abort-{test_id}",
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=websocket_test_image,
            server_name=f"openenv-ws-client-abort-{test_id}",
            runtime_class_name="gvisor",
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.STOP,
            server_kind=ServerKind.OPENENV,
        ) as server:
            ws_url = _ws_url_from_openenv_url(server.openenv_url)

            async with websockets.connect(ws_url) as socket:
                await socket.send("ping")
                assert await socket.recv() == "pong"

                assert socket.transport is not None
                socket.transport.abort()

                await asyncio.wait_for(socket.wait_closed(), timeout=15)
                assert socket.state == State.CLOSED

                with pytest.raises(ConnectionClosed):
                    await socket.send("ping")
