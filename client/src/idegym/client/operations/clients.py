from typing import Optional
from uuid import UUID

from idegym.api.orchestrator.clients import (
    AvailabilityStatus,
    FinishClientRequest,
    RegisterClientRequest,
    RegisteredClientResponse,
    SendClientHeartbeatRequest,
    StopClientRequest,
    StopClientResponse,
)
from idegym.api.orchestrator.servers import ErrorResponse
from idegym.client.operations.utils import HTTPUtils, PollingConfig
from idegym.utils.logging import get_logger

logger = get_logger(__name__)


class ClientOperations:
    def __init__(self, utils: HTTPUtils) -> None:
        self._utils = utils

    async def send_heartbeat(
        self, availability: AvailabilityStatus, client_id: Optional[UUID] = None
    ) -> RegisteredClientResponse:
        client_id = self._utils.validate_client_id(client_id)
        request = SendClientHeartbeatRequest(client_id=client_id, availability=availability)
        response_raw = await self._utils.make_request("POST", "/api/clients/heartbeat", request)
        return RegisteredClientResponse.model_validate(response_raw)

    async def register_client(
        self,
        name: str,
        namespace: Optional[str] = None,
        nodes_count: int = 0,
        polling_config: PollingConfig = PollingConfig(wait_timeout_in_sec=600),
    ) -> RegisteredClientResponse | ErrorResponse:
        namespace = self._utils.validate_namespace(namespace)
        request = RegisterClientRequest(name=name, namespace=namespace, nodes_count=nodes_count)
        response_raw = await self._utils.make_request("POST", "/api/clients", request, request_timeout=60)

        response: RegisteredClientResponse | ErrorResponse = self._utils.parse_response(
            response_raw=response_raw, model_class=RegisteredClientResponse
        )
        if response.operation_id is not None:
            response = await self._utils.wait_for_async_operation_to_end(
                operation_id=response.operation_id,
                success_response_model=RegisteredClientResponse,
                error_response_model=ErrorResponse,
                polling_config=polling_config,
            )
        self._utils.current_client_id = response.id
        return response

    async def stop_client(
        self,
        client_id: Optional[UUID] = None,
        namespace: Optional[str] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> RegisteredClientResponse | ErrorResponse:
        client_id = self._utils.validate_client_id(client_id)
        namespace = self._utils.validate_namespace(namespace)
        request = StopClientRequest(client_id=client_id, namespace=namespace)
        response_raw = await self._utils.make_request("DELETE", "/api/clients", request)
        response: StopClientResponse = self._utils.parse_response(
            response_raw=response_raw, model_class=StopClientResponse
        )
        return await self._utils.wait_for_async_operation_to_end(
            operation_id=response.operation_id,
            success_response_model=RegisteredClientResponse,
            error_response_model=ErrorResponse,
            polling_config=polling_config,
        )

    async def finish_client(
        self, client_id: Optional[UUID] = None, namespace: Optional[str] = None
    ) -> RegisteredClientResponse:
        client_id = self._utils.validate_client_id(client_id)
        namespace = self._utils.validate_namespace(namespace)
        request = FinishClientRequest(client_id=client_id, namespace=namespace)
        response_raw = await self._utils.make_request("POST", "/api/clients/finish", request)
        return RegisteredClientResponse.model_validate(response_raw)
