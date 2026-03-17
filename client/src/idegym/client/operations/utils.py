import asyncio
import math
import random
from asyncio import CancelledError, sleep
from json import JSONDecodeError, loads
from typing import Any, Dict, List, Optional, Type, TypeVar, Union
from uuid import UUID

from httpx import AsyncClient, HTTPStatusError
from idegym.api.orchestrator.operations import (
    AsyncOperationStatus,
    AsyncOperationStatusResponse,
)
from idegym.utils.logging import get_logger
from pydantic import BaseModel, Field

logger = get_logger(__name__)


class PollingConfig(BaseModel):
    initial_delay_in_sec: float = Field(default=0.05, description="How much time to wait before the first poll.")
    wait_timeout_in_sec: int = Field(default=60, description="How much time to wait for the operation to complete.")

    poll_interval_in_sec: float = Field(
        default=0.0, description="Linear poll interval in seconds for the operation status check."
    )

    factor_for_exponential_wait: float = Field(
        default=1.5, description="Factor for exponential poll interval for the operation status check."
    )
    max_delay_for_exponential_wait_in_sec: float = Field(
        default=120.0, description="Max delay for exponential poll interval for the operation status check."
    )


S = TypeVar("S", bound=BaseModel)
E = TypeVar("E", bound=BaseModel)


def retry_with_backoff(attempts: int, base_delay=0.5):
    def decorator(func):
        async def wrapper(*args, **kwargs):
            retries = 0
            while retries < attempts:
                try:
                    return await func(*args, **kwargs)
                except Exception:
                    retries += 1
                    if retries >= attempts:
                        raise
                    delay = base_delay * 2 ** (retries - 1)
                    await asyncio.sleep(delay)
            raise AssertionError("Unreachable")

        return wrapper

    return decorator


class HTTPUtils:
    """
    HTTP utility helper that wraps low-level request/response handling and async operation polling.

    Note: This class is independent of IdeGYMHTTPClient and receives all dependencies explicitly
    through its constructor.
    """

    def __init__(self, http_client: AsyncClient, current_namespace: Optional[str], current_client_id: Optional[UUID]):
        self._http_client: AsyncClient = http_client
        self._current_namespace: Optional[str] = current_namespace
        self._current_client_id: Optional[UUID] = current_client_id

    @property
    def current_namespace(self) -> Optional[str]:
        return self._current_namespace

    @property
    def current_client_id(self) -> Optional[UUID]:
        return self._current_client_id

    @current_client_id.setter
    def current_client_id(self, value: Optional[UUID]) -> None:
        self._current_client_id = value

    def validate_namespace(self, override: Optional[str] = None) -> str:
        namespace = override or self._current_namespace
        if not namespace:
            raise ValueError("Namespace must be provided")
        return namespace

    def validate_client_id(self, override: Optional[UUID] = None) -> UUID:
        client_id = override or self._current_client_id
        if not client_id:
            raise ValueError("Client ID must be provided or client must be registered first")
        return client_id

    async def make_request(
        self,
        method: str,
        url: str,
        body: Optional[BaseModel] = None,
        headers: Optional[Dict[str, str]] = None,
        request_timeout: Optional[int] = None,
    ) -> Dict[str, Any] | List[Dict[str, Any]]:
        try:
            response = await self._http_client.request(
                method=method,
                url=url,
                headers=headers,
                json=body.model_dump(mode="json") if body is not None else None,
                timeout=request_timeout,
            )
            content = await response.aread()
            response.raise_for_status()
            return response.json() if content else {}

        except CancelledError as ex:
            logger.warning(f"Request cancelled: url={url}")
            raise ex

        except HTTPStatusError as ex:
            message = (
                f"Request failed: url={url} "
                f"status={ex.response.status_code} "
                f"reason='{ex.response.reason_phrase}' "
                f"data='{ex.response.text}'"
            )
            logger.error(message)
            raise RuntimeError(message)

        except JSONDecodeError as ex:
            logger.exception(f"Failed to parse JSON response: url={url} error='{str(ex)}' data='{response.text}' '")
            raise ex

        except Exception as ex:
            logger.exception(f"Request error: url={url} type='{ex.__class__.__name__}' error='{str(ex)}'")
            raise ex

    def parse_response(self, response_raw: Dict[str, Any], model_class: Type[S]) -> S:
        return model_class.model_validate(response_raw)

    async def wait_for_async_operation_to_end(
        self,
        operation_id: int,
        success_response_model: Type[S] = None,
        error_response_model: Type[E] = None,
        polling_config: PollingConfig = PollingConfig(),
    ) -> Union[S, E, Optional[str]]:
        logger.debug(f"Polling async operation status with ID {operation_id}")

        async with asyncio.timeout(polling_config.wait_timeout_in_sec):
            retry = 0
            while True:
                await sleep(self._calculate_wait_time_with_jitter(retry=retry, polling_config=polling_config))

                full_status_raw = await self.make_request("GET", f"/api/operations/status/{operation_id}")
                full_status = AsyncOperationStatusResponse.model_validate(full_status_raw)
                short_status = AsyncOperationStatus(full_status.status)

                if short_status is AsyncOperationStatus.SUCCEEDED:
                    return self._parse_async_operation_response(
                        result=full_status.result, short_status=short_status, response_model=success_response_model
                    )

                if short_status in (AsyncOperationStatus.FAILED, AsyncOperationStatus.CANCELLED):
                    logger.debug(
                        f"Async operation {operation_id} ended with status {short_status}. "
                        f"Full details: {full_status.result}"
                    )
                    return self._parse_async_operation_response(
                        result=full_status.result, short_status=short_status, response_model=error_response_model
                    )

                retry += 1

    def _calculate_wait_time_with_jitter(self, retry: int, polling_config: PollingConfig) -> float:
        if retry == 0:
            result = polling_config.initial_delay_in_sec
        elif polling_config.poll_interval_in_sec > 0:
            result = polling_config.poll_interval_in_sec
        else:
            result = min(
                polling_config.initial_delay_in_sec * math.pow(polling_config.factor_for_exponential_wait, retry),
                polling_config.max_delay_for_exponential_wait_in_sec,
            )
        return result + random.uniform(0.01, 0.05)

    def _parse_async_operation_response(
        self, result: Optional[str], short_status: AsyncOperationStatus, response_model: Type[S] = None
    ) -> Union[S, Optional[str]]:
        if response_model is not None and result is None:
            raise RuntimeError(f"Async operation {short_status} but result is missing")

        if response_model:
            try:
                result_dict = loads(result)
            except Exception as e:
                raise RuntimeError(f"Failed to parse async operation result: {type(e).__name__}: {e}")

            return response_model.model_validate(result_dict)
        else:
            return result
