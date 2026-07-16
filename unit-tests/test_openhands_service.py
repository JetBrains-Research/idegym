"""REST + MCP contract tests for the loopback service, including cross-surface state sharing."""

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from idegym.plugins.openhands.api.models import ContentBlock, TerminalBackend, ToolCallResult, ToolDescriptor
from idegym.plugins.openhands.runtime import compat
from idegym.plugins.openhands.runtime.config import RuntimeConfig
from idegym.plugins.openhands.runtime.service import ToolRuntime
from idegym.plugins.openhands.service.app import build_app
from idegym.plugins.openhands.service.mcp import build_mcp_server

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
        **overrides,
    )


@pytest.fixture
def client(tmp_path):
    app = build_app(_config(tmp_path))
    with TestClient(app) as c:
        yield c


def test_health_and_readyz(client):
    assert client.get("/v1/health").json()["ready"] is True
    assert client.get("/readyz").status_code == 200


def test_per_tool_routes_in_openapi(client):
    paths = client.get("/openapi.json").json()["paths"]
    # Each enabled tool gets its own operation.
    for name in ("terminal", "grep", "file_editor", "apply_patch", "read_file", "write_file"):
        assert f"/v1/tools/{name}" in paths, name


def test_capabilities_and_tools(client):
    caps = client.get("/v1/capabilities").json()
    assert caps["backends"]["default"] == "subprocess"
    assert {c["name"] for c in caps["capabilities"]} >= {"terminal", "grep", "task"}
    tool_names = {t["name"] for t in client.get("/v1/tools").json()}
    if _OPENHANDS:
        assert {"terminal", "grep", "file_editor"} <= tool_names
    else:
        assert tool_names == {"terminal"}


def test_terminal_lifecycle_and_state(client):
    tid = client.post("/v1/terminals", json={"backend": "subprocess", "name": "t"}).json()["terminal_id"]
    client.post(f"/v1/terminals/{tid}/execute", json={"command": "export S=svc && cd /tmp"})
    res = client.post(f"/v1/terminals/{tid}/execute", json={"command": "echo S=$S at $(pwd)"}).json()
    assert "S=svc" in res["output"] and "/tmp" in res["output"]
    assert client.get(f"/v1/terminals/{tid}").json()["backend"] == "subprocess"
    assert client.request("DELETE", f"/v1/terminals/{tid}").json()["closed"] == tid


def test_error_mapping(client):
    # tool requiring a runtime dependency -> 422 tool_disabled
    r = client.post("/v1/tools/grep", json={"arguments": {"pattern": "x"}})
    assert r.status_code == 422 and r.json()["error"] == "tool_disabled"
    # unknown terminal -> 404
    r = client.post("/v1/terminals/nope/execute", json={"command": "echo x"})
    assert r.status_code == 404 and r.json()["error"] == "unknown_terminal"
    # disabled backend -> 422 (no fallback)
    r = client.post("/v1/terminals", json={"backend": "tmux"})
    assert r.status_code == 422 and r.json()["error"] == "terminal_backend_disabled"


def test_reset_route(client):
    assert client.post("/v1/reset").json()["environment_generation"] >= 1


def test_disabled_terminal_hides_rest_routes(tmp_path):
    """OH-01: with the terminal disabled, no terminal route is registered and /call rejects it."""
    app = build_app(_config(tmp_path, disabled_tools=["terminal"]))
    with TestClient(app) as c:
        paths = c.get("/openapi.json").json()["paths"]
        assert not any(p.startswith("/v1/terminals") for p in paths)
        assert "/v1/tools/terminal" not in paths
        # generic dispatch is rejected before any command runs
        r = c.post("/v1/call", json={"tool": "terminal", "arguments": {"command": "echo pwned"}})
        assert r.status_code == 422 and r.json()["error"] == "tool_disabled"
        # the lifecycle create route is absent (404), not merely method-guarded
        assert c.post("/v1/terminals", json={"backend": "subprocess"}).status_code == 404
        # readiness holds without a terminal backend
        assert c.get("/v1/health").json()["ready"] is True


async def test_mcp_publishes_native_input_schema(tmp_path, monkeypatch):
    """OH-03: MCP inputSchema must equal the native tool schema; dispatch uses flat native args."""
    rt = ToolRuntime(_config(tmp_path))
    rt.prepare()

    native_schema = {
        "type": "object",
        "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}},
        "required": ["pattern"],
    }
    grep = ToolDescriptor(name="grep", family="grep", description="search", input_schema=native_schema)
    monkeypatch.setattr(rt, "list_tools", lambda: [rt._terminal_descriptor(), grep])

    seen: dict = {}

    async def fake_call_tool(name, arguments, *, terminal_id=None, request_id=None):
        seen["name"], seen["arguments"], seen["terminal_id"] = name, arguments, terminal_id
        return ToolCallResult(call_id="x", tool=name, content=[ContentBlock.of_text("ok")])

    monkeypatch.setattr(rt, "call_tool", fake_call_tool)

    server = build_mcp_server(rt)
    async with Client(server) as mcp:
        tools = {t.name: t for t in await mcp.list_tools()}
        # published schema is the native flat schema, not a wrapper {"arguments": ...}
        assert tools["grep"].inputSchema == native_schema
        assert "arguments" not in tools["grep"].inputSchema.get("properties", {})
        await mcp.call_tool("grep", {"pattern": "x", "path": "sub"})
    # dispatched flat native arguments, with transport context kept separate (no terminal_id leak)
    assert seen == {"name": "grep", "arguments": {"pattern": "x", "path": "sub"}, "terminal_id": None}


async def test_disabled_terminal_absent_from_mcp(tmp_path):
    """OH-01: the MCP tool list must omit the terminal and every lifecycle tool when disabled."""
    rt = ToolRuntime(_config(tmp_path, disabled_tools=["terminal"]))
    rt.prepare()
    server = build_mcp_server(rt)
    async with Client(server) as mcp:
        names = {t.name for t in await mcp.list_tools()}
    assert not any(n == "terminal" or n.startswith("terminal_") for n in names)


async def test_mcp_tool_list_matches_rest(tmp_path):
    rt = ToolRuntime(_config(tmp_path))
    rt.prepare()
    server = build_mcp_server(rt)
    async with Client(server) as mcp:
        names = {t.name for t in await mcp.list_tools()}
    # canonical terminal + lifecycle tools present; names consistent across surfaces.
    assert {"terminal", "terminal_create", "terminal_execute", "terminal_reset_all"} <= names


async def test_cross_surface_state_rest_then_mcp(tmp_path):
    """A terminal created over REST is usable over MCP by the same id.

    REST (via ASGI transport) and the in-memory MCP client share one runtime and one event loop.
    """
    import httpx

    cfg = _config(tmp_path)
    rt = ToolRuntime(cfg)
    await rt.start()
    app = build_app(cfg, rt)
    server = build_mcp_server(rt)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://svc") as http:
            created = await http.post("/v1/terminals", json={"backend": "subprocess"})
            tid = created.json()["terminal_id"]
            await http.post(f"/v1/terminals/{tid}/execute", json={"command": "export CROSS=surface"})
            async with Client(server) as mcp:
                res = await mcp.call_tool("terminal_execute", {"terminal_id": tid, "command": "echo V=$CROSS"})
                assert "surface" in str(res.structured_content)
    finally:
        await rt.stop()
