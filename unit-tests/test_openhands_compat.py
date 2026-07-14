"""OpenHands compatibility tests.

These verify the agentless construction paths against the pinned OpenHands version and are skipped
when ``openhands-tools`` is not installed (dev checkout / client-only path). In the container image
and in a combined venv they exercise the real OpenHands executors, tool definitions, MCP schema
generation, and terminal session.
"""

import pytest
from idegym.plugins.openhands.runtime import compat

pytestmark = pytest.mark.unit

openhands = pytest.importorskip("openhands.tools", reason="openhands-tools not installed")


def test_versions_reported():
    versions = compat.openhands_versions()
    assert versions["openhands-tools"] is not None


def test_installed_families_match_manifest():
    assert set(compat.list_tool_family_modules()) == set(compat.KNOWN_TOOL_FAMILIES)


async def test_build_family_tools_and_acall(tmp_path):
    from idegym.plugins.openhands.runtime.adapters.openhands import OpenHandsToolAdapter

    (tmp_path / "a.py").write_text("x = 1\n# TODO fixme\n")
    tools = compat.build_family_tools(
        "grep",
        working_dir=str(tmp_path),
        persistence_dir=str(tmp_path / "p"),
        env_persistence_dir=str(tmp_path / "e"),
    )
    assert tools, "grep should yield at least one tool"
    adapter = OpenHandsToolAdapter("grep", tools[0])
    assert adapter.name == "grep"
    assert "pattern" in adapter.input_schema.get("properties", {})
    run = await adapter.run({"pattern": "TODO", "path": str(tmp_path)})
    assert not run.is_error
    assert any("a.py" in (b.text or "") for b in run.content)


async def test_file_editor_built_without_agent(tmp_path):
    from idegym.plugins.openhands.runtime.adapters.openhands import OpenHandsToolAdapter

    target = tmp_path / "f.txt"
    target.write_text("original\n")
    tools = compat.build_family_tools(
        "file_editor",
        working_dir=str(tmp_path),
        persistence_dir=str(tmp_path / "p"),
        env_persistence_dir=str(tmp_path / "e"),
    )
    adapter = OpenHandsToolAdapter("file_editor", tools[0])
    assert adapter.name == "file_editor"
    run = await adapter.run({"command": "view", "path": str(target)})
    assert "original" in "".join(b.text or "" for b in run.content)


async def test_openhands_subprocess_terminal_state(tmp_path):
    """The OpenHands-backed subprocess terminal preserves cwd/env across calls."""
    from idegym.plugins.openhands.api.models import TerminalBackend, TerminalCreateRequest
    from idegym.plugins.openhands.runtime.config import RuntimeConfig
    from idegym.plugins.openhands.runtime.terminal.manager import TerminalSessionManager

    (tmp_path / "sub").mkdir()
    config = RuntimeConfig(
        workspace_root=str(tmp_path),
        default_terminal_backend=TerminalBackend.SUBPROCESS,
        allowed_terminal_backends=[TerminalBackend.SUBPROCESS],
        no_change_timeout_seconds=2.0,
    )
    manager = TerminalSessionManager(config, lambda: "env")
    manager.probe_backends()
    descriptor = await manager.create(TerminalCreateRequest(backend=TerminalBackend.SUBPROCESS))
    await manager.execute(descriptor.terminal_id, "cd sub && export OHVAR=42")
    result = await manager.execute(descriptor.terminal_id, "echo V=$OHVAR at $(pwd)")
    assert "V=42" in result.output and "/sub" in result.output
    await manager.reset_all()
