"""Unit tests for the ToolRuntime dispatch, dedup, path policy, and reset."""

import pytest
from idegym.plugins.openhands.api.errors import ErrorCode, ServiceError
from idegym.plugins.openhands.api.models import (
    CallStatus,
    Profile,
    SupportStatus,
    TerminalBackend,
    TerminalCreateRequest,
)
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.service import ToolRuntime

pytestmark = pytest.mark.unit

SUB = TerminalBackend.SUBPROCESS
_OPENHANDS = compat.openhands_available()


def _config(tmp_path, **overrides):
    return RuntimeConfig(
        workspace_root=str(tmp_path),
        state_dir=str(tmp_path / "state"),
        output_dir=str(tmp_path / "art"),
        log_dir=str(tmp_path / "log"),
        default_terminal_backend=SUB,
        allowed_terminal_backends=[SUB],
        no_change_timeout_seconds=1.5,
        max_output_bytes=200,
        **overrides,
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


async def test_disabled_terminal_rejects_every_dispatch(tmp_path):
    """OH-01: disabling the terminal tool must block generic dispatch and lifecycle mutations."""
    rt = ToolRuntime(_config(tmp_path, disabled_tools=["terminal"]))
    await rt.start()
    try:
        assert not rt.terminal_enabled()
        # generic + canonical dispatch rejected before the command can run
        with pytest.raises(ServiceError) as exc:
            await rt.call_tool("terminal", {"command": "echo pwned"})
        assert exc.value.code == ErrorCode.TOOL_DISABLED
        # every lifecycle facade method rejects (direct runtime calls must also reject)
        with pytest.raises(ServiceError):
            await rt.terminal_create(TerminalCreateRequest(backend=SUB))
        with pytest.raises(ServiceError):
            await rt.terminal_execute("default", "echo pwned")
        with pytest.raises(ServiceError):
            await rt.terminal_reset_all()
        # readiness must not require a terminal backend when the terminal is disabled
        assert rt.health().ready
    finally:
        await rt.stop()


def test_path_boundary_rejects_escape(tmp_path):
    rt = ToolRuntime(_config(tmp_path))
    with pytest.raises(ServiceError) as exc:
        rt._normalize_path("../../etc/passwd")
    assert exc.value.code == ErrorCode.PATH_OUTSIDE_WORKSPACE


def test_lock_requests_reflect_tool_resources(tmp_path):
    """OH-10: lock requests must model real resources (workspace mutation vs contained file ops)."""
    rt = ToolRuntime(_config(tmp_path))
    ws = f"workspace:{rt.config.workspace_root}"

    def reqs(name, args=None):
        return set(rt._lock_requests(rt.catalog.get(name), args or {}))

    file_a = f"file:{rt._normalize_path('a.txt')}"
    # apply_patch mutates the workspace -> exclusive workspace lock (conflicts with all file ops)
    assert reqs("apply_patch") == {(ws, True)}
    # a file write: shared workspace lease + exclusive per-file lock
    assert reqs("write_file", {"file_path": "a.txt"}) == {(ws, False), (file_a, True)}
    # read_file declares the same file resource, so it conflicts with a write to that file
    assert reqs("read_file", {"path": "a.txt"}) == {(ws, False), (file_a, True)}
    # grep is a parallel-safe read: shared workspace only
    assert reqs("grep", {"pattern": "x"}) == {(ws, False)}
    # glob's Python fallback uses process-global chdir -> tool-wide exclusive lock
    assert reqs("glob", {"pattern": "**/*.py"}) == {(ws, False), ("tool:glob", True)}


def test_read_search_tools_enforce_workspace_boundary(tmp_path):
    """OH-02: workspace boundary must apply to read/search tools (LockScope.NONE), not just writes."""
    rt = ToolRuntime(_config(tmp_path))
    grep = rt.catalog.get("grep")
    glob = rt.catalog.get("glob")
    read_file = rt.catalog.get("read_file")
    ls = rt.catalog.get("list_directory")

    # sibling / absolute / traversal / dir_path escapes rejected for every filesystem tool
    escapes = [
        (grep, {"pattern": "x", "path": "/etc"}),
        (read_file, {"path": "/etc/passwd"}),
        (read_file, {"path": "../../etc/passwd"}),
        (ls, {"dir_path": "/etc"}),
    ]
    for entry, args in escapes:
        with pytest.raises(ServiceError) as exc:
            rt._validate_action_paths(entry, args)
        assert exc.value.code == ErrorCode.PATH_OUTSIDE_WORKSPACE

    # absolute and traversal glob patterns rejected (search root parsed out of the pattern)
    for pattern in ("/etc/*.conf", "../../*.py"):
        with pytest.raises(ServiceError):
            rt._validate_action_paths(glob, {"pattern": pattern})

    # symlink escape is caught at canonicalization (Path.resolve follows the link)
    (tmp_path / "escape").symlink_to("/etc")
    with pytest.raises(ServiceError):
        rt._validate_action_paths(read_file, {"path": "escape/passwd"})

    # in-workspace paths are accepted and canonicalized to absolute
    (tmp_path / "sub").mkdir()
    out = rt._validate_action_paths(read_file, {"path": "sub"})
    assert out["path"] == str((tmp_path / "sub").resolve())
    rt._validate_action_paths(glob, {"pattern": "sub/**/*.py"})  # relative pattern within workspace is fine


async def test_capability_status_matches_callable_set(tmp_path, monkeypatch):
    """OH-16: capability/tool-list/readiness must agree with the actually-callable set."""
    # openhands reports available, but importing its tools fails -> every adapter build errors.
    monkeypatch.setattr(compat, "openhands_available", lambda: True)
    rt = ToolRuntime(_config(tmp_path, profile=Profile.FULL, browser_enabled=True))
    await rt.start()
    try:
        caps = {c.name: c.status for c in rt.list_capabilities()}
        # browser is in-profile but has no adapter/route/MCP tool -> never advertised callable
        assert caps["browser_use"] != SupportStatus.ENABLED
        # filesystem/search tools whose adapters failed to build are incompatible, not enabled
        assert caps["read_file"] == SupportStatus.ADAPTER_INCOMPATIBLE
        assert caps["grep"] == SupportStatus.ADAPTER_INCOMPATIBLE
        # capabilities and the tool list agree exactly (only the terminal is callable here)
        listed = {t.name for t in rt.list_tools()}
        enabled = {n for n, s in caps.items() if s == SupportStatus.ENABLED}
        assert enabled == listed == {"terminal"}
        # an in-profile tool that could not be constructed fails readiness
        assert rt.health().ready is False
        # build errors are exposed in diagnostics
        assert set(rt.diagnostics().adapter_errors) >= {"gemini", "grep"}
    finally:
        await rt.stop()


def test_prepare_purges_orphaned_artifacts(tmp_path):
    """OH-14: startup must discard artifact files left by a previous process."""
    from pathlib import Path

    cfg = _config(tmp_path)
    Path(cfg.output_dir).mkdir(parents=True, exist_ok=True)
    orphan = Path(cfg.output_dir) / "deadbeef"
    orphan.write_bytes(b"stale")
    ToolRuntime(cfg).prepare()
    assert not orphan.exists()


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
