from uuid import UUID

from idegym.backend.utils.kubernetes_client import async_kube_api, delete_with_retries, wait_for_pods_ready
from idegym.orchestrator.database.helpers import need_to_release_nodes_for_client
from idegym.utils.logging import get_logger
from kubernetes_asyncio.client import (
    ApiException,
    V1Container,
    V1Deployment,
    V1DeploymentSpec,
    V1LabelSelector,
    V1ObjectMeta,
    V1PodDisruptionBudget,
    V1PodDisruptionBudgetSpec,
    V1PodSpec,
    V1PodTemplateSpec,
    V1ResourceRequirements,
)

logger = get_logger(__name__)

# Common multi purpose prefix for node manipulations
idegym_nodes_holder_prefix = "idegym-nodes-holder"


async def create_or_patch_resource(
    create_resource_method, patch_resource_method, resource_name: str, namespace: str, body, resource_type: str
):
    """Helper function to create or patch a Kubernetes resource."""
    try:
        await create_resource_method(namespace=namespace, body=body)
        logger.info(f"Created {resource_type} {resource_name} in namespace {namespace}")
    except ApiException as e:
        if e.status == 409:  # Conflict - resource already exists
            await patch_resource_method(name=resource_name, namespace=namespace, body=body)
            logger.info(f"Patched existing {resource_type} {resource_name} in namespace {namespace}")
        else:
            logger.exception(f"Failed to create {resource_type} {resource_name}: {e}")
            raise


async def spin_up_or_update_nodes_for_client(
    client_name: str,
    nodes_count: int,
    namespace: str,
    runtime_class_name: str = "gvisor",
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
    deployment_name = f"{idegym_nodes_holder_prefix}-{client_name}"

    # Set up resource requirements
    resources = V1ResourceRequirements(
        limits={"memory": "150Mi", "cpu": "100m"}, requests={"memory": "150Mi", "cpu": "100m"}
    )

    # Create container
    container = V1Container(
        name=deployment_name,
        image="busybox:1.37.0",
        image_pull_policy="IfNotPresent",
        resources=resources,
        command=["sleep", "infinity"],
    )

    # Create pod template with anti-affinity
    pod_spec = V1PodSpec(
        containers=[container],
        runtime_class_name=runtime_class_name,
        affinity={
            "podAntiAffinity": {
                "requiredDuringSchedulingIgnoredDuringExecution": [
                    {
                        "labelSelector": {
                            "matchExpressions": [
                                {"key": "role", "operator": "In", "values": [idegym_nodes_holder_prefix]}
                            ]
                        },
                        "topologyKey": "kubernetes.io/hostname",
                    }
                ]
            }
        },
    )

    pod_template = V1PodTemplateSpec(
        metadata=V1ObjectMeta(labels={"app": deployment_name, "role": idegym_nodes_holder_prefix}),
        spec=pod_spec,
    )

    # Create deployment
    deployment = V1Deployment(
        api_version="apps/v1",
        kind="Deployment",
        metadata=V1ObjectMeta(name=deployment_name),
        spec=V1DeploymentSpec(
            replicas=nodes_count, selector=V1LabelSelector(match_labels={"app": deployment_name}), template=pod_template
        ),
    )

    # Create PodDisruptionBudget
    pdb = V1PodDisruptionBudget(
        api_version="policy/v1",
        kind="PodDisruptionBudget",
        metadata=V1ObjectMeta(name=f"{deployment_name}-pdb"),
        spec=V1PodDisruptionBudgetSpec(
            min_available=nodes_count, selector=V1LabelSelector(match_labels={"app": deployment_name})
        ),
    )

    # Create resources in Kubernetes
    async with async_kube_api() as (apps, _, _, policy):
        # Create or patch deployment
        await create_or_patch_resource(
            create_resource_method=apps.create_namespaced_deployment,
            patch_resource_method=apps.patch_namespaced_deployment,
            resource_name=deployment_name,
            namespace=namespace,
            body=deployment,
            resource_type="deployment",
        )

        # Create or patch PodDisruptionBudget
        await create_or_patch_resource(
            create_resource_method=policy.create_namespaced_pod_disruption_budget,
            patch_resource_method=policy.patch_namespaced_pod_disruption_budget,
            resource_name=f"{deployment_name}-pdb",
            namespace=namespace,
            body=pdb,
            resource_type="pod disruption budget",
        )

    if wait_timeout > 0:
        # Wait for pods to be ready
        label_selector = f"app={deployment_name}"
        await wait_for_pods_ready(label_selector=label_selector, namespace=namespace, wait_timeout=wait_timeout)
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
    deployment_name = f"{idegym_nodes_holder_prefix}-{client_name}"
    pdb_name = f"{deployment_name}-pdb"

    logger.info(f"Releasing nodes for client {client_name} in namespace {namespace}")

    async with async_kube_api() as (apps, _, _, policy):
        # Delete PodDisruptionBudget
        pdb_deleted = await delete_with_retries(
            policy.delete_namespaced_pod_disruption_budget, "pod disruption budget", pdb_name, namespace, max_retries
        )

        # Delete deployment
        deployment_deleted = await delete_with_retries(
            apps.delete_namespaced_deployment, "deployment", deployment_name, namespace, max_retries
        )

    # Report final status
    failures = []
    if not deployment_deleted:
        failures.append(f"deployment '{deployment_name}'")
    if not pdb_deleted:
        failures.append(f"pod disruption budget '{pdb_name}'")

    if failures:
        error_msg = f"Failed to release client nodes resources: {', '.join(failures)}"
        logger.warning(error_msg)
        return False

    logger.info(f"Successfully released all nodes for client {client_name}")
    return True


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
