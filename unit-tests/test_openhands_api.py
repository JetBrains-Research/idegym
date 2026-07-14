"""Unit tests for the shared OpenHands-plugin API models and error mapping."""

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError, http_status_for
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
    assert http_status_for(ErrorCode.UNKNOWN_TOOL) == 404
    assert http_status_for(ErrorCode.PATH_OUTSIDE_WORKSPACE) == 403
    assert http_status_for(ErrorCode.DUPLICATE_REQUEST_ID) == 409
    assert http_status_for(ErrorCode.TERMINAL_BACKEND_UNAVAILABLE) == 422
    assert http_status_for(ErrorCode.SERVICE_UNAVAILABLE) == 503
    assert http_status_for(ErrorCode.DEADLINE_EXCEEDED) == 504


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
