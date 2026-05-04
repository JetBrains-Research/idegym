import asyncio
from os import environ as env
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from idegym.api.config import NodePoolConfig
from idegym.api.orchestrator.clients import (
    AvailabilityStatus,
    FinishClientRequest,
    RegisterClientRequest,
    RegisteredClientResponse,
    SendClientHeartbeatRequest,
    StopClientRequest,
    StopClientResponse,
)
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.backend.utils.kubernetes_client import clean_up_server
from idegym.orchestrator.database.helpers import (
    create_async_operation,
    find_alive_servers,
    safely_register_new_client_in_db,
    update_client_status,
    update_operation_status,
    update_operation_with_error,
    update_server_status,
    validate_client,
)
from idegym.orchestrator.database.models import Client
from idegym.orchestrator.nodes_holder import change_number_of_spun_nodes, spin_up_or_update_nodes_for_client
from idegym.orchestrator.util.decorators import handle_async_task_exceptions, handle_general_exceptions
from idegym.utils.decorators import executes_operation_in_background
from idegym.utils.logging import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.post("/api/clients/heartbeat")
@handle_general_exceptions(error_message="Failed to update client heartbeat")
async def accept_client_heartbeat(request: SendClientHeartbeatRequest):
    client = await update_client_status(request.client_id, request.availability)
    return RegisteredClientResponse.model_validate(client, from_attributes=True)


@executes_operation_in_background
@router.post("/api/clients")
@handle_general_exceptions(error_message="Failed to register client")
async def register_client(request: RegisterClientRequest, low_level_request: Request, http_response: Response):
    response = await register_client_with_node_pool(
        request=request,
        node_pool=low_level_request.app.state.config.orchestrator.node_pool,
    )
    if response.operation_id is not None:
        http_response.status_code = status.HTTP_202_ACCEPTED

    return response


async def register_client_with_node_pool(
    request: RegisterClientRequest,
    node_pool: NodePoolConfig,
) -> RegisteredClientResponse:
    client, spin_up_nodes = await safely_register_new_client_in_db(
        name=request.name, nodes_count=request.nodes_count, namespace=request.namespace
    )
    client_response = RegisteredClientResponse.model_validate(client, from_attributes=True)
    if not spin_up_nodes:
        return client_response

    async_operation_id = await create_async_operation(
        async_operation_type=AsyncOperationType.REGISTER_CLIENT_WITH_NODES,
        client_id=client.id,
        request=request,
    )
    asyncio.create_task(
        _task_spin_up_client_nodes(
            client=client,
            nodes_count=request.nodes_count,
            namespace=request.namespace,
            async_operation_id=async_operation_id,
            node_pool=node_pool,
        )
    )
    return client_response.model_copy(update={"operation_id": async_operation_id})


@executes_operation_in_background
@router.delete("/api/clients", status_code=status.HTTP_202_ACCEPTED)
@handle_general_exceptions(error_message="Failed to stop client")
async def stop_client(request: StopClientRequest):
    await validate_client(client_id=request.client_id)
    servers_info = await find_alive_servers(client_id=request.client_id)
    async_operation_id = await create_async_operation(
        async_operation_type=AsyncOperationType.STOP_CLIENT,
        client_id=request.client_id,
        request=request,
    )
    asyncio.create_task(
        _task_stop_client(
            servers_info=servers_info,
            client_id=request.client_id,
            namespace=request.namespace,
            async_operation_id=async_operation_id,
        )
    )
    return StopClientResponse(operation_id=async_operation_id)


@router.post("/api/clients/finish")
@handle_general_exceptions(error_message="Failed to finish client")
async def finish_client(request: FinishClientRequest):
    await validate_client(client_id=request.client_id)
    servers_info = await find_alive_servers(client_id=request.client_id)
    for server_info in servers_info:
        try:
            await update_server_status(server_id=server_info["id"], availability_status=AvailabilityStatus.FINISHED)
        except Exception:
            logger.exception(
                f"Error finishing IdeGYM server {server_info['generated_name']} with ID {server_info['id']} for client ID {request.client_id}"
            )

    updated_client = await update_client_status(
        client_id=request.client_id, availability_status=AvailabilityStatus.FINISHED
    )
    return RegisteredClientResponse.model_validate(updated_client, from_attributes=True)


@handle_async_task_exceptions(operation_description="spinning up nodes in a coroutine")
async def _task_spin_up_client_nodes(
    client: Client,
    nodes_count: int,
    namespace: str,
    async_operation_id: int,
    node_pool: NodePoolConfig,
):
    logger.info(f"Spinning up nodes for client with ID {client.id} in namespace {namespace} in background")
    await update_operation_status(
        async_operation_id=async_operation_id,
        async_operation_status=AsyncOperationStatus.IN_PROGRESS,
        orchestrator_pod=env.get("__POD_NAME"),
    )
    await spin_up_or_update_nodes_for_client(
        client_name=client.name,
        namespace=namespace,
        nodes_count=nodes_count,
        node_pool_taint_key=node_pool.taint_key if node_pool.enabled else None,
        node_pool_preference_weight=node_pool.preference_weight,
    )
    await update_operation_status(
        async_operation_id=async_operation_id,
        async_operation_status=AsyncOperationStatus.SUCCEEDED,
        result=RegisteredClientResponse.model_validate(client, from_attributes=True),
    )


@handle_async_task_exceptions(
    operation_description="stopping client",
    error_availability_status=AvailabilityStatus.DELETION_FAILED,
)
async def _task_stop_client(servers_info, client_id: UUID, namespace: str, async_operation_id: int):
    logger.info(f"Stopping client with ID {client_id} in namespace {namespace} in background")

    await update_operation_status(
        async_operation_id=async_operation_id,
        async_operation_status=AsyncOperationStatus.IN_PROGRESS,
        orchestrator_pod=env.get("__POD_NAME"),
    )

    has_deletion_errors = False
    for server_info in servers_info:
        try:
            await update_server_status(server_id=server_info["id"], availability_status=AvailabilityStatus.STOPPED)
            await clean_up_server(
                name=server_info["generated_name"],
                namespace=namespace,
            )
            logger.info(f"Successfully stopped IdeGYM server {server_info['generated_name']}")

        except Exception:
            logger.exception(
                f"Error stopping IdeGYM server {server_info['generated_name']} "
                f"with ID {server_info['id']} for client ID {client_id}"
            )
            await update_server_status(
                server_id=server_info["id"], availability_status=AvailabilityStatus.DELETION_FAILED
            )
            has_deletion_errors = True

    failed_to_release_nodes = await change_number_of_spun_nodes(client_id=client_id, namespace=namespace)
    has_deletion_errors = has_deletion_errors | failed_to_release_nodes

    client_status = AvailabilityStatus.DELETION_FAILED if has_deletion_errors else AvailabilityStatus.STOPPED

    updated_client = await update_client_status(client_id=client_id, availability_status=client_status)
    if has_deletion_errors:
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            body="One or more servers or nodes failed to stop. Check individual server statuses for details.",
        )
    else:
        await update_operation_status(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.SUCCEEDED,
            result=RegisteredClientResponse.model_validate(updated_client, from_attributes=True),
        )
