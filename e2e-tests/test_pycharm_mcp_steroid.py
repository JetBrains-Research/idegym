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

The window-listing / screenshot / dialog-dismissal helpers are shared with the
IDEA virtual-display test in ``utils.mcp_steroid``.

Tests
-----
``test_mcp_steroid_pycharm``
    PyCharm + mcp-steroid starts without a project. After startup,
    mcp-steroid is reachable on port 6315, MCP initialize succeeds,
    tools/list returns at least ``steroid_open_project`` and ``steroid_list_projects``,
    then opens the python-project test project using the ``steroid_open_project`` MCP tool
    and verifies that the project is successfully opened and available in the IDE.

Downloads PyCharm (~800 MB); takes 10-15 min end-to-end. Runs in CI in the
``e2e-ide-integrations`` job (``pytest -m 'e2e and ide_integrations'``).
"""

import asyncio
import json
import os
import time
from importlib.resources import files

import pytest
import resources as e2e_resources
from idegym.api.resources import KubernetesResources, ResourceQuantities
from idegym.image.builder import Image
from idegym.plugins.defaults.image import Project
from idegym.plugins.pycharm.image import PyCharm
from idegym.utils.logging import get_logger
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.mcp_steroid import (
    dismiss_modal_dialogs,
    list_windows,
    nonempty_lines,
    take_screenshots_all_windows,
)

logger = get_logger(__name__)

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

# PyCharm 2026.1.1+ is required.
_PYCHARM_VERSION = "2026.1.1"

# PyCharm resources: more memory than a plain server because the IDE + mcp-steroid JVM
# runs alongside the IdeGYM server process.
_PYCHARM_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="1000m", memory="4Gi", ephemeral_storage="12Gi"),
    limits=ResourceQuantities(cpu="2000m", memory="8Gi", ephemeral_storage="12Gi"),
)

# Project path inside the container (Project.from_local copies here).
_PROJECT_PATH = "/root/work"

# mcp-steroid tools that must always be present (subset of the full 9-tool set).
# Full list from https://github.com/jonnyzzz/mcp-steroid README:
#   steroid_open_project, steroid_list_projects, steroid_list_windows, steroid_execute_code,
#   steroid_take_screenshot, steroid_input, steroid_action_discovery, steroid_fetch_resource,
#   steroid_execute_feedback
_REQUIRED_TOOLS = {"steroid_open_project", "steroid_list_projects"}

_WAIT_MCP_STEROID_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_wait_300s.sh").read_text(encoding="utf-8")
_TOOLS_LIST_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_tools_list.sh").read_text(encoding="utf-8")
_LIST_PROJECTS_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_list_projects.sh").read_text(encoding="utf-8")
_OPEN_PROJECT_SCRIPT = f"PROJECT_PATH={_PROJECT_PATH}\n" + files(e2e_resources).joinpath(
    "mcp_steroid_open_project.sh"
).read_text(encoding="utf-8")


@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_mcp_steroid_pycharm(test_id: str):
    """PyCharm + mcp-steroid starts, lists tools, then opens python-project via MCP.

    Build pipeline:
      Project.from_local("e2e-tests/test_projects/python-project", target="/root/work")
      → PyCharm(open_project=False, mcp_steroid=True)
      → Downloads mcp-steroid 0.94.0 ZIP from GitHub releases
      → Installs to ${IDE_DIR}/plugins/
      → Copies the start-ide entrypoint (mcp-steroid mode: waits for port 6315, not 64342)

    Runtime sequence (supervisord → the start-ide entrypoint):
      1. PyCharm launches with Xvfb (no project argument).
      2. mcp-steroid plugin loads and binds on 127.0.0.1:6315.
      3. socat bridges 0.0.0.0:6316 → 127.0.0.1:6315.

    The test verifies:
      - MCP initialize succeeds on /mcp
      - tools/list returns at least steroid_open_project and steroid_list_projects
      - startup modal dialogs (e.g. cwm-plugin error) are dismissed via xdotool before opening
      - steroid_open_project opens python-project without errors
      - steroid_list_windows reports the project within 2 minutes (primary check)
      - steroid_list_projects also reports the project (fallback / double-check)

    On failure, idea.log, pycharm.log, thread dumps, and the last list_windows response
    are written to /tmp/pycharm-artifacts/<test_id>/ for post-mortem inspection.
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
            artifacts_dir = f"/tmp/pycharm-artifacts/{test_id}"

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
            tools_result = await server.execute_bash(script=_TOOLS_LIST_SCRIPT, command_timeout=30.0)
            assert tools_result.exit_code == 0, f"tools/list request failed:\n{tools_result.stderr}"

            lines = nonempty_lines(tools_result.stdout)
            assert lines, "tools/list returned empty output"
            response = json.loads(lines[-1])
            assert "result" in response, f"tools/list returned no result field.\nResponse: {response}"

            tool_names = {t["name"] for t in response["result"]["tools"]}
            logger.info(f"mcp-steroid tools available ({len(tool_names)}): {', '.join(sorted(tool_names))}")

            for expected_tool in _REQUIRED_TOOLS:
                assert expected_tool in tool_names, (
                    f"Required mcp-steroid tool {expected_tool!r} not found.\nAvailable tools: {sorted(tool_names)}"
                )

            await take_screenshots_all_windows(server, "00_before_open_project", artifacts_dir)

            # --- Dismiss any startup modal dialogs before opening the project ----
            for attempt in range(10):
                dismissed = await dismiss_modal_dialogs(server)
                if not dismissed:
                    break
                logger.info(f"Dismissed modal dialog (attempt {attempt + 1}), waiting 2s...")
                await asyncio.sleep(2)

            # --- Open project via steroid_open_project tool ----------------
            open_result = await server.execute_bash(script=_OPEN_PROJECT_SCRIPT, command_timeout=120.0)
            assert open_result.exit_code == 0, f"steroid_open_project call failed:\n{open_result.stderr}"
            open_lines = nonempty_lines(open_result.stdout)
            if open_lines:
                open_response = json.loads(open_lines[-1])
                logger.info(f"steroid_open_project response: {json.dumps(open_response, indent=2)}")
                if "error" in open_response:
                    raise AssertionError(f"steroid_open_project failed: {open_response['error']}")

            # --- Poll steroid_list_windows for 2 minutes -------------------
            last_windows_text = ""
            project_in_windows = False
            deadline = time.monotonic() + 120  # 2 minutes
            next_screenshot_at = time.monotonic()  # take first screenshot immediately
            attempt = 0
            while True:
                parsed = await list_windows(server)
                if parsed.raw_text:
                    last_windows_text = parsed.raw_text
                    logger.debug(f"Attempt {attempt + 1}: steroid_list_windows: {parsed.raw_text}")
                if any(w.project_path == _PROJECT_PATH for w in parsed.windows):
                    project_in_windows = True
                    logger.info(f"Project {_PROJECT_PATH} found in windows list.")
                    break

                if time.monotonic() >= next_screenshot_at:
                    await take_screenshots_all_windows(server, f"poll_{attempt:02d}", artifacts_dir)
                    next_screenshot_at = time.monotonic() + 30

                attempt += 1
                if time.monotonic() >= deadline:
                    logger.warning("2 minutes elapsed; project not found in windows list. Checking list_projects...")
                    break
                await asyncio.sleep(5)

            # --- Check steroid_list_projects (double-check or fallback) ----
            project_in_list = False
            list_result = await server.execute_bash(script=_LIST_PROJECTS_SCRIPT, command_timeout=15.0)
            if list_result.exit_code == 0:
                proj_lines = nonempty_lines(list_result.stdout)
                if proj_lines:
                    try:
                        proj_response = json.loads(proj_lines[-1])
                        content = proj_response.get("result", {}).get("content", [])
                        if content:
                            projects_text = content[0].get("text", "")
                            logger.info(f"steroid_list_projects: {projects_text}")
                            project_in_list = _PROJECT_PATH in projects_text
                    except (json.JSONDecodeError, KeyError):
                        pass

            if project_in_windows or project_in_list:
                logger.info(
                    f"Project {_PROJECT_PATH} opened successfully"
                    f" (in_windows={project_in_windows}, in_list={project_in_list})"
                )
                return

            # --- Both checks failed: collect debug info and fail -----------
            idea_log_result = await server.execute_bash(
                script="cat /tmp/ide-system/log/idea.log || true",
                command_timeout=15.0,
            )
            logger.info(f"Idea log:\n{idea_log_result.stdout}")
            pycharm_log_result = await server.execute_bash(
                script="cat /tmp/pycharm.log || true",
                command_timeout=15.0,
            )
            thread_dumps_result = await server.execute_bash(
                script=(
                    "for f in /tmp/ide-system/log/bg-wa/thread-dump-*.txt; do "
                    '[ -f "$f" ] || continue; '
                    'echo "=== THREAD DUMP: $f ==="; cat "$f"; echo; '
                    "done"
                ),
                command_timeout=30.0,
            )

            os.makedirs(artifacts_dir, exist_ok=True)
            for filename, file_content in (
                ("idea.log", idea_log_result.stdout),
                ("pycharm.log", pycharm_log_result.stdout),
                ("thread-dumps.txt", thread_dumps_result.stdout),
                ("last_windows.json", last_windows_text),
            ):
                path = os.path.join(artifacts_dir, filename)
                with open(path, "w") as f:
                    f.write(file_content)
                logger.info(f"Debug file written: {path}")

            raise AssertionError(
                f"Project {_PROJECT_PATH} not found after 2 minutes.\n"
                f"in_windows={project_in_windows}, in_list={project_in_list}\n"
                f"Debug files written to: {artifacts_dir}\n"
            )
