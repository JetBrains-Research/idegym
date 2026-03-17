from http import HTTPStatus
from json import loads
from typing import Any, Dict, Optional
from uuid import UUID

from idegym.api.exceptions import InspectionsNotReadyException
from idegym.api.orchestrator.operations import ForwardRequestResponse
from idegym.api.orchestrator.servers import ErrorResponse
from idegym.api.paths import API_BASE_PATH
from idegym.client.operations.utils import HTTPUtils, PollingConfig
from pydantic import BaseModel


class ForwardingOperations:
    def __init__(self, utils: HTTPUtils) -> None:
        self._utils = utils

    async def forward_request(
        self,
        method: str,
        server_id: int,
        path: str,
        body: Optional[BaseModel] = None,
        client_id: Optional[UUID] = None,
        request_timeout: Optional[int] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> Dict[str, Any]:
        client_id = self._utils.validate_client_id(client_id)

        url = f"/api/forward/{client_id}/{server_id}/{API_BASE_PATH.lstrip('/')}/{path.lstrip('/')}"
        response_raw = await self._utils.make_request(method, url, body, request_timeout=request_timeout)
        response: ForwardRequestResponse | ErrorResponse = self._utils.parse_response(
            response_raw=response_raw, model_class=ForwardRequestResponse
        )

        response = await self._utils.wait_for_async_operation_to_end(
            operation_id=response.async_operation_id,
            success_response_model=ForwardRequestResponse,
            error_response_model=ErrorResponse,
            polling_config=PollingConfig(
                initial_delay_in_sec=polling_config.initial_delay_in_sec,
                wait_timeout_in_sec=request_timeout or polling_config.wait_timeout_in_sec,
                poll_interval_in_sec=polling_config.poll_interval_in_sec,
                factor_for_exponential_wait=polling_config.factor_for_exponential_wait,
                max_delay_for_exponential_wait_in_sec=polling_config.max_delay_for_exponential_wait_in_sec,
            ),
        )

        if isinstance(response, ForwardRequestResponse):
            try:
                response_dict = loads(response.body or "{}")
            except Exception as e:
                raise RuntimeError(f"Failed to parse forwarded response body: {type(e).__name__}: {e}")
            return response_dict
        else:
            if response.status_code == HTTPStatus.TOO_EARLY.value:
                raise InspectionsNotReadyException()

            raise RuntimeError(f"Failed to forward request {method} {url}: {response.model_dump()}")
