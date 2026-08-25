import json
from asyncio import CancelledError, Lock, gather, sleep, timeout
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from os import environ as env
from random import getrandbits
from typing import Any, Optional, cast

from idegym.api import __version__
from idegym.api.download import DownloadRequest
from idegym.api.exceptions import ResourceDeletionFailedException
from idegym.api.orchestrator.servers import ServerKind
from idegym.api.paths import API_BASE_PATH, ActuatorPath, OpenenvPath
from idegym.api.status import Status
from idegym.api.type import ConditionStatus
from idegym.utils.dict import deep_merge
from idegym.utils.functools import cached_async_result
from idegym.utils.logging import get_logger
from kubernetes.utils.quantity import parse_quantity
from kubernetes_asyncio.client import (
    ApiClient,
    ApiException,
    AppsV1Api,
    BatchV1Api,
    Configuration,
    CoreV1Api,
    CustomObjectsApi,
    PolicyV1Api,
    V1Affinity,
    V1ConfigMap,
    V1ConfigMapKeySelector,
    V1ConfigMapList,
    V1ConfigMapVolumeSource,
    V1Container,
    V1ContainerPort,
    V1DeleteOptions,
    V1Deployment,
    V1DeploymentList,
    V1DeploymentSpec,
    V1DeploymentStrategy,
    V1EnvVar,
    V1EnvVarSource,
    V1HTTPGetAction,
    V1Job,
    V1JobSpec,
    V1KeyToPath,
    V1LabelSelector,
    V1LocalObjectReference,
    V1NodeAffinity,
    V1NodeSelectorRequirement,
    V1NodeSelectorTerm,
    V1ObjectFieldSelector,
    V1ObjectMeta,
    V1OwnerReference,
    V1Pod,
    V1PodDisruptionBudget,
    V1PodDisruptionBudgetList,
    V1PodDisruptionBudgetSpec,
    V1PodSpec,
    V1PodTemplateSpec,
    V1PreferredSchedulingTerm,
    V1Probe,
    V1ResourceFieldSelector,
    V1ResourceRequirements,
    V1SecretKeySelector,
    V1SecretVolumeSource,
    V1SecurityContext,
    V1Service,
    V1ServiceList,
    V1ServicePort,
    V1ServiceSpec,
    V1Status,
    V1Toleration,
    V1Volume,
    V1VolumeMount,
)
from kubernetes_asyncio.config import (
    ConfigException,
    load_incluster_config,
    load_kube_config,
)

KubernetesV1Apis = tuple[AppsV1Api, BatchV1Api, CoreV1Api, PolicyV1Api, CustomObjectsApi]

V1ResourceList = V1ConfigMapList | V1DeploymentList | V1PodDisruptionBudgetList | V1ServiceList

logger = get_logger(__name__)

SANDBOX_COMPONENT_LABEL = "app.kubernetes.io/component"
SANDBOX_COMPONENT_VALUE = "sandbox"
SANDBOX_CAPACITY_RESOURCE = "idegym.jetbrains.com/sandbox"
SANDBOX_CAPACITY_OWNER_ANNOTATION = "idegym.jetbrains.com/sandbox-capacity-owner"
SANDBOX_CAPACITY_LIMIT_ANNOTATION = "idegym.jetbrains.com/sandbox-capacity-limit"
SANDBOX_CAPACITY_RECONCILE_INTERVAL_SECONDS = 30
SANDBOX_CAPACITY_CONVERGENCE_TIMEOUT_SECONDS = 60
SANDBOX_CAPACITY_CONVERGENCE_POLL_SECONDS = 1
_prepared_sandbox_capacity: Optional[tuple[int, str]] = None
_sandbox_capacity_prepare_lock = Lock()


def _requests_sandbox_capacity(pod_spec: V1PodSpec) -> bool:
    """Return whether any scheduled container reserves the managed sandbox resource."""
    containers = [*(pod_spec.containers or []), *(pod_spec.init_containers or [])]
    for container in containers:
        requests = container.resources.requests if container.resources else None
        value = requests.get(SANDBOX_CAPACITY_RESOURCE) if requests else None
        if value is None:
            continue
        try:
            if parse_quantity(value) >= 1:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _add_sandbox_capacity_request(
    resources: Optional[V1ResourceRequirements],
) -> V1ResourceRequirements:
    """Reserve one scheduler-accounted sandbox unit without changing caller resources."""
    resources = resources or V1ResourceRequirements()
    requests = dict(resources.requests or {})
    limits = dict(resources.limits or {})
    if SANDBOX_CAPACITY_RESOURCE in requests or SANDBOX_CAPACITY_RESOURCE in limits:
        raise ValueError(f"{SANDBOX_CAPACITY_RESOURCE} is managed by max_sandboxes_per_node")

    requests[SANDBOX_CAPACITY_RESOURCE] = "1"
    limits[SANDBOX_CAPACITY_RESOURCE] = "1"
    return V1ResourceRequirements(claims=resources.claims, requests=requests, limits=limits)


async def find_legacy_sandbox_workloads() -> list[str]:
    """Find live sandbox workloads that do not reserve managed capacity."""
    selector = f"{SANDBOX_COMPONENT_LABEL}={SANDBOX_COMPONENT_VALUE}"
    async with async_kube_api() as (apps, _, core, _, _):
        deployments, pods = await gather(
            apps.list_deployment_for_all_namespaces(label_selector=selector),
            core.list_pod_for_all_namespaces(label_selector=selector),
        )

    legacy = {
        f"Deployment {deployment.metadata.namespace}/{deployment.metadata.name}"
        for deployment in deployments.items
        if not _requests_sandbox_capacity(deployment.spec.template.spec)
    }
    legacy.update(
        f"Pod {pod.metadata.namespace}/{pod.metadata.name}"
        for pod in pods.items
        if (not pod.status or pod.status.phase not in {"Succeeded", "Failed"})
        and not _requests_sandbox_capacity(pod.spec)
    )
    return sorted(legacy)


def _json_pointer(value: str) -> str:
    return value.replace("~", "~0").replace("/", "~1")


def _node_owner(node) -> Optional[str]:
    return (node.metadata.annotations or {}).get(SANDBOX_CAPACITY_OWNER_ANNOTATION)


def _resource_matches(resources, expected: int) -> bool:
    value = (resources or {}).get(SANDBOX_CAPACITY_RESOURCE)
    if value is None:
        return expected == 0
    try:
        return parse_quantity(value) == expected
    except (TypeError, ValueError):
        return False


def _assert_sandbox_capacity_owner(nodes, owner: str) -> None:
    conflicting = sorted({value for node in nodes if (value := _node_owner(node)) and value != owner})
    if conflicting:
        raise RuntimeError(f"Sandbox capacity is owned by another IdeGYM installation: {', '.join(conflicting)}")


async def _claim_sandbox_capacity_owner(core, nodes, owner: str, limit: int) -> None:
    """Atomically establish one cluster-global owner using the first node as an anchor."""
    for _ in range(5):
        _assert_sandbox_capacity_owner(nodes, owner)
        if any(_node_owner(node) == owner for node in nodes):
            return
        if not nodes:
            raise RuntimeError("Cannot manage sandbox capacity because the cluster has no nodes")

        anchor = min(nodes, key=lambda node: node.metadata.name)
        patch = [{"op": "test", "path": "/metadata/resourceVersion", "value": anchor.metadata.resource_version}]
        if anchor.metadata.annotations is None:
            patch.append(
                {
                    "op": "add",
                    "path": "/metadata/annotations",
                    "value": {
                        SANDBOX_CAPACITY_OWNER_ANNOTATION: owner,
                        SANDBOX_CAPACITY_LIMIT_ANNOTATION: str(limit),
                    },
                }
            )
        else:
            patch.extend(
                [
                    {
                        "op": "add",
                        "path": f"/metadata/annotations/{_json_pointer(SANDBOX_CAPACITY_OWNER_ANNOTATION)}",
                        "value": owner,
                    },
                    {
                        "op": "add",
                        "path": f"/metadata/annotations/{_json_pointer(SANDBOX_CAPACITY_LIMIT_ANNOTATION)}",
                        "value": str(limit),
                    },
                ]
            )
        try:
            await core.patch_node(
                name=anchor.metadata.name,
                body=patch,
                _content_type="application/json-patch+json",
            )
            return
        except ApiException as error:
            if error.status not in {404, 409, 422}:
                raise
            nodes = (await core.list_node()).items

    raise RuntimeError("Could not claim sandbox capacity ownership after concurrent node updates")


async def _wait_for_sandbox_capacity(
    core,
    node_names: set[str],
    expected: int,
    timeout_seconds: float = SANDBOX_CAPACITY_CONVERGENCE_TIMEOUT_SECONDS,
) -> None:
    """Wait until both capacity and scheduler-visible allocatable have converged."""
    try:
        async with timeout(timeout_seconds):
            while True:
                nodes = {node.metadata.name: node for node in (await core.list_node()).items}
                if all(
                    name not in nodes
                    or (
                        _resource_matches(nodes[name].status.capacity, expected)
                        and _resource_matches(nodes[name].status.allocatable, expected)
                    )
                    for name in node_names
                ):
                    return
                await sleep(SANDBOX_CAPACITY_CONVERGENCE_POLL_SECONDS)
    except TimeoutError as error:
        raise RuntimeError(
            f"Timed out waiting for sandbox capacity and allocatable to converge to {expected}"
        ) from error


async def reconcile_sandbox_node_capacity(max_sandboxes_per_node: int, owner: str) -> None:
    """Claim and advertise scheduler-accounted sandbox capacity on every current node."""
    if max_sandboxes_per_node <= 0:
        raise ValueError("max_sandboxes_per_node must be positive")
    if not owner:
        raise ValueError("sandbox capacity owner must be non-empty")

    json_pointer_resource = _json_pointer(SANDBOX_CAPACITY_RESOURCE)
    async with async_kube_api() as (_, _, core, _, _):
        nodes = (await core.list_node()).items
        await _claim_sandbox_capacity_owner(core, nodes, owner, max_sandboxes_per_node)
        patches = []
        for node in nodes:
            patches.append(
                core.patch_node(
                    name=node.metadata.name,
                    body={
                        "metadata": {
                            "annotations": {
                                SANDBOX_CAPACITY_OWNER_ANNOTATION: owner,
                                SANDBOX_CAPACITY_LIMIT_ANNOTATION: str(max_sandboxes_per_node),
                            }
                        }
                    },
                    _content_type="application/merge-patch+json",
                )
            )
            if _resource_matches(node.status.capacity, max_sandboxes_per_node):
                continue
            patches.append(
                core.patch_node_status(
                    name=node.metadata.name,
                    body=[
                        {
                            "op": "add",
                            "path": f"/status/capacity/{json_pointer_resource}",
                            "value": str(max_sandboxes_per_node),
                        }
                    ],
                    _content_type="application/json-patch+json",
                )
            )

        if patches:
            await gather(*patches)
        await _wait_for_sandbox_capacity(core, {node.metadata.name for node in nodes}, max_sandboxes_per_node)

    logger.info(
        "Reconciled sandbox capacity on Kubernetes nodes",
        nodes=len(nodes),
        updated_nodes=sum(not _resource_matches(node.status.capacity, max_sandboxes_per_node) for node in nodes),
        max_sandboxes_per_node=max_sandboxes_per_node,
        owner=owner,
    )


async def reconcile_sandbox_node_capacity_periodically(max_sandboxes_per_node: int, owner: str) -> None:
    """Keep capacity present on nodes added after orchestrator startup."""
    while True:
        await sleep(SANDBOX_CAPACITY_RECONCILE_INTERVAL_SECONDS)
        try:
            await reconcile_sandbox_node_capacity(max_sandboxes_per_node, owner)
        except CancelledError:
            raise
        except Exception:
            logger.exception("Failed to reconcile sandbox node capacity")


async def prepare_sandbox_node_capacity(max_sandboxes_per_node: int, owner: str) -> None:
    """Reject unsafe enablement transitions and ensure nodes advertise capacity."""
    global _prepared_sandbox_capacity
    prepared = (max_sandboxes_per_node, owner)
    if _prepared_sandbox_capacity == prepared:
        return

    async with _sandbox_capacity_prepare_lock:
        if _prepared_sandbox_capacity == prepared:
            return

        legacy = await find_legacy_sandbox_workloads()
        if legacy:
            preview = ", ".join(legacy[:5])
            suffix = f" and {len(legacy) - 5} more" if len(legacy) > 5 else ""
            raise RuntimeError(
                "Cannot enable max_sandboxes_per_node while sandbox workloads without capacity requests exist; "
                f"drain them first: {preview}{suffix}"
            )

        await reconcile_sandbox_node_capacity(max_sandboxes_per_node, owner)
        _prepared_sandbox_capacity = prepared


async def cleanup_sandbox_node_capacity(owner: str) -> None:
    """Safely zero managed capacity and release ownership after all requesters are drained."""
    global _prepared_sandbox_capacity
    if not owner:
        raise ValueError("sandbox capacity owner must be non-empty")

    async with async_kube_api() as (apps, _, core, _, _):
        nodes = (await core.list_node()).items
        _assert_sandbox_capacity_owner(nodes, owner)
        if any(not _resource_matches(node.status.capacity, 0) for node in nodes) and not any(
            _node_owner(node) == owner for node in nodes
        ):
            raise RuntimeError("Refusing to clean up unowned sandbox capacity")

        capacity_patches = [
            core.patch_node_status(
                name=node.metadata.name,
                body=[
                    {
                        "op": "add",
                        "path": f"/status/capacity/{_json_pointer(SANDBOX_CAPACITY_RESOURCE)}",
                        "value": "0",
                    }
                ],
                _content_type="application/json-patch+json",
            )
            for node in nodes
            if not _resource_matches(node.status.capacity, 0)
        ]
        if capacity_patches:
            await gather(*capacity_patches)
        await _wait_for_sandbox_capacity(core, {node.metadata.name for node in nodes}, 0)

        selector = f"{SANDBOX_COMPONENT_LABEL}={SANDBOX_COMPONENT_VALUE}"
        deployments, pods = await gather(
            apps.list_deployment_for_all_namespaces(label_selector=selector),
            core.list_pod_for_all_namespaces(),
        )
        requesting = {
            f"Deployment {item.metadata.namespace}/{item.metadata.name}"
            for item in deployments.items
            if _requests_sandbox_capacity(item.spec.template.spec)
        }
        requesting.update(
            f"Pod {item.metadata.namespace}/{item.metadata.name}"
            for item in pods.items
            if (not item.status or item.status.phase not in {"Succeeded", "Failed"})
            and _requests_sandbox_capacity(item.spec)
        )
        if requesting:
            preview = ", ".join(sorted(requesting)[:5])
            suffix = f" and {len(requesting) - 5} more" if len(requesting) > 5 else ""
            raise RuntimeError(f"Cannot clean up sandbox capacity while requesting workloads exist: {preview}{suffix}")

        await gather(*(_release_sandbox_capacity_owner(core, node, owner) for node in nodes))

    _prepared_sandbox_capacity = None
    logger.info("Cleaned up sandbox node capacity", nodes=len(nodes), owner=owner)


async def _release_sandbox_capacity_owner(core, node, owner: str) -> None:
    """Release one node only while its owner still matches the cleanup installation."""
    for _ in range(3):
        annotations = node.metadata.annotations or {}
        current_owner = annotations.get(SANDBOX_CAPACITY_OWNER_ANNOTATION)
        if current_owner is None:
            return
        if current_owner != owner:
            raise RuntimeError(f"Sandbox capacity is now owned by another IdeGYM installation: {current_owner}")

        owner_path = f"/metadata/annotations/{_json_pointer(SANDBOX_CAPACITY_OWNER_ANNOTATION)}"
        patch = [{"op": "test", "path": owner_path, "value": owner}]
        if SANDBOX_CAPACITY_LIMIT_ANNOTATION in annotations:
            patch.append(
                {
                    "op": "remove",
                    "path": f"/metadata/annotations/{_json_pointer(SANDBOX_CAPACITY_LIMIT_ANNOTATION)}",
                }
            )
        patch.append({"op": "remove", "path": owner_path})
        try:
            await core.patch_node(
                name=node.metadata.name,
                body=patch,
                _content_type="application/json-patch+json",
            )
            return
        except ApiException as error:
            if error.status == 404:
                return
            if error.status not in {409, 422}:
                raise
            current_nodes = {item.metadata.name: item for item in (await core.list_node()).items}
            if node.metadata.name not in current_nodes:
                return
            node = current_nodes[node.metadata.name]

    raise RuntimeError(f"Could not release sandbox capacity ownership on node {node.metadata.name}")


def build_node_affinity(taint_key: str, preference_weight: int) -> V1NodeAffinity:
    requirement = V1NodeSelectorRequirement(
        key=taint_key,
        operator="Exists",
    )
    term = V1PreferredSchedulingTerm(
        weight=preference_weight,
        preference=V1NodeSelectorTerm(
            match_expressions=[requirement],
        ),
    )
    return V1NodeAffinity(
        preferred_during_scheduling_ignored_during_execution=[term],
    )


def build_node_pool_affinity(taint_key: str, preference_weight: int) -> V1Affinity:
    affinity = build_node_affinity(
        taint_key=taint_key,
        preference_weight=preference_weight,
    )
    return V1Affinity(
        node_affinity=affinity,
    )


def get_server_probe_config(server_kind: ServerKind, container_port: int) -> tuple[str, dict[str, str]]:
    """
    Return health probe path and Prometheus annotations for a server deployment.
    OpenEnv servers use a different health path and have no metrics endpoint.
    IdeGYM servers expose actuator health and Prometheus metrics.
    """
    match server_kind:
        case ServerKind.OPENENV:
            return str(OpenenvPath.HEALTH), {"prometheus.io/scrape": "false"}
        case _:
            return (
                API_BASE_PATH + ActuatorPath.HEALTH,
                {
                    "prometheus.io/scrape": "true",
                    "prometheus.io/path": API_BASE_PATH + ActuatorPath.METRICS,
                    "prometheus.io/port": str(container_port),
                    "prometheus.io/scheme": "http|https",
                },
            )


@cached_async_result
async def create_clients() -> KubernetesV1Apis:
    """
    Lazily create and cache a single set of Kubernetes API clients for the app lifetime.
    Safe for concurrent calls.
    """
    configuration = Configuration.get_default_copy()
    api_client = ApiClient(configuration)
    logger.info("Initialized Kubernetes API client singleton!")
    return (
        AppsV1Api(api_client),
        BatchV1Api(api_client),
        CoreV1Api(api_client),
        PolicyV1Api(api_client),
        CustomObjectsApi(api_client),
    )


@asynccontextmanager
async def async_kube_api() -> AsyncGenerator[KubernetesV1Apis, Any]:
    yield await create_clients()


def to_env_var(dictionary: dict[str, Any]) -> V1EnvVar:
    name: Optional[str] = dictionary.get("name")
    value: Optional[str] = dictionary.get("value")
    value_from: Optional[dict[str, Any]] = dictionary.get("valueFrom")

    if not value_from:
        return V1EnvVar(
            name=name,
            value=value,
        )

    kwargs: dict[str, Any] = {}

    if value_from.get("secretKeyRef"):
        secret_key_ref = value_from["secretKeyRef"]
        kwargs["secret_key_ref"] = V1SecretKeySelector(
            name=secret_key_ref.get("name"),
            key=secret_key_ref.get("key"),
            optional=secret_key_ref.get("optional"),
        )

    if value_from.get("configMapKeyRef"):
        config_map_key_ref = value_from["configMapKeyRef"]
        kwargs["config_map_key_ref"] = V1ConfigMapKeySelector(
            name=config_map_key_ref.get("name"),
            key=config_map_key_ref.get("key"),
            optional=config_map_key_ref.get("optional"),
        )

    if value_from.get("fieldRef"):
        field_ref = value_from["fieldRef"]
        kwargs["field_ref"] = V1ObjectFieldSelector(
            field_path=field_ref.get("fieldPath"),
            api_version=field_ref.get("apiVersion"),
        )

    if value_from.get("resourceFieldRef"):
        resource_field_from = value_from["resourceFieldRef"]
        kwargs["resource_field_ref"] = V1ResourceFieldSelector(
            resource=resource_field_from.get("resource"),
            container_name=resource_field_from.get("containerName"),
            divisor=resource_field_from.get("divisor"),
        )

    return V1EnvVar(
        name=name,
        value=value,
        value_from=V1EnvVarSource(**kwargs),
    )


def deserialize_k8s(api_client: ApiClient, data: Any, klass: str) -> Any:
    """Deserialize a native (camelCase) Kubernetes-shaped dict/list into client model(s).

    ``klass`` is a kubernetes_asyncio model name or container expression, for example
    ``"V1PodSpec"`` or ``"list[V1Volume]"``. The client's deserializer maps camelCase keys to
    model attributes via each model's ``attribute_map`` and silently ignores unknown keys.
    """

    class _Response:
        # ApiClient.deserialize reads the payload from a response-like object's ``.data``.
        def __init__(self, payload: str) -> None:
            self.data = payload

    return api_client.deserialize(_Response(json.dumps(data)), klass)


async def load_kubernetes_config():
    try:
        load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration!")
        return
    except ConfigException:
        pass

    try:
        loader = await load_kube_config()
        current_context = loader.current_context
        logger.info(
            event="Loaded local Kubernetes configuration!",
            context=current_context["name"],
            **current_context["context"],
        )
    except:
        logger.exception("Could not load Kubernetes configuration!")
        raise


async def deploy_server(
    image_tag: str,
    server_name: str,
    namespace: str,
    service_port: int = 80,
    container_port: int = 8000,
    service_account_name: Optional[str] = None,
    runtime_class_name: Optional[str] = None,
    run_as_root: bool = False,
    node_selector: Optional[dict[str, str]] = None,
    node_pool_taint_key: Optional[str] = None,
    node_pool_preference_weight: int = 100,
    max_sandboxes_per_node: int = 0,
    sandbox_capacity_owner: Optional[str] = None,
    resources: Optional[V1ResourceRequirements | dict[str, Any]] = None,
    environment_variables: Iterable[V1EnvVar | dict[str, Any]] = (),
    volumes: Optional[Iterable[dict[str, Any]]] = None,
    volume_mounts: Optional[Iterable[dict[str, Any]]] = None,
    env_from: Optional[Iterable[dict[str, Any]]] = None,
    pod_overrides: Optional[dict[str, Any]] = None,
    server_kind: ServerKind = ServerKind.IDEGYM,
    snapshot_id: Optional[str] = None,
    snapshot_tag: Optional[str] = None,
):
    """
    Create a Kubernetes Deployment, Service, and PodDisruptionBudget for a server.

    The Service and PDB are created with the Deployment as their owner reference so
    they are garbage-collected when the Deployment is deleted.
    """
    logger.debug(f"Deploying '{server_name}' in namespace '{namespace}' with runtime class '{runtime_class_name}'.")

    # Reuse the cached API client (no extra aiohttp session) for camelCase deserialization.
    api_client = (await create_clients())[0].api_client

    uid = 0 if run_as_root else 1000
    security_context = V1SecurityContext(
        run_as_non_root=not run_as_root,
        run_as_user=uid,
        run_as_group=uid,
    )

    if isinstance(resources, dict):  # noinspection PyUnnecessaryCast
        dictionary = cast(dict, resources)
        resources = V1ResourceRequirements(**dictionary)

    if max_sandboxes_per_node < 0:
        raise ValueError("max_sandboxes_per_node must be non-negative")
    if max_sandboxes_per_node:
        if not sandbox_capacity_owner:
            raise ValueError("sandbox_capacity_owner is required when max_sandboxes_per_node is enabled")
        await prepare_sandbox_node_capacity(max_sandboxes_per_node, sandbox_capacity_owner)
        resources = _add_sandbox_capacity_request(resources)

    env = [
        environment_variable if isinstance(environment_variable, V1EnvVar) else to_env_var(environment_variable)
        for environment_variable in environment_variables
    ]

    port = V1ContainerPort(
        name="http",
        container_port=container_port,
        protocol="TCP",
    )
    health_probe_path, prometheus_annotations = get_server_probe_config(server_kind, port.container_port)
    readiness_probe = V1Probe(
        http_get=V1HTTPGetAction(
            path=health_probe_path,
            port=port.container_port,
        ),
        initial_delay_seconds=10,
        period_seconds=3,
    )
    container = V1Container(
        name="server",
        image=image_tag,
        image_pull_policy="IfNotPresent",
        ports=[port],
        readiness_probe=readiness_probe,
        security_context=security_context,
        resources=resources,
        env=env,
        env_from=deserialize_k8s(api_client, list(env_from), "list[V1EnvFromSource]") if env_from else None,
        volume_mounts=(
            deserialize_k8s(api_client, list(volume_mounts), "list[V1VolumeMount]") if volume_mounts else None
        ),
    )

    image_pull_secret = V1LocalObjectReference(name="regcred")
    annotations = {
        "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
        **prometheus_annotations,
    }
    if snapshot_tag:
        # Restore a specific GKE PodSnapshot instead of the latest one in the group.
        annotations["podsnapshot.gke.io/ps-name"] = snapshot_tag
    match_labels = {
        "app": server_name,
        SANDBOX_COMPONENT_LABEL: SANDBOX_COMPONENT_VALUE,
        "app.kubernetes.io/name": server_name,
        "app.kubernetes.io/part-of": "idegym",
    }
    labels = {
        **match_labels,
        "app.kubernetes.io/version": __version__,
        "idegym.jetbrains.com/snapshot-id": snapshot_id or server_name,
    }

    toleration = (
        V1Toleration(
            key=node_pool_taint_key,
            operator="Exists",
            effect="NoSchedule",
        )
        if node_pool_taint_key
        else None
    )

    affinity = (
        build_node_pool_affinity(
            taint_key=node_pool_taint_key,
            preference_weight=node_pool_preference_weight,
        )
        if node_pool_taint_key
        else None
    )

    pod_spec = V1PodSpec(
        containers=[container],
        image_pull_secrets=[image_pull_secret],
        service_account_name=service_account_name,
        runtime_class_name=runtime_class_name,
        node_selector=node_selector,
        tolerations=[toleration] if toleration else None,
        affinity=affinity,
        volumes=deserialize_k8s(api_client, list(volumes), "list[V1Volume]") if volumes else None,
    )

    if pod_overrides:
        # Layer arbitrary pod-level fields on top of the managed spec: serialize to a camelCase
        # dict, deep-merge, and deserialize back, concatenating list fields (tolerations, volumes,
        # ...) instead of replacing them. Before merging, enforce the invariants the API promises:
        #   - drop null values so callers cannot delete a managed field by setting it to null;
        #   - the ServiceAccount is owned by `service_account_name` (so the snapshot ServiceAccount
        #     stays authoritative during snapshot preparation), never by pod_overrides;
        #   - a hard node cap requires the Kubernetes scheduler, so nodeName and custom schedulers
        #     cannot bypass its managed extended-resource accounting;
        #   - the managed "server" container may be augmented with sidecars but never replaced.
        overrides = {key: value for key, value in pod_overrides.items() if value is not None}

        if overrides.keys() & {"serviceAccountName", "service_account_name"}:
            raise ValueError("pod_overrides must not set serviceAccountName; use service_account_name instead")

        if max_sandboxes_per_node and overrides.keys() & {
            "nodeName",
            "node_name",
            "schedulerName",
            "scheduler_name",
        }:
            raise ValueError("pod_overrides must not bypass scheduling while max_sandboxes_per_node is enabled")

        sidecars = overrides.get("containers")
        if sidecars is not None:
            if not isinstance(sidecars, list):
                raise ValueError("pod_overrides.containers must be a list of sidecar containers")
            if any(isinstance(container, dict) and container.get("name") == "server" for container in sidecars):
                raise ValueError("pod_overrides.containers must not redefine the managed 'server' container")

        merged_spec = deep_merge(api_client.sanitize_for_serialization(pod_spec), overrides, concat_lists=True)
        pod_spec = deserialize_k8s(api_client, merged_spec, "V1PodSpec")

    deployment = V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=V1ObjectMeta(
            name=server_name,
            labels=labels,
        ),
        spec=V1DeploymentSpec(
            replicas=1,
            strategy=V1DeploymentStrategy(type="Recreate") if max_sandboxes_per_node else None,
            selector=V1LabelSelector(
                match_labels=match_labels,
            ),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(
                    annotations=annotations,
                    labels=labels,
                ),
                spec=pod_spec,
            ),
        ),
    )

    port = V1ServicePort(
        port=service_port,
        target_port=port.container_port,
        protocol=port.protocol,
        name=port.name,
    )
    service = V1Service(
        api_version="v1",
        kind="Service",
        metadata=V1ObjectMeta(
            name=server_name,
            labels=labels,
        ),
        spec=V1ServiceSpec(
            type="ClusterIP",
            ports=[port],
            selector=match_labels,
        ),
    )

    pdb = V1PodDisruptionBudget(
        api_version="policy/v1",
        kind="PodDisruptionBudget",
        metadata=V1ObjectMeta(
            name=server_name,
            labels=labels,
        ),
        spec=V1PodDisruptionBudgetSpec(
            min_available=1,
            selector=V1LabelSelector(
                match_labels=match_labels,
            ),
        ),
    )

    async with async_kube_api() as (apps, _, core, policy, _):
        deployment = await apps.create_namespaced_deployment(
            body=deployment,
            namespace=namespace,
        )

        owner_reference = V1OwnerReference(
            api_version=deployment.api_version,
            kind=deployment.kind,
            name=deployment.metadata.name,
            uid=deployment.metadata.uid,
        )

        service.metadata.owner_references = [owner_reference]
        pdb.metadata.owner_references = [owner_reference]

        await gather(
            core.create_namespaced_service(
                body=service,
                namespace=namespace,
            ),
            policy.create_namespaced_pod_disruption_budget(
                body=pdb,
                namespace=namespace,
            ),
        )


async def wait_for_pods_ready(
    label_selector: str, namespace: str, wait_timeout: int = 60, max_image_pull_attempts: int = 3
):
    """
    Poll until all matching pods are Running and ready.

    Fails fast if image pull errors occur `max_image_pull_attempts` times in a row,
    or if pods remain Unschedulable for ~30 seconds (~15 consecutive checks at 2 s interval).
    Raises asyncio.TimeoutError if `wait_timeout` seconds elapse without all pods becoming ready.
    """
    consecutive_image_pull_errors = 0
    consecutive_unschedulable = 0
    max_consecutive_unschedulable = 15  # ~30s at 2s poll interval

    async with timeout(wait_timeout):
        while True:
            pods_ready, has_image_pull_error, has_terminating_pods, has_unschedulable_pods = await pods_are_ready(
                label_selector, namespace
            )

            if pods_ready and not has_terminating_pods:
                logger.info(f"Pods with label '{label_selector}' are ready and stable.")
                return

            if has_unschedulable_pods:
                consecutive_unschedulable += 1
                if consecutive_unschedulable >= max_consecutive_unschedulable:
                    raise RuntimeError(
                        f"Failed to start pods: Unschedulable condition detected {consecutive_unschedulable} times in a row"
                    )
            else:
                consecutive_unschedulable = 0

            if has_image_pull_error:
                consecutive_image_pull_errors += 1
                logger.warning(f"Image pull error detected ({consecutive_image_pull_errors}/{max_image_pull_attempts})")

                if consecutive_image_pull_errors >= max_image_pull_attempts:
                    raise RuntimeError(
                        f"Failed to start pods: Image pull errors detected {max_image_pull_attempts} times in a row"
                    )
            else:
                consecutive_image_pull_errors = 0

            await sleep(2)


async def pods_are_ready(label_selector: str, namespace: str) -> tuple[bool, bool, bool, bool]:
    """
    Return (pods_ready, has_image_pull_error, has_terminating_pods, has_unschedulable_pods).

    Terminating pods are excluded from the readiness check but their presence is reported
    so callers can wait for them to disappear before considering the deployment stable.
    """

    async with async_kube_api() as (_, _, core, _, _):
        pods = (await core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)).items

    has_image_pull_error = False
    has_terminating_pods = False
    has_unschedulable_pods = False

    if len(pods) > 0:
        for pod in pods:
            if pod.metadata.deletion_timestamp is not None:
                has_terminating_pods = True
                logger.debug(f"Pod {pod.metadata.name} is terminating")
                continue

            if pod.status.conditions:
                for condition in pod.status.conditions:
                    if (
                        condition.type == "PodScheduled"
                        and condition.status == ConditionStatus.FALSE
                        and condition.reason == "Unschedulable"
                    ):
                        has_unschedulable_pods = True
                        logger.warning(f"Pod {pod.metadata.name} is unschedulable: {condition.message}")

            if pod.status.container_statuses:
                for container in pod.status.container_statuses:
                    if container.state and container.state.waiting:
                        reason = container.state.waiting.reason
                        if reason in ["ImagePullBackOff", "ErrImagePull"]:
                            has_image_pull_error = True
                            logger.warning(
                                f"Pod {pod.metadata.name} has image pull error: {reason} with message: {container.state.waiting.message}"
                            )
                            break

    non_terminating_pods = [pod for pod in pods if pod.metadata.deletion_timestamp is None]

    pods_ready = len(non_terminating_pods) > 0 and all(
        pod.status.phase == "Running" and all(c.ready for c in pod.status.container_statuses)
        for pod in non_terminating_pods
    )

    return pods_ready, has_image_pull_error, has_terminating_pods, has_unschedulable_pods


async def list_pods(label_selector: str, namespace: str) -> list[V1Pod]:
    """Return all pods in a namespace matching the label selector."""

    async with async_kube_api() as (_, _, core, _, _):
        return (await core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)).items


async def are_any_pods_alive(label_selector: str, namespace: str) -> bool:
    """Return True if at least one non-terminating Running pod matches the selector."""

    async with async_kube_api() as (_, _, core, _, _):
        pods = (await core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)).items

    def is_pod_alive(p):
        if p.metadata.deletion_timestamp is not None:
            return False
        return p.status.phase == "Running"

    return any(is_pod_alive(pod) for pod in pods)


async def delete_with_retries(
    delete_func: Callable[..., Awaitable[V1Status]],
    resource_type: str,
    resource_name: str,
    namespace: str,
    max_retries: int = 3,
) -> bool:
    """
    Delete a Kubernetes resource with exponential-backoff retries.

    Returns True on success or if the resource was already gone (404). Returns False
    if all attempts are exhausted. Re-raises CancelledError immediately.
    """
    for attempt in range(max_retries):
        try:
            await delete_func(
                name=resource_name,
                namespace=namespace,
                body=V1DeleteOptions(),
            )
            logger.info(f"Successfully deleted {resource_type} '{resource_name}' in namespace '{namespace}'.")
            return True
        except CancelledError:
            raise
        except Exception as ex:
            if isinstance(ex, ApiException) and ex.status == 404:
                logger.info(
                    f"No {resource_type} '{resource_name}' found in namespace '{namespace}', nothing to delete."
                )
                return True

            if attempt < max_retries - 1:
                backoff = 2**attempt
                logger.warning(
                    f"Error deleting {resource_type} '{resource_name}': "
                    f"{ex.__class__.__name__}: {ex!s}. "
                    f"Retrying in {backoff} seconds..."
                )
                await sleep(backoff)
            else:
                logger.exception(f"Failed to delete {resource_type} '{resource_name}' after {max_retries} attempts!")
    return False


async def exists_with_retries(
    query_func: Callable[..., Awaitable[V1ResourceList]],
    resource_name: str,
    resource_type: str,
    namespace: str,
    max_retries: int = 3,
) -> bool:
    """
    Check whether a named Kubernetes resource exists, with exponential-backoff retries.

    Returns True if found, False if not found or all attempts are exhausted.
    Re-raises CancelledError immediately.
    """
    for attempt in range(max_retries):
        try:
            results = await query_func(
                field_selector=f"metadata.name={resource_name}",
                namespace=namespace,
            )
            return len(results.items) > 0
        except CancelledError:
            raise
        except Exception as ex:
            if attempt < max_retries - 1:
                backoff = 2**attempt
                logger.warning(
                    f"Error querying {resource_type} '{resource_name}': "
                    f"{ex.__class__.__name__}: {ex!s}. "
                    f"Retrying in {backoff} seconds..."
                )
                await sleep(backoff)
            else:
                logger.exception(f"Failed to query {resource_type} '{resource_name}' after {max_retries} attempts!")
    return False


async def check_and_delete(
    query_func: Callable[..., Awaitable[V1ResourceList]],
    delete_func: Callable[..., Awaitable[V1Status]],
    resource_name: str,
    resource_type: str,
    namespace: str,
    max_retries: int = 3,
) -> bool:
    """Delete a resource if it exists. Returns True if absent or successfully deleted."""
    exists = await exists_with_retries(
        query_func=query_func,
        resource_name=resource_name,
        resource_type=resource_type,
        namespace=namespace,
        max_retries=max_retries,
    )

    if not exists:
        logger.debug(f"'{resource_name}' {resource_type} not present in '{namespace}', skipping deletion...")
        return True

    return await delete_with_retries(
        delete_func=delete_func,
        resource_type=resource_type,
        resource_name=resource_name,
        namespace=namespace,
        max_retries=max_retries,
    )


async def clean_up_server(name: str, namespace: str, max_retries: int = 3):
    """
    Delete the Deployment for a server.

    Raises ResourceDeletionFailedException if the Deployment cannot be deleted.
    """
    async with async_kube_api() as (apps, _, _, _, _):
        deployment_deleted = await check_and_delete(
            query_func=apps.list_namespaced_deployment,
            delete_func=apps.delete_namespaced_deployment,
            resource_name=name,
            resource_type="deployment",
            namespace=namespace,
            max_retries=max_retries,
        )
        if not deployment_deleted:
            raise ResourceDeletionFailedException(f"Failed to clean up deployment: {name}")


async def restart_pods(name: str, namespace: str, wait_timeout: int = 60, max_retries: int = 3):
    """
    Restart pods for a deployment by deleting them individually and waiting for replacements.

    The Deployment and Service are left intact; only the pods are deleted so Kubernetes
    recreates them from the existing Deployment spec.
    """
    try:
        async with async_kube_api() as (_apps, _, core, _, _):
            label_selector = f"app={name}"
            pods = (await core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)).items

            if not pods:
                logger.warning(f"No pods found for deployment '{name}' in namespace '{namespace}'")
                return

            for pod in pods:
                pod_name = pod.metadata.name
                logger.info(f"Deleting pod '{pod_name}' in namespace '{namespace}'")
                await delete_with_retries(core.delete_namespaced_pod, "pod", pod_name, namespace, max_retries)

            await wait_for_pods_ready(label_selector=label_selector, namespace=namespace, wait_timeout=wait_timeout)

        logger.info(f"Successfully restarted pods for deployment '{name}' in namespace '{namespace}'")

    except Exception:
        logger.exception(f"Error restarting pods for deployment '{name}'")
        raise


async def build_and_push_image_with_kaniko(
    tag: str,
    service_version: str,
    dockerfile_content: str,
    namespace: str,
    request: Optional[DownloadRequest] = None,
    labels: Optional[dict[str, str]] = None,
    ttl_seconds_after_finished: int = 300,
    runtime_class_name: Optional[str] = None,
    resources: Optional[V1ResourceRequirements | dict[str, Any]] = None,
    insecure_registry: bool = False,
    node_pool_taint_key: Optional[str] = None,
    node_pool_preference_weight: int = 100,
    secret_build_args: Optional[list[str]] = None,
    context: Optional[str] = None,
) -> str:
    """
    Build a Docker image using Kaniko in a Kubernetes Job and push it to a registry.

    The Dockerfile is delivered via a ConfigMap mounted at /workspace. When `request` is
    provided, the archive URL and auth credentials are passed as both build args and
    container env vars. The ConfigMap and a PodDisruptionBudget are created as children of
    the Job (owner references) so they are garbage-collected automatically.

    `context` is the Kaniko build context. It defaults to `dir:///workspace` (only the mounted
    Dockerfile), which is all a download/inline-based build needs. Images whose Dockerfile
    `COPY`s files from the idegym repo (e.g. the idea/pycharm plugins) pass a git context such
    as `git://github.com/JetBrains-Research/idegym.git#refs/tags/v1.2.3`; Kaniko still reads the
    generated Dockerfile from the absolute `/workspace/Dockerfile` mount, so the `COPY` paths
    resolve against the checkout.

    When `insecure_registry` is True the regcred secret volume is omitted and --insecure is
    passed to Kaniko, which allows pushing to plain-HTTP registries (e.g. in-cluster registries
    used during tests).

    Returns the Job name.
    """
    name = f"kaniko-build-{getrandbits(32):08x}"
    args = [
        "--dockerfile=/workspace/Dockerfile",
        f"--destination={tag}",
        f"--context={context or 'dir:///workspace'}",
    ]

    if request is not None:
        args.extend(
            [
                f"--build-arg=IDEGYM_PROJECT_ARCHIVE_URL={request.descriptor.url}",
                f"--build-arg=IDEGYM_PROJECT_ARCHIVE_PATH={request.descriptor.name}",
            ]
        )
        if request.auth.type is not None:
            args.append(f"--build-arg=IDEGYM_AUTH_TYPE={request.auth.type}")
        if request.auth.token is not None:
            args.append(f"--build-arg=IDEGYM_AUTH_TOKEN={request.auth.token}")

    # Forward build-time secrets (e.g. private-plugin tokens) from the orchestrator's
    # environment as Kaniko build args. Only the names travel in the spec; values are
    # resolved here so they never reach the ConfigMap-mounted Dockerfile or an image layer.
    # Not added to the container env — Kaniko substitutes ARGs from --build-arg, so a second
    # copy would only widen the plaintext footprint in the Job spec. A distinct loop
    # variable avoids clobbering the ``name`` used below for the Job/ConfigMap/PDB names.
    for arg_name in secret_build_args or []:
        arg_value = env.get(arg_name)
        if arg_value:
            args.append(f"--build-arg={arg_name}={arg_value}")

    if insecure_registry:
        args.append("--insecure")

    if labels:
        for key, value in labels.items():
            args.append(f"--label={key}={value}")

    if isinstance(resources, dict):  # noinspection PyUnnecessaryCast
        dictionary = cast(dict, resources)
        resources = V1ResourceRequirements(**dictionary)

    annotations = {
        "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
    }
    match_labels = {
        "app": name,
        "app.kubernetes.io/component": "image-builder",
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/part-of": "idegym",
    }
    labels = {
        **match_labels,
        "app.kubernetes.io/version": __version__,
    }

    configmap = V1ConfigMap(
        metadata=V1ObjectMeta(
            name=name,
            labels=labels,
        ),
        data={
            "Dockerfile": dockerfile_content,
        },
    )

    container_env = [V1EnvVar(name="IDEGYM_VERSION", value=service_version)]
    if request is not None:
        container_env.extend(
            [
                V1EnvVar(name="IDEGYM_PROJECT_ARCHIVE_URL", value=request.descriptor.url),
                V1EnvVar(name="IDEGYM_PROJECT_ARCHIVE_PATH", value=request.descriptor.name),
                V1EnvVar(name="IDEGYM_AUTH_TYPE", value=request.auth.type),
                V1EnvVar(name="IDEGYM_AUTH_TOKEN", value=request.auth.token),
            ]
        )

    container = V1Container(
        name="kaniko",
        image="gcr.io/kaniko-project/executor:v1.24.0",
        args=args,
        env=container_env,
        volume_mounts=[
            V1VolumeMount(
                name="dockerfile-volume",
                mount_path="/workspace",
            ),
        ]
        + (
            [
                V1VolumeMount(
                    name="docker-config",
                    mount_path="/kaniko/.docker",
                ),
            ]
            if not insecure_registry
            else []
        ),
        security_context=V1SecurityContext(run_as_user=0),
        resources=resources,
    )

    toleration = (
        V1Toleration(
            key=node_pool_taint_key,
            operator="Exists",
            effect="NoSchedule",
        )
        if node_pool_taint_key
        else None
    )

    affinity = (
        build_node_pool_affinity(
            taint_key=node_pool_taint_key,
            preference_weight=node_pool_preference_weight,
        )
        if node_pool_taint_key
        else None
    )

    job = V1Job(
        api_version="batch/v1",
        kind="Job",
        metadata=V1ObjectMeta(
            name=name,
            labels=labels,
        ),
        spec=V1JobSpec(
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(
                    annotations=annotations,
                    labels=labels,
                ),
                spec=V1PodSpec(
                    containers=[container],
                    restart_policy="Never",
                    volumes=[
                        V1Volume(
                            name="dockerfile-volume",
                            config_map=V1ConfigMapVolumeSource(
                                name=configmap.metadata.name,
                            ),
                        ),
                    ]
                    + (
                        [
                            V1Volume(
                                name="docker-config",
                                secret=V1SecretVolumeSource(
                                    secret_name="regcred",
                                    items=[
                                        V1KeyToPath(
                                            key=".dockerconfigjson",
                                            path="config.json",
                                        ),
                                    ],
                                ),
                            ),
                        ]
                        if not insecure_registry
                        else []
                    ),
                    runtime_class_name=runtime_class_name,
                    tolerations=[toleration] if toleration else None,
                    affinity=affinity,
                ),
            ),
            backoff_limit=0,
            ttl_seconds_after_finished=ttl_seconds_after_finished,
        ),
    )

    pdb = V1PodDisruptionBudget(
        api_version="policy/v1",
        kind="PodDisruptionBudget",
        metadata=V1ObjectMeta(
            name=name,
            labels=labels,
        ),
        spec=V1PodDisruptionBudgetSpec(
            min_available=1,
            selector=V1LabelSelector(
                match_labels=match_labels,
            ),
        ),
    )

    async with async_kube_api() as (_, batch, core, policy, _):
        job = await batch.create_namespaced_job(
            body=job,
            namespace=namespace,
        )

        owner_reference = V1OwnerReference(
            api_version=job.api_version,
            kind=job.kind,
            name=job.metadata.name,
            uid=job.metadata.uid,
        )
        configmap.metadata.owner_references = [owner_reference]
        pdb.metadata.owner_references = [owner_reference]

        await gather(
            core.create_namespaced_config_map(
                body=configmap,
                namespace=namespace,
            ),
            policy.create_namespaced_pod_disruption_budget(
                body=pdb,
                namespace=namespace,
            ),
        )
        return name


async def get_job_status(job_name: str, namespace: str) -> Status:
    """Return SUCCESS, FAILURE, or IN_PROGRESS for a Kubernetes Job. Returns FAILURE on API errors."""
    try:
        async with async_kube_api() as (_, batch, _, _, _):
            job = await batch.read_namespaced_job(name=job_name, namespace=namespace)

        if job.status.succeeded is not None and job.status.succeeded > 0:
            return Status.SUCCESS

        if job.status.failed is not None and job.status.failed > 0:
            return Status.FAILURE

        return Status.IN_PROGRESS
    except Exception as e:  # noqa: BLE001  # report job FAILURE on any error
        logger.error(f"Error getting job status: {e}")
        return Status.FAILURE
