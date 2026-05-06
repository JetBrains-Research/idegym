"""
E2E tests for mcp-steroid integration with PyCharm.

mcp-steroid (https://github.com/jonnyzzz/mcp-steroid) is a JetBrains plugin that
runs an MCP server inside the IDE's JVM, providing direct access to the project
model, semantic index, PSI tree, test runner, debugger, and VCS layer.

Key capabilities:
  - 9 MCP tools: steroid_open_project, steroid_list_projects, steroid_list_windows,
    steroid_execute_code (Kotlin), steroid_take_screenshot, steroid_input,
    steroid_action_discovery, steroid_fetch_resource, steroid_execute_feedback
  - 58 MCP resources covering LSP, refactoring, debugging, testing, and VCS

Unlike the bundled JetBrains MCP plugin (port 64342/SSE), mcp-steroid:
  - Binds on port 6315 (configurable via registry key mcp.steroid.server.port)
  - Uses the streamable HTTP MCP transport at /mcp
  - Runs inside the IDE JVM with full IntelliJ Platform API access

Tests
-----
``test_mcp_steroid_pycharm``
    PyCharm + mcp-steroid starts without a project. After startup,
    mcp-steroid is reachable on port 6315, MCP initialize succeeds,
    tools/list returns at least ``steroid_open_project`` and ``steroid_list_projects``,
    then opens the python-project test project using the ``steroid_open_project`` MCP tool
    and verifies that the project is successfully opened and available in the IDE.

Downloads PyCharm (~800 MB); takes 10-15 min end-to-end. Excluded from CI.
Run with: ``pytest -m 'e2e and ide_integrations'``
"""

import json
from importlib.resources import files

import pytest
import resources as e2e_resources
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.pycharm.image import PyCharm
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

# PyCharm 2026.1.1+ is required.
_PYCHARM_VERSION = "2026.1.1"

# mcp-steroid binds on 127.0.0.1:6315; endpoint is /mcp (streamable HTTP).
_MCP_STEROID_URL = "http://localhost:6315/mcp"

# PyCharm resources: more memory than a plain server because the IDE + mcp-steroid JVM
# runs alongside the IdeGYM server process.
_PYCHARM_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="1000m", memory="4Gi", ephemeral_storage="12Gi"),
    limits=ResourceQuantities(cpu="2000m", memory="8Gi", ephemeral_storage="12Gi"),
)

# Shell script that polls mcp-steroid until it responds to MCP initialize (up to 300s).
_WAIT_MCP_STEROID_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_wait_300s.sh").read_text(encoding="utf-8")

# mcp-steroid tools that must always be present (subset of the full 9-tool set).
# Full list from https://github.com/jonnyzzz/mcp-steroid README:
#   steroid_open_project, steroid_list_projects, steroid_list_windows, steroid_execute_code,
#   steroid_take_screenshot, steroid_input, steroid_action_discovery, steroid_fetch_resource,
#   steroid_execute_feedback
_REQUIRED_TOOLS = {"steroid_open_project", "steroid_list_projects"}

# Project path inside the container (Project.from_local copies here).
_PROJECT_PATH = "/root/work"


@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_mcp_steroid_pycharm(test_id):
    """PyCharm + mcp-steroid starts, lists tools, then opens python-project via MCP.

    Build pipeline:
      Project.from_local("e2e-tests/test_projects/python-project", target="/root/work")
      → PyCharm(open_project=False, mcp_steroid=True)
      → Downloads mcp-steroid 0.94.0 ZIP from GitHub releases
      → Installs to ${PYCHARM_DIR}/plugins/
      → Copies start-pycharm-mcp-steroid.sh (waits for port 6315, not 64342)

    Runtime sequence (supervisord → start-pycharm-mcp-steroid.sh):
      1. PyCharm launches with Xvfb (no project argument).
      2. mcp-steroid plugin loads and binds on 127.0.0.1:6315.
      3. socat bridges 0.0.0.0:6316 → 127.0.0.1:6315.

    The test verifies:
      - MCP initialize succeeds on /mcp
      - tools/list returns at least steroid_open_project and steroid_list_projects
      - steroid_open_project opens python-project successfully
      - steroid_list_projects returns the opened project
    """
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"pycharm-mcp-steroid-{test_id}")
        .with_plugin(
            Project.from_local(
                "e2e-tests/test_projects/python-project",
                target=_PROJECT_PATH,
            )
        )
        # Start PyCharm with Xvfb without a project; mcp-steroid handles project opening.
        .with_plugin(PyCharm(version=_PYCHARM_VERSION, open_project=False, mcp_steroid=True))
    )

    built = image.build()
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag=image_tag, timeout=600)

    async with create_http_client(
        name=f"pycharm-mcp-steroid-{test_id}",
        nodes_count=0,
        request_timeout_in_seconds=600,
    ) as client:
        async with client.with_server(
            image_tag=image_tag,
            server_name=f"pycharm-mcp-steroid-server-{test_id}",
            run_as_root=True,
            resources=_PYCHARM_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server:
            # Wait for mcp-steroid (PyCharm starts asynchronously via supervisord).
            wait_result = await server.execute_bash(
                script=_WAIT_MCP_STEROID_SCRIPT,
                command_timeout=320.0,
            )
            assert wait_result.exit_code == 0, (
                f"mcp-steroid did not become ready within 300s.\n"
                f"stdout: {wait_result.stdout}\n"
                f"stderr: {wait_result.stderr}"
            )

            # --- tools/list -------------------------------------------------
            # MCP initialize must be sent first on each new connection.
            tools_result = await server.execute_bash(
                script=f"""
curl -s --max-time 10 \\
    -H "Content-Type: application/json" \\
    -H "Accept: application/json, text/event-stream" \\
    -d '{{"jsonrpc":"2.0","id":1,"method":"initialize","params":{{"protocolVersion":"2025-03-26","capabilities":{{}},"clientInfo":{{"name":"idegym-e2e","version":"0.1.0"}}}}}}' \\
    {_MCP_STEROID_URL} > /dev/null 2>&1
curl -s --max-time 10 \\
    -H "Content-Type: application/json" \\
    -H "Accept: application/json, text/event-stream" \\
    -d '{{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{{}}}}' \\
    {_MCP_STEROID_URL}
""",
                command_timeout=30.0,
            )
            assert tools_result.exit_code == 0, f"tools/list request failed:\n{tools_result.stderr}"

            # The last line of stdout is the tools/list JSON response.
            lines = [ln for ln in tools_result.stdout.strip().splitlines() if ln.strip()]
            response = json.loads(lines[-1])
            assert "result" in response, f"tools/list returned no result field.\nResponse: {response}"

            tool_names = {t["name"] for t in response["result"]["tools"]}

            print(f"\nmcp-steroid tools available ({len(tool_names)}):")
            for name in sorted(tool_names):
                print(f"  - {name}")

            for expected_tool in _REQUIRED_TOOLS:
                assert expected_tool in tool_names, (
                    f"Required mcp-steroid tool {expected_tool!r} not found.\nAvailable tools: {sorted(tool_names)}"
                )

            # --- Open project via steroid_open_project tool ----------------
            # steroid_open_project requires: project_path, task_id, reason.
            open_result = await server.execute_bash(
                script=f"""
curl -s --max-time 10 \\
    -H "Content-Type: application/json" \\
    -H "Accept: application/json, text/event-stream" \\
    -d '{{"jsonrpc":"2.0","id":1,"method":"initialize","params":{{"protocolVersion":"2025-03-26","capabilities":{{}},"clientInfo":{{"name":"idegym-e2e","version":"0.1.0"}}}}}}' \\
    {_MCP_STEROID_URL} > /dev/null 2>&1
curl -s --max-time 30 \\
    -H "Content-Type: application/json" \\
    -H "Accept: application/json, text/event-stream" \\
    -d '{{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{{"name":"steroid_open_project","arguments":{{"project_path":"{_PROJECT_PATH}","task_id":"e2e-test","reason":"E2E test: verify project opens via mcp-steroid"}}}}}}' \\
    {_MCP_STEROID_URL}
""",
                command_timeout=60.0,
            )
            assert open_result.exit_code == 0, f"steroid_open_project call failed:\n{open_result.stderr}"

            lines = [ln for ln in open_result.stdout.strip().splitlines() if ln.strip()]
            if lines:
                response = json.loads(lines[-1])
                print(f"\nsteroid_open_project response: {json.dumps(response, indent=2)}")
                if "error" in response:
                    raise AssertionError(f"steroid_open_project failed: {response['error']}")

            # --- Poll steroid_list_projects until the project appears -------
            # Wait up to 60s for the project to appear (IDE may take time to index).
            for attempt in range(12):  # 12 attempts × 5s = 60s
                list_result = await server.execute_bash(
                    script=f"""
curl -s --max-time 10 \\
    -H "Content-Type: application/json" \\
    -H "Accept: application/json, text/event-stream" \\
    -d '{{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{{"name":"steroid_list_projects","arguments":{{}}}}}}' \\
    {_MCP_STEROID_URL}
""",
                    command_timeout=15.0,
                )
                assert list_result.exit_code == 0, f"steroid_list_projects call failed:\n{list_result.stderr}"

                lines = [ln for ln in list_result.stdout.strip().splitlines() if ln.strip()]
                if lines:
                    response = json.loads(lines[-1])
                    if "result" in response:
                        result_content = response.get("result", {})
                        if "content" in result_content:
                            content = result_content["content"]
                            if isinstance(content, list) and content:
                                projects_text = content[0].get("text", "")
                                print(f"\nAttempt {attempt + 1}: steroid_list_projects:\n{projects_text}")
                                if _PROJECT_PATH in projects_text:
                                    print(f"\n✓ Project {_PROJECT_PATH} successfully opened and listed!")
                                    return

                if attempt < 11:
                    await server.execute_bash(script="sleep 5", command_timeout=10.0)

            raise AssertionError(
                f"Project {_PROJECT_PATH} did not appear in steroid_list_projects after 60 seconds.\n"
                f"Last response: " + (lines[-1] if lines else "no response")
            )
