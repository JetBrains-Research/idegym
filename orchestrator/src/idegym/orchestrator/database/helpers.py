from functools import wraps
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.orchestrator.servers import AliveServerInfo, ErrorResponse, StartServerRequest
from idegym.orchestrator.database.database import (
    check_resources_and_save_server,
    create_client,
    find_matching_finished_server,
    find_snapshot_by_request_hash,
    get_async_operation,
    get_client,
    get_client_name,
    get_db_session,
    get_idegym_server,
    get_idegym_servers_by_client_id,
    get_job_status,
    get_snapshot_job,
    get_snapshot_job_with_name,
    get_snapshot_prepare_request_with_results,
    increment_snapshot_prepare_failed,
    increment_snapshot_prepare_succeeded,
    need_to_release_nodes,
    need_to_spin_up_nodes,
    save_async_operation,
    save_snapshot,
    save_snapshot_job,
    save_snapshot_prepare_batch,
    save_snapshot_prepare_request,
    update_async_operation,
    update_client_heartbeat,
    update_idegym_server_heartbeat,
    update_idegym_server_owner,
    update_snapshot_job,
)
from idegym.utils.logging import get_logger
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = get_logger(__name__)


def with_db_session(func):
    """Decorator that injects a database session as the first positional argument."""

    @wraps(func)
    async def wrapper(*args, **kwargs):
        async with get_db_session() as db:
            return await func(db, *args, **kwargs)

    return wrapper


@with_db_session
async def validate_client(db: AsyncSession, client_id: UUID):
    client = await get_client(db, client_id)
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Client with ID {client_id} not found")
    return client


@with_db_session
async def safely_register_new_client_in_db(db: AsyncSession, name: str, nodes_count: int, namespace: str):
    # Table-level exclusive lock prevents two concurrent registrations for the same client name.
    await db.execute(text("LOCK TABLE clients IN EXCLUSIVE MODE"))

    client = await create_client(db, name, nodes_count, namespace)
    spin_up_nodes = await need_to_spin_up_nodes(db=db, client_id=client.id)
    await db.commit()
    return client, spin_up_nodes


@with_db_session
async def need_to_release_nodes_for_client(db: AsyncSession, client_id: UUID):
    return await need_to_release_nodes(db=db, client_id=client_id)


@with_db_session
async def update_client_status(db: AsyncSession, client_id: UUID, availability_status: AvailabilityStatus):
    client = await update_client_heartbeat(db=db, client_id=client_id, availability=availability_status)
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Client with ID {client_id} not found")
    logger.debug(f"Updated client with ID {client_id} status to {availability_status}")
    return client


@with_db_session
async def validate_server(db: AsyncSession, client_id: UUID, server_id: int):
    """Validate that the client owns the server and that it is in a usable state (ALIVE or REUSED)."""
    client = await get_client(db, client_id)
    if not client:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Client with ID {client_id} not found")

    server = await get_idegym_server(db=db, server_id=server_id)
    if not server:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"IdeGYM server with ID {server_id} not found"
        )

    if server.client_id != client_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"IdeGYM server with ID {server_id} is not associated with client ID {client_id}",
        )

    if server.availability not in {AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED}:
        detail = f"IdeGYM server with ID {server_id} is not available (status: {server.availability})"
        if server.details:
            detail = f"{detail}: {server.details}"
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=detail,
        )

    return server


@with_db_session
async def check_resources_and_save_server_in_db(
    db: AsyncSession,
    client_id: UUID,
    client_name: str,
    server_name: str,
    namespace: str,
    cpu_request: float,
    ram_request: float,
    image_tag: Optional[str] = None,
    container_runtime: Optional[str] = None,
    server_kind: str = "idegym",
    service_port: int = 80,
    run_as_root: bool = False,
    snapshot_id: Optional[str] = None,
    max_restarts: int = 0,
):
    server = await check_resources_and_save_server(
        db=db,
        client_id=client_id,
        client_name=client_name,
        server_name=server_name,
        namespace=namespace,
        cpu_request=cpu_request,
        ram_request=ram_request,
        image_tag=image_tag,
        container_runtime=container_runtime,
        server_kind=server_kind,
        service_port=service_port,
        run_as_root=run_as_root,
        snapshot_id=snapshot_id,
        max_restarts=max_restarts,
    )
    if not server:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Resource limit exceeded. Please try again later or stop some existing servers.",
        )
    return server


@with_db_session
async def find_matching_finished_server_in_db(
    db: AsyncSession, request: StartServerRequest, enable_fifo_check: bool = False
):
    client_name = await get_client_name(db, request.client_id)
    if not client_name:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail=f"Client with ID {request.client_id} not found"
        )

    lookup_result = await find_matching_finished_server(
        db=db,
        client_name=client_name,
        server_name=request.server_name,
        image_tag=request.image_tag,
        container_runtime=request.runtime_class_name,
        run_as_root=request.run_as_root,
        server_kind=request.server_kind,
        enable_fifo_check=enable_fifo_check,
    )

    if lookup_result.server:
        logger.info(f"Found existing finished server {lookup_result.server.generated_name} that can be reused")
    elif lookup_result.blocked_by_fifo:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Server reuse blocked due to pending START_SERVER operations scheduled earlier (FIFO queue)",
        )

    return lookup_result.server, client_name


@with_db_session
async def find_alive_servers(db: AsyncSession, client_id: UUID) -> list[AliveServerInfo]:
    servers_info = []
    servers = await get_idegym_servers_by_client_id(db, client_id)
    for server in servers:
        if server.availability in {AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED}:
            servers_info.append(AliveServerInfo(id=server.id, generated_name=server.generated_name))
    return servers_info


@with_db_session
async def update_server_status(db: AsyncSession, server_id: int, availability_status: AvailabilityStatus):
    await update_idegym_server_heartbeat(db=db, server_id=server_id, availability=availability_status)
    logger.info(f"Updated IdeGYM server with ID {server_id} status to {availability_status}")


@with_db_session
async def update_server_owner(db: AsyncSession, server_id: int, client_id: UUID):
    await update_idegym_server_owner(db=db, server_id=server_id, client_id=client_id)
    logger.info(f"Updated IdeGYM server with ID {server_id} owner to {client_id}")


@with_db_session
async def find_kaniko_job_status(db: AsyncSession, job_name: str):
    return await get_job_status(db, job_name)


@with_db_session
async def create_async_operation(
    db: AsyncSession,
    async_operation_type: AsyncOperationType,
    client_id: Optional[UUID] = None,
    server_id: Optional[int] = None,
    request: Optional[Any] = None,
):
    operation = await save_async_operation(
        db=db, async_operation_type=async_operation_type, client_id=client_id, server_id=server_id, request=request
    )
    return operation.id


@with_db_session
async def update_operation_status(
    db: AsyncSession,
    async_operation_id: int,
    async_operation_status: str,
    orchestrator_pod: Optional[str] = None,
    result: Optional[Any] = None,
):
    await update_async_operation(
        db=db,
        async_operation_id=async_operation_id,
        async_operation_status=async_operation_status,
        orchestrator_pod=orchestrator_pod,
        result=result,
    )


@with_db_session
async def update_operation_with_error(
    db: AsyncSession,
    async_operation_id: int,
    status_code: int,
    body: str,
    async_operation_status: AsyncOperationStatus = AsyncOperationStatus.FAILED,
):
    await update_async_operation(
        db=db,
        async_operation_id=async_operation_id,
        async_operation_status=async_operation_status,
        result=ErrorResponse(status_code=status_code, body=body),
    )


@with_db_session
async def find_async_operation(db: AsyncSession, operation_id: int):
    return await get_async_operation(db, operation_id)


@with_db_session
async def create_snapshot(
    db: AsyncSession,
    snapshot_name: str,
    request_hash: str,
    namespace: str,
    image_tag: str,
    server_name: str,
    runtime_class_name: Optional[str],
    run_as_root: bool,
    server_kind: str,
    pod_snapshot_name: Optional[str] = None,
):
    return await save_snapshot(
        db,
        snapshot_name=snapshot_name,
        request_hash=request_hash,
        namespace=namespace,
        image_tag=image_tag,
        server_name=server_name,
        runtime_class_name=runtime_class_name,
        run_as_root=run_as_root,
        server_kind=server_kind,
        pod_snapshot_name=pod_snapshot_name,
    )


@with_db_session
async def create_snapshot_job(
    db: AsyncSession,
    job_id: str,
    request_hash: str,
    request: str,
    prepare_request_id: Optional[UUID] = None,
):
    return await save_snapshot_job(
        db, job_id=job_id, request_hash=request_hash, request=request, prepare_request_id=prepare_request_id
    )


@with_db_session
async def update_snapshot_job_status(
    db: AsyncSession,
    job_id: str,
    status: str,
    snapshot_id: Optional[int] = None,
    details: Optional[str] = None,
):
    return await update_snapshot_job(db, job_id=job_id, status=status, snapshot_id=snapshot_id, details=details)


@with_db_session
async def find_snapshot_job(db: AsyncSession, job_id: str):
    return await get_snapshot_job(db, job_id)


@with_db_session
async def find_snapshot_job_with_name(db: AsyncSession, job_id: str):
    return await get_snapshot_job_with_name(db, job_id)


@with_db_session
async def find_snapshot_for_request(db: AsyncSession, request_hash: str):
    return await find_snapshot_by_request_hash(db, request_hash)


@with_db_session
async def create_snapshot_prepare_request(db: AsyncSession, request_id: UUID, total_requested: int):
    return await save_snapshot_prepare_request(db, request_id=request_id, total_requested=total_requested)


@with_db_session
async def create_snapshot_prepare_batch(db: AsyncSession, request_id: UUID, jobs: list[dict]):
    return await save_snapshot_prepare_batch(db, request_id=request_id, jobs=jobs)


@with_db_session
async def update_prepare_request_succeeded(db: AsyncSession, request_id: UUID):
    await increment_snapshot_prepare_succeeded(db, request_id=request_id)


@with_db_session
async def update_prepare_request_failed(db: AsyncSession, request_id: UUID):
    await increment_snapshot_prepare_failed(db, request_id=request_id)


@with_db_session
async def find_snapshot_prepare_request_with_results(db: AsyncSession, request_id: UUID):
    return await get_snapshot_prepare_request_with_results(db, request_id=request_id)
