"""A terminal in a server pod, relayed from the browser through the orchestrator.

This is ``kubectl exec -it`` without kubectl: the orchestrator opens the same Kubernetes exec
stream kubectl does, with its own service account, and copies bytes between it and the browser's
WebSocket. The exec stream multiplexes channels over one WebSocket, each binary frame starting with
a channel byte (``v4.channel.k8s.io``); the browser side speaks a simpler protocol of its own, JSON
text frames in and raw output bytes out, so the page never has to know Kubernetes' framing.

Only IdeGYM server pods are reachable (see :func:`is_sandbox`): a shell in the orchestrator's own
pod, or in PostgreSQL's, would hand out the database credentials.
"""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, NamedTuple, Optional

from aiohttp import WSMsgType
from fastapi import WebSocket, WebSocketDisconnect
from kubernetes_asyncio.client import CoreV1Api
from kubernetes_asyncio.client.configuration import Configuration
from kubernetes_asyncio.stream import WsApiClient

STDIN_CHANNEL = 0
STDOUT_CHANNEL = 1
STDERR_CHANNEL = 2
ERROR_CHANNEL = 3
RESIZE_CHANNEL = 4

# Prefer bash when the image has it; ``exec`` keeps the shell as the process Kubernetes watches, so
# the stream ends when the person types ``exit``.
SHELL_COMMAND = [
    "/bin/sh",
    "-c",
    "export TERM=xterm-256color; if command -v bash >/dev/null 2>&1; then exec bash; fi; exec sh",
]

# A forgotten tab should not keep a shell (and an orchestrator connection) open indefinitely.
IDLE_TIMEOUT_SECONDS = 30 * 60

_SANDBOX_LABELS = {"app.kubernetes.io/part-of": "idegym", "app.kubernetes.io/component": "sandbox"}


def is_sandbox(pod: Any) -> bool:
    """Whether ``pod`` belongs to an IdeGYM server Deployment, the only pods a shell may open in."""
    labels = pod.metadata.labels or {}
    return all(labels.get(key) == value for key, value in _SANDBOX_LABELS.items())


def _status(payload: bytes) -> dict[str, Any]:
    """The ``Status`` object Kubernetes sends on the error channel when a command ends, or nothing."""
    try:
        parsed = json.loads(payload)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def exit_code(status: bytes) -> Optional[int]:
    """The command's exit code, when the ``Status`` carries one."""
    payload = _status(status)
    if payload.get("status") == "Success":
        return 0
    for cause in (payload.get("details") or {}).get("causes") or []:
        if cause.get("reason") == "ExitCode":
            try:
                return int(cause.get("message"))
            except (TypeError, ValueError):
                return None
    return None


def failure_message(status: bytes) -> Optional[str]:
    """Why the command never ran, when the ``Status`` is a failure without an exit code.

    That is what an image without ``/bin/sh``, or a container that just went away, produces: an
    ``executable file not found`` or ``container not found`` message and no exit code at all.
    """
    payload = _status(status)
    if payload.get("status") == "Failure" and exit_code(status) is None:
        return payload.get("message") or None
    return None


@asynccontextmanager
async def exec_stream(namespace: str, pod: str, container: str) -> AsyncIterator[Any]:
    """Open an interactive shell in ``container`` and yield the raw exec WebSocket."""
    async with WsApiClient(configuration=Configuration.get_default_copy(), heartbeat=30) as api:
        connection = await CoreV1Api(api_client=api).connect_get_namespaced_pod_exec(
            name=pod,
            namespace=namespace,
            container=container,
            command=SHELL_COMMAND,
            stdin=True,
            stdout=True,
            stderr=True,
            tty=True,
            _preload_content=False,
        )
        async with connection as stream:
            yield stream


class ShellOutcome(NamedTuple):
    """Why a session ended: ``exited``, ``idle``, ``browser_closed``, ``pod_closed``, or ``disconnected``."""

    reason: str
    exit_code: Optional[int] = None


async def relay(browser: WebSocket, stream: Any, idle_timeout: float = IDLE_TIMEOUT_SECONDS) -> ShellOutcome:
    """Copy keystrokes and resizes to the pod and its output back until either side ends."""
    loop = asyncio.get_running_loop()
    last_input = loop.time()
    code: list[Optional[int]] = []

    async def from_browser() -> ShellOutcome:
        nonlocal last_input
        while True:
            message = await browser.receive()
            if message["type"] == "websocket.disconnect":
                return ShellOutcome("browser_closed")
            try:
                payload = json.loads(message.get("text") or "{}")
            except ValueError:
                continue
            if payload.get("type") == "input" and isinstance(payload.get("data"), str):
                last_input = loop.time()
                await stream.send_bytes(bytes([STDIN_CHANNEL]) + payload["data"].encode())
            elif payload.get("type") == "resize":
                size = {"Width": int(payload.get("cols") or 80), "Height": int(payload.get("rows") or 24)}
                await stream.send_bytes(bytes([RESIZE_CHANNEL]) + json.dumps(size).encode())

    async def from_pod() -> ShellOutcome:
        async for message in stream:
            if message.type not in (WSMsgType.BINARY, WSMsgType.TEXT):
                break
            data = message.data if isinstance(message.data, bytes) else message.data.encode()
            if not data:
                continue
            channel, payload = data[0], data[1:]
            if channel in (STDOUT_CHANNEL, STDERR_CHANNEL) and payload:
                await browser.send_bytes(payload)
            elif channel == ERROR_CHANNEL:
                code.append(exit_code(payload))
                if code[-1] is None and (message := failure_message(payload)):
                    await browser.send_json({"type": "error", "message": message})
        return ShellOutcome("exited", code[-1]) if code else ShellOutcome("pod_closed")

    async def idle() -> ShellOutcome:
        while loop.time() - last_input < idle_timeout:
            await asyncio.sleep(min(30.0, idle_timeout))
        return ShellOutcome("idle")

    browser_side, pod_side, idle_side = tasks = [asyncio.create_task(side()) for side in (from_browser, from_pod, idle)]
    try:
        finished, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # Prefer a side that ended cleanly. A side that failed did so because a peer went away mid-send
    # (the browser tab closed, the exec connection dropped), which is simply how the session ended.
    for task in (pod_side, browser_side, idle_side):
        if task in finished and task.exception() is None:
            return task.result()
    error = next(task for task in (pod_side, browser_side, idle_side) if task in finished).exception()
    if isinstance(error, (WebSocketDisconnect, RuntimeError, ConnectionError)):
        return ShellOutcome("disconnected")
    raise error
