from asyncio import CancelledError, gather, sleep, timeout
from contextlib import asynccontextmanager
from random import getrandbits
from typing import Any, AsyncGenerator, Awaitable, Callable, Iterable, Optional, Union, cast

from idegym.api import __version__
from idegym.api.download import DownloadRequest
from idegym.api.exceptions import ResourceDeletionFailedException
from idegym.api.orchestrator.servers import ServerKind
from idegym.api.paths import API_BASE_PATH, ActuatorPath, OpenenvPath
from idegym.api.status import Status
from idegym.utils.functools import cached_async_result
from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import (
    ApiClient,
    ApiException,
    AppsV1Api,
    BatchV1Api,
    Configuration,
    CoreV1Api,
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

KubernetesV1Apis = tuple[AppsV1Api, BatchV1Api, CoreV1Api, PolicyV1Api]

V1ResourceList = Union[V1ConfigMapList, V1DeploymentList, V1PodDisruptionBudgetList, V1ServiceList]

logger = get_logger(__name__)


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

    if "secretKeyRef" in value_from and value_from["secretKeyRef"]:
        secret_key_ref = value_from["secretKeyRef"]
        kwargs["secret_key_ref"] = V1SecretKeySelector(
            name=secret_key_ref.get("name"),
            key=secret_key_ref.get("key"),
            optional=secret_key_ref.get("optional"),
        )

    if "configMapKeyRef" in value_from and value_from["configMapKeyRef"]:
        config_map_key_ref = value_from["configMapKeyRef"]
        kwargs["config_map_key_ref"] = V1ConfigMapKeySelector(
            name=config_map_key_ref.get("name"),
            key=config_map_key_ref.get("key"),
            optional=config_map_key_ref.get("optional"),
        )

    if "fieldRef" in value_from and value_from["fieldRef"]:
        field_ref = value_from["fieldRef"]
        kwargs["field_ref"] = V1ObjectFieldSelector(
            field_path=field_ref.get("fieldPath"),
            api_version=field_ref.get("apiVersion"),
        )

    if "resourceFieldRef" in value_from and value_from["resourceFieldRef"]:
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
    runtime_class_name: Optional[str] = None,
    run_as_root: bool = False,
    node_selector: Optional[dict[str, str]] = None,
    node_pool_taint_key: Optional[str] = None,
    node_pool_preference_weight: int = 100,
    resources: Optional[Union[V1ResourceRequirements, dict[str, Any]]] = None,
    environment_variables: Iterable[Union[V1EnvVar, dict[str, Any]]] = (),
    server_kind: ServerKind = ServerKind.IDEGYM,
):
    """
    Create a Kubernetes Deployment, Service, and PodDisruptionBudget for a server.

    The Service and PDB are created with the Deployment as their owner reference so
    they are garbage-collected when the Deployment is deleted.
    """
    logger.debug(f"Deploying '{server_name}' in namespace '{namespace}' with runtime class '{runtime_class_name}'.")

    uid = 0 if run_as_root else 1000
    security_context = V1SecurityContext(
        run_as_non_root=not run_as_root,
        run_as_user=uid,
        run_as_group=uid,
    )

    if isinstance(resources, dict):  # noinspection PyUnnecessaryCast
        dictionary = cast(dict, resources)
        resources = V1ResourceRequirements(**dictionary)

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
    )

    image_pull_secret = V1LocalObjectReference(name="regcred")
    annotations = {
        "cluster-autoscaler.kubernetes.io/safe-to-evict": "false",
        **prometheus_annotations,
    }
    match_labels = {
        "app": server_name,
        "app.kubernetes.io/component": "sandbox",
        "app.kubernetes.io/name": server_name,
        "app.kubernetes.io/part-of": "idegym",
    }
    labels = {
        **match_labels,
        "app.kubernetes.io/version": __version__,
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

    deployment = V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=V1ObjectMeta(
            name=server_name,
            labels=labels,
        ),
        spec=V1DeploymentSpec(
            replicas=1,
            selector=V1LabelSelector(
                match_labels=match_labels,
            ),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(
                    annotations=annotations,
                    labels=labels,
                ),
                spec=V1PodSpec(
                    containers=[container],
                    image_pull_secrets=[image_pull_secret],
                    runtime_class_name=runtime_class_name,
                    node_selector=node_selector,
                    tolerations=[toleration] if toleration else None,
                    affinity=affinity,
                ),
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

    async with async_kube_api() as (apps, _, core, policy):
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
                    raise Exception(
                        f"Failed to start pods: Unschedulable condition detected {consecutive_unschedulable} times in a row"
                    )
            else:
                consecutive_unschedulable = 0

            if has_image_pull_error:
                consecutive_image_pull_errors += 1
                logger.warning(f"Image pull error detected ({consecutive_image_pull_errors}/{max_image_pull_attempts})")

                if consecutive_image_pull_errors >= max_image_pull_attempts:
                    raise Exception(
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

    async with async_kube_api() as (_, _, core, _):
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
                        and condition.status == "False"
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


async def are_any_pods_alive(label_selector: str, namespace: str) -> bool:
    """Return True if at least one non-terminating Running pod matches the selector."""

    async with async_kube_api() as (_, _, core, _):
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
                    f"{ex.__class__.__name__}: {str(ex)}. "
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
                    f"{ex.__class__.__name__}: {str(ex)}. "
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
    Delete the Deployment for a server (Service and PDB are garbage-collected via owner references).

    Raises ResourceDeletionFailedException if the Deployment cannot be deleted.
    """
    async with async_kube_api() as (apps, _, _, _):
        if not await check_and_delete(
            query_func=apps.list_namespaced_deployment,
            delete_func=apps.delete_namespaced_deployment,
            resource_name=name,
            resource_type="deployment",
            namespace=namespace,
            max_retries=max_retries,
        ):
            raise ResourceDeletionFailedException(f"Failed to clean up deployment: {name}")


async def restart_pods(name: str, namespace: str, wait_timeout: int = 60, max_retries: int = 3):
    """
    Restart pods for a deployment by deleting them individually and waiting for replacements.

    The Deployment and Service are left intact; only the pods are deleted so Kubernetes
    recreates them from the existing Deployment spec.
    """
    try:
        async with async_kube_api() as (apps, _, core, _):
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
    resources: Optional[Union[V1ResourceRequirements, dict[str, Any]]] = None,
    insecure_registry: bool = False,
    node_pool_taint_key: Optional[str] = None,
    node_pool_preference_weight: int = 100,
) -> str:
    """
    Build a Docker image using Kaniko in a Kubernetes Job and push it to a registry.

    The Dockerfile is delivered via a ConfigMap mounted at /workspace. When `request` is
    provided, the archive URL and auth credentials are passed as both build args and
    container env vars. The ConfigMap and a PodDisruptionBudget are created as children of
    the Job (owner references) so they are garbage-collected automatically.

    When `insecure_registry` is True the regcred secret volume is omitted and --insecure is
    passed to Kaniko, which allows pushing to plain-HTTP registries (e.g. in-cluster registries
    used during tests).

    Returns the Job name.
    """
    name = f"kaniko-build-{getrandbits(32):08x}"
    args = [
        "--dockerfile=/workspace/Dockerfile",
        f"--destination={tag}",
        "--context=dir:///workspace",
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

    async with async_kube_api() as (_, batch, core, policy):
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
        async with async_kube_api() as (_, batch, _, _):
            job = await batch.read_namespaced_job(name=job_name, namespace=namespace)

        if job.status.succeeded is not None and job.status.succeeded > 0:
            return Status.SUCCESS

        if job.status.failed is not None and job.status.failed > 0:
            return Status.FAILURE

        return Status.IN_PROGRESS
    except Exception as e:
        logger.error(f"Error getting job status: {e}")
        return Status.FAILURE
