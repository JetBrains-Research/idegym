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


class IdeGYMHTTPError(IdeGYMException, RuntimeError):
    """A call to the orchestrator, or to a server through it, failed.

    ``status_code`` is the HTTP status the failure carried. It is ``None`` when the request
    never produced one — a client-side timeout, for instance.
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


class IdeGYMTimeoutError(IdeGYMHTTPError):
    """The call did not complete in time. Safe to retry if the operation is idempotent."""


class IdeGYMBusyError(IdeGYMHTTPError):
    """The control plane is rate-limiting or temporarily out of capacity. Retry with backoff."""


class IdeGYMCancelledError(IdeGYMHTTPError):
    """The operation was cancelled before it finished, usually by a disconnect."""


class IdeGYMServerError(IdeGYMHTTPError):
    """The orchestrator or the sandbox failed while handling the request."""


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


def error_class_for_status(status_code: Optional[int]) -> type[IdeGYMHTTPError]:
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
    return error_class_for_status(status_code)(message, status_code=status_code, body=body, method=method, url=url)
