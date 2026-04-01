"""E2E test for OpenEnv websocket forwarding through orchestrator."""

from urllib.parse import urlsplit, urlunsplit

import pytest
import websockets
from idegym.api.orchestrator.servers import ServerKind, ServerReuseStrategy
from idegym.client.client import ServerCloseAction
from utils.idegym_utils import create_http_client


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
            server_start_wait_timeout_in_seconds=600,
            reuse_strategy=ServerReuseStrategy.NONE,
            close_action=ServerCloseAction.STOP,
            server_kind=ServerKind.OPENENV,
        ) as server:
            openenv_url = server.openenv_url.rstrip("/")
            parsed_url = urlsplit(openenv_url)
            assert parsed_url.scheme in {"http", "https"}
            ws_scheme = "wss" if parsed_url.scheme == "https" else "ws"
            ws_url = urlunsplit((ws_scheme, parsed_url.netloc, f"{parsed_url.path}/ws", "", ""))

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
