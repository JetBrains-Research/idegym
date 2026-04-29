from idegym.api.config import PodSnapshotConfig
from idegym.backend.utils.kubernetes_client import async_kube_api
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

CRD_GROUP = "podsnapshot.gke.io"
CRD_VERSION = "v1"


class PodSnapshotService:
    """
    Triggers pod snapshots via PodSnapshotManualTrigger CRD.
    """

    def __init__(self, config: PodSnapshotConfig, namespace: str):
        self._config = config
        self._namespace = namespace

    async def get_pod_name_for_server(self, server_name: str) -> str:
        """Resolve the running pod name for a server via label selector."""
        async with async_kube_api() as (_, _, core, _, _):
            pods = (
                await core.list_namespaced_pod(
                    namespace=self._namespace,
                    label_selector=f"app={server_name}",
                )
            ).items

        running_pods = [
            pod for pod in pods
            if pod.metadata.deletion_timestamp is None and pod.status.phase == "Running"
        ]

        if not running_pods:
            raise RuntimeError(f"No running pod found for server '{server_name}' in namespace '{self._namespace}'")

        pod_name = running_pods[0].metadata.name
        logger.debug(f"Resolved pod name '{pod_name}' for server '{server_name}'")
        return pod_name

    async def create_trigger(self, server_name: str, pod_name: str) -> str:
        """Create a PodSnapshotManualTrigger targeting the given pod."""
        trigger_name = f"snapshot-{pod_name}"

        body = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "PodSnapshotManualTrigger",
            "metadata": {
                "name": trigger_name,
                "namespace": self._namespace,
                "labels": {
                    "app": server_name,
                },
            },
            "spec": {
                "targetPod": pod_name,
            },
        }

        async with async_kube_api() as (_, _, _, _, custom):
            await custom.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=self._namespace,
                plural="podsnapshotmanualtriggers",
                body=body,
            )
        logger.info(f"Created PodSnapshotManualTrigger '{trigger_name}' in namespace '{self._namespace}'")
        return trigger_name

    async def snapshot_server(self, server_name: str) -> str:
        """Resolve the running pod for a server and create a manual snapshot trigger."""
        pod_name = await self.get_pod_name_for_server(server_name)
        trigger_name = await self.create_trigger(server_name=server_name, pod_name=pod_name)
        logger.info(f"Snapshot initiated for server '{server_name}' via trigger '{trigger_name}'")
        return trigger_name
