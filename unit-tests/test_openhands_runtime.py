"""Unit tests for the ToolRuntime dispatch, dedup, path policy, and reset."""

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import CallStatus, SupportStatus, TerminalBackend
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.service import ToolRuntime

pytestmark = pytest.mark.unit

SUB = TerminalBackend.SUBPROCESS
_OPENHANDS = compat.openhands_available()


def _config(tmp_path):
    return RuntimeConfig(
        workspace_root=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        output_dir=str(tmp_path / "art"),
        log_dir=str(tmp_path / "log"),
        default_terminal_backend=SUB,
        allowed_terminal_backends=[SUB],
        no_change_timeout_seconds=1.5,
        max_output_bytes=200,
    )


@pytest.fixture
async def runtime(tmp_path):
    rt = ToolRuntime(_config(tmp_path))
    await rt.start()
    yield rt
    await rt.stop()


async def test_capabilities_classify_all_families(runtime):
    caps = {c.name: c.status for c in runtime.list_capabilities()}
    assert caps["terminal"] == SupportStatus.ENABLED
    assert caps["task"] == SupportStatus.UNSUPPORTED_REQUIRES_AGENT
    # File tools are always classified (never omitted): enabled when OpenHands is installed,
    # otherwise reported as a missing dependency.
    expected = SupportStatus.ENABLED if _OPENHANDS else SupportStatus.MISSING_DEPENDENCY
    assert caps["grep"] == expected
    assert set(caps) >= {"terminal", "grep", "task", "workflow", "delegate", "preset", "utils", "browser_use"}


async def test_health_ready(runtime):
    h = runtime.health()
    assert h.live and h.ready and h.checks["default_backend"]


async def test_terminal_tool_runs_and_persists_state(runtime):
    r1 = await runtime.call_tool("terminal", {"command": "cd /tmp && export K=9"})
    assert r1.status == CallStatus.COMPLETED
    r2 = await runtime.call_tool("terminal", {"command": "echo K=$K at $(pwd)"})
    assert "K=9" in r2.text() and "/tmp" in r2.text()


async def test_unknown_tool(runtime):
    with pytest.raises(ServiceError) as exc:
        await runtime.call_tool("nope", {})
    assert exc.value.code == ErrorCode.UNKNOWN_TOOL


async def test_agent_tool_reports_requires_agent(runtime):
    with pytest.raises(ServiceError) as exc:
        await runtime.call_tool("task", {})
    assert exc.value.code == ErrorCode.TOOL_REQUIRES_AGENT


async def test_grep_dispatch_depends_on_openhands(runtime):
    if _OPENHANDS:
        # With OpenHands installed the tool is callable and returns a result envelope.
        result = await runtime.call_tool("grep", {"pattern": "x", "path": "."})
        assert result.tool == "grep"
    else:
        with pytest.raises(ServiceError) as exc:
            await runtime.call_tool("grep", {"pattern": "x"})
        assert exc.value.code == ErrorCode.TOOL_DISABLED


async def test_dedup_same_and_conflict(runtime):
    a = await runtime.call_tool("terminal", {"command": "echo one"}, request_id="r1")
    b = await runtime.call_tool("terminal", {"command": "echo one"}, request_id="r1")
    assert a.call_id == b.call_id
    with pytest.raises(ServiceError) as exc:
        await runtime.call_tool("terminal", {"command": "echo two"}, request_id="r1")
    assert exc.value.code == ErrorCode.DUPLICATE_REQUEST_ID


async def test_reset_bumps_generation_and_terminates(runtime):
    await runtime.terminals.ensure_default()
    before = runtime.health().environment_generation
    resp = await runtime.reset_environment("test")
    assert resp.environment_generation == before + 1
    assert resp.terminated_terminals >= 1


def test_path_boundary_rejects_escape(tmp_path):
    rt = ToolRuntime(_config(tmp_path))
    with pytest.raises(ServiceError) as exc:
        rt._normalize_path("../../etc/passwd")
    assert exc.value.code == ErrorCode.PATH_OUTSIDE_WORKSPACE


def test_list_tools_reflects_openhands_availability(tmp_path):
    rt = ToolRuntime(_config(tmp_path))
    rt.prepare()
    names = {t.name for t in rt.list_tools()}
    assert "terminal" in names
    if _OPENHANDS:
        # With OpenHands the enabled file/search tools are callable and listed.
        assert {"grep", "glob", "file_editor", "read_file"} <= names
    else:
        # Without OpenHands only the terminal tool is callable; file tools need the runtime dep.
        assert names == {"terminal"}
