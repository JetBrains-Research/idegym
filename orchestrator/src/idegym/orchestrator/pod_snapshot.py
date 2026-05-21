from typing import Optional

from fastapi import HTTPException, status
from idegym.api.config import PodSnapshotConfig
from idegym.api.orchestrator.snapshots import (
    PodSnapshotManualTrigger,
    PodSnapshotManualTriggerMetadata,
    PodSnapshotManualTriggerSpec,
)
from idegym.backend.utils.kubernetes_client import async_kube_api
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

CRD_GROUP = "podsnapshot.gke.io"
CRD_VERSION = "v1"

_NODE_INSTANCE_TYPE_LABELS = (
    "node.kubernetes.io/instance-type",
    "beta.kubernetes.io/instance-type",
)


class PodSnapshotService:
    """
    Triggers pod snapshots via PodSnapshotManualTrigger CRD.
    """

    def __init__(self, config: PodSnapshotConfig, namespace: str):
        self._config = config
        self._namespace = namespace

    async def get_pod_name_for_server(self, server_name: str) -> tuple[str, Optional[str]]:
        """Resolve the running pod name and node name for a server via label selector."""
        async with async_kube_api() as (_, _, core, _, _):
            pods = (
                await core.list_namespaced_pod(
                    namespace=self._namespace,
                    label_selector=f"app={server_name}",
                )
            ).items

        running_pods = [
            pod for pod in pods if pod.metadata.deletion_timestamp is None and pod.status.phase == "Running"
        ]

        if not running_pods:
            raise RuntimeError(f"No running pod found for server '{server_name}' in namespace '{self._namespace}'")

        pod = running_pods[0]
        pod_name = pod.metadata.name
        node_name = pod.spec.node_name
        logger.debug(f"Resolved pod name '{pod_name}' on node '{node_name}' for server '{server_name}'")
        return pod_name, node_name

    async def validate_node_eligible_for_snapshot(self, node_name: Optional[str]) -> None:
        """Raise if the node's GCP instance type does not support pod snapshots (e.g., E2 instances)."""
        if not node_name:
            logger.debug("No node name available; skipping E2 instance type check")
            return

        async with async_kube_api() as (_, _, core, _, _):
            node = await core.read_node(name=node_name)

        labels = node.metadata.labels or {}
        instance_type = next((labels[label] for label in _NODE_INSTANCE_TYPE_LABELS if label in labels), None)

        if instance_type and instance_type.startswith("e2-"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Server is not eligible for snapshot: node '{node_name}' uses GCP E2 instance type "
                    f"'{instance_type}' which does not support pod snapshots"
                ),
            )

    async def create_trigger(self, server_name: str, pod_name: str) -> str:
        """Create a PodSnapshotManualTrigger targeting the given pod."""
        trigger_name = f"snapshot-{pod_name}"

        trigger = PodSnapshotManualTrigger(
            api_version=f"{CRD_GROUP}/{CRD_VERSION}",
            kind="PodSnapshotManualTrigger",
            metadata=PodSnapshotManualTriggerMetadata(
                name=trigger_name,
                namespace=self._namespace,
                labels={"app": server_name},
            ),
            spec=PodSnapshotManualTriggerSpec(target_pod=pod_name),
        )

        async with async_kube_api() as (_, _, _, _, custom):
            await custom.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=self._namespace,
                plural="podsnapshotmanualtriggers",
                body=trigger.model_dump(by_alias=True),
            )
        logger.info(f"Created PodSnapshotManualTrigger '{trigger_name}' in namespace '{self._namespace}'")
        return trigger_name

    async def snapshot_server(self, server_name: str) -> str:
        """Resolve the running pod for a server, validate eligibility, and create a snapshot trigger."""
        pod_name, node_name = await self.get_pod_name_for_server(server_name)
        await self.validate_node_eligible_for_snapshot(node_name)
        trigger_name = await self.create_trigger(server_name=server_name, pod_name=pod_name)
        logger.info(f"Snapshot initiated for server '{server_name}' via trigger '{trigger_name}'")
        return trigger_name
