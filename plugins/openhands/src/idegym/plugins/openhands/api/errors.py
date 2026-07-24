"""Stable error codes and the error envelope shared across transports.

The runtime raises :class:`ServiceError` with a machine-readable :class:`ErrorCode`. Each code
carries its own suggested HTTP status (``code.http_status``), so the REST layer maps a code to a
status by asking the code itself; the MCP layer maps protocol-level codes to JSON-RPC errors and
everything else to ``isError`` tool results.
"""

from enum import StrEnum
from typing import Any, Optional, Self


class ErrorCode(StrEnum):
    """Machine-readable error codes, each carrying its own suggested HTTP status.

    The status travels with the code (``ErrorCode.TERMINAL_BUSY.http_status == 409``) so callers map
    an error by asking the code itself rather than consulting a separate table. Command exit codes
    never appear here: a nonzero shell command is a 200 response with ``is_error`` metadata.
    """

    # http_status is attached to each member in __new__; declared here for type-checkers.
    http_status: int

    def __new__(cls, value: str, http_status: int = 500) -> Self:
        obj = str.__new__(cls, value)
        obj._value_ = value
        obj.http_status = http_status
        return obj

    INVALID_ARGUMENTS = ("invalid_arguments", 422)
    UNKNOWN_TOOL = ("unknown_tool", 404)
    UNKNOWN_TERMINAL = ("unknown_terminal", 404)
    UNKNOWN_BROWSER = ("unknown_browser", 404)
    UNKNOWN_ARTIFACT = ("unknown_artifact", 404)
    PATH_OUTSIDE_WORKSPACE = ("path_outside_workspace", 403)
    DUPLICATE_REQUEST_ID = ("duplicate_request_id", 409)
    TERMINAL_BUSY = ("terminal_busy", 409)
    TERMINAL_NOT_RUNNING = ("terminal_not_running", 409)
    TERMINAL_LOST = ("terminal_lost", 409)
    TERMINAL_BACKEND_UNAVAILABLE = ("terminal_backend_unavailable", 422)
    TERMINAL_BACKEND_DISABLED = ("terminal_backend_disabled", 422)
    TOOL_DISABLED = ("tool_disabled", 422)
    TOOL_REQUIRES_AGENT = ("tool_requires_agent", 422)
    QUOTA_EXCEEDED = ("quota_exceeded", 429)
    SERVICE_UNAVAILABLE = ("service_unavailable", 503)
    DEADLINE_EXCEEDED = ("deadline_exceeded", 504)
    INTERNAL_ERROR = ("internal_error", 500)


class ServiceError(Exception):
    """Raised by the runtime for request/protocol/service failures (not command exit codes)."""

    def __init__(self, code: ErrorCode, message: str, detail: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail

    @property
    def http_status(self) -> int:
        return self.code.http_status

    def to_dict(self) -> dict[str, Any]:
        """The JSON-serialisable error envelope: ``{error, message, detail}``."""
        return {"error": self.code.value, "message": self.message, "detail": self.detail}
