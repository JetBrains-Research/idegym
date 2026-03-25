from asyncio import CancelledError, gather, sleep, timeout
from contextlib import asynccontextmanager
from random import getrandbits
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, Iterable, Optional, Tuple, Union

from idegym.api import __version__
from idegym.api.download import DownloadRequest
from idegym.api.exceptions import ResourceDeletionFailedException
from idegym.api.paths import API_BASE_PATH, ActuatorPath
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
    V1ConfigMap,
    V1ConfigMapKeySelector,
    V1ConfigMapList,
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
    V1LabelSelector,
    V1LocalObjectReference,
    V1ObjectFieldSelector,
    V1ObjectMeta,
    V1PodDisruptionBudget,
    V1PodDisruptionBudgetList,
    V1PodDisruptionBudgetSpec,
    V1PodSpec,
    V1PodTemplateSpec,
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
    V1Volume,
    V1VolumeMount,
)
from kubernetes_asyncio.config import (
    ConfigException,
    load_incluster_config,
    load_kube_config,
)

KubernetesV1Apis = Tuple[AppsV1Api, BatchV1Api, CoreV1Api, PolicyV1Api]

V1ResourceList = Union[V1ConfigMapList, V1DeploymentList, V1PodDisruptionBudgetList, V1ServiceList]

logger = get_logger(__name__)


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
    """
    Load all Kubernetes API clients.

    Returns:
        Tuple of AppsV1Api, BatchV1Api, CoreV1Api, and PolicyV1Api clients
    """
    yield await create_clients()


def to_env_var(dictionary: Dict[str, Any]) -> V1EnvVar:
    name: Optional[str] = dictionary.get("name")
    value: Optional[str] = dictionary.get("value")
    value_from: Optional[Dict[str, Any]] = dictionary.get("valueFrom")

    if not value_from:
        return V1EnvVar(
            name=name,
            value=value,
        )

    kwargs: Dict[str, Any] = {}

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
    """Initialize the Kubernetes configurations."""
    try:
        load_incluster_config()
        logger.info("Loaded in-cluster Kubernetes configuration!")
        return
    except ConfigException:
        pass  # In-cluster config wasn't found, try local instead...

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
    container_name: str = "server",
    service_port: int = 80,
    container_port: int = 8000,
    replicas_count: int = 1,
    runtime_class_name: Optional[str] = None,
    run_as_root: bool = False,
    node_selector: Optional[Dict[str, str]] = None,
    resources: Union[V1ResourceRequirements, Dict[str, Any], None] = None,
    environment_variables: Iterable[Union[V1EnvVar, Dict[str, Any]]] = (),
):
    """
    Deploy a server in Kubernetes.

    Args:
        image_tag: Docker image tag
        server_name: Name for the server
        container_name: Name for the container
        service_port: Service port
        container_port: Container port
        namespace: Kubernetes namespace
        replicas_count: Number of replicas
        runtime_class_name: Kubernetes runtime class name
        run_as_root: Run container as root
        resources: Kubernetes resource requirements (can be a V1ResourceRequirements object or a dictionary)
        environment_variables: Environment variables to set in the container (can be a V1EnvVar object or a dictionary)
    """
    logger.debug(f"Deploying '{server_name}' in namespace '{namespace}' with runtime class '{runtime_class_name}'.")

    container_ports = [V1ContainerPort(container_port=container_port)]
    readiness_probe = V1Probe(
        http_get=V1HTTPGetAction(path=API_BASE_PATH + ActuatorPath.HEALTH, port=container_port),
        initial_delay_seconds=10,
        period_seconds=3,
    )

    if run_as_root:
        security_context = V1SecurityContext(run_as_user=0)
    else:
        security_context = V1SecurityContext(run_as_non_root=True, run_as_user=1000, run_as_group=1000)

    # Convert dictionary resources to V1ResourceRequirements if needed
    if resources and isinstance(resources, dict):
        resources = V1ResourceRequirements(**resources)

    # Convert environment variables to V1EnvVar objects if needed
    env_vars = [
        environment_variable if isinstance(environment_variable, V1EnvVar) else to_env_var(environment_variable)
        for environment_variable in environment_variables
    ]

    container = V1Container(
        name=container_name,
        image=image_tag,
        image_pull_policy="IfNotPresent",
        ports=container_ports,
        readiness_probe=readiness_probe,
        security_context=security_context,
        resources=resources,
        env=[*env_vars],
    )

    image_pull_secrets = [V1LocalObjectReference(name="regcred")]
    pod_spec = V1PodSpec(
        containers=[container],
        image_pull_secrets=image_pull_secrets,
        runtime_class_name=runtime_class_name,
        node_selector=node_selector,
    )

    deployment_metadata = V1ObjectMeta(name=server_name)
    template_metadata = V1ObjectMeta(
        annotations={
            "prometheus.io/scrape": "true",
            "prometheus.io/path": API_BASE_PATH + ActuatorPath.METRICS,
            "prometheus.io/port": str(container_port),
            "prometheus.io/scheme": "http|https",
        },
        labels={
            "app": server_name,
        },
    )
    label_selector = V1LabelSelector(match_labels={"app": server_name})

    template_spec = V1PodTemplateSpec(metadata=template_metadata, spec=pod_spec)

    deployment_spec = V1DeploymentSpec(replicas=replicas_count, selector=label_selector, template=template_spec)

    deployment = V1Deployment(
        api_version="apps/v1", kind="Deployment", metadata=deployment_metadata, spec=deployment_spec
    )

    service_metadata = V1ObjectMeta(name=f"{server_name}-service")
    service_port_spec = V1ServicePort(protocol="TCP", port=service_port, target_port=container_port)

    service_spec = V1ServiceSpec(selector={"app": server_name}, ports=[service_port_spec], type="ClusterIP")

    service = V1Service(api_version="v1", kind="Service", metadata=service_metadata, spec=service_spec)

    # Create PodDisruptionBudget
    pdb_metadata = V1ObjectMeta(name=f"{server_name}-pdb")
    pdb_spec = V1PodDisruptionBudgetSpec(min_available=1, selector=V1LabelSelector(match_labels={"app": server_name}))
    pdb = V1PodDisruptionBudget(
        api_version="policy/v1", kind="PodDisruptionBudget", metadata=pdb_metadata, spec=pdb_spec
    )

    async with async_kube_api() as (apps, _, core, policy):
        # Create resources
        await core.create_namespaced_service(namespace=namespace, body=service)
        await apps.create_namespaced_deployment(namespace=namespace, body=deployment)
        await policy.create_namespaced_pod_disruption_budget(namespace=namespace, body=pdb)


async def wait_for_pods_ready(
    label_selector: str, namespace: str, wait_timeout: int = 60, max_image_pull_attempts: int = 3
):
    """
    Wait for pods to be ready.

    Will fail fast if image pull errors are detected 3 times in a row.

    Args:
        label_selector: Label selector for pods
        namespace: Kubernetes namespace
        wait_timeout: Timeout in seconds
        max_image_pull_attempts: How many consecutive image pull errors are allowed before failing fast

    Raises:
        Exception: If image pull errors are detected max_image_pull_attempts times in a row
    """
    consecutive_image_pull_errors = 0

    async with timeout(wait_timeout):
        while True:
            pods_ready, has_image_pull_error, has_terminating_pods = await pods_are_ready(label_selector, namespace)

            if pods_ready and not has_terminating_pods:
                logger.info(f"Pods with label '{label_selector}' are ready and stable.")
                return

            if has_image_pull_error:
                consecutive_image_pull_errors += 1
                logger.warning(f"Image pull error detected ({consecutive_image_pull_errors}/{max_image_pull_attempts})")

                if consecutive_image_pull_errors >= max_image_pull_attempts:
                    raise Exception(
                        f"Failed to start pods: Image pull errors detected {max_image_pull_attempts} times in a row"
                    )
            else:
                # Reset counter if no image pull errors
                consecutive_image_pull_errors = 0

            await sleep(2)


async def pods_are_ready(label_selector: str, namespace: str) -> tuple[bool, bool, bool]:
    """
    Check if pods are ready.

    Args:
        label_selector: Label selector for pods
        namespace: Kubernetes namespace

    Returns:
        Tuple of (pods_ready, has_image_pull_error):
        - pods_ready: True if all pods are ready, False otherwise
        - has_image_pull_error: True if any pod has an image pull error, False otherwise
    """

    async with async_kube_api() as (_, _, core, _):
        pods = (await core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)).items

    has_image_pull_error = False
    has_terminating_pods = False

    if len(pods) > 0:
        for pod in pods:
            if pod.metadata.deletion_timestamp is not None:
                has_terminating_pods = True
                logger.debug(f"Pod {pod.metadata.name} is terminating")
                continue

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

    return pods_ready, has_image_pull_error, has_terminating_pods


async def are_any_pods_alive(label_selector: str, namespace: str) -> bool:
    """
    Check if any pods are alive and keeping resources.

    Args:
        label_selector: Label selector for pods
        namespace: Kubernetes namespace

    Returns:
        If there are any pods that are alive, return True. Otherwise, return False.
    """

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
    Generic function to delete a Kubernetes resource with retries and exponential backoff.

    Args:
        delete_func: The function to call to delete the resource
        resource_type: Type of resource (for logging)
        resource_name: Name of the resource
        namespace: Kubernetes namespace
        max_retries: Maximum number of retry attempts

    Returns:
        bool: True if deletion was successful, False otherwise
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
                backoff = 2**attempt  # Exponential backoff
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
    Generic function to query a Kubernetes resource with retries and exponential backoff.

    Args:
        query_func: The function to call to query the resource
        resource_name: Name of the resource
        resource_type: Type of resource (for logging)
        namespace: Kubernetes namespace
        max_retries: Maximum number of retry attempts

    Returns:
        bool: True if the resource exists, False otherwise
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
                backoff = 2**attempt  # Exponential backoff
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
    """
    Checks if a resource exists and deletes it if it does.

    Args:
        query_func: The function to call to query the resource
        delete_func: The function to call to delete the resource
        resource_name: Name of the resource
        resource_type: Type of resource (for logging)
        namespace: Kubernetes namespace
        max_retries: Maximum number of retry attempts for each operation

    Returns:
        bool: True if deletion was successful, False otherwise
    """
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
    Clean up a created server: delete the deployment, service, and pod disruption budget.

    Args:
        name: Name of the server
        namespace: Kubernetes namespace
        max_retries: Maximum number of retry attempts for each operation
    """
    async with async_kube_api() as (apps, _, core, policy):
        delete_pdb = check_and_delete(
            query_func=policy.list_namespaced_pod_disruption_budget,
            delete_func=policy.delete_namespaced_pod_disruption_budget,
            resource_name=f"{name}-pdb",
            resource_type="pod disruption budget",
            namespace=namespace,
            max_retries=max_retries,
        )

        delete_service = check_and_delete(
            query_func=core.list_namespaced_service,
            delete_func=core.delete_namespaced_service,
            resource_name=f"{name}-service",
            resource_type="service",
            namespace=namespace,
            max_retries=max_retries,
        )

        delete_deployment = check_and_delete(
            query_func=apps.list_namespaced_deployment,
            delete_func=apps.delete_namespaced_deployment,
            resource_name=name,
            resource_type="deployment",
            namespace=namespace,
            max_retries=max_retries,
        )

        pdb_deleted, service_deleted, deployment_deleted = await gather(
            delete_pdb,
            delete_service,
            delete_deployment,
        )

    # Report final status
    failures = []
    if not deployment_deleted:
        failures.append(f"deployment '{name}'")
    if not service_deleted:
        failures.append(f"service '{name}-service'")
    if not pdb_deleted:
        failures.append(f"pod disruption budget '{name}-pdb'")

    if len(failures) > 0:
        raise ResourceDeletionFailedException(failures)


async def restart_pods(name: str, namespace: str, wait_timeout: int = 60, max_retries: int = 3):
    """
    Restart pods associated with a deployment without deleting the service or deployment.

    This deletes each pod individually and waits for Kubernetes to create new pods,
    maintaining the same service and deployment configuration.

    Args:
        name: Name of the deployment
        namespace: Kubernetes namespace
        wait_timeout: Timeout in seconds for waiting for pods to be ready
        max_retries: Maximum number of retry attempts for each pod deletion
    """
    try:
        async with async_kube_api() as (apps, _, core, _):
            # Get pods with the label selector app=name (matching the deployment's selector)
            label_selector = f"app={name}"
            pods = (await core.list_namespaced_pod(namespace=namespace, label_selector=label_selector)).items

            if not pods:
                logger.warning(f"No pods found for deployment '{name}' in namespace '{namespace}'")
                return

            # Delete each pod individually with retries
            for pod in pods:
                pod_name = pod.metadata.name
                logger.info(f"Deleting pod '{pod_name}' in namespace '{namespace}'")
                await delete_with_retries(core.delete_namespaced_pod, "pod", pod_name, namespace, max_retries)

            # Wait for new pods to be ready using wait_for_pods_ready
            await wait_for_pods_ready(label_selector=label_selector, namespace=namespace, wait_timeout=wait_timeout)

        logger.info(f"Successfully restarted pods for deployment '{name}' in namespace '{namespace}'")

    except Exception:
        logger.exception(f"Error restarting pods for deployment '{name}'")
        raise


async def build_and_push_image_with_kaniko(
    request: DownloadRequest,
    tag: str,
    base: str,
    service_version: str,
    dockerfile_content: str,
    namespace: str,
    labels: Optional[Dict[str, str]] = None,
    ttl_seconds_after_finished: int = 300,
    runtime_class_name: Optional[str] = None,
    resources: Optional[Any] = None,
) -> str:
    """
    Build a Docker image using kaniko in a Kubernetes job.

    Args:
        request: Download request for the project
        tag: The full image tag to use for the built image
        base: Base image to use for the build
        service_version: Version of the service
        dockerfile_content: Content of the Dockerfile template
        labels: Labels to add to the image
        namespace: Kubernetes namespace
        ttl_seconds_after_finished: Time in seconds to automatically delete the job after it finishes (default: 300)
        runtime_class_name: Kubernetes runtime class name (e.g., "gvisor") to use for the Kaniko pod
        resources: Kubernetes resource requirements (can be a V1ResourceRequirements object or a dictionary)

    Returns:
        The job name
    """
    name = f"kaniko-build-{getrandbits(32):08x}"  # Generate a unique job name
    args = [
        "--dockerfile=/workspace/Dockerfile",
        f"--destination={tag}",
        "--context=dir:///workspace",
        f"--build-arg=IDEGYM_BASE={base}",
        f"--build-arg=IDEGYM_VERSION={service_version}",
        f"--build-arg=IDEGYM_PROJECT_ARCHIVE_URL={request.descriptor.url}",
        f"--build-arg=IDEGYM_PROJECT_ARCHIVE_PATH={request.descriptor.name}",
    ]

    if request.auth.type is not None:
        args.append(f"--build-arg=IDEGYM_AUTH_TYPE={request.auth.type}")
    if request.auth.token is not None:
        args.append(f"--build-arg=IDEGYM_AUTH_TOKEN={request.auth.token}")

    if labels:
        for key, value in labels.items():
            args.append(f"--label={key}={value}")

    if resources and isinstance(resources, dict):
        resources = V1ResourceRequirements(**resources)
    labels = {
        "app": name,
        "app.kubernetes.io/component": "image-builder",
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/version": __version__,
        "app.kubernetes.io/managed-by": "idegym-orchestrator",
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

    container = V1Container(
        name="kaniko",
        image="gcr.io/kaniko-project/executor:v1.24.0",
        args=args,
        env=[
            V1EnvVar(
                name="IDEGYM_VERSION",
                value=service_version,
            ),
            V1EnvVar(
                name="IDEGYM_PROJECT_ARCHIVE_URL",
                value=request.descriptor.url,
            ),
            V1EnvVar(
                name="IDEGYM_PROJECT_ARCHIVE_PATH",
                value=request.descriptor.name,
            ),
            V1EnvVar(
                name="IDEGYM_AUTH_TYPE",
                value=request.auth.type,
            ),
            V1EnvVar(
                name="IDEGYM_AUTH_TOKEN",
                value=request.auth.token,
            ),
        ],
        volume_mounts=[
            V1VolumeMount(
                name="dockerfile-volume",
                mount_path="/workspace",
            ),
            V1VolumeMount(
                name="docker-config",
                mount_path="/kaniko/.docker",
            ),
        ],
        security_context=V1SecurityContext(run_as_user=0),
        resources=resources,
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
                    labels=labels,
                ),
                spec=V1PodSpec(
                    containers=[container],
                    restart_policy="Never",
                    volumes=[
                        V1Volume(
                            name="dockerfile-volume",
                            config_map={
                                "name": configmap.metadata.name,
                            },
                        ),
                        V1Volume(
                            name="docker-config",
                            secret=V1SecretVolumeSource(
                                secret_name="regcred",
                                items=[
                                    {
                                        "key": ".dockerconfigjson",
                                        "path": "config.json",
                                    },
                                ],
                            ),
                        ),
                    ],
                    runtime_class_name=runtime_class_name,
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
                match_labels=labels,
            ),
        ),
    )

    async with async_kube_api() as (_, batch, core, policy):
        await core.create_namespaced_config_map(namespace=namespace, body=configmap)
        await batch.create_namespaced_job(namespace=namespace, body=job)
        await policy.create_namespaced_pod_disruption_budget(namespace=namespace, body=pdb)

    return name


async def get_job_status(job_name: str, namespace: str) -> Status:
    """
    Get the status of a Kubernetes job.

    Args:
        job_name: Name of the job
        namespace: Kubernetes namespace

    Returns:
        Status enum value representing the job status
    """
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


async def clean_up_after_job(
    name: str,
    namespace: str,
    max_retries: int = 3,
):
    """
    Clean up resources created for a Kubernetes job.

    This function deletes the ConfigMap and PodDisruptionBudget that were created for the job.
    The ConfigMap is named with the pattern "{job_name}-dockerfile".
    The PodDisruptionBudget is named with the pattern "{job_name}-pdb".

    Args:
        name: Name of the job
        namespace: Kubernetes namespace where the job, ConfigMap, and PDB were created
        max_retries: Maximum number of retry attempts for resource deletion
    """
    async with async_kube_api() as (_, _, core, policy):
        delete_pdb = check_and_delete(
            query_func=policy.list_namespaced_pod_disruption_budget,
            delete_func=policy.delete_namespaced_pod_disruption_budget,
            resource_name=f"{name}-pdb",
            resource_type="pod disruption budget",
            namespace=namespace,
            max_retries=max_retries,
        )

        delete_config_map = check_and_delete(
            query_func=core.list_namespaced_config_map,
            delete_func=core.delete_namespaced_config_map,
            resource_name=f"{name}-dockerfile",
            resource_type="config map",
            namespace=namespace,
            max_retries=max_retries,
        )

        pdb_deleted, config_map_deleted = await gather(delete_pdb, delete_config_map)

        if not pdb_deleted:
            logger.warning(f"Failed to delete pod disruption budget '{name}-pdb'")
            # We don't raise an exception here as this is a cleanup operation
            # and failure shouldn't stop the main workflow

        if not config_map_deleted:
            logger.warning(f"Failed to delete config map '{name}-dockerfile'")
            # We don't raise an exception here as this is a cleanup operation
            # and failure shouldn't stop the main workflow
