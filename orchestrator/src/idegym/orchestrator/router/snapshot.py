import asyncio
from os import environ as env
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from idegym.api.config import Config
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.orchestrator.snapshots import (
    CreateSnapshotRequest,
    CreateSnapshotResponse,
    PrepareSnapshotsRequest,
    PrepareSnapshotsResponse,
    PrepareSnapshotsStatusResponse,
    SnapshotExistsRequest,
    SnapshotExistsResponse,
    SnapshotJobResult,
    SnapshotJobStatusResponse,
    SnapshotPipelineJob,
)
from idegym.orchestrator.database.helpers import (
    create_async_operation,
    create_snapshot_prepare_batch,
    find_snapshot_for_request,
    find_snapshot_job_with_name,
    find_snapshot_prepare_request_with_results,
    update_operation_status,
    update_operation_with_error,
    validate_server,
)
from idegym.orchestrator.pod_snapshot import PodSnapshotService
from idegym.orchestrator.snapshot_pipeline import (
    compute_hash_for_start_request,
    compute_snapshot_request_hash,
    run_snapshot_pipeline_job,
    serialize_start_request,
)
from idegym.orchestrator.util.decorators import handle_general_exceptions, handle_server_exceptions
from idegym.orchestrator.util.errors import format_error
from idegym.utils.decorators import executes_operation_in_background
from idegym.utils.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@executes_operation_in_background
@router.post("/api/idegym-servers/snapshot", status_code=status.HTTP_202_ACCEPTED)
@handle_server_exceptions(server_operation_description="creating pod snapshot")
async def create_snapshot(request: CreateSnapshotRequest, low_level_request: Request):
    config: Config = low_level_request.app.state.config
    snapshot_config = config.orchestrator.pod_snapshot

    if not snapshot_config.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pod snapshot feature is not enabled",
        )

    server = await validate_server(client_id=request.client_id, server_id=request.server_id)

    if server.container_runtime != "gvisor":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Server {request.server_id} is not eligible for snapshot: "
                f"pod snapshotting requires gVisor runtime, "
                f"but server uses '{server.container_runtime or 'default runtime'}'"
            ),
        )

    async_operation_id = await create_async_operation(
        async_operation_type=AsyncOperationType.SNAPSHOT_SERVER,
        client_id=request.client_id,
        server_id=request.server_id,
        request=request,
    )

    asyncio.create_task(
        _task_create_snapshot(
            config=config,
            server_id=server.id,
            server_generated_name=server.generated_name,
            snapshot_name=server.snapshot_name,
            namespace=request.namespace,
            async_operation_id=async_operation_id,
        )
    )

    return CreateSnapshotResponse(
        server_id=server.id,
        server_name=server.generated_name,
        operation_id=async_operation_id,
    )


async def _task_create_snapshot(
    config: Config,
    server_id: int,
    server_generated_name: str,
    snapshot_name: str,
    namespace: str,
    async_operation_id: int,
):
    try:
        await update_operation_status(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.IN_PROGRESS,
            orchestrator_pod=env.get("__POD_NAME"),
        )

        service = PodSnapshotService(
            config=config.orchestrator.pod_snapshot,
            namespace=namespace,
        )
        await service.snapshot_server(server_name=server_generated_name)

        await update_operation_status(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.SUCCEEDED,
            result=CreateSnapshotResponse(
                server_id=server_id,
                server_name=server_generated_name,
                snapshot_id=snapshot_name,
                operation_id=async_operation_id,
            ),
        )

        logger.info(f"Snapshot operation {async_operation_id} succeeded for server {server_generated_name}")

    except asyncio.CancelledError:
        logger.warning(f"Snapshot task cancelled for server {server_generated_name}, operation ID {async_operation_id}")
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.CANCELLED,
            status_code=499,
            body=f"Snapshot operation {async_operation_id} was cancelled",
        )

    except HTTPException as he:
        logger.warning(f"HTTP error in snapshot task for server {server_generated_name}: {he.status_code} {he.detail}")
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            status_code=he.status_code,
            body=he.detail,
        )

    except Exception as e:
        message = f"Error creating snapshot for server {server_generated_name}"
        logger.exception(message)
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            body=format_error(message=message, exception=e),
        )


@router.post("/api/snapshots/prepare", status_code=status.HTTP_202_ACCEPTED)
@handle_general_exceptions(error_message="Failed to start snapshot pipeline jobs")
async def prepare_snapshots(request: PrepareSnapshotsRequest, low_level_request: Request):
    """Accept a batch of start-server requests, prepare a snapshot for each independently."""
    config: Config = low_level_request.app.state.config

    if not config.orchestrator.pod_snapshot.enabled:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Pod snapshot feature is not enabled",
        )

    for start_request in request.requests:
        if start_request.runtime_class_name != "gvisor":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Request for image '{start_request.image_tag}' is not eligible for snapshot: "
                    f"pod snapshotting requires gVisor runtime, "
                    f"but runtime_class_name is '{start_request.runtime_class_name or 'default runtime'}'"
                ),
            )

    prepare_request_id = uuid4()
    jobs_to_start = [
        SnapshotPipelineJob(
            job_id=str(uuid4()),
            request_hash=compute_hash_for_start_request(start_request),
            serialized_request=serialize_start_request(start_request),
            start_request=start_request,
        )
        for start_request in request.requests
    ]
    await create_snapshot_prepare_batch(request_id=prepare_request_id, jobs=jobs_to_start)

    for job in jobs_to_start:
        asyncio.create_task(
            run_snapshot_pipeline_job(
                job_id=job.job_id,
                request=job.start_request,
                config=config,
                prepare_request_id=prepare_request_id,
            )
        )
        logger.info(f"Started snapshot pipeline job {job.job_id} for image {job.start_request.image_tag}")

    return PrepareSnapshotsResponse(request_id=str(prepare_request_id))


@router.get("/api/snapshots/prepare/{request_id}")
@handle_general_exceptions(error_message="Failed to get snapshot prepare request status")
async def get_prepare_snapshots_status(request_id: str):
    """Poll the status of a batch snapshot prepare request."""
    try:
        request_uuid = UUID(request_id)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invalid request_id: {request_id}")

    result = await find_snapshot_prepare_request_with_results(request_id=request_uuid)
    if not result:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Snapshot prepare request {request_id} not found"
        )

    prepare, job_results = result
    done = prepare.succeeded + prepare.failed
    is_ready = done >= prepare.total_requested

    results = None
    if is_ready:
        results = [
            SnapshotJobResult(
                request_hash=request_hash,
                status=job_status,
                snapshot_name=snapshot_name,
                details=details,
            )
            for request_hash, job_status, snapshot_name, details in job_results
        ]

    return PrepareSnapshotsStatusResponse(
        request_id=request_id,
        status="READY" if is_ready else "IN_PROGRESS",
        total_requested=prepare.total_requested,
        succeeded=prepare.succeeded,
        failed=prepare.failed,
        results=results,
    )


@router.get("/api/snapshots/jobs/{job_id}")
@handle_general_exceptions(error_message="Failed to get snapshot job status")
async def get_snapshot_job_status(job_id: str):
    """Poll the status of a snapshot pipeline job by its job ID."""
    result = await find_snapshot_job_with_name(job_id=job_id)
    if not result:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Snapshot job {job_id} not found")
    record, snapshot_name = result
    return SnapshotJobStatusResponse(
        job_id=record.job_id,
        status=record.status,
        snapshot_name=snapshot_name,
        details=record.details,
    )


@router.get("/api/snapshots/exists")
@handle_general_exceptions(error_message="Failed to check snapshot existence")
async def check_snapshot_exists(request: SnapshotExistsRequest = Depends()):
    """Check whether a snapshot exists for the given server configuration."""
    request_hash = compute_snapshot_request_hash(
        namespace=request.namespace,
        image_tag=str(request.image_tag),
        server_name=str(request.server_name),
        runtime_class_name=request.runtime_class_name,
        run_as_root=request.run_as_root,
        server_kind=str(request.server_kind),
    )
    record = await find_snapshot_for_request(request_hash=request_hash)
    if record:
        return SnapshotExistsResponse(exists=True, snapshot_name=record.snapshot_name)
    return SnapshotExistsResponse(exists=False)
