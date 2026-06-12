import asyncio
import time
from http import HTTPStatus
from typing import Optional

from fastapi import HTTPException, status
from idegym.api.config import PodSnapshotConfig
from idegym.api.orchestrator.snapshots import (
    OwnerReference,
    PodSnapshotManualTrigger,
    PodSnapshotManualTriggerMetadata,
    PodSnapshotManualTriggerSpec,
    PodSnapshotManualTriggerStatus,
)
from idegym.backend.utils.kubernetes_client import ApiException, async_kube_api
from idegym.utils.logging import get_logger

logger = get_logger(__name__)

CRD_GROUP = "podsnapshot.gke.io"
CRD_VERSION = "v1"
CRD_PLURAL = "podsnapshotmanualtriggers"


class SnapshotFailedError(RuntimeError):
    """Raised when the PodSnapshotManualTrigger reports a non-successful terminal status."""


class SnapshotTimeoutError(RuntimeError):
    """Raised when the PodSnapshotManualTrigger does not reach a terminal status within the configured timeout."""


class PodSnapshotService:
    """
    Drives the full lifecycle of a manual pod snapshot: create the trigger, wait for completion, delete the trigger.
    """

    def __init__(self, config: PodSnapshotConfig, namespace: str):
        self._config = config
        self._namespace = namespace

    async def get_pod_for_server(self, server_name: str) -> tuple[str, str, str]:
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
        pod_uid = pod.metadata.uid
        node_name = pod.spec.node_name
        logger.debug(
            f"Resolved pod name '{pod_name}' with uid '{pod_uid}' on node '{node_name}' for server '{server_name}'"
        )
        return pod_name, pod_uid, node_name

    async def validate_node_eligible_for_snapshot(self, node_name: Optional[str]) -> None:
        if not node_name:
            logger.debug("No node name available; skipping E2 instance type check")
            return

        async with async_kube_api() as (_, _, core, _, _):
            node = await core.read_node(name=node_name)

        labels = node.metadata.labels or {}
        instance_type = labels.get("node.kubernetes.io/instance-type") or labels.get("beta.kubernetes.io/instance-type")

        if instance_type and instance_type.startswith("e2-"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Server is not eligible for snapshot: node '{node_name}' uses GCP E2 instance type "
                    f"'{instance_type}' which does not support pod snapshots"
                ),
            )

    async def create_trigger(self, server_name: str, pod_name: str, pod_uid: str) -> str:
        trigger_name = f"snapshot-{pod_name}"

        trigger = PodSnapshotManualTrigger(
            api_version=f"{CRD_GROUP}/{CRD_VERSION}",
            kind="PodSnapshotManualTrigger",
            metadata=PodSnapshotManualTriggerMetadata(
                name=trigger_name,
                namespace=self._namespace,
                labels={"app": server_name},
                owner_references=[
                    OwnerReference(
                        api_version="v1",
                        kind="Pod",
                        name=pod_name,
                        uid=pod_uid,
                    )
                ],
            ),
            spec=PodSnapshotManualTriggerSpec(target_pod=pod_name),
        )

        async with async_kube_api() as (_, _, _, _, custom):
            await custom.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=self._namespace,
                plural=CRD_PLURAL,
                body=trigger.model_dump(by_alias=True),
            )
        logger.info(f"Created PodSnapshotManualTrigger '{trigger_name}' in namespace '{self._namespace}'")
        return trigger_name

    async def wait_for_completion(self, trigger_name: str) -> Optional[str]:
        """Wait for the trigger to finish; return the created PodSnapshot resource name, if reported."""
        timeout = self._config.completion_timeout.total_seconds()
        poll_interval = self._config.poll_interval.total_seconds()
        deadline = time.monotonic() + timeout

        while True:
            async with async_kube_api() as (_, _, _, _, custom):
                obj = await custom.get_namespaced_custom_object(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=self._namespace,
                    plural=CRD_PLURAL,
                    name=trigger_name,
                )

            trigger_status = PodSnapshotManualTriggerStatus.model_validate(obj.get("status") or {})
            condition = trigger_status.triggered_condition
            if condition:
                triggered = condition.status == "True"
                if triggered:
                    snapshot_name = trigger_status.created_snapshot_name
                    logger.info(
                        f"PodSnapshotManualTrigger '{trigger_name}' completed (snapshot '{snapshot_name or 'unknown'}')"
                    )
                    await asyncio.sleep(3)  # Delay to ensure image got uploaded to the GCS
                    return snapshot_name
                else:
                    message = condition.message or condition.reason or "unknown reason"
                    raise SnapshotFailedError(f"PodSnapshotManualTrigger '{trigger_name}' failed: {message}")

            if time.monotonic() >= deadline:
                raise SnapshotTimeoutError(
                    f"PodSnapshotManualTrigger '{trigger_name}' did not complete within {timeout:.0f}s"
                )

            await asyncio.sleep(poll_interval)

    async def delete_trigger(self, trigger_name: str) -> None:
        try:
            async with async_kube_api() as (_, _, _, _, custom):
                await custom.delete_namespaced_custom_object(
                    group=CRD_GROUP,
                    version=CRD_VERSION,
                    namespace=self._namespace,
                    plural=CRD_PLURAL,
                    name=trigger_name,
                )
            logger.info(f"Deleted PodSnapshotManualTrigger '{trigger_name}'")
        except ApiException as ex:
            if ex.status == HTTPStatus.NOT_FOUND:
                logger.debug(f"PodSnapshotManualTrigger '{trigger_name}' already gone")
                return
            raise

    async def snapshot_server(self, server_name: str) -> Optional[str]:
        """Snapshot the server's pod and return the created PodSnapshot resource name, if reported."""
        pod_name, pod_uid, node_name = await self.get_pod_for_server(server_name)
        await self.validate_node_eligible_for_snapshot(node_name)
        trigger_name = await self.create_trigger(server_name=server_name, pod_name=pod_name, pod_uid=pod_uid)
        try:
            return await self.wait_for_completion(trigger_name)
        finally:
            try:
                await self.delete_trigger(trigger_name)
            except Exception:
                logger.exception(f"Failed to delete PodSnapshotManualTrigger '{trigger_name}'")
