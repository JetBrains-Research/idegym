import asyncio
from asyncio import CancelledError
from os import environ as env

from fastapi import APIRouter, HTTPException, Request, status
from idegym.api.config import Config, OTELConfig
from idegym.api.orchestrator.clients import AvailabilityStatus
from idegym.api.orchestrator.operations import AsyncOperationStatus, AsyncOperationType
from idegym.api.orchestrator.servers import (
    FinishServerRequest,
    RestartServerRequest,
    ServerActionResponse,
    ServerReuseStrategy,
    StartServerRequest,
    StartServerResponse,
    StopServerRequest,
)
from idegym.backend.utils.kubernetes_client import (
    clean_up_server,
    deploy_server,
    restart_pods,
    wait_for_pods_ready,
)
from idegym.orchestrator.database.helpers import (
    check_resources_and_save_server_in_db,
    create_async_operation,
    find_matching_finished_server_in_db,
    update_operation_status,
    update_operation_with_error,
    update_server_owner,
    update_server_status,
    validate_client,
    validate_server,
)
from idegym.orchestrator.util.decorators import handle_async_task_exceptions, handle_server_exceptions
from idegym.orchestrator.util.errors import format_error
from idegym.utils.decorators import executes_operation_in_background
from idegym.utils.logging import get_logger
from idegym.utils.quantity import parse_quantity

router = APIRouter()
logger = get_logger(__name__)


@executes_operation_in_background
@router.post("/api/idegym-servers")
@handle_server_exceptions(server_operation_description="starting IdeGYM server")
async def start_server(request: StartServerRequest, low_level_request: Request):
    logger.info(f"Received start request for {request.image_tag} for client ID {request.client_id}")
    async_operation_id = await create_async_operation(
        async_operation_type=AsyncOperationType.START_SERVER, client_id=request.client_id, request=request
    )
    asyncio.create_task(
        _task_start_server(
            config=low_level_request.app.state.config, request=request, async_operation_id=async_operation_id
        )
    )
    return StartServerResponse(
        namespace=request.namespace,
        client_id=request.client_id,
        operation_id=async_operation_id,
    )


@executes_operation_in_background
@router.delete("/api/idegym-servers")
@handle_server_exceptions(server_operation_description="stopping IdeGYM server")
async def stop_server(request: StopServerRequest):
    logger.info(f"Received stop request for server ID {request.client_id} for client ID {request.client_id}")
    server = await validate_server(client_id=request.client_id, server_id=request.server_id)
    async_operation_id = await create_async_operation(
        async_operation_type=AsyncOperationType.STOP_SERVER,
        client_id=request.client_id,
        server_id=request.server_id,
        request=request,
    )
    asyncio.create_task(
        _task_stop_server(
            server_id=server.id,
            server_generated_name=server.generated_name,
            namespace=server.namespace,
            async_operation_id=async_operation_id,
        )
    )
    return ServerActionResponse(
        server_name=server.generated_name,
        message=f"Stop initiated for {server.generated_name}",
        operation_id=async_operation_id,
    )


@router.post("/api/idegym-servers/finish")
@handle_server_exceptions("finishing IdeGYM server")
async def finish_server(request: FinishServerRequest):
    logger.info(f"Received finish request for server ID {request.client_id} for client ID {request.client_id}")
    server = await validate_server(client_id=request.client_id, server_id=request.server_id)
    await update_server_status(
        server_id=server.id,
        availability_status=AvailabilityStatus.FINISHED,
    )
    return ServerActionResponse(
        server_name=server.generated_name,
        message=f"Finished IdeGYM server {server.generated_name} (available for reuse)",
    )


@executes_operation_in_background
@router.post("/api/idegym-servers/restart")
@handle_server_exceptions("restarting IdeGYM server")
async def restart_server(request: RestartServerRequest):
    server = await validate_server(client_id=request.client_id, server_id=request.server_id)
    async_operation_id = await create_async_operation(
        async_operation_type=AsyncOperationType.RESTART_SERVER,
        client_id=request.client_id,
        server_id=request.server_id,
        request=request,
    )
    asyncio.create_task(
        _task_restart_server(
            server_id=server.id,
            server_generated_name=server.generated_name,
            namespace=server.namespace,
            server_start_wait_timeout_in_seconds=request.server_start_wait_timeout_in_seconds,
            async_operation_id=async_operation_id,
        )
    )
    return ServerActionResponse(
        server_name=server.generated_name,
        message=f"Restart initiated for {server.generated_name}",
        operation_id=async_operation_id,
    )


async def _task_start_server(config: Config, request: StartServerRequest, async_operation_id: int):
    client_name = None
    server_id = None
    server_generated_name = None
    server_server_name = None
    server_image_tag = None

    try:
        await update_operation_status(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.IN_PROGRESS,
            orchestrator_pod=env.get("__POD_NAME"),
        )

        cpu_request, ram_request = extract_resources_request(config, request)

        existing_server = None
        client_name_from_request = None
        used_reset_reuse = False

        if request.reuse_strategy in (ServerReuseStrategy.RESTART, ServerReuseStrategy.RESET):
            existing_server, client_name_from_request = await find_matching_finished_server_in_db(request=request)
            if request.reuse_strategy == ServerReuseStrategy.RESET:
                used_reset_reuse = existing_server is not None

        client_name = client_name_from_request

        if existing_server:
            if request.reuse_strategy == ServerReuseStrategy.RESTART:
                await restart_pods(
                    name=existing_server.generated_name,
                    namespace=request.namespace,
                    wait_timeout=request.server_start_wait_timeout_in_seconds,
                )

            await update_server_owner(server_id=existing_server.id, client_id=request.client_id)

            server_id = existing_server.id
            server_generated_name = existing_server.generated_name
            server_server_name = existing_server.server_name
            server_image_tag = existing_server.image_tag

            # For RESTART, we'll mark ALIVE later below; for RESET we skip marking ALIVE
        else:
            if client_name is None:
                client = await validate_client(request.client_id)
                client_name = client.name

            server = await check_resources_and_save_server_in_db(
                client_id=request.client_id,
                client_name=client_name,
                server_name=request.server_name,
                namespace=request.namespace,
                cpu_request=cpu_request,
                ram_request=ram_request,
                image_tag=request.image_tag,
                container_runtime=request.runtime_class_name,
                run_as_root=request.run_as_root,
            )

            server_id = server.id
            server_generated_name = server.generated_name
            server_server_name = server.server_name
            server_image_tag = server.image_tag

            otel_config: OTELConfig = config.otel
            environment_variables = (
                {
                    "name": "__POD_UID",
                    "valueFrom": {
                        "fieldRef": {
                            "fieldPath": "metadata.uid",
                        },
                    },
                },
                {
                    "name": "__POD_NAME",
                    "valueFrom": {
                        "fieldRef": {
                            "fieldPath": "metadata.name",
                        },
                    },
                },
                {
                    "name": "__NAMESPACE",
                    "valueFrom": {
                        "fieldRef": {
                            "fieldPath": "metadata.namespace",
                        },
                    },
                },
                {
                    "name": "__NODE_NAME",
                    "valueFrom": {
                        "fieldRef": {
                            "fieldPath": "spec.nodeName",
                        },
                    },
                },
                {
                    "name": "IDEGYM_OTEL_ATTRIBUTES",
                    "value": "{ "
                    'k8s.pod.uid: "$(__POD_UID)", '
                    'k8s.pod.name: "$(__POD_NAME)", '
                    'k8s.namespace.name: "$(__NAMESPACE)", '
                    'k8s.node.name: "$(__NODE_NAME)" '
                    "}",
                },
                {
                    "name": "IDEGYM_OTEL_TRACING_ENDPOINT",
                    "value": otel_config.tracing.endpoint,
                },
                {
                    "name": "IDEGYM_OTEL_TRACING_AUTH_USERNAME",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "tracing",
                            "key": "username",
                            "optional": True,
                        }
                    },
                },
                {
                    "name": "IDEGYM_OTEL_TRACING_AUTH_PASSWORD",
                    "valueFrom": {
                        "secretKeyRef": {
                            "name": "tracing",
                            "key": "password",
                            "optional": True,
                        }
                    },
                },
                {
                    "name": "IDEGYM_OTEL_TRACING_TIMEOUT",
                    "value": str(otel_config.tracing.timeout),
                },
            )

            # Deploy the server
            await deploy_server(
                image_tag=request.image_tag,
                server_name=server_generated_name,
                namespace=request.namespace,
                service_port=request.service_port,
                container_port=request.container_port,
                runtime_class_name=request.runtime_class_name,
                run_as_root=request.run_as_root,
                node_selector=request.node_selector,
                resources=request.resources,
                environment_variables=environment_variables,
            )

            # Wait for pods to be ready
            await wait_for_pods_ready(
                label_selector=f"app={server_generated_name}",
                namespace=request.namespace,
                wait_timeout=request.server_start_wait_timeout_in_seconds,
            )

        # Update availability to ALIVE unless we're in RESET reuse case with an existing finished server
        if not used_reset_reuse:
            await update_server_status(server_id=server_id, availability_status=AvailabilityStatus.ALIVE)

        await update_operation_status(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.SUCCEEDED,
            result=StartServerResponse(
                namespace=request.namespace,
                client_id=request.client_id,
                server_id=server_id,
                server_name=server_server_name,
                image_tag=server_image_tag,
                need_to_reset=used_reset_reuse,
            ),
        )

        logger.info(f"Started IdeGYM server {server_generated_name} with ID {server_id}")

    except CancelledError:
        logger.warning(
            f"Server creation task cancelled for client ID {request.client_id}, operation ID {async_operation_id}"
        )
        await update_operation_with_error(
            async_operation_id=async_operation_id,
            async_operation_status=AsyncOperationStatus.CANCELLED,
            status_code=499,  # there is no HTTPStatus code for 499 - client closed the request
            body=f"Server creation with operation ID {async_operation_id} was cancelled",
        )

    except HTTPException as he:
        logger.warning(
            f"HTTP error in background server creation task for client ID {request.client_id}: {he.status_code} {he.detail}"
        )
        await update_operation_with_error(
            async_operation_id=async_operation_id, status_code=he.status_code, body=he.detail
        )

    except Exception as e:
        message = f"Error creating IdeGYM server for client ID {request.client_id} with tag {request.image_tag}"
        logger.exception(message)

        await clean_kubernetes(request, server_generated_name)

        await update_operation_with_error(
            async_operation_id=async_operation_id,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            body=format_error(message=message, exception=e),
        )

        # TODO: Theoretically it may raise an exception from the db, keep an eye on that
        if server_id:
            await update_server_status(
                server_id=server_id,
                availability_status=AvailabilityStatus.FAILED_TO_START,
            )


async def clean_kubernetes(request, server_generated_name):
    try:
        await clean_up_server(name=server_generated_name, namespace=request.namespace)
        logger.info(
            f"Deleted k8s resources for IdeGYM server {server_generated_name} in namespace {request.namespace} after startup failure"
        )
    except Exception as k8s_e:
        logger.warning(
            f"Failed to delete k8s resources for {server_generated_name} during cleanup after startup failure: {k8s_e}"
        )


def extract_resources_request(config: Config, request: StartServerRequest):
    cpu_request = config.orchestrator.resources.default_cpu_request
    ram_request = config.orchestrator.resources.default_ram_request
    if request.resources:
        if "requests" in request.resources:
            requests = request.resources["limits"]
            if "cpu" in requests:
                cpu_decimal = parse_quantity(requests["cpu"])
                cpu_request = float(cpu_decimal)

            if "memory" in requests:
                memory_decimal = parse_quantity(requests["memory"])
                ram_request = float(memory_decimal) / (1024 * 1024 * 1024)

    return cpu_request, ram_request


@handle_async_task_exceptions(
    operation_description="stopping IdeGYM server in coroutine",
    error_availability_status=AvailabilityStatus.DELETION_FAILED,
)
async def _task_stop_server(server_id: int, server_generated_name: str, namespace: str, async_operation_id: int):
    await update_operation_status(
        async_operation_id=async_operation_id,
        async_operation_status=AsyncOperationStatus.IN_PROGRESS,
        orchestrator_pod=env.get("__POD_NAME"),
    )
    await update_server_status(
        server_id=server_id,
        availability_status=AvailabilityStatus.STOPPED,
    )
    # cleaning
    await clean_up_server(
        name=server_generated_name,
        namespace=namespace,
    )
    await update_operation_status(
        async_operation_id=async_operation_id,
        async_operation_status=AsyncOperationStatus.SUCCEEDED,
        result=ServerActionResponse(
            server_name=server_generated_name,
            message=f"Successfully stopped IdeGYM server {server_generated_name}",
        ),
    )


@handle_async_task_exceptions(
    operation_description="restarting IdeGYM server in coroutine",
    error_availability_status=AvailabilityStatus.RESTART_FAILED,
)
async def _task_restart_server(
    server_id: int,
    server_generated_name: str,
    namespace: str,
    server_start_wait_timeout_in_seconds: int,
    async_operation_id: int,
):
    await update_operation_status(
        async_operation_id=async_operation_id,
        async_operation_status=AsyncOperationStatus.IN_PROGRESS,
        orchestrator_pod=env.get("__POD_NAME"),
    )
    # restarting
    await restart_pods(
        name=server_generated_name,
        namespace=namespace,
        wait_timeout=server_start_wait_timeout_in_seconds,
    )
    await update_server_status(
        server_id=server_id,
        availability_status=AvailabilityStatus.ALIVE,
    )
    await update_operation_status(
        async_operation_id=async_operation_id,
        async_operation_status=AsyncOperationStatus.SUCCEEDED,
        result=ServerActionResponse(
            server_name=server_generated_name,
            message=f"Successfully restarted IdeGYM server {server_generated_name}",
        ),
    )
