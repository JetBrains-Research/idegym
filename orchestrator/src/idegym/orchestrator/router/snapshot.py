import asyncio
from os import environ as env

from fastapi import APIRouter, HTTPException, Request, status
from idegym.api.config import Config
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.orchestrator.snapshots import CreateSnapshotRequest, CreateSnapshotResponse
from idegym.orchestrator.database.helpers import (
    create_async_operation,
    update_operation_status,
    update_operation_with_error,
    validate_server,
)
from idegym.orchestrator.pod_snapshot import PodSnapshotService
from idegym.orchestrator.util.decorators import handle_server_exceptions
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
            namespace=request.namespace,
            async_operation_id=async_operation_id,
        )
    )

    return CreateSnapshotResponse(
        server_id=server.id,
        server_name=server.generated_name,
        trigger_name="",
        operation_id=async_operation_id,
    )


async def _task_create_snapshot(
    config: Config,
    server_id: int,
    server_generated_name: str,
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
        trigger_name = await service.snapshot_server(server_name=server_generated_name)

        await update_operation_status(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.SUCCEEDED,
            result=CreateSnapshotResponse(
                server_id=server_id,
                server_name=server_generated_name,
                trigger_name=trigger_name,
                operation_id=async_operation_id,
            ),
        )

        logger.info(
            f"Snapshot operation {async_operation_id} succeeded for server {server_generated_name} "
            f"(trigger: {trigger_name})"
        )

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
