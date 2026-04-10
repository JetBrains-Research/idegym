import asyncio
import inspect
import threading
import time
from collections.abc import Awaitable, Callable
from typing import TypeVar, cast

from kubernetes_asyncio.client import (
    ApiClient,
    ApiException,
    AppsV1Api,
    CoreV1Api,
    PolicyV1Api,
    V1DeleteOptions,
    V1Namespace,
    V1ObjectMeta,
    V1Pod,
)
from kubernetes_asyncio.stream import WsApiClient

T = TypeVar("T")


async def _await_api_result(result: Awaitable[T] | T) -> T:
    """
    Await Kubernetes client results across inconsistent stub/runtime behavior.

    kubernetes-asyncio methods are awaitable at runtime, but some type stubs
    do not declare Awaitable return types, which confuses IDE/static analysis.
    """
    if inspect.isawaitable(result):
        return await cast(Awaitable[T], result)
    return result


def _run_async(coro: Awaitable[T]) -> T:
    """Run an async coroutine from sync code."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    result: dict[str, T] = {}
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            result["value"] = asyncio.run(coro)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    thread.join()

    if errors:
        raise errors[0]

    return result["value"]


async def _with_clients(func: Callable[[CoreV1Api, AppsV1Api, PolicyV1Api], Awaitable[T]]) -> T:
    """
    Execute a function with Kubernetes API clients.
    Assumes kubernetes config is already loaded (e.g., via pytest fixture).
    """
    async with ApiClient() as api_client:
        core = CoreV1Api(api_client)
        apps = AppsV1Api(api_client)
        policy = PolicyV1Api(api_client)
        return await func(core, apps, policy)


def namespace_exists(namespace: str) -> bool:
    async def _op(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> bool:
        try:
            await _await_api_result(core.read_namespace(name=namespace))
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    return _run_async(_with_clients(_op))


def ensure_namespace_exists(namespace: str) -> bool:
    async def _op(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> bool:
        try:
            await _await_api_result(
                core.create_namespace(
                    body=V1Namespace(metadata=V1ObjectMeta(name=namespace)),
                ),
            )
            return True
        except ApiException as exc:
            if exc.status == 409:
                return True
            raise

    return _run_async(_with_clients(_op))


def delete_namespace(namespace: str, timeout: int = 180, check_interval: int = 2) -> bool:
    async def _delete(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> None:
        try:
            await _await_api_result(core.delete_namespace(name=namespace, body=V1DeleteOptions()))
        except ApiException as exc:
            if exc.status != 404:
                raise

    _run_async(_with_clients(_delete))

    start_time = time.time()
    while time.time() - start_time < timeout:
        if not namespace_exists(namespace):
            return True
        time.sleep(check_interval)
    return False


def patch_service_type(name: str, namespace: str, service_type: str) -> bool:
    async def _op(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> bool:
        try:
            await _await_api_result(
                core.patch_namespaced_service(
                    name=name,
                    namespace=namespace,
                    body={"spec": {"type": service_type}},
                ),
            )
            return True
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise

    return _run_async(_with_clients(_op))


def list_pods(namespace: str, label_selector: str | None = None) -> list[V1Pod]:
    async def _op(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> list[V1Pod]:
        response = await _await_api_result(core.list_namespaced_pod(namespace=namespace, label_selector=label_selector))
        return response.items or []

    return _run_async(_with_clients(_op))


def list_pod_names(namespace: str, label_selector: str | None = None) -> list[str]:
    return [
        pod.metadata.name for pod in list_pods(namespace=namespace, label_selector=label_selector) if pod.metadata.name
    ]


def is_any_pod_ready(namespace: str, label_selector: str | None = None) -> bool:
    for pod in list_pods(namespace=namespace, label_selector=label_selector):
        conditions = pod.status.conditions if pod.status else None
        if not conditions:
            continue
        if any(condition.type == "Ready" and condition.status == "True" for condition in conditions):
            return True
    return False


def resolve_pod_selector(app_label: str, namespace: str, label_key: str = "app.kubernetes.io/name") -> str:
    """
    Return a working label selector for pods, trying the preferred label key first.
    Falls back to the legacy 'app' label if no pods are found with the preferred key.
    """
    selectors = [f"{label_key}={app_label}"]
    if label_key != "app":
        selectors.append(f"app={app_label}")

    for selector in selectors:
        if list_pod_names(namespace=namespace, label_selector=selector):
            return selector

    return selectors[0]


def delete_pods(namespace: str, pod_names: list[str]) -> None:
    if not pod_names:
        return

    async def _op(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> None:
        for pod_name in pod_names:
            try:
                await _await_api_result(
                    core.delete_namespaced_pod(
                        name=pod_name,
                        namespace=namespace,
                        body=V1DeleteOptions(),
                    ),
                )
            except ApiException as exc:
                if exc.status != 404:
                    raise

    _run_async(_with_clients(_op))


def wait_for_pods_deleted(
    namespace: str,
    pod_names: list[str],
    timeout: int = 120,
    check_interval: int = 2,
) -> bool:
    if not pod_names:
        return True

    async def _remaining(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> set[str]:
        existing = set()
        for pod_name in pod_names:
            try:
                await _await_api_result(core.read_namespaced_pod(name=pod_name, namespace=namespace))
                existing.add(pod_name)
            except ApiException as exc:
                if exc.status != 404:
                    raise
        return existing

    start_time = time.time()
    while time.time() - start_time < timeout:
        remaining = _run_async(_with_clients(_remaining))
        if not remaining:
            return True
        time.sleep(check_interval)

    return False


def wait_for_pods_by_label_deleted(
    namespace: str,
    label_selector: str,
    timeout: int = 120,
    check_interval: int = 2,
) -> bool:
    """Wait until no pods matching label_selector exist in the namespace."""

    async def _count(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> int:
        pods = await _await_api_result(core.list_namespaced_pod(namespace=namespace, label_selector=label_selector))
        return len(pods.items)

    start_time = time.time()
    while time.time() - start_time < timeout:
        if _run_async(_with_clients(_count)) == 0:
            return True
        time.sleep(check_interval)

    return False


def list_deployment_names(namespace: str, label_selector: str | None = None) -> list[str]:
    async def _op(_core: CoreV1Api, apps: AppsV1Api, _policy: PolicyV1Api) -> list[str]:
        response = await _await_api_result(
            apps.list_namespaced_deployment(namespace=namespace, label_selector=label_selector),
        )
        return [item.metadata.name for item in (response.items or []) if item.metadata and item.metadata.name]

    return _run_async(_with_clients(_op))


def delete_deployment(namespace: str, deployment_name: str) -> None:
    async def _op(_core: CoreV1Api, apps: AppsV1Api, _policy: PolicyV1Api) -> None:
        try:
            await _await_api_result(
                apps.delete_namespaced_deployment(
                    name=deployment_name,
                    namespace=namespace,
                    body=V1DeleteOptions(),
                ),
            )
        except ApiException as exc:
            if exc.status != 404:
                raise

    _run_async(_with_clients(_op))


def delete_services(namespace: str, service_names: list[str]) -> None:
    if not service_names:
        return

    async def _op(core: CoreV1Api, _apps: AppsV1Api, _policy: PolicyV1Api) -> None:
        for service_name in service_names:
            try:
                await _await_api_result(core.delete_namespaced_service(name=service_name, namespace=namespace))
            except ApiException as exc:
                if exc.status != 404:
                    raise

    _run_async(_with_clients(_op))


def exec_in_pod(pod_name: str, namespace: str, command: list[str]) -> str:
    async def _op() -> str:
        async with WsApiClient() as ws_client:
            core = CoreV1Api(ws_client)
            ws = await core.connect_get_namespaced_pod_exec(
                name=pod_name,
                namespace=namespace,
                command=command,
                stderr=True,
                stdin=False,
                stdout=True,
                tty=False,
                _preload_content=False,
            )
            await ws.run_until_complete()
            stdout = ws.read_stdout()
            stderr = ws.read_stderr()
            if ws.returncode != 0:
                raise RuntimeError(f"exec in pod {pod_name} failed (rc={ws.returncode}): {stderr.strip()}")
            return stdout

    return _run_async(_op())
