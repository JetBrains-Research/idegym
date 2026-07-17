"""Adapter-contract tests that run without OpenHands installed (via a minimal fake tool)."""

from types import SimpleNamespace

import pytest
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.adapters.base import AdapterRun, ToolAdapter
from idegym.plugins.openhands.runtime.adapters.openhands import OpenHandsToolAdapter

pytestmark = pytest.mark.unit


class _FakeTool:
    """The minimal slice of the OpenHands tool surface the adapter drives."""

    name = "grep"
    description = "search files"
    annotations = SimpleNamespace(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    def to_mcp_tool(self) -> dict:
        return {
            "name": "grep",
            "description": "search files",
            "inputSchema": {"type": "object", "properties": {"pattern": {"type": "string"}}},
        }

    def action_from_arguments(self, arguments: dict):
        return SimpleNamespace(**arguments)

    async def acall(self, action, conversation=None):
        return SimpleNamespace(content=[], is_error=False, model_dump=lambda mode="json": {"ok": True})


def test_tool_conforms_to_the_openhands_tool_protocol():
    # The typed tool surface the adapter drives (dispatch/schema/annotations) is captured by a
    # Protocol rather than Any, so a conforming tool is recognised structurally.
    assert isinstance(_FakeTool(), compat.OpenHandsTool)


def test_adapter_implements_the_tool_adapter_protocol():
    # It explicitly inherits the protocol (nominal) and satisfies it structurally.
    assert ToolAdapter in OpenHandsToolAdapter.__mro__
    adapter = OpenHandsToolAdapter("grep", _FakeTool())
    assert isinstance(adapter, ToolAdapter)
    assert adapter.name == "grep"
    assert adapter.family == "grep"
    assert adapter.input_schema["type"] == "object"
    assert adapter.annotations == {"read_only": True, "destructive": False, "idempotent": True, "open_world": False}


async def test_adapter_run_returns_an_adapter_run():
    adapter = OpenHandsToolAdapter("grep", _FakeTool())
    run = await adapter.run({"pattern": "x"})
    assert isinstance(run, AdapterRun)
    assert run.is_error is False
    assert run.structured == {"ok": True}
