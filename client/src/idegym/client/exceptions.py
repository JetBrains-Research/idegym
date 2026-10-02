"""Typed failures for IdeGYM SDK calls.

A retry policy has to tell "the sandbox is gone" from "the control plane is busy" from "the
command timed out", so these exceptions carry the status code and response body as attributes
rather than only in the message. They also subclass ``RuntimeError``, and keep their messages
stable, so existing ``except RuntimeError`` handlers and message parsing keep working.
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

    Covers both a timeout status from the orchestrator and a deadline the SDK enforces on a
    request or on polling. It is also a builtin ``TimeoutError`` so that ``except TimeoutError``
    catches the client-side deadlines.
    """


class IdeGYMConnectionError(IdeGYMHTTPError):
    """The request never got a response: the connection failed or broke off mid-exchange.

    Typically the orchestrator is restarting or unreachable; ``status_code`` is ``None``. Safe to
    retry only if the operation is idempotent, since the request may have been acted on.
    """


class IdeGYMBusyError(IdeGYMHTTPError):
    """The control plane is rate-limiting or temporarily out of capacity. Retry with backoff."""


class IdeGYMCancelledError(IdeGYMHTTPError):
    """The operation was cancelled before it finished, usually by a disconnect."""


class IdeGYMServerError(IdeGYMHTTPError):
    """The orchestrator or the sandbox failed while handling the request."""


class IdeGYMSandboxError(IdeGYMHTTPError):
    """The sandbox itself answered a forwarded request with an error status.

    Kept apart from the status-based types because the sandbox is alive: its ``404 Path not
    found`` must not read as ``IdeGYMNotFoundError``, which means the sandbox is gone.
    ``status_code`` and ``body`` are the sandbox's own. Failures the orchestrator reports about the
    forward itself (pod unreachable, call cancelled) keep their usual types.
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

    Some operations report failure as a return value; an unchecked one can record a live pod as
    stopped. Passing the result through here makes the failure raise like the rest of the API.
    """
    if isinstance(response, ErrorResponse):
        raise http_error(
            f"{operation} failed: {response.model_dump()}",
            status_code=response.status_code,
            body=response.body,
        )
    return response
