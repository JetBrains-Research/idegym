"""Stable error codes and the error envelope shared across transports.

The runtime raises :class:`ServiceError` with a machine-readable :class:`ErrorCode`; the REST
layer maps each code to the HTTP status from :func:`http_status_for` and the
MCP layer maps protocol-level codes to JSON-RPC errors and everything else to ``isError`` tool
results.
"""

from enum import StrEnum
from typing import Any, Optional

from pydantic import BaseModel


class ErrorCode(StrEnum):
    """Machine-readable error codes returned in :class:`ErrorResponse`."""

    INVALID_ARGUMENTS = "invalid_arguments"
    UNKNOWN_TOOL = "unknown_tool"
    UNKNOWN_TERMINAL = "unknown_terminal"
    UNKNOWN_BROWSER = "unknown_browser"
    UNKNOWN_ARTIFACT = "unknown_artifact"
    PATH_OUTSIDE_WORKSPACE = "path_outside_workspace"
    DUPLICATE_REQUEST_ID = "duplicate_request_id"
    TERMINAL_BUSY = "terminal_busy"
    TERMINAL_NOT_RUNNING = "terminal_not_running"
    TERMINAL_LOST = "terminal_lost"
    TERMINAL_BACKEND_UNAVAILABLE = "terminal_backend_unavailable"
    TERMINAL_BACKEND_DISABLED = "terminal_backend_disabled"
    TOOL_DISABLED = "tool_disabled"
    TOOL_REQUIRES_AGENT = "tool_requires_agent"
    QUOTA_EXCEEDED = "quota_exceeded"
    SERVICE_UNAVAILABLE = "service_unavailable"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    INTERNAL_ERROR = "internal_error"


# Mapping from error code to suggested HTTP status. Command exit codes never
# appear here: a nonzero shell command is a 200 response with ``is_error`` metadata.
_HTTP_STATUS: dict[ErrorCode, int] = {
    ErrorCode.INVALID_ARGUMENTS: 422,
    ErrorCode.UNKNOWN_TOOL: 404,
    ErrorCode.UNKNOWN_TERMINAL: 404,
    ErrorCode.UNKNOWN_BROWSER: 404,
    ErrorCode.UNKNOWN_ARTIFACT: 404,
    ErrorCode.PATH_OUTSIDE_WORKSPACE: 403,
    ErrorCode.DUPLICATE_REQUEST_ID: 409,
    ErrorCode.TERMINAL_BUSY: 409,
    ErrorCode.TERMINAL_NOT_RUNNING: 409,
    ErrorCode.TERMINAL_LOST: 409,
    ErrorCode.TERMINAL_BACKEND_UNAVAILABLE: 422,
    ErrorCode.TERMINAL_BACKEND_DISABLED: 422,
    ErrorCode.TOOL_DISABLED: 422,
    ErrorCode.TOOL_REQUIRES_AGENT: 422,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.SERVICE_UNAVAILABLE: 503,
    ErrorCode.DEADLINE_EXCEEDED: 504,
    ErrorCode.INTERNAL_ERROR: 500,
}


def http_status_for(code: ErrorCode) -> int:
    """Return the suggested HTTP status for an error code (defaults to 500)."""
    return _HTTP_STATUS.get(code, 500)


class ErrorResponse(BaseModel):
    """Stable error envelope."""

    error: ErrorCode
    message: str
    detail: Optional[dict[str, Any]] = None


class ServiceError(Exception):
    """Raised by the runtime for request/protocol/service failures (not command exit codes)."""

    def __init__(self, code: ErrorCode, message: str, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    @property
    def http_status(self) -> int:
        return http_status_for(self.code)

    def to_response(self) -> ErrorResponse:
        return ErrorResponse(error=self.code, message=self.message, detail=self.detail)
