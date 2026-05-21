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

import asyncio
import base64
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
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT

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
_LIST_WINDOWS_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_list_windows.sh").read_text(encoding="utf-8")
_OPEN_PROJECT_SCRIPT = f"PROJECT_PATH={_PROJECT_PATH}\n" + files(e2e_resources).joinpath(
    "mcp_steroid_open_project.sh"
).read_text(encoding="utf-8")
_TAKE_SCREENSHOT_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_take_screenshot.sh").read_text(encoding="utf-8")


async def _take_screenshot(
    server, label: str, out_dir: str, project_name: str = "LightEditProject", window_id: str = ""
) -> None:
    env = f"PROJECT_NAME={project_name}\nTASK_ID={label}\nREASON={label}\n"
    if window_id:
        env += f"WINDOW_ID={window_id}\n"
    result = await server.execute_bash(script=env + _TAKE_SCREENSHOT_SCRIPT, command_timeout=20.0)
    if result.exit_code != 0:
        print(f"\nScreenshot [{label}]: script failed (exit_code={result.exit_code})")
        return
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        print(f"\nScreenshot [{label}]: empty response")
        return
    try:
        response = json.loads(lines[-1])
        content = response.get("result", {}).get("content", [])
        for item in content:
            if item.get("type") == "image" and item.get("data"):
                os.makedirs(out_dir, exist_ok=True)
                path = os.path.join(out_dir, f"{label}.png")
                with open(path, "wb") as f:
                    f.write(base64.b64decode(item["data"]))
                print(f"\nScreenshot [{label}] saved: {path}")
                return
        print(f"\nScreenshot [{label}]: no image in response: {lines[-1][:200]}")
    except Exception as e:
        print(f"\nScreenshot [{label}]: error: {e}")


async def _dismiss_modal_dialogs(server) -> bool:
    """Click through modal dialogs blocking the IDE EDT using xdotool.

    Finds dialog windows (visible, no projectName) and clicks at y=89% (button
    row) across three x positions (25%, 50%, 75%).  Falls back to Escape when
    no dialog window with explicit bounds is found.

    Returns True if a modal dialog was detected.
    """
    windows_result = await server.execute_bash(script=_LIST_WINDOWS_SCRIPT, command_timeout=15.0)
    if windows_result.exit_code != 0:
        return False
    lines = [ln for ln in windows_result.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        return False
    try:
        response = json.loads(lines[-1])
        content = response.get("result", {}).get("content", [])
        windows_data = json.loads(content[0].get("text", "{}")) if content else {}
        windows = windows_data.get("windows", [])
    except Exception:
        return False

    if not any(w.get("modalDialogShowing", False) for w in windows):
        return False

    dialog_windows = [w for w in windows if not w.get("projectName") and w.get("bounds")]
    if dialog_windows:
        for dialog in dialog_windows:
            b = dialog["bounds"]
            btn_y = b["y"] + int(b["height"] * 0.89)
            for x_pct in (0.25, 0.50, 0.75):
                btn_x = b["x"] + int(b["width"] * x_pct)
                await server.execute_bash(
                    script=f"xdotool mousemove {btn_x} {btn_y} click 1",
                    command_timeout=5.0,
                )
                await asyncio.sleep(0.3)
    else:
        await server.execute_bash(script="xdotool key --clearmodifiers Escape", command_timeout=5.0)

    return True


async def _take_screenshots_all_windows(server, label: str, out_dir: str) -> None:
    """List all open IDE windows and take a screenshot of each one."""
    windows_result = await server.execute_bash(script=_LIST_WINDOWS_SCRIPT, command_timeout=15.0)
    if windows_result.exit_code != 0:
        print(f"\nScreenshots [{label}]: list_windows failed")
        return
    lines = [ln for ln in windows_result.stdout.strip().splitlines() if ln.strip()]
    if not lines:
        print(f"\nScreenshots [{label}]: empty list_windows response")
        return
    try:
        response = json.loads(lines[-1])
        content = response.get("result", {}).get("content", [])
        windows_data = json.loads(content[0].get("text", "{}")) if content else {}
        windows = windows_data.get("windows", [])
    except Exception as e:
        print(f"\nScreenshots [{label}]: failed to parse list_windows: {e}")
        return

    if not windows:
        print(f"\nScreenshots [{label}]: no windows reported")
        return

    print(f"\nScreenshots [{label}]: {len(windows)} window(s) found: {json.dumps(windows)}")
    for i, window in enumerate(windows):
        project_name = window.get("projectName", "")
        if not project_name:
            print(f"\nScreenshots [{label}]: skipping window {i} (no projectName — likely a dialog)")
            continue
        window_id = window.get("windowId", window.get("id", ""))
        win_label = f"{label}_w{i}_{project_name}"
        await _take_screenshot(server, win_label, out_dir, project_name=project_name, window_id=window_id)


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
            screenshot_dir = f"/tmp/pycharm-screenshots/{test_id}"

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

            lines = [ln for ln in tools_result.stdout.strip().splitlines() if ln.strip()]
            response = json.loads(lines[-1])
            assert "result" in response, f"tools/list returned no result field.\nResponse: {response}"

            tool_names = {t["name"] for t in response["result"]["tools"]}

            # print(f"\nmcp-steroid tools response:\n{json.dumps(response, indent=2)}")
            print(f"\nmcp-steroid tools available ({len(tool_names)}):")
            for name in sorted(tool_names):
                print(f"  - {name}")

            for expected_tool in _REQUIRED_TOOLS:
                assert expected_tool in tool_names, (
                    f"Required mcp-steroid tool {expected_tool!r} not found.\nAvailable tools: {sorted(tool_names)}"
                )

            await _take_screenshots_all_windows(server, "00_before_open_project", screenshot_dir)

            await asyncio.sleep(30)

            await _take_screenshots_all_windows(server, "01_after_sleep_before_open", screenshot_dir)

            # --- Dismiss any startup modal dialogs before opening the project ----
            # The cwm-plugin descriptor error dialog appears ~20s after startup and
            # blocks the EDT, causing steroid_open_project to never execute.
            for attempt in range(10):
                dismissed = await _dismiss_modal_dialogs(server)
                if not dismissed:
                    break
                print(f"\nDismissed modal dialog (attempt {attempt + 1}), waiting 2s...")
                await asyncio.sleep(2)

            # --- Open project via steroid_open_project tool ----------------
            open_result = await server.execute_bash(script=_OPEN_PROJECT_SCRIPT, command_timeout=120.0)
            assert open_result.exit_code == 0, f"steroid_open_project call failed:\n{open_result.stderr}"
            open_lines = [ln for ln in open_result.stdout.strip().splitlines() if ln.strip()]
            if open_lines:
                open_response = json.loads(open_lines[-1])
                print(f"\nsteroid_open_project response: {json.dumps(open_response, indent=2)}")
                if "error" in open_response:
                    raise AssertionError(f"steroid_open_project failed: {open_response['error']}")

            # --- Poll steroid_list_windows for 2 minutes -------------------
            last_windows_text = ""
            project_in_windows = False
            deadline = time.monotonic() + 120  # 2 minutes
            next_screenshot_at = time.monotonic()  # take first screenshot immediately
            attempt = 0
            while True:
                windows_result = await server.execute_bash(script=_LIST_WINDOWS_SCRIPT, command_timeout=15.0)
                if windows_result.exit_code == 0:
                    lines = [ln for ln in windows_result.stdout.strip().splitlines() if ln.strip()]
                    if lines:
                        try:
                            response = json.loads(lines[-1])
                            content = response.get("result", {}).get("content", [])
                            if isinstance(content, list) and content:
                                windows_text = content[0].get("text", "")
                                last_windows_text = windows_text
                                print(f"\nAttempt {attempt + 1}: steroid_list_windows:\n{windows_text}")
                                windows_data = json.loads(windows_text)
                                windows = windows_data.get("windows", [])
                                if any(w.get("projectPath") == _PROJECT_PATH for w in windows):
                                    project_in_windows = True
                                    print(f"\nProject {_PROJECT_PATH} found in windows list.")
                                    break
                        except (json.JSONDecodeError, KeyError):
                            pass

                if time.monotonic() >= next_screenshot_at:
                    await _take_screenshots_all_windows(server, f"poll_{attempt:02d}", screenshot_dir)
                    next_screenshot_at = time.monotonic() + 30

                attempt += 1
                if time.monotonic() >= deadline:
                    print("\n2 minutes elapsed; project not found in windows list. Checking list_projects...")
                    break
                await asyncio.sleep(5)

            # --- Check steroid_list_projects (double-check or fallback) ----
            project_in_list = False
            list_result = await server.execute_bash(script=_LIST_PROJECTS_SCRIPT, command_timeout=15.0)
            if list_result.exit_code == 0:
                proj_lines = [ln for ln in list_result.stdout.strip().splitlines() if ln.strip()]
                if proj_lines:
                    try:
                        proj_response = json.loads(proj_lines[-1])
                        proj_content = proj_response.get("result", {}).get("content", [])
                        if isinstance(proj_content, list) and proj_content:
                            projects_text = proj_content[0].get("text", "")
                            print(f"\nsteroid_list_projects:\n{projects_text}")
                            project_in_list = _PROJECT_PATH in projects_text
                    except (json.JSONDecodeError, KeyError):
                        pass

            if project_in_windows or project_in_list:
                print(
                    f"\nProject {_PROJECT_PATH} opened successfully (in_windows={project_in_windows}, in_list={project_in_list})"
                )
                return

            # --- Both checks failed: collect debug info and fail -----------
            idea_log_result = await server.execute_bash(
                script="cat /tmp/ide-system/log/idea.log || true",
                command_timeout=15.0,
            )
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

            print(f"============LOGGGGSSSS========:\n{idea_log_result.stdout}\n{pycharm_log_result.stdout}\n")
            with open("thread-dumps.txt", "w") as f:
                f.write(f"============THREAD DUMPS========:\n{thread_dumps_result.stdout}\n")
            print("Thread dumps written to thread-dumps.txt")
            print(f"============last window response========:\n{last_windows_text}\n")
            raise AssertionError(
                f"Project {_PROJECT_PATH} not found after 2 minutes.\n"
                f"in_windows={project_in_windows}, in_list={project_in_list}\n"
            )
