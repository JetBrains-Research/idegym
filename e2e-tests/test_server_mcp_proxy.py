"""E2E test: orchestrator forwards MCP tool calls to a running IdeGYM server on demand.

Flow
----
1. A minimal ``EchoMcpPlugin`` image plugin writes a FastMCP echo server script and
   a supervisor config into the container.  The plugin declares an MCP upstream so
   ``server/mcp_proxy.py`` proxies the echo server through the IdeGYM server's own
   ``/mcp`` endpoint.

2. The image is built locally with ``IdeGYMDockerAPI`` and loaded into minikube (no
   Kaniko).

3. The orchestrator MCP client starts the server via ``START_SERVER``.

4. Once the server is ALIVE, the test uses ``LIST_SERVER_MCP_TOOLS`` to discover
   available tools on that specific server, and ``CALL_SERVER_MCP_TOOL`` to invoke
   them.  Both tools proxy directly to the server's ``/mcp`` endpoint on demand —
   no in-process mounting required, so the approach works across multiple orchestrator
   replicas.

5. The test verifies that the echo tool is accessible via ``CALL_SERVER_MCP_TOOL``
   and returns the correct response.

6. After ``STOP_SERVER`` the tool is unreachable: ``LIST_SERVER_MCP_TOOLS`` must
   report an error.

Tool-name chain
---------------
  echo server tool   "echo"
  → IdeGYM server mounts with namespace "echo"  →  "echo_echo"
  (orchestrator forwards calls to "echo_echo" on the target server)
"""

import asyncio
import base64 as _base64
import subprocess
import time
from typing import Optional
from uuid import UUID

import pytest
from fastmcp.exceptions import ToolError
from from_root import from_root
from idegym.api.orchestrator.mcp import MCPToolName
from idegym.api.plugin import BuildContext, PluginBase, image_plugin
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.image.builder import Image
from idegym.image.docker_api import IdeGYMDockerAPI
from idegym.plugins.defaults.image import IdeGYMServer, User
from utils.constants import DEFAULT_NAMESPACE, DEFAULT_SERVER_START_TIMEOUT
from utils.mcp_utils import create_mcp_client, parse_operation_result, wait_for_mcp_operation

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"
_ECHO_PORT = 7890

_DEFAULT_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="500m", memory="500Mi", ephemeral_storage="1Gi"),
    limits=ResourceQuantities(cpu="500m", memory="500Mi", ephemeral_storage="1Gi"),
)

# Minimal FastMCP echo server – runs inside the container via supervisor.
_ECHO_SERVER_SCRIPT = f"""\
#!/opt/idegym/.venv/bin/python
from fastmcp import FastMCP
import uvicorn

mcp = FastMCP("echo")


@mcp.tool
def echo(message: str) -> str:
    \"\"\"Echo back a message.\"\"\"
    return message


if __name__ == "__main__":
    app = mcp.http_app(path="/mcp")
    uvicorn.run(app, host="0.0.0.0", port={_ECHO_PORT})
"""

_ECHO_SUPERVISOR_CONF = """\
[program:echo-mcp]
command=/opt/idegym/.venv/bin/python /opt/idegym/echo_mcp_server.py
priority=5
autostart=true
autorestart=true
startsecs=2
stdout_logfile=/dev/stdout
stdout_logfile_maxbytes=0
stderr_logfile=/dev/stderr
stderr_logfile_maxbytes=0
redirect_stderr=false
"""


@image_plugin("echo")
class EchoMcpPlugin(PluginBase):
    """Image plugin: installs a minimal FastMCP echo server under supervisor.

    The plugin also declares an MCP upstream so ``Image.to_spec()`` writes
    ``/etc/idegym/mcp-upstreams.d/echo.json``, which causes the IdeGYM server
    to proxy the echo server through its own ``/mcp`` endpoint.
    """

    def render(self, ctx: BuildContext) -> str:
        # Base64-encode the file contents so the Dockerfile RUN instruction stays
        # single-line.  Multi-line shell-quoted strings inside RUN would cause
        # Docker to misparse lines beginning with "from" as FROM instructions.
        script_b64 = _base64.b64encode(_ECHO_SERVER_SCRIPT.encode()).decode()
        conf_b64 = _base64.b64encode(_ECHO_SUPERVISOR_CONF.encode()).decode()
        lines = []
        if ctx.current_user != "root":
            lines.append("USER root")
        lines.append(
            "RUN set -eux; \\\n"
            f"    printf '%s' '{script_b64}' | base64 -d > /opt/idegym/echo_mcp_server.py; \\\n"
            "    mkdir -p /etc/supervisor/conf.d; \\\n"
            f"    printf '%s' '{conf_b64}' | base64 -d > /etc/supervisor/conf.d/echo-mcp.conf"
        )
        if ctx.current_user != "root":
            lines.append(f"USER {ctx.current_user}")
        return "\n".join(lines)

    def get_mcp_upstream(self, ctx: BuildContext) -> Optional[str]:
        return f"http://localhost:{_ECHO_PORT}/mcp"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_and_load_echo_image(test_id: str) -> str:
    """Build a server image with the echo MCP plugin and load it into minikube."""
    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"echo-mcp-server-{test_id}")
        .with_plugin(User(username="appuser", uid=1000, gid=1000, sudo=True))
        # EchoMcpPlugin must come AFTER IdeGYMServer: IdeGYMServer's setup includes
        # a "chown -R appuser /opt/idegym" step that fails if root-owned files are
        # already present in that directory.
        .with_plugin(IdeGYMServer.from_local(root=from_root()))
        .with_plugin(EchoMcpPlugin())
    )
    built = IdeGYMDockerAPI().build_image(image)
    image_tag = str(built.repo_tags[0])
    subprocess.run(
        ["minikube", "image", "load", image_tag],
        check=True,
        capture_output=True,
        timeout=300,
    )
    return image_tag


async def _poll_for_server_tool(
    mcp,
    client_id: UUID,
    server_id: int,
    tool_name: str,
    timeout: float = 30.0,
) -> None:
    """Poll ``LIST_SERVER_MCP_TOOLS`` until *tool_name* appears on the server.

    The echo-server supervisor process needs a moment to start after the pod
    becomes ready, so we retry until the tool is visible or the timeout expires.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            result = await mcp.call_tool(
                MCPToolName.LIST_SERVER_MCP_TOOLS,
                {"request": {"client_id": str(client_id), "server_id": server_id}},
            )
            tools = result.structured_content.get("tools", [])
            if any(t["name"] == tool_name for t in tools):
                return
            tool_names = [t["name"] for t in tools]
        except Exception:
            tool_names = []

        if time.monotonic() >= deadline:
            raise AssertionError(
                f"Tool {tool_name!r} not found on server {server_id} after {timeout}s. Last seen tools: {tool_names}"
            )
        await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_forwards_server_mcp_echo_tool(test_id):
    """
    Full-stack verification of the on-demand MCP forwarding chain.

    After START_SERVER succeeds, LIST_SERVER_MCP_TOOLS must expose the echo
    tool on the target server and CALL_SERVER_MCP_TOOL must return the correct
    response.  After STOP_SERVER, LIST_SERVER_MCP_TOOLS must fail.
    """
    image_tag = _build_and_load_echo_image(test_id)

    server_name = f"echo-mcp-{test_id}"
    client_id = None
    active_server_id = None

    async with create_mcp_client(timeout=900.0) as mcp:
        try:
            # 1. Register a client -------------------------------------------------
            reg = await mcp.call_tool(
                MCPToolName.REGISTER_CLIENT,
                {
                    "request": {
                        "name": f"echo-mcp-client-{test_id}",
                        "namespace": DEFAULT_NAMESPACE,
                        "nodes_count": 0,
                    }
                },
            )
            client_id = UUID(reg.structured_content["id"])

            # 2. Start the echo-enabled server -------------------------------------
            start = await mcp.call_tool(
                MCPToolName.START_SERVER,
                {
                    "request": {
                        "client_id": str(client_id),
                        "namespace": DEFAULT_NAMESPACE,
                        "image_tag": image_tag,
                        "server_name": server_name,
                        "runtime_class_name": "gvisor",
                        "run_as_root": True,
                        "resources": _DEFAULT_RESOURCES.model_dump(),
                        "server_start_wait_timeout_in_seconds": DEFAULT_SERVER_START_TIMEOUT,
                    }
                },
            )
            start_status = await wait_for_mcp_operation(
                mcp,
                start.structured_content["operation_id"],
                timeout=DEFAULT_SERVER_START_TIMEOUT,
                poll_interval=2.0,
            )
            start_resp = parse_operation_result(start_status)
            server_id: int = start_resp["server_id"]
            active_server_id = server_id

            # 3. Wait for echo tool to appear on the server -------------------------
            # The IdeGYM server mounts the echo upstream with namespace "echo",
            # making the tool name "echo_echo".
            expected_tool = "echo_echo"
            await _poll_for_server_tool(mcp, client_id, server_id, expected_tool, timeout=30.0)

            # Fetch the full tool list once the server is ready and verify both
            # the proxied echo tool and the built-in file tools are present.
            list_result = await mcp.call_tool(
                MCPToolName.LIST_SERVER_MCP_TOOLS,
                {"request": {"client_id": str(client_id), "server_id": server_id}},
            )
            all_tool_names = {t["name"] for t in list_result.structured_content.get("tools", [])}
            assert expected_tool in all_tool_names, (
                f"Expected proxied tool {expected_tool!r} in server tools: {all_tool_names}"
            )
            for file_tool in ("create_file", "edit_file", "patch_file"):
                assert file_tool in all_tool_names, (
                    f"Expected built-in file tool {file_tool!r} in server tools: {all_tool_names}"
                )

            # 4. Call the echo tool through the orchestrator -----------------------
            echo_result = await mcp.call_tool(
                MCPToolName.CALL_SERVER_MCP_TOOL,
                {
                    "request": {
                        "client_id": str(client_id),
                        "server_id": server_id,
                        "tool_name": expected_tool,
                        "arguments": {"message": "hello e2e"},
                    }
                },
            )
            assert not echo_result.structured_content.get("is_error"), (
                f"Tool call returned an error: {echo_result.structured_content!r}"
            )
            content = echo_result.structured_content.get("content", [])
            text_items = [c["text"] for c in content if c.get("type") == "text"]
            assert text_items == ["hello e2e"], f"Unexpected echo content: {content!r}"

            # 5. Stop the server ---------------------------------------------------
            stop = await mcp.call_tool(
                MCPToolName.STOP_SERVER,
                {
                    "request": {
                        "client_id": str(client_id),
                        "namespace": DEFAULT_NAMESPACE,
                        "server_id": server_id,
                    }
                },
            )
            await wait_for_mcp_operation(mcp, stop.structured_content["operation_id"], timeout=120.0)
            active_server_id = None

            # 6. Tool must be unreachable after stop --------------------------------
            with pytest.raises(ToolError):
                await mcp.call_tool(
                    MCPToolName.LIST_SERVER_MCP_TOOLS,
                    {"request": {"client_id": str(client_id), "server_id": server_id}},
                )

        finally:
            if active_server_id is not None and client_id is not None:
                try:
                    stop = await mcp.call_tool(
                        MCPToolName.STOP_SERVER,
                        {
                            "request": {
                                "client_id": str(client_id),
                                "namespace": DEFAULT_NAMESPACE,
                                "server_id": active_server_id,
                            }
                        },
                    )
                    op_id = stop.structured_content.get("operation_id")
                    if op_id is not None:
                        await wait_for_mcp_operation(mcp, op_id, timeout=120.0)
                except Exception:
                    pass

            if client_id is not None:
                try:
                    stop_client = await mcp.call_tool(
                        MCPToolName.STOP_CLIENT,
                        {
                            "request": {
                                "client_id": str(client_id),
                                "namespace": DEFAULT_NAMESPACE,
                            }
                        },
                    )
                    op_id = stop_client.structured_content.get("operation_id")
                    if op_id is not None:
                        await wait_for_mcp_operation(mcp, op_id, timeout=120.0)
                except Exception:
                    pass
