from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.backend.utils.kubernetes_client import clean_up_server, list_pods
from idegym.backend.utils.utils import log_exceptions
from idegym.orchestrator.database.database import (
    get_idegym_servers_by_status,
    update_idegym_server_heartbeat,
)
from idegym.utils.logging import get_logger
from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from kubernetes_asyncio.client import V1Pod

logger = get_logger(__name__)

# Every server pod carries this label (see deploy_server in kubernetes_client.py), so a single
# list per namespace returns all of them at once. The per-server "app" label is the generated_name.
IDEGYM_POD_SELECTOR = "app.kubernetes.io/part-of=idegym"


def _max_restart_count(pod: V1Pod) -> int:
    statuses = pod.status.container_statuses or []
    return max((status.restart_count or 0 for status in statuses), default=0)


def _terminated_description(pod: V1Pod) -> Optional[str]:
    """Best-effort, human-readable description of why a container last terminated or is stuck waiting."""
    for status in pod.status.container_statuses or []:
        terminated = None
        if status.last_state and status.last_state.terminated:
            terminated = status.last_state.terminated
        elif status.state and status.state.terminated:
            terminated = status.state.terminated

        if terminated:
            parts = []
            if terminated.reason:
                parts.append(terminated.reason)
            if terminated.exit_code is not None:
                parts.append(f"exit {terminated.exit_code}")
            if terminated.signal:
                parts.append(f"signal {terminated.signal}")
            description = ", ".join(parts) if parts else "terminated"
            if terminated.message:
                description = f"{description}: {terminated.message.strip()}"
            return description

        # e.g. CrashLoopBackOff while the kubelet keeps restarting the container.
        if status.state and status.state.waiting and status.state.waiting.reason:
            waiting = status.state.waiting
            message = waiting.message.strip() if waiting.message else None
            return f"{waiting.reason}: {message}" if message else waiting.reason

    return None


def evaluate_pod_crash(pod: V1Pod, max_restarts: int) -> Optional[str]:
    """
    Return a human-readable crash reason if the pod has failed beyond its restart budget, else None.

    Catches three signals from the pod status alone (no Events API):
      - pod-level eviction / Failed phase (e.g. out-of-storage / disk pressure), which may not
        increment container restart_count because the pod is replaced rather than restarted;
      - container restart_count exceeding the budget (OOMKilled, non-zero exit, gvisor failures
        all surface here because the Deployment keeps restarting the container);
      - the reason text additionally exposes OOMKilled / exit codes / CrashLoopBackOff for the user.
    """
    if pod.metadata and pod.metadata.deletion_timestamp is not None:
        # Pod is already being deleted (cleanup / rollout); not our concern.
        return None

    status = pod.status
    if status is None:
        return None

    if status.phase == "Failed" or status.reason == "Evicted":
        reason = status.reason or "Failed"
        message = status.message.strip() if status.message else _terminated_description(pod)
        description = f"Pod {reason.lower()}"
        return f"{description}: {message}" if message else description

    restart_count = _max_restart_count(pod)
    if restart_count > max_restarts:
        description = f"Pod restarted {restart_count} time(s), exceeding the restart budget of {max_restarts}"
        terminated = _terminated_description(pod)
        return f"{description}. Last termination: {terminated}" if terminated else description

    return None


def _index_pods_by_app(pods: list[V1Pod]) -> dict[str, V1Pod]:
    """Index pods by their ``app`` label (the server's generated_name), preferring live pods."""
    indexed: dict[str, V1Pod] = {}
    for pod in pods:
        labels = (pod.metadata.labels or {}) if pod.metadata else {}
        app = labels.get("app")
        if not app:
            continue
        existing = indexed.get(app)
        # During a rollout an old terminating pod can overlap a new one; keep the live one
        # regardless of the order Kubernetes returns them in.
        if existing is not None:
            existing_terminating = bool(existing.metadata and existing.metadata.deletion_timestamp is not None)
            incoming_terminating = pod.metadata.deletion_timestamp is not None
            if incoming_terminating and not existing_terminating:
                continue
        indexed[app] = pod
    return indexed


@log_exceptions("Error detecting crashed servers", logger, swallow=True)
async def detect_crashed_servers(db: AsyncSession) -> None:
    """
    Mark servers whose pods crashed/OOMed/were evicted beyond their restart budget as CRASHED,
    record the reason, and delete their Deployment to break the restart loop.

    Issues a single pod list per distinct namespace (never per server) and reads no Events.
    """
    statuses = {AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED}
    servers = await get_idegym_servers_by_status(db, statuses)
    if not servers:
        return

    pods_by_namespace: dict[str, dict[str, V1Pod]] = {}
    for namespace in {server.namespace for server in servers}:
        pods = await list_pods(IDEGYM_POD_SELECTOR, namespace)
        pods_by_namespace[namespace] = _index_pods_by_app(pods)

    for server in servers:
        pod = pods_by_namespace.get(server.namespace, {}).get(server.generated_name)
        if pod is None:
            # No pod yet, or it is already gone; the timeout-based cleanup handles those.
            continue

        reason = evaluate_pod_crash(pod, server.max_restarts)
        if reason is None:
            continue

        logger.warning(f"IdeGYM server {server.generated_name} crashed: {reason}")

        # Tear the Deployment down first to stop the restart loop, mirroring cleanup_servers:
        # only declare the server terminal (which also releases its resource quota) once we know
        # whether the deletion succeeded. The crash reason is recorded either way so the client
        # sees it on the next forward.
        try:
            await clean_up_server(name=server.generated_name, namespace=server.namespace)
            await update_idegym_server_heartbeat(db, server.id, AvailabilityStatus.CRASHED, details=reason)
            logger.info(f"Tore down crashed IdeGYM server {server.generated_name}")
        except Exception:
            logger.exception(f"Failed to tear down crashed IdeGYM server {server.generated_name}")
            await update_idegym_server_heartbeat(db, server.id, AvailabilityStatus.DELETION_FAILED, details=reason)
