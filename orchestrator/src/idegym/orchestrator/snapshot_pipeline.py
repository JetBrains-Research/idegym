import hashlib
import json
from asyncio import sleep
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from idegym.api.config import Config
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.servers import StartServerRequest
from idegym.api.status import Status
from idegym.backend.utils.kubernetes_client import clean_up_server, deploy_server, wait_for_pods_ready
from idegym.orchestrator.database.helpers import (
    check_resources_and_save_server_in_db,
    create_snapshot,
    update_prepare_request_failed,
    update_prepare_request_succeeded,
    update_server_status,
    update_snapshot_job_status,
    validate_client,
)
from idegym.orchestrator.pod_snapshot import PodSnapshotService
from idegym.orchestrator.router.server import extract_resources_request
from idegym.utils.logging import get_logger
from idegym.utils.serializer import serialize_as_json_string

logger = get_logger(__name__)

_HASH_FIELDS = ("namespace", "image_tag", "server_name", "runtime_class_name", "run_as_root", "server_kind")


def compute_snapshot_request_hash(**fields) -> str:
    """Compute a stable SHA-256 hash from the fields that identify a unique server snapshot configuration."""
    key = {k: str(v) if v is not None else None for k, v in fields.items()}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode()).hexdigest()


def compute_hash_for_start_request(request: StartServerRequest) -> str:
    return compute_snapshot_request_hash(
        namespace=request.namespace,
        image_tag=str(request.image_tag),
        server_name=str(request.server_name),
        runtime_class_name=request.runtime_class_name,
        run_as_root=request.run_as_root,
        server_kind=str(request.server_kind),
    )


async def run_snapshot_pipeline_job(
    job_id: str,
    request: StartServerRequest,
    config: Config,
    prepare_request_id: Optional[UUID] = None,
) -> None:
    """
    Full pipeline: start server → trigger snapshot → stop server → record result.

    Updates the snapshot_jobs record throughout. On failure, sets status=FAILURE with details.
    """
    server_id = None
    server_generated_name = None

    try:
        cpu_request, ram_request = extract_resources_request(config, request)
        client = await validate_client(request.client_id)

        server = await check_resources_and_save_server_in_db(
            client_id=request.client_id,
            client_name=client.name,
            server_name=request.server_name,
            namespace=request.namespace,
            cpu_request=cpu_request,
            ram_request=ram_request,
            image_tag=request.image_tag,
            container_runtime=request.runtime_class_name,
            server_kind=request.server_kind,
            service_port=request.service_port,
            run_as_root=request.run_as_root,
        )

        server_id = server.id
        server_generated_name = server.generated_name

        node_pool = config.orchestrator.node_pool
        pod_snapshot = config.orchestrator.pod_snapshot

        resources = request.resources.model_dump(by_alias=True, exclude_none=True) if request.resources else None

        await deploy_server(
            image_tag=request.image_tag,
            server_name=server_generated_name,
            namespace=request.namespace,
            service_port=request.service_port,
            container_port=request.container_port,
            service_account_name=pod_snapshot.service_account_name if pod_snapshot.enabled else None,
            runtime_class_name=request.runtime_class_name,
            run_as_root=request.run_as_root,
            node_selector=request.node_selector,
            node_pool_taint_key=node_pool.taint_key if node_pool.enabled else None,
            node_pool_preference_weight=node_pool.preference_weight,
            resources=resources,
            environment_variables=(),
            server_kind=request.server_kind,
            snapshot_id=str(server_id),
        )

        await wait_for_pods_ready(
            label_selector=f"app={server_generated_name}",
            namespace=request.namespace,
            wait_timeout=request.server_start_wait_timeout_in_seconds,
        )

        await update_server_status(server_id=server_id, availability_status=AvailabilityStatus.ALIVE)

        snapshot_service = PodSnapshotService(config=pod_snapshot, namespace=request.namespace)
        await snapshot_service.snapshot_server(server_name=server_generated_name)

        # Brief pause to let the snapshot trigger propagate before stopping the pod.
        await sleep(2)

        await _cleanup_server(
            server_id=server_id, server_generated_name=server_generated_name, namespace=request.namespace
        )

        request_hash = compute_hash_for_start_request(request)
        snapshot = await create_snapshot(
            snapshot_name=str(server_id),
            request_hash=request_hash,
            namespace=request.namespace,
            image_tag=str(request.image_tag),
            server_name=str(request.server_name),
            runtime_class_name=request.runtime_class_name,
            run_as_root=request.run_as_root,
            server_kind=str(request.server_kind),
        )

        await update_snapshot_job_status(
            job_id=job_id,
            status=Status.SUCCESS,
            snapshot_id=snapshot.id,
        )

        if prepare_request_id:
            await update_prepare_request_succeeded(request_id=prepare_request_id)

        logger.info(
            f"Snapshot pipeline job {job_id} completed for server {server_generated_name} (snapshot_name={server_id})"
        )

    except Exception as e:
        detail = e.detail if isinstance(e, HTTPException) else str(e)
        logger.exception(f"Snapshot pipeline job {job_id} failed")

        if server_id and server_generated_name:
            await _cleanup_server(
                server_id=server_id,
                server_generated_name=server_generated_name,
                namespace=request.namespace,
                failed=True,
            )

        await update_snapshot_job_status(
            job_id=job_id,
            status=Status.FAILURE,
            details=detail,
        )

        if prepare_request_id:
            await update_prepare_request_failed(request_id=prepare_request_id)


async def _cleanup_server(server_id: int, server_generated_name: str, namespace: str, failed: bool = False) -> None:
    availability = AvailabilityStatus.FAILED_TO_START if failed else AvailabilityStatus.STOPPED
    try:
        await update_server_status(server_id=server_id, availability_status=availability)
        await clean_up_server(name=server_generated_name, namespace=namespace)
    except Exception:
        logger.exception(f"Failed to clean up server {server_generated_name} after snapshot pipeline job")


def serialize_start_request(request: StartServerRequest) -> str:
    return serialize_as_json_string(request)
