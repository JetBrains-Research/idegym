import asyncio
import math
import random
from asyncio import CancelledError, sleep
from http import HTTPStatus
from json import JSONDecodeError, loads
from typing import Any, Optional, TypeVar
from uuid import UUID

from httpx import AsyncClient, HTTPStatusError, TimeoutException, TransportError
from idegym.api.orchestrator.operations import (
    AsyncOperationStatus,
    AsyncOperationStatusResponse,
)
from idegym.client.exceptions import IdeGYMConnectionError, IdeGYMHTTPError, IdeGYMTimeoutError, http_error
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


# How long past the deadline the final poll, sent at the deadline, may take to answer.
_FINAL_POLL_ALLOWANCE_IN_SEC = 10.0

S = TypeVar("S", bound=BaseModel)
E = TypeVar("E", bound=BaseModel)


# Any other 4xx means the request itself is wrong, so resending it unchanged cannot succeed.
_RETRYABLE_CLIENT_ERROR_STATUSES = frozenset({HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS})


def _is_permanent_failure(error: Exception) -> bool:
    status_code = error.status_code if isinstance(error, IdeGYMHTTPError) else None
    return (
        status_code is not None
        and HTTPStatus.BAD_REQUEST <= status_code < HTTPStatus.INTERNAL_SERVER_ERROR
        and status_code not in _RETRYABLE_CLIENT_ERROR_STATUSES
    )


def retry_with_backoff(attempts: int, base_delay: float = 0.5):
    """Decorator that retries an async function with exponential backoff.

    Any exception is retried except an ``IdeGYMHTTPError`` with a permanent 4xx status, which is
    re-raised at once — a ``404`` for a reaped server will not change on retry.
    """

    def decorator(func):
        async def wrapper(*args, **kwargs):
            retries = 0
            while retries < attempts:
                try:
                    return await func(*args, **kwargs)
                except Exception as error:
                    if _is_permanent_failure(error):
                        raise
                    retries += 1
                    if retries >= attempts:
                        raise
                    delay = base_delay * 2 ** (retries - 1)
                    await asyncio.sleep(delay)
            raise AssertionError("Unreachable")

        return wrapper

    return decorator


class HTTPUtils:
    """HTTP utility helper for request/response handling and async operation polling."""

    def __init__(self, http_client: AsyncClient, current_namespace: Optional[str], current_client_id: Optional[UUID]):
        self._http_client: AsyncClient = http_client
        self._current_namespace: Optional[str] = current_namespace
        self._current_client_id: Optional[UUID] = current_client_id

    @property
    def base_url(self) -> str:
        """Base URL of the HTTP client, without trailing slash."""
        return str(self._http_client.base_url).rstrip("/")

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
        headers: Optional[dict[str, str]] = None,
        request_timeout: Optional[int] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any] | list[dict[str, Any]]:
        try:
            response = await self._http_client.request(
                method=method,
                url=url,
                headers=headers,
                json=body.model_dump(mode="json") if body is not None else None,
                params=params,
                timeout=request_timeout,
            )
            content = await response.aread()
            response.raise_for_status()
            return response.json() if content else {}

        except CancelledError:
            logger.warning(f"Request cancelled: url={url}")
            raise

        except TimeoutException as ex:
            message = f"Request timed out: url={url} error='{ex}'"
            logger.error(message)
            raise IdeGYMTimeoutError(message, method=method, url=url) from ex

        # Must follow TimeoutException, a TransportError subclass. No response at all (refused,
        # reset, cut off during an orchestrator rollout) is still an IdeGYMHTTPError.
        except TransportError as ex:
            message = f"Request failed without a response: url={url} error='{type(ex).__name__}: {ex}'"
            logger.error("Request failed without a response", url=url, error=repr(ex))
            raise IdeGYMConnectionError(message, method=method, url=url) from ex

        except HTTPStatusError as ex:
            # The message is deliberately unchanged: it predates the typed exceptions and
            # callers parse it. New code should read `status_code` and `body` instead.
            message = (
                f"Request failed: url={url} "
                f"status={ex.response.status_code} "
                f"reason='{ex.response.reason_phrase}' "
                f"data='{ex.response.text}'"
            )
            logger.error(message)
            raise http_error(
                message,
                status_code=ex.response.status_code,
                body=ex.response.text,
                method=method,
                url=url,
            ) from ex

        except JSONDecodeError:
            logger.exception(f"Failed to parse JSON response: url={url} data={response.text!r}")
            raise

        except Exception:
            logger.exception(f"Request error: url={url}")
            raise

    def parse_response(self, response_raw: dict[str, Any], model_class: type[S]) -> S:
        return model_class.model_validate(response_raw)

    async def wait_for_async_operation_to_end(
        self,
        operation_id: int,
        success_response_model: Optional[type[S]] = None,
        error_response_model: Optional[type[E]] = None,
        polling_config: Optional[PollingConfig] = None,
    ) -> S | E | Optional[str]:
        """
        Poll ``/api/operations/status/{operation_id}`` until the operation reaches a terminal state.

        Returns an instance of ``success_response_model`` on success, ``error_response_model`` on
        failure or cancellation, or the raw result string if no model is provided.
        Raises ``IdeGYMTimeoutError`` if ``polling_config.wait_timeout_in_sec`` is exceeded.

        The last backoff sleep is cut short so a final poll lands on the deadline; otherwise an
        operation that succeeded after the last early poll would be reported as timed out,
        orphaning, say, a server that did start. A hanging request gets a short allowance past it.
        """
        polling_config = polling_config or PollingConfig()
        logger.debug(f"Polling async operation status with ID {operation_id}")

        wait_timeout = polling_config.wait_timeout_in_sec
        hard_stop = asyncio.timeout(wait_timeout + _FINAL_POLL_ALLOWANCE_IN_SEC)
        try:
            async with hard_stop:
                loop = asyncio.get_running_loop()
                deadline = loop.time() + wait_timeout
                retry = 0
                while True:
                    remaining = deadline - loop.time()
                    delay = self._calculate_wait_time_with_jitter(retry=retry, polling_config=polling_config)
                    last_poll = delay >= remaining
                    await sleep(max(0.0, min(delay, remaining)))

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

                    if last_poll:
                        break
                    retry += 1
        except TimeoutError as ex:
            # A request timeout is already an IdeGYMTimeoutError; only the hard stop is translated.
            if not hard_stop.expired():
                raise
            raise IdeGYMTimeoutError(
                f"Async operation {operation_id} did not finish within {wait_timeout} seconds"
            ) from ex
        raise IdeGYMTimeoutError(f"Async operation {operation_id} did not finish within {wait_timeout} seconds")

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
        self, result: Optional[str], short_status: AsyncOperationStatus, response_model: Optional[type[S]] = None
    ) -> S | Optional[str]:
        if response_model is not None and result is None:
            raise RuntimeError(f"Async operation {short_status} but result is missing")

        if response_model:
            try:
                result_dict = loads(result)
            except Exception as e:  # noqa: BLE001  # surface any result-parse failure as a RuntimeError
                raise RuntimeError(f"Failed to parse async operation result: {type(e).__name__}: {e}")

            return response_model.model_validate(result_dict)
        else:
            return result
