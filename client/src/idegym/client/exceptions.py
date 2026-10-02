"""Typed failures for IdeGYM SDK calls.

A retry policy has to tell "the sandbox is gone" from "the control plane is busy" from "the
command timed out". Every failure used to arrive as a plain ``RuntimeError`` whose message
embedded the status, which left callers parsing message text that changes whenever the format
does. These exceptions carry the status code and the response body as attributes instead.

They subclass ``RuntimeError`` as well as ``IdeGYMException`` so that code written against the
old behaviour — including ``except RuntimeError`` around a client call — keeps working, and the
messages are unchanged for the same reason.
"""

from http import HTTPStatus
from typing import Optional

from idegym.api.exceptions import IdeGYMException
from idegym.api.orchestrator.servers import ErrorResponse


class IdeGYMHTTPError(IdeGYMException, RuntimeError):
    """A call to the orchestrator, or to a server through it, failed.

    ``status_code`` is the HTTP status the failure carried. It is ``None`` when the request
    never produced one — a client-side timeout or a connection failure, for instance.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        body: Optional[str] = None,
        method: Optional[str] = None,
        url: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body
        self.method = method
        self.url = url


class IdeGYMBadRequestError(IdeGYMHTTPError):
    """The request was rejected as malformed or invalid. Retrying it unchanged will not help."""


class IdeGYMAuthError(IdeGYMHTTPError):
    """Credentials were missing, wrong, or insufficient for the resource."""


class IdeGYMNotFoundError(IdeGYMHTTPError):
    """The addressed client, server, or operation does not exist any more.

    Also raised for ``410 Gone``, which is what the orchestrator returns when it cannot reach
    the pod: from the caller's side the sandbox is equally gone either way.
    """


class IdeGYMTimeoutError(IdeGYMHTTPError, TimeoutError):
    """The call did not complete in time. Safe to retry if the operation is idempotent.

    Covers both a timeout status from the orchestrator and a deadline the SDK itself enforces —
    on a request, or on polling an async operation. It is also a builtin ``TimeoutError``,
    which is what those client-side deadlines used to raise, so an existing
    ``except TimeoutError`` keeps catching them.
    """


class IdeGYMConnectionError(IdeGYMHTTPError):
    """The request never got a response: the connection failed or broke off mid-exchange.

    Typically the orchestrator is restarting or unreachable. ``status_code`` is ``None``. Safe to
    retry if the operation is idempotent — the request may or may not have been acted on.
    """


class IdeGYMBusyError(IdeGYMHTTPError):
    """The control plane is rate-limiting or temporarily out of capacity. Retry with backoff."""


class IdeGYMCancelledError(IdeGYMHTTPError):
    """The operation was cancelled before it finished, usually by a disconnect."""


class IdeGYMServerError(IdeGYMHTTPError):
    """The orchestrator or the sandbox failed while handling the request."""


class IdeGYMSandboxError(IdeGYMHTTPError):
    """The sandbox itself answered a forwarded request with an error status.

    The sandbox is alive — it produced the response — so this is kept apart from the status-based
    types: an application-level ``404 Path not found`` from a live sandbox must not read as
    ``IdeGYMNotFoundError``, which tells the caller the sandbox is gone and a new one is needed.
    ``status_code`` and ``body`` are the sandbox's own. Failures the orchestrator reports about the
    forward itself — the pod cannot be reached, the call was cancelled — keep their usual types.
    """


# 499 is nginx's non-standard "client closed request"; the orchestrator reuses it for a
# cancelled background operation, so it has no HTTPStatus member to name it by.
_CLIENT_CLOSED_REQUEST = 499

_ERROR_BY_STATUS: dict[int, type[IdeGYMHTTPError]] = {
    HTTPStatus.BAD_REQUEST: IdeGYMBadRequestError,
    HTTPStatus.UNPROCESSABLE_ENTITY: IdeGYMBadRequestError,
    HTTPStatus.UNAUTHORIZED: IdeGYMAuthError,
    HTTPStatus.FORBIDDEN: IdeGYMAuthError,
    HTTPStatus.NOT_FOUND: IdeGYMNotFoundError,
    HTTPStatus.GONE: IdeGYMNotFoundError,
    HTTPStatus.REQUEST_TIMEOUT: IdeGYMTimeoutError,
    HTTPStatus.GATEWAY_TIMEOUT: IdeGYMTimeoutError,
    HTTPStatus.TOO_MANY_REQUESTS: IdeGYMBusyError,
    HTTPStatus.SERVICE_UNAVAILABLE: IdeGYMBusyError,
    _CLIENT_CLOSED_REQUEST: IdeGYMCancelledError,
}


def _error_class_for_status(status_code: Optional[int]) -> type[IdeGYMHTTPError]:
    """Pick the exception type for a status code, falling back by class of status."""
    if status_code is None:
        return IdeGYMHTTPError
    if (specific := _ERROR_BY_STATUS.get(status_code)) is not None:
        return specific
    if status_code >= HTTPStatus.INTERNAL_SERVER_ERROR:
        return IdeGYMServerError
    if status_code >= HTTPStatus.BAD_REQUEST:
        return IdeGYMBadRequestError
    return IdeGYMHTTPError


def http_error(
    message: str,
    *,
    status_code: Optional[int] = None,
    body: Optional[str] = None,
    method: Optional[str] = None,
    url: Optional[str] = None,
) -> IdeGYMHTTPError:
    """Build the most specific exception for ``status_code``, ready to raise."""
    return _error_class_for_status(status_code)(message, status_code=status_code, body=body, method=method, url=url)


def raise_for_error_response[T](response: T | ErrorResponse, operation: str) -> T:
    """Turn an ``ErrorResponse`` from an async operation into the matching exception.

    Some operations report failure as a *return value* rather than by raising, which makes it
    possible to record a live pod as stopped simply by not checking. Passing the result through
    here makes failure loud, consistently with the rest of the API.
    """
    if isinstance(response, ErrorResponse):
        raise http_error(
            f"{operation} failed: {response.model_dump()}",
            status_code=response.status_code,
            body=response.body,
        )
    return response
