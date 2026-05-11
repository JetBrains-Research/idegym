"""E2E test: orchestrator dynamically exposes echo MCP tool from a running IdeGYM server.

Flow
----
1. A minimal ``EchoMcpPlugin`` image plugin writes a FastMCP echo server script and
   a supervisor config into the container.  The plugin declares an MCP upstream so
   ``server/mcp.py`` proxies the echo server through the IdeGYM server's own ``/mcp``
   endpoint.

2. The image is built locally with ``IdeGYMDockerAPI`` and loaded into minikube (no
   Kaniko).

3. The orchestrator MCP client starts the server via ``START_SERVER``.

4. Once the server is ALIVE, ``server_mcp_registry`` mounts the server's ``/mcp``
   endpoint onto the orchestrator's FastMCP with ``namespace=generated_name``.

5. The test verifies that the echo tool is accessible through the orchestrator's MCP
   under the expected double-namespaced name and returns the correct response.

6. After ``STOP_SERVER`` the tool must disappear from the orchestrator's tool list.

Tool-name chain
---------------
  echo server tool   "echo"
  → IdeGYM server mounts with namespace "echo"    →  "echo_echo"
  → orchestrator mounts with namespace <generated_name>  →  "<generated_name>_echo_echo"
"""

import base64 as _base64
import subprocess
import time
from typing import Optional

import pytest
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


async def _poll_for_tool(mcp, tool_name: str, timeout: float = 30.0) -> None:
    """Retry listing orchestrator tools until *tool_name* appears.

    The echo-server supervisor process needs a moment to start; the proxy
    connection from the orchestrator to the IdeGYM server is lazy, so the
    first successful ``list_tools()`` call confirms end-to-end reachability.
    """
    deadline = time.monotonic() + timeout
    while True:
        tools = await mcp.list_tools()
        if tool_name in {t.name for t in tools}:
            return
        if time.monotonic() >= deadline:
            names = {t.name for t in tools}
            raise AssertionError(
                f"Tool {tool_name!r} not found after {timeout}s. "
                f"Tools containing 'echo': {sorted(t for t in names if 'echo' in t)}"
            )
        import asyncio

        await asyncio.sleep(2.0)


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestrator_exposes_server_mcp_echo_tool(test_id):
    """
    Full-stack verification of the dynamic MCP proxy chain.

    After START_SERVER succeeds, the orchestrator must expose the echo tool
    namespaced by the server's generated name.  After STOP_SERVER it must
    be absent.
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
            client_id = reg.structured_content["id"]

            # 2. Start the echo-enabled server -------------------------------------
            start = await mcp.call_tool(
                MCPToolName.START_SERVER,
                {
                    "request": {
                        "client_id": client_id,
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
            server_id = start_resp["server_id"]
            active_server_id = server_id

            # generated_name mirrors the DB logic: "{server_name}-{server_id}"
            generated_name = f"{server_name}-{server_id}"

            # 3. Verify echo tool appears in orchestrator MCP ----------------------
            # Double-namespaced:
            #   "echo"  →  "echo_echo" (IdeGYM server)  →  "{generated_name}_echo_echo" (orchestrator)
            expected_tool = f"{generated_name}_echo_echo"
            await _poll_for_tool(mcp, expected_tool, timeout=30.0)

            # 4. Call the echo tool through the orchestrator -----------------------
            echo_result = await mcp.call_tool(expected_tool, {"message": "hello e2e"})
            assert echo_result.structured_content.get("result") == "hello e2e", (
                f"Unexpected echo result: {echo_result.structured_content!r}"
            )

            # 5. Stop the server ---------------------------------------------------
            stop = await mcp.call_tool(
                MCPToolName.STOP_SERVER,
                {
                    "request": {
                        "client_id": client_id,
                        "namespace": DEFAULT_NAMESPACE,
                        "server_id": server_id,
                    }
                },
            )
            await wait_for_mcp_operation(mcp, stop.structured_content["operation_id"], timeout=120.0)
            active_server_id = None

            # 6. Echo tool must be gone after stop ---------------------------------
            tools_after = await mcp.list_tools()
            assert expected_tool not in {t.name for t in tools_after}, (
                f"Tool {expected_tool!r} still listed after server stop"
            )

        finally:
            if active_server_id is not None and client_id is not None:
                try:
                    stop = await mcp.call_tool(
                        MCPToolName.STOP_SERVER,
                        {
                            "request": {
                                "client_id": client_id,
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
                                "client_id": client_id,
                                "namespace": DEFAULT_NAMESPACE,
                            }
                        },
                    )
                    op_id = stop_client.structured_content.get("operation_id")
                    if op_id is not None:
                        await wait_for_mcp_operation(mcp, op_id, timeout=120.0)
                except Exception:
                    pass
