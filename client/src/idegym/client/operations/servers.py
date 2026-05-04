import time
from asyncio import sleep
from http import HTTPStatus
from typing import Optional
from uuid import UUID

from idegym.api.orchestrator.servers import (
    ErrorResponse,
    FinishServerRequest,
    RestartServerRequest,
    ServerActionResponse,
    ServerKind,
    ServerReuseStrategy,
    StartServerRequest,
    StartServerResponse,
    StopServerRequest,
)
from idegym.api.orchestrator.snapshots import CreateSnapshotRequest, CreateSnapshotResponse
from idegym.api.resources import KubernetesResources
from idegym.api.status import Status
from idegym.api.type import KubernetesNodeSelector, KubernetesObjectName, OCIImageName
from idegym.client.operations.project import ProjectOperations
from idegym.client.operations.utils import HTTPUtils, PollingConfig
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


class ServerOperations:
    def __init__(self, utils: HTTPUtils, project: ProjectOperations) -> None:
        self._utils = utils
        self._project = project

    async def start_server(
        self,
        image_tag: OCIImageName,
        server_name: KubernetesObjectName = "default-idegym-server",
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        runtime_class_name: Optional[str] = None,
        run_as_root: bool = False,
        service_port: int = 80,
        container_port: int = 8000,
        resources: Optional[KubernetesResources] = None,
        node_selector: Optional[KubernetesNodeSelector] = None,
        server_start_wait_timeout_in_seconds: int = 60,
        retry_delay_in_seconds: int = 15,
        polling_config: PollingConfig = PollingConfig(),
        reuse_strategy: ServerReuseStrategy = ServerReuseStrategy.RESET,
        server_kind: ServerKind = ServerKind.IDEGYM,
        snapshot_id: Optional[str] = None,
    ) -> StartServerResponse | ErrorResponse:
        client_id = self._utils.validate_client_id(client_id)
        namespace = self._utils.validate_namespace(namespace)

        start_time = time.time()
        attempts = 0

        while True:
            elapsed_time = time.time() - start_time
            if elapsed_time >= server_start_wait_timeout_in_seconds:
                raise TimeoutError(f"Server start timed out after {server_start_wait_timeout_in_seconds} seconds")

            remaining_time = int(server_start_wait_timeout_in_seconds - elapsed_time)

            request = StartServerRequest(
                client_id=client_id,
                namespace=namespace,
                image_tag=image_tag,
                server_name=server_name,
                runtime_class_name=runtime_class_name,
                run_as_root=run_as_root,
                service_port=service_port,
                container_port=container_port,
                resources=resources,
                node_selector=node_selector,
                server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
                reuse_strategy=reuse_strategy,
                server_kind=server_kind,
                snapshot_id=snapshot_id,
            )
            response_raw = await self._utils.make_request(
                "POST", "/api/idegym-servers", request, request_timeout=remaining_time
            )
            response: StartServerResponse | ErrorResponse = self._utils.parse_response(
                response_raw=response_raw, model_class=StartServerResponse
            )

            response = await self._utils.wait_for_async_operation_to_end(
                operation_id=response.operation_id,
                success_response_model=StartServerResponse,
                error_response_model=ErrorResponse,
                polling_config=PollingConfig(
                    initial_delay_in_sec=polling_config.initial_delay_in_sec,
                    wait_timeout_in_sec=remaining_time,
                    poll_interval_in_sec=polling_config.poll_interval_in_sec,
                    factor_for_exponential_wait=polling_config.factor_for_exponential_wait,
                    max_delay_for_exponential_wait_in_sec=polling_config.max_delay_for_exponential_wait_in_sec,
                ),
            )

            if isinstance(response, StartServerResponse):
                if response.need_to_reset:
                    reset_result = await self._project.reset_project(server_id=response.server_id, client_id=client_id)
                    if reset_result.status != Status.SUCCESS:
                        return ErrorResponse(
                            status_code=HTTPStatus.INTERNAL_SERVER_ERROR.value,
                            body=f"Failed to reset project: {reset_result.model_dump()}",
                        )
                    response.need_to_reset = False
                return response

            if isinstance(response, ErrorResponse):
                if response.status_code == HTTPStatus.TOO_MANY_REQUESTS.value:
                    attempts += 1
                    logger.warning(
                        f"Received 429 Too Many Requests error (attempt {attempts}). "
                        f"Retrying in {retry_delay_in_seconds} seconds..."
                    )

                    if elapsed_time + retry_delay_in_seconds >= server_start_wait_timeout_in_seconds:
                        raise TimeoutError(
                            f"Server start timed out after {server_start_wait_timeout_in_seconds} seconds"
                        )

                    await sleep(retry_delay_in_seconds)
                else:
                    return response

    async def stop_server(
        self,
        server_id: int,
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> ServerActionResponse:
        client_id = self._utils.validate_client_id(client_id)
        namespace = self._utils.validate_namespace(namespace)
        request = StopServerRequest(client_id=client_id, namespace=namespace, server_id=server_id)
        response_raw = await self._utils.make_request("DELETE", "/api/idegym-servers", request)
        response: ServerActionResponse = self._utils.parse_response(
            response_raw=response_raw, model_class=ServerActionResponse
        )
        return await self._utils.wait_for_async_operation_to_end(
            operation_id=response.operation_id,
            success_response_model=ServerActionResponse,
            error_response_model=ErrorResponse,
            polling_config=polling_config,
        )

    async def restart_server(
        self,
        server_id: int,
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        server_start_wait_timeout_in_seconds: int = 60,
        polling_config: PollingConfig = PollingConfig(),
    ) -> ServerActionResponse:
        client_id = self._utils.validate_client_id(client_id)
        namespace = self._utils.validate_namespace(namespace)
        request = RestartServerRequest(
            client_id=client_id,
            namespace=namespace,
            server_id=server_id,
            server_start_wait_timeout_in_seconds=server_start_wait_timeout_in_seconds,
        )
        response_raw = await self._utils.make_request(
            "POST", "/api/idegym-servers/restart", request, request_timeout=server_start_wait_timeout_in_seconds
        )
        response: ServerActionResponse = self._utils.parse_response(
            response_raw=response_raw, model_class=ServerActionResponse
        )
        return await self._utils.wait_for_async_operation_to_end(
            operation_id=response.operation_id,
            success_response_model=ServerActionResponse,
            error_response_model=ErrorResponse,
            polling_config=PollingConfig(
                initial_delay_in_sec=polling_config.initial_delay_in_sec,
                wait_timeout_in_sec=server_start_wait_timeout_in_seconds or polling_config.wait_timeout_in_sec,
                poll_interval_in_sec=polling_config.poll_interval_in_sec,
                factor_for_exponential_wait=polling_config.factor_for_exponential_wait,
                max_delay_for_exponential_wait_in_sec=polling_config.max_delay_for_exponential_wait_in_sec,
            ),
        )

    async def finish_server(
        self, server_id: int, client_id: Optional[UUID] = None, namespace: Optional[str] = None
    ) -> ServerActionResponse:
        client_id = self._utils.validate_client_id(client_id)
        namespace = self._utils.validate_namespace(namespace)
        request = FinishServerRequest(client_id=client_id, namespace=namespace, server_id=server_id)
        response_raw = await self._utils.make_request("POST", "/api/idegym-servers/finish", request)
        return ServerActionResponse.model_validate(response_raw)

    async def snapshot_server(
        self,
        server_id: int,
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> CreateSnapshotResponse | ErrorResponse:
        client_id = self._utils.validate_client_id(client_id)
        namespace = self._utils.validate_namespace(namespace)
        request = CreateSnapshotRequest(client_id=client_id, namespace=namespace, server_id=server_id)
        response_raw = await self._utils.make_request("POST", "/api/idegym-servers/snapshot", request)
        response: CreateSnapshotResponse = self._utils.parse_response(
            response_raw=response_raw, model_class=CreateSnapshotResponse
        )
        return await self._utils.wait_for_async_operation_to_end(
            operation_id=response.operation_id,
            success_response_model=CreateSnapshotResponse,
            error_response_model=ErrorResponse,
            polling_config=polling_config,
        )
