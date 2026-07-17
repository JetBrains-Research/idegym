"""Unit tests for the shared OpenHands-plugin API models and error mapping."""

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import (
    CallStatus,
    ContentBlock,
    TerminalBackend,
    ToolActionRequest,
    ToolCallResult,
)
from idegym.plugins.openhands.api.names import API_VERSION, MCP_NAMESPACE, PLUGIN_NAME

pytestmark = pytest.mark.unit


def test_error_code_http_status_mapping():
    # Each code carries its own suggested HTTP status, and stays a plain string value.
    assert ErrorCode.UNKNOWN_TOOL.http_status == 404
    assert ErrorCode.PATH_OUTSIDE_WORKSPACE.http_status == 403
    assert ErrorCode.DUPLICATE_REQUEST_ID.http_status == 409
    assert ErrorCode.TERMINAL_BACKEND_UNAVAILABLE.http_status == 422
    assert ErrorCode.SERVICE_UNAVAILABLE.http_status == 503
    assert ErrorCode.DEADLINE_EXCEEDED.http_status == 504
    assert ErrorCode.UNKNOWN_TOOL == "unknown_tool" and ErrorCode.UNKNOWN_TOOL.value == "unknown_tool"


def test_service_error_envelope():
    err = ServiceError(ErrorCode.TERMINAL_BUSY, "busy", {"terminal_id": "t"})
    assert err.http_status == 409
    payload = err.to_response()
    assert payload.error == ErrorCode.TERMINAL_BUSY
    assert payload.detail == {"terminal_id": "t"}


def test_request_models_reject_unknown_fields():
    with pytest.raises(Exception):
        ToolActionRequest.model_validate({"arguments": {}, "context": {}, "typo": 1})


def test_tool_call_result_text_helper():
    result = ToolCallResult(
        call_id="c",
        tool="grep",
        status=CallStatus.COMPLETED,
        content=[ContentBlock.of_text("hello "), ContentBlock.of_text("world")],
    )
    assert result.text() == "hello world"


def test_names_are_stable():
    assert PLUGIN_NAME == MCP_NAMESPACE == "openhands"
    assert API_VERSION == "1"
    assert TerminalBackend.TMUX == "tmux" and TerminalBackend.SUBPROCESS == "subprocess"
