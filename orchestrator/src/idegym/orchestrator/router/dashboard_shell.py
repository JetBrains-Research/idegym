"""The dashboard's pod shell: a terminal page and the WebSocket that relays it to the pod.

Off unless ``IDEGYM_DASHBOARD_SHELL_ENABLED`` is set. A browser opens a WebSocket to any origin it
likes and sends the proxy's login cookie along, so the handshake's ``Origin`` must name the
dashboard's own host, exactly as for the POST actions; and the pod must be an IdeGYM server pod,
checked here rather than trusted from the page. Every session is logged with the user the proxy
reports, when it opened and why it ended.
"""

from contextlib import suppress
from time import monotonic
from typing import Any, Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi import status as http_status
from fastapi.responses import HTMLResponse
from idegym.api.config import Config
from idegym.backend.utils.kubernetes_client import async_kube_api
from idegym.orchestrator.pod_shell import IDLE_TIMEOUT_SECONDS, exec_stream, is_sandbox, relay
from idegym.orchestrator.router.dashboard import api_error_message, not_found, pod_view, render
from idegym.orchestrator.router.dashboard_actions import acting_user, same_origin
from idegym.orchestrator.util.decorators import render_dashboard_error
from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import ApiException
from starlette.requests import HTTPConnection

router = APIRouter(prefix="/dashboard")
logger = get_logger(__name__)

# WebSocket close code for "the request broke a policy": refused origin, pod, or disabled feature.
POLICY_VIOLATION = 1008


def shell_enabled(connection: HTTPConnection) -> bool:
    config: Config = connection.app.state.config
    return config.orchestrator.dashboard.shell_enabled


async def _read_pod(namespace: str, pod_name: str) -> Any:
    async with async_kube_api() as (_, _, core, _, _):
        return await core.read_namespaced_pod(name=pod_name, namespace=namespace)


def _pick_container(pod: Any, container: Optional[str]) -> Optional[str]:
    names = [item.name for item in pod.spec.containers or []]
    if container is None:
        return names[0] if names else None
    return container if container in names else None


@router.get("/pods/{namespace}/{pod_name}/shell", response_class=HTMLResponse)
@render_dashboard_error("Failed to open the shell", back_url="/dashboard/pods")
async def shell_page(request: Request, namespace: str, pod_name: str, container: Optional[str] = None):
    if not shell_enabled(request):
        return render(
            request,
            "error.html",
            active="pods",
            status_code=http_status.HTTP_404_NOT_FOUND,
            message="The pod shell is disabled",
            details="Set IDEGYM_DASHBOARD_SHELL_ENABLED (dashboard.shell.enabled in the chart) to enable it.",
            back_url=f"/dashboard/pods/{namespace}/{pod_name}",
        )
    try:
        pod = await _read_pod(namespace, pod_name)
    except ApiException as error:
        if error.status == http_status.HTTP_404_NOT_FOUND:
            return not_found(request, f"Pod {namespace}/{pod_name} does not exist", "/dashboard/pods", "pods")
        raise
    if not is_sandbox(pod):
        return render(
            request,
            "error.html",
            active="pods",
            status_code=http_status.HTTP_403_FORBIDDEN,
            message="A shell can only be opened in an IdeGYM server pod",
            back_url=f"/dashboard/pods/{namespace}/{pod_name}",
        )
    selected = _pick_container(pod, container) or _pick_container(pod, None)
    return render(
        request,
        "shell.html",
        active="pods",
        pod=pod_view(pod),
        containers=[item.name for item in pod.spec.containers or []],
        container=selected,
        idle_minutes=IDLE_TIMEOUT_SECONDS // 60,
    )


async def _check(
    websocket: WebSocket, namespace: str, pod_name: str, container: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """Why no shell may open (or ``None``), and the container it would open in."""
    if not shell_enabled(websocket):
        return "The pod shell is disabled on this orchestrator.", None
    if not same_origin(websocket):
        return "The shell must be opened from the dashboard itself.", None
    try:
        pod = await _read_pod(namespace, pod_name)
    except ApiException as error:
        return f"Cannot read the pod: {api_error_message(error)}", None
    if not is_sandbox(pod):
        return "A shell can only be opened in an IdeGYM server pod.", None
    if pod.status.phase != "Running":
        return f"The pod is {pod.status.phase}, not Running.", None
    selected = _pick_container(pod, container)
    if selected is None:
        return f"The pod has no container named {container}.", None
    return None, selected


@router.websocket("/pods/{namespace}/{pod_name}/exec")
async def pod_exec(websocket: WebSocket, namespace: str, pod_name: str, container: Optional[str] = None):
    """Relay a terminal between the browser and a shell in the pod until either side ends."""
    await websocket.accept()
    problem, container = await _check(websocket, namespace, pod_name, container)
    if problem:
        await websocket.send_json({"type": "error", "message": problem})
        await websocket.close(code=POLICY_VIOLATION)
        return

    session = {"user": acting_user(websocket), "namespace": namespace, "pod": pod_name, "container": container}
    logger.info("Dashboard shell opened", **session)
    started = monotonic()
    outcome = None
    try:
        async with exec_stream(namespace, pod_name, container) as stream:
            await _send_quietly(websocket, {"type": "status", "message": f"Connected to {pod_name} ({container})"})
            outcome = await relay(websocket, stream)
    except Exception as error:
        logger.exception("Dashboard shell failed", **session)
        await _send_quietly(websocket, {"type": "error", "message": f"{type(error).__name__}: {error}"})
    finally:
        logger.info(
            "Dashboard shell closed",
            reason=outcome.reason if outcome else "failed",
            exit_code=outcome.exit_code if outcome else None,
            seconds=round(monotonic() - started, 1),
            **session,
        )

    if outcome is not None:
        await _send_quietly(websocket, {"type": "exit", "reason": outcome.reason, "code": outcome.exit_code})
    with suppress(RuntimeError, WebSocketDisconnect):  # the browser already closed its side
        await websocket.close()


async def _send_quietly(websocket: WebSocket, message: dict[str, Any]) -> None:
    """Tell the browser something if it is still listening; a closed tab is not an error.

    Starlette raises ``WebSocketDisconnect`` when the browser has gone mid-send and ``RuntimeError``
    once a close has been sent; neither is worth a traceback here.
    """
    with suppress(RuntimeError, ConnectionError, WebSocketDisconnect):
        await websocket.send_json(message)
