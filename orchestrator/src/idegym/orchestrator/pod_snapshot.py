import asyncio
import time
from http import HTTPStatus
from typing import Any

from idegym.api.config import PodSnapshotConfig
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

    async def get_pod_for_server(self, server_name: str) -> tuple[str, str]:
        """Resolve (pod_name, pod_uid) for the running pod backing the given server."""
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

        md = running_pods[0].metadata
        logger.debug(f"Resolved pod '{md.name}' (uid={md.uid}) for server '{server_name}'")
        return md.name, md.uid

    async def create_trigger(self, server_name: str, pod_name: str, pod_uid: str) -> str:
        """Create a PodSnapshotManualTrigger targeting the given pod, owned by it for GC safety."""
        trigger_name = f"snapshot-{pod_name}"

        body = {
            "apiVersion": f"{CRD_GROUP}/{CRD_VERSION}",
            "kind": "PodSnapshotManualTrigger",
            "metadata": {
                "name": trigger_name,
                "namespace": self._namespace,
                "labels": {"app": server_name},
                "ownerReferences": [
                    {
                        "apiVersion": "v1",
                        "kind": "Pod",
                        "name": pod_name,
                        "uid": pod_uid,
                        "controller": False,
                        "blockOwnerDeletion": False,
                    }
                ],
            },
            "spec": {"targetPod": pod_name},
        }

        async with async_kube_api() as (_, _, _, _, custom):
            await custom.create_namespaced_custom_object(
                group=CRD_GROUP,
                version=CRD_VERSION,
                namespace=self._namespace,
                plural=CRD_PLURAL,
                body=body,
            )
        logger.info(f"Created PodSnapshotManualTrigger '{trigger_name}' in namespace '{self._namespace}'")
        return trigger_name

    async def wait_for_completion(self, trigger_name: str) -> None:
        """
        Poll the trigger until its `Triggered` condition becomes True (success) or False (failure).

        Raises SnapshotFailedError on terminal failure, or SnapshotTimeoutError if the configured
        timeout elapses first.
        """
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

            condition = self._triggered_condition(obj)
            if condition:
                status = condition.get("status")
                if status == "True":
                    internal_name = ((obj.get("status") or {}).get("snapshotCreated") or {}).get("name") or ""
                    logger.info(
                        f"PodSnapshotManualTrigger '{trigger_name}' completed, snapshot name: '{internal_name}'"
                    )
                    return
                if status == "False":
                    message = condition.get("message") or condition.get("reason") or "unknown reason"
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

    async def snapshot_server(self, server_name: str) -> None:
        """
        End-to-end snapshot flow: resolve pod, create trigger, wait for completion, delete trigger.
        """
        pod_name, pod_uid = await self.get_pod_for_server(server_name)
        trigger_name = await self.create_trigger(server_name=server_name, pod_name=pod_name, pod_uid=pod_uid)
        try:
            await self.wait_for_completion(trigger_name)
        finally:
            try:
                await self.delete_trigger(trigger_name)
            except Exception:
                logger.exception(f"Failed to delete PodSnapshotManualTrigger '{trigger_name}'")

    @staticmethod
    def _triggered_condition(obj: dict[str, Any]) -> dict[str, Any] | None:
        """Return the `Triggered` condition from the trigger's status, or None if not yet emitted."""
        for cond in (obj.get("status") or {}).get("conditions") or []:
            if cond.get("type") == "Triggered":
                return cond
        return None
