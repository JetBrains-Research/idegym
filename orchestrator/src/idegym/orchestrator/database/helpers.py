from functools import wraps
from typing import Any, Optional
from uuid import UUID

from fastapi import HTTPException, status
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.orchestrator.servers import ErrorResponse, StartServerRequest
from idegym.orchestrator.database.database import (
    check_resources_and_save_server,
    create_client,
    find_matching_finished_server,
    get_async_operation,
    get_client,
    get_client_name,
    get_db_session,
    get_idegym_server,
    get_idegym_servers_by_client_id,
    get_job_status,
    need_to_release_nodes,
    need_to_spin_up_nodes,
    save_async_operation,
    update_async_operation,
    update_client_heartbeat,
    update_idegym_server_heartbeat,
    update_idegym_server_owner,
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
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=f"IdeGYM server with ID {server_id} is not available (status: {server.availability})",
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
async def find_alive_servers(db: AsyncSession, client_id: UUID):
    servers_info = []
    servers = await get_idegym_servers_by_client_id(db, client_id)
    for server in servers:
        if server.availability in {AvailabilityStatus.ALIVE, AvailabilityStatus.REUSED}:
            servers_info.append({"id": server.id, "generated_name": server.generated_name})
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
