"""E2E test for IntelliJ IDEA running with a virtual display (``headless=False``).

Requires IDEA 2026.1.1+. Older versions are not supported.

Unlike the default headless build (see ``test_idea.py`` / ``test_idea_mcp_steroid.py``),
``Idea(headless=False)`` installs Xvfb (+ xdotool) and the ``start-ide`` entrypoint launches the
IDE against a virtual X11 display on ``:99`` — the same way PyCharm always runs. This
enables the GUI-only mcp-steroid tools (``steroid_list_windows``, ``steroid_take_screenshot``)
that return nothing in true headless mode.

This test mirrors ``test_pycharm_mcp_steroid.py`` (windows + screenshots), and adds the
checks that are specific to the virtual-display feature (Xvfb alive, ``DISPLAY=:99``,
``IDEA_HEADLESS=false``).

``test_idea_virtual_display_windows``
    Builds IDEA + mcp-steroid with ``headless=False``. Runtime sequence
    (via supervisord → the start-ide entrypoint):
    1. Xvfb starts on :99 and IDEA launches with a real AWT toolkit (not headless).
    2. mcp-steroid loads and binds on 127.0.0.1:6315; socat bridges 0.0.0.0:6316.
    The test verifies mcp-steroid is ready, the IDE genuinely runs under the virtual
    display, ``steroid_list_windows`` reports rendered IDE windows (impossible headless),
    then opens java-project via ``steroid_open_project`` and confirms it opens.

Downloads IDEA (~800 MB); takes 10-15 min end-to-end. Runs in CI in the
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
from idegym.plugins.idea.image import Idea
from idegym.utils.logging import get_logger
from utils.build_images import minikube_load_image
from utils.constants import DEFAULT_SERVER_START_TIMEOUT
from utils.mcp_steroid import (
    list_windows,
    nonempty_lines,
    take_screenshots_all_windows,
)

logger = get_logger(__name__)

_LOCAL_BASE_IMAGE = "ghcr.io/jetbrains-research/idegym/server-debian-bookworm-20250520-slim:latest"

# IDEA 2026.1.1+ is required.
_IDEA_VERSION = "2026.1.1"

# Project path inside the container (Project.from_local copies here).
_PROJECT_PATH = "/root/work"

# Running under Xvfb (not headless) plus the mcp-steroid JVM needs more memory than
# a plain server, so size this like the PyCharm mcp-steroid test.
_IDEA_RESOURCES = KubernetesResources(
    requests=ResourceQuantities(cpu="1000m", memory="4Gi", ephemeral_storage="12Gi"),
    limits=ResourceQuantities(cpu="2000m", memory="8Gi", ephemeral_storage="12Gi"),
)

# mcp-steroid tools that must always be present (subset of the full 9-tool set).
_REQUIRED_TOOLS = {"steroid_open_project", "steroid_list_projects", "steroid_list_windows"}

_WAIT_MCP_STEROID_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_wait_300s.sh").read_text(encoding="utf-8")
_TOOLS_LIST_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_tools_list.sh").read_text(encoding="utf-8")
_LIST_PROJECTS_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_list_projects.sh").read_text(encoding="utf-8")
_OPEN_PROJECT_SCRIPT = f"PROJECT_PATH={_PROJECT_PATH}\n" + files(e2e_resources).joinpath(
    "mcp_steroid_open_project.sh"
).read_text(encoding="utf-8")

# Verifies the IDE is running under the virtual display rather than headless. The image
# bakes DISPLAY / IDEA_HEADLESS as ENV, and the start-ide entrypoint spawns Xvfb — all visible to
# execute_bash, which inherits the server process environment (cleanenv keeps them).
_VIRTUAL_DISPLAY_CHECK = r"""
set -eu
echo "IDEA_HEADLESS=${IDEA_HEADLESS:-<unset>}"
echo "DISPLAY=${DISPLAY:-<unset>}"
[ "${IDEA_HEADLESS:-}" = "false" ] || { echo "FAIL: IDEA_HEADLESS is not false"; exit 1; }
[ "${DISPLAY:-}" = ":99" ] || { echo "FAIL: DISPLAY is not :99"; exit 1; }
pgrep -x Xvfb > /dev/null || { echo "FAIL: Xvfb process is not running"; exit 1; }
echo "SUCCESS: IDEA running under virtual display"
"""


@pytest.mark.ide_integrations
@pytest.mark.asyncio
async def test_idea_virtual_display_windows(test_id):
    """IDEA with a virtual display (headless=False) renders GUI windows via mcp-steroid.

    Build pipeline:
      Project.from_local("e2e-tests/test_projects/java-project", target="/root/work")
      → Idea(headless=False, open_project=False, mcp_steroid=True)
      → Installs Xvfb + xdotool, downloads mcp-steroid, copies the start-ide entrypoint (waits for 6315)

    The test verifies:
      - mcp-steroid becomes ready on /mcp
      - the IDE is genuinely under the virtual display (Xvfb alive, DISPLAY=:99,
        IDEA_HEADLESS=false) — none of which hold in the default headless build
      - tools/list includes the GUI tools (steroid_list_windows, ...)
      - steroid_list_windows reports at least one rendered IDE window (impossible headless)
      - steroid_open_project opens java-project, which then shows up in the windows /
        projects listing

    On failure, idea.log, launcher log, and the last windows response are written to
    /tmp/idea-artifacts/<test_id>/ for post-mortem inspection.
    """
    from utils.idegym_utils import create_http_client

    image = (
        Image.from_base(_LOCAL_BASE_IMAGE)
        .named(f"idea-vdisplay-{test_id}")
        .with_plugin(
            Project.from_local(
                "e2e-tests/test_projects/java-project",
                target=_PROJECT_PATH,
            )
        )
        # Virtual display (headless=False); mcp-steroid handles project opening at runtime.
        .with_plugin(Idea(version=_IDEA_VERSION, headless=False, open_project=False, mcp_steroid=True))
    )

    built = image.build()
    image_tag = str(built.repo_tags[0])
    minikube_load_image(image_tag=image_tag, timeout=600)

    async with (
        create_http_client(
            name=f"idea-vdisplay-{test_id}",
            nodes_count=0,
            request_timeout_in_seconds=600,
        ) as client,
        client.with_server(
            image_tag=image_tag,
            server_name=f"idea-vdisplay-server-{test_id}",
            run_as_root=True,
            resources=_IDEA_RESOURCES,
            server_start_wait_timeout_in_seconds=DEFAULT_SERVER_START_TIMEOUT,
        ) as server,
    ):
        artifacts_dir = f"/tmp/idea-artifacts/{test_id}"

        # --- Wait for mcp-steroid (IDEA starts asynchronously via supervisord) ---
        wait_result = await server.execute_bash(script=_WAIT_MCP_STEROID_SCRIPT, command_timeout=320.0)
        assert wait_result.exit_code == 0, (
            f"mcp-steroid did not become ready within 300s.\nstdout: {wait_result.stdout}\nstderr: {wait_result.stderr}"
        )

        # --- Virtual-display feature check (deterministic; false in headless build) ---
        vdisplay = await server.execute_bash(script=_VIRTUAL_DISPLAY_CHECK)
        assert vdisplay.exit_code == 0 and "SUCCESS" in vdisplay.stdout, (
            f"IDE is not running under a virtual display.\nstdout:\n{vdisplay.stdout}\nstderr:\n{vdisplay.stderr}"
        )

        # --- tools/list: GUI tools must be present ----------------------
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

        # --- steroid_list_windows must report a rendered window (GUI-under-display proof) ---
        # In true headless mode no AWT windows exist, so this list would be empty.
        windows_seen = False
        deadline = time.monotonic() + 90
        while True:
            parsed = await list_windows(server)
            if parsed.windows:
                windows_seen = True
                logger.info(
                    f"steroid_list_windows reported {len(parsed.windows)} window(s): "
                    f"{[w.project_name or 'welcome/dialog' for w in parsed.windows]}"
                )
                break
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(5)
        await take_screenshots_all_windows(server, "00_startup", artifacts_dir)
        assert windows_seen, (
            "steroid_list_windows reported no windows within 90s — the IDE is not "
            "rendering under the virtual display (would be headless)."
        )

        # --- Open java-project via steroid_open_project -----------------
        open_result = await server.execute_bash(script=_OPEN_PROJECT_SCRIPT, command_timeout=120.0)
        assert open_result.exit_code == 0, f"steroid_open_project call failed:\n{open_result.stderr}"
        open_lines = nonempty_lines(open_result.stdout)
        if open_lines:
            open_response = json.loads(open_lines[-1])
            logger.info(f"steroid_open_project response: {json.dumps(open_response, indent=2)}")
            if "error" in open_response:
                raise AssertionError(f"steroid_open_project failed: {open_response['error']}")

        # --- Poll steroid_list_windows for the opened project (2 min) ---
        last_windows_text = ""
        project_in_windows = False
        deadline = time.monotonic() + 120
        next_screenshot_at = time.monotonic()
        attempt = 0
        while True:
            parsed = await list_windows(server)
            if parsed.raw_text:
                last_windows_text = parsed.raw_text
            if any(w.project_path == _PROJECT_PATH for w in parsed.windows):
                project_in_windows = True
                logger.info(f"Project {_PROJECT_PATH} found in windows list.")
                break
            if time.monotonic() >= next_screenshot_at:
                await take_screenshots_all_windows(server, f"poll_{attempt:02d}", artifacts_dir)
                next_screenshot_at = time.monotonic() + 30
            attempt += 1
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(5)

        # --- steroid_list_projects (double-check / fallback) ------------
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
                f"Project {_PROJECT_PATH} opened successfully "
                f"(in_windows={project_in_windows}, in_list={project_in_list})"
            )
            return

        # --- Both checks failed: collect debug info and fail ------------
        idea_log = await server.execute_bash("cat /tmp/ide-system/log/idea.log || true", command_timeout=15.0)
        launcher_log = await server.execute_bash("cat /tmp/idea.log || true", command_timeout=15.0)
        os.makedirs(artifacts_dir, exist_ok=True)
        for filename, file_content in (
            ("idea.log", idea_log.stdout),
            ("launcher.log", launcher_log.stdout),
            ("last_windows.json", last_windows_text),
        ):
            path = os.path.join(artifacts_dir, filename)
            with open(path, "w") as f:
                f.write(file_content)
            logger.info(f"Debug file written: {path}")

        raise AssertionError(
            f"Project {_PROJECT_PATH} not found after opening.\n"
            f"in_windows={project_in_windows}, in_list={project_in_list}\n"
            f"Debug files written to: {artifacts_dir}\n"
        )
