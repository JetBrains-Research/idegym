from hashlib import md5
from http import HTTPStatus
from typing import Awaitable, Callable, Optional, TypeVar
from uuid import UUID

from idegym.api import __version__
from idegym.backend.utils.kubernetes_client import async_kube_api, delete_with_retries, wait_for_pods_ready
from idegym.orchestrator.database.helpers import need_to_release_nodes_for_client
from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import (
    ApiException,
    V1Affinity,
    V1Container,
    V1Deployment,
    V1DeploymentSpec,
    V1LabelSelector,
    V1LabelSelectorRequirement,
    V1ObjectMeta,
    V1OwnerReference,
    V1PodAffinityTerm,
    V1PodAntiAffinity,
    V1PodDisruptionBudget,
    V1PodDisruptionBudgetSpec,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
)

T = TypeVar("T", V1Deployment, V1PodDisruptionBudget)

logger = get_logger(__name__)

component = "node-holder"
"""Name of the component. Value is used as a common, multi-purpose label or prefix for operations."""


async def create_or_patch_resource(
    create_resource_method: Callable[..., Awaitable[T]],
    patch_resource_method: Callable[..., Awaitable[T]],
    resource_name: str,
    namespace: str,
    body: T,
    resource_type: str,
) -> T:
    """Helper function to create or patch a Kubernetes resource."""
    try:
        resource = await create_resource_method(
            namespace=namespace,
            body=body,
        )
        logger.info(f"Created {resource_type} {resource_name} in namespace {namespace}")
        return resource
    except ApiException as ex:
        if ex.status == HTTPStatus.CONFLICT:
            pass  # Resource already exists, try patching it instead
        else:
            logger.exception(f"Failed to create {resource_type} {resource_name}")
            raise

    try:
        resource = await patch_resource_method(
            name=resource_name,
            namespace=namespace,
            body=body,
        )
        logger.info(f"Patched existing {resource_type} {resource_name} in namespace {namespace}")
        return resource
    except ApiException:
        logger.exception(f"Failed to patch {resource_type} {resource_name}")
        raise


async def spin_up_or_update_nodes_for_client(
    client_name: str,
    nodes_count: int,
    namespace: str,
    runtime_class_name: Optional[str] = None,
    wait_timeout: int = 600,
):
    """
    Spin up a set of nodes for a client.

    This creates a deployment with pods that are scheduled on different nodes
    using podAntiAffinity and a PodDisruptionBudget to ensure they are not killed.
    """
    if nodes_count <= 0:
        logger.debug(f"No nodes requested for client {client_name}, skipping.")
        return

    logger.info(f"Spinning up {nodes_count} nodes for client {client_name} in namespace {namespace}")

    # Create a unique name for the deployment based on client ID
    client_hash = md5(client_name.encode()).hexdigest()
    name = f"{component}-{client_hash}"
    match_labels = {
        "app": name,
        "app.kubernetes.io/component": component,
        "app.kubernetes.io/name": name,
        "app.kubernetes.io/part-of": "idegym",
    }
    labels = {
        **match_labels,
        "app.kubernetes.io/version": __version__,
    }

    container = V1Container(
        name="sleeper",
        image="registry.k8s.io/pause:3.10.1",
        image_pull_policy="IfNotPresent",
        resources=V1ResourceRequirements(
            limits={
                "cpu": "1m",
                "memory": "5Mi",
            },
            requests={
                "cpu": "1m",
                "memory": "1Mi",
            },
        ),
    )

    label_selector_requirement = V1LabelSelectorRequirement(
        key="app.kubernetes.io/component",
        operator="In",
        values=[component],
    )

    term = V1PodAffinityTerm(
        topology_key="kubernetes.io/hostname",
        label_selector=V1LabelSelector(
            match_expressions=[label_selector_requirement],
        ),
    )

    deployment = V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=V1ObjectMeta(
            name=name,
            labels=labels,
        ),
        spec=V1DeploymentSpec(
            replicas=nodes_count,
            selector=V1LabelSelector(
                match_labels=match_labels,
            ),
            template=V1PodTemplateSpec(
                metadata=V1ObjectMeta(
                    labels=labels,
                ),
                spec=V1PodSpec(
                    containers=[container],
                    runtime_class_name=runtime_class_name,
                    affinity=V1Affinity(
                        pod_anti_affinity=V1PodAntiAffinity(
                            required_during_scheduling_ignored_during_execution=[term],
                        ),
                    ),
                ),
            ),
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
            min_available=nodes_count,
            selector=V1LabelSelector(
                match_labels=match_labels,
            ),
        ),
    )

    async with async_kube_api() as (apps, _, _, policy):
        deployment = await create_or_patch_resource(
            create_resource_method=apps.create_namespaced_deployment,
            patch_resource_method=apps.patch_namespaced_deployment,
            resource_name=name,
            namespace=namespace,
            body=deployment,
            resource_type="deployment",
        )

        owner_reference = V1OwnerReference(
            api_version=deployment.api_version,
            kind=deployment.kind,
            name=deployment.metadata.name,
            uid=deployment.metadata.uid,
        )
        pdb.metadata.owner_references = [owner_reference]

        await create_or_patch_resource(
            create_resource_method=policy.create_namespaced_pod_disruption_budget,
            patch_resource_method=policy.patch_namespaced_pod_disruption_budget,
            resource_name=name,
            namespace=namespace,
            body=pdb,
            resource_type="pod disruption budget",
        )

    if wait_timeout > 0:
        await wait_for_pods_ready(
            label_selector=f"app={name}",
            namespace=namespace,
            wait_timeout=wait_timeout,
        )
        logger.info(f"All {nodes_count} nodes for client {client_name} are ready")
    else:
        logger.info(
            f"No need to wait for nodes to be ready for client {client_name}"
            f" because nodes count is downscaled to {nodes_count}."
        )


async def release_nodes_for_client(
    client_name: str,
    namespace: str,
    max_retries: int = 3,
):
    """
    Delete the deployment and PodDisruptionBudget that were created for a client to hold its nodes.
    """
    client_hash = md5(client_name.encode()).hexdigest()
    name = f"{component}-{client_hash}"
    logger.info(f"Releasing nodes for client {client_name} in namespace {namespace}")

    async with async_kube_api() as (apps, _, _, _):
        deleted = await delete_with_retries(
            delete_func=apps.delete_namespaced_deployment,
            resource_type="deployment",
            resource_name=name,
            namespace=namespace,
            max_retries=max_retries,
        )
        if not deleted:
            logger.warning(f"Failed to clean up node holder deployment '{name}' for client '{client_name}'")
        else:
            logger.info(f"Successfully cleaned up node holder for '{client_name}'")
        return deleted


async def change_number_of_spun_nodes(client_id: UUID, namespace: str):
    client_nodes = await need_to_release_nodes_for_client(client_id=client_id)

    if client_nodes is None:
        logger.info(f"Client {client_id} does not exist in database or did not request any nodes, skipping.")
        return False

    if client_nodes.nodes < 0:
        logger.info(
            f"Do not release nodes for client {client_id} "
            f"because there are other clients {client_nodes.name} with higher requests, skipping."
        )
        return False

    had_errors = False
    if client_nodes.nodes == 0:  # Clean everything
        try:
            nodes_released = await release_nodes_for_client(client_name=client_nodes.name, namespace=namespace)
            if not nodes_released:
                had_errors = True
        except Exception as e:
            logger.exception(f"Error deleting client nodes for client ID {client_id}: {str(e)}")
            had_errors = True

    if client_nodes.nodes > 0:  # Update node holders
        try:
            nodes_released = await spin_up_or_update_nodes_for_client(
                client_name=client_nodes.name,
                nodes_count=client_nodes.nodes,
                namespace=namespace,
                wait_timeout=0,
            )
            if not nodes_released:
                had_errors = True
        except Exception as e:
            logger.exception(f"Error updating client nodes for client ID {client_id}: {str(e)}")
            had_errors = True

    return had_errors
