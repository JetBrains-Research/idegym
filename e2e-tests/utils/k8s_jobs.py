import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Optional, TypeVar

from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import ApiClient, ApiException, BatchV1Api, CoreV1Api, V1DeleteOptions, V1Job
from kubernetes_asyncio.watch import Watch
from utils.constants import DEFAULT_NAMESPACE, KUBE_SYSTEM_NAMESPACE
from yaml import safe_load

logger = get_logger(__name__)

T = TypeVar("T")


async def _with_clients(func: Callable[[BatchV1Api, CoreV1Api], Awaitable[T]]) -> T:
    """Execute a function with Kubernetes Batch and Core API clients."""
    async with ApiClient() as api:
        return await func(BatchV1Api(api), CoreV1Api(api))


async def _create_job(batch: BatchV1Api, job_spec: dict, namespace: str) -> V1Job:
    return await batch.create_namespaced_job(namespace=namespace, body=job_spec)


async def _delete_job(batch: BatchV1Api, job_name: str, namespace: str) -> None:
    try:
        await batch.delete_namespaced_job(
            name=job_name,
            namespace=namespace,
            body=V1DeleteOptions(propagation_policy="Foreground"),
        )
        logger.debug(f"Deleted job {job_name} in namespace {namespace}")
    except ApiException as e:
        if e.status == 404:
            logger.debug(f"Job {job_name} not found (already deleted)")
        else:
            raise


async def _wait_for_job_completion(batch: BatchV1Api, job_name: str, namespace: str, timeout: int) -> bool:
    try:
        async with asyncio.timeout(timeout):
            w = Watch()
            try:
                async for event in w.stream(
                    batch.list_namespaced_job,
                    namespace=namespace,
                    field_selector=f"metadata.name={job_name}",
                    timeout_seconds=timeout,
                ):
                    job: V1Job = event["object"]
                    status = job.status
                    if status is None:
                        continue
                    succeeded = getattr(status, "succeeded", 0) or 0
                    failed = getattr(status, "failed", 0) or 0
                    if succeeded > 0:
                        logger.debug(f"Job {job_name} succeeded")
                        return True
                    elif failed > 0:
                        logger.warning(f"Job {job_name} failed")
                        return False
            finally:
                await w.close()
    except asyncio.TimeoutError:
        logger.warning(f"Job {job_name} timed out after {timeout}s")
        return False
    except Exception as e:
        logger.error(f"Error waiting for job {job_name}: {e}")
        return False


async def _get_job_logs(core: CoreV1Api, job_name: str, namespace: str) -> Optional[str]:
    try:
        pods = await core.list_namespaced_pod(
            namespace=namespace,
            label_selector=f"job-name={job_name}",
        )
        if not pods.items:
            logger.warning(f"No pods found for job {job_name}")
            return None
        pod = pods.items[0]
        return await core.read_namespaced_pod_log(
            name=pod.metadata.name,
            namespace=namespace,
        )
    except ApiException as e:
        logger.warning(f"Failed to get logs for job {job_name}: {e}")
        return None


async def _collect_pod_diagnostics(core: CoreV1Api, pod, namespace: str) -> str:
    """Build a human-readable diagnostics string for a single pod.

    Collects phase, conditions, container statuses (with restart counts and
    waiting/terminated reasons), recent pod events, and container logs. When a
    container has restarted (crash loop), the previous container's logs are also
    fetched since the current logs are often empty right after a restart.
    """
    pod_name = pod.metadata.name
    phase = pod.status.phase if pod.status else "Unknown"

    # Collect pod conditions for scheduling/readiness diagnosis
    conditions_info = ""
    if pod.status and pod.status.conditions:
        condition_lines = [f"  {c.type}={c.status} reason={c.reason} msg={c.message}" for c in pod.status.conditions]
        conditions_info = "\n".join(condition_lines)

    # Collect per-container statuses (restart counts, waiting/terminated reasons)
    container_info = ""
    restarted_containers: list[str] = []
    if pod.status and pod.status.container_statuses:
        container_lines = []
        for cs in pod.status.container_statuses:
            state_desc = "running"
            state = cs.state
            if state and state.waiting:
                state_desc = f"waiting reason={state.waiting.reason} msg={state.waiting.message}"
            elif state and state.terminated:
                state_desc = (
                    f"terminated reason={state.terminated.reason} "
                    f"exit_code={state.terminated.exit_code} msg={state.terminated.message}"
                )
            container_lines.append(f"  {cs.name} ready={cs.ready} restarts={cs.restart_count} state={state_desc}")
            if cs.restart_count:
                restarted_containers.append(cs.name)
        container_info = "\n".join(container_lines)

    # Collect recent pod events
    events_info = ""
    try:
        events = await core.list_namespaced_event(
            namespace=namespace,
            field_selector=f"involvedObject.name={pod_name}",
        )
        if events.items:
            event_lines = [
                f"  [{e.reason}] {e.message}"
                for e in sorted(events.items, key=lambda x: x.last_timestamp or x.event_time or datetime.min)[-10:]
            ]
            events_info = "\n".join(event_lines)
    except ApiException as e:
        events_info = f"  (failed to collect events: {e})"

    try:
        logs = await core.read_namespaced_pod_log(name=pod_name, namespace=namespace)
    except ApiException as e:
        logs = f"(failed to get logs: {e})"

    # For crash-looping containers, the current logs are usually empty, so also
    # pull the previous container instance's logs which contain the crash reason.
    previous_logs = ""
    for container_name in restarted_containers:
        try:
            prev = await core.read_namespaced_pod_log(
                name=pod_name, namespace=namespace, container=container_name, previous=True
            )
            if prev:
                previous_logs += f"\nprevious logs ({container_name}):\n{prev}"
        except ApiException as e:
            previous_logs += f"\nprevious logs ({container_name}): (failed to get: {e})"

    return (
        f"phase={phase}\n"
        f"conditions:\n{conditions_info or '  (none)'}\n"
        f"containers:\n{container_info or '  (none)'}\n"
        f"events:\n{events_info or '  (none)'}\n"
        f"logs:\n{logs or '  (empty)'}"
        f"{previous_logs}"
    )


async def _get_all_server_pod_logs(core: CoreV1Api, namespace: str) -> dict[str, str]:
    logs_dict: dict[str, str] = {}
    try:
        pods = await core.list_namespaced_pod(
            namespace=namespace,
            label_selector="app.kubernetes.io/component=sandbox",
        )
        if not pods.items:
            logger.warning(f"No sandbox pods found in namespace {namespace}")
            return logs_dict
        for pod in pods.items:
            logs_dict[pod.metadata.name] = await _collect_pod_diagnostics(core, pod, namespace)
        return logs_dict
    except ApiException as e:
        logger.warning(f"Failed to list sandbox pods: {e}")
        return logs_dict


async def _get_namespace_pod_diagnostics(
    core: CoreV1Api, namespace: str, label_selector: Optional[str] = None
) -> dict[str, str]:
    """Collect diagnostics for every pod in a namespace (optionally filtered by label)."""
    diagnostics: dict[str, str] = {}
    try:
        pods = await core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)
        if not pods.items:
            logger.warning(f"No pods found in namespace {namespace} (selector={label_selector or 'none'})")
            return diagnostics
        for pod in pods.items:
            diagnostics[pod.metadata.name] = await _collect_pod_diagnostics(core, pod, namespace)
        return diagnostics
    except ApiException as e:
        logger.warning(f"Failed to list pods in namespace {namespace}: {e}")
        return diagnostics


async def _get_namespace_events(core: CoreV1Api, namespace: str, limit: int = 30) -> list[str]:
    """Return the most recent namespace-wide events as formatted strings."""
    try:
        events = await core.list_namespaced_event(namespace=namespace)
        if not events.items:
            return []
        ordered = sorted(events.items, key=lambda x: x.last_timestamp or x.event_time or datetime.min)[-limit:]
        return [
            f"[{e.type}] {e.reason} ({e.involved_object.kind}/{e.involved_object.name}): {e.message}" for e in ordered
        ]
    except ApiException as e:
        logger.warning(f"Failed to list events in namespace {namespace}: {e}")
        return []


async def create_job_from_yaml(yaml_content: str, namespace: str = KUBE_SYSTEM_NAMESPACE) -> V1Job:
    job_spec = safe_load(yaml_content)

    async def _op(batch: BatchV1Api, _core: CoreV1Api) -> V1Job:
        return await _create_job(batch, job_spec, namespace)

    return await _with_clients(_op)


async def delete_job(job_name: str, namespace: str = KUBE_SYSTEM_NAMESPACE) -> None:
    async def _op(batch: BatchV1Api, _core: CoreV1Api) -> None:
        await _delete_job(batch, job_name, namespace)

    await _with_clients(_op)


async def wait_for_job_completion(job_name: str, namespace: str = KUBE_SYSTEM_NAMESPACE, timeout: int = 120) -> bool:
    async def _op(batch: BatchV1Api, _core: CoreV1Api) -> bool:
        return await _wait_for_job_completion(batch, job_name, namespace, timeout)

    return await _with_clients(_op)


async def get_job_logs(job_name: str, namespace: str = KUBE_SYSTEM_NAMESPACE) -> Optional[str]:
    async def _op(_batch: BatchV1Api, core: CoreV1Api) -> Optional[str]:
        return await _get_job_logs(core, job_name, namespace)

    return await _with_clients(_op)


async def get_all_server_pod_logs(namespace: str = DEFAULT_NAMESPACE) -> dict[str, str]:
    async def _op(_batch: BatchV1Api, core: CoreV1Api) -> dict[str, str]:
        return await _get_all_server_pod_logs(core, namespace)

    return await _with_clients(_op)


async def get_namespace_pod_diagnostics(
    namespace: str = DEFAULT_NAMESPACE, label_selector: Optional[str] = None
) -> dict[str, str]:
    async def _op(_batch: BatchV1Api, core: CoreV1Api) -> dict[str, str]:
        return await _get_namespace_pod_diagnostics(core, namespace, label_selector)

    return await _with_clients(_op)


async def get_namespace_events(namespace: str = DEFAULT_NAMESPACE, limit: int = 30) -> list[str]:
    async def _op(_batch: BatchV1Api, core: CoreV1Api) -> list[str]:
        return await _get_namespace_events(core, namespace, limit)

    return await _with_clients(_op)


async def run_job(yaml_content: str, namespace: str = KUBE_SYSTEM_NAMESPACE, timeout: int = 120) -> bool:
    job_spec = safe_load(yaml_content)
    job_name = job_spec["metadata"]["name"]

    async def _op(batch: BatchV1Api, core: CoreV1Api) -> bool:
        # Delete any existing job and wait until it's fully gone
        await _delete_job(batch, job_name, namespace)

        # Poll until job is actually deleted to avoid 409 AlreadyExists errors
        while True:
            try:
                await batch.read_namespaced_job(name=job_name, namespace=namespace)
                await asyncio.sleep(0.5)
            except ApiException as e:
                if e.status == 404:
                    logger.debug(f"Job {job_name} fully deleted")
                    break
                raise

        # Create the job
        await _create_job(batch, job_spec, namespace)
        logger.info(f"Created job {job_name} in namespace {namespace}")

        # Wait for completion
        success = await _wait_for_job_completion(batch, job_name, namespace, timeout)

        # Get logs if failed
        if not success:
            logs = await _get_job_logs(core, job_name, namespace)
            if logs:
                logger.error(f"Job {job_name} logs:\n{logs}")

        return success

    return await _with_clients(_op)


def run_job_sync(yaml_content: str, namespace: str = KUBE_SYSTEM_NAMESPACE, timeout: int = 120) -> bool:
    """
    Synchronous wrapper to create a job, wait for completion, and get logs.

    This should only be called from synchronous code outside of an async event loop
    (e.g., from build_images.py during test setup).
    """
    return asyncio.run(run_job(yaml_content, namespace, timeout))
