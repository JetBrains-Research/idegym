"""Shared helpers for mcp-steroid GUI e2e tests (window listing + screenshots).

mcp-steroid exposes GUI-only MCP tools — ``steroid_list_windows`` and
``steroid_take_screenshot`` — that only return meaningful results when the IDE
runs against a real display: Xvfb for PyCharm (always) and for IntelliJ IDEA when
built with ``headless=False``. These helpers parse window listings, capture
per-window screenshots, and dismiss startup modal dialogs via ``xdotool``.

Used by both ``test_pycharm_mcp_steroid.py`` and ``test_idea_virtual_display.py``.
"""

import asyncio
import base64
import json
import os
from importlib.resources import files
from typing import Optional

import resources as e2e_resources
from idegym.utils.logging import get_logger
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

logger = get_logger(__name__)

_LIST_WINDOWS_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_list_windows.sh").read_text(encoding="utf-8")
_TAKE_SCREENSHOT_SCRIPT = files(e2e_resources).joinpath("mcp_steroid_take_screenshot.sh").read_text(encoding="utf-8")


class WindowBounds(BaseModel):
    x: int
    y: int
    width: int
    height: int


class IdeWindow(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")

    modal_dialog_showing: bool = False
    project_name: Optional[str] = None
    bounds: Optional[WindowBounds] = None
    project_path: Optional[str] = None
    window_id: Optional[str] = None
    id: Optional[str] = None


class McpWindowsResult(BaseModel):
    windows: list[IdeWindow] = Field(default_factory=list)
    raw_text: str = ""


def nonempty_lines(stdout: str) -> list[str]:
    return [ln for ln in stdout.strip().splitlines() if ln.strip()]


def parse_mcp_windows(result) -> McpWindowsResult:
    """Parse the windows list from a steroid_list_windows bash result.

    Returns McpWindowsResult with empty fields on any parse failure.
    """
    lines = nonempty_lines(result.stdout)
    if not lines:
        return McpWindowsResult()
    try:
        response = json.loads(lines[-1])
        content = response.get("result", {}).get("content", [])
        if not content:
            return McpWindowsResult()
        raw_text = content[0].get("text", "")
        windows = [IdeWindow.model_validate(w) for w in json.loads(raw_text).get("windows", [])]
        return McpWindowsResult(windows=windows, raw_text=raw_text)
    except Exception:
        logger.warning(f"steroid_list_windows parse failed: {result.stdout}\n{result.stderr}")
        return McpWindowsResult()


async def list_windows(server) -> McpWindowsResult:
    """Call steroid_list_windows via execute_bash and return the parsed result."""
    result = await server.execute_bash(script=_LIST_WINDOWS_SCRIPT, command_timeout=15.0)
    if result.exit_code != 0:
        logger.warning(f"steroid_list_windows failed (exit_code={result.exit_code}): {result.stderr}")
        return McpWindowsResult()
    return parse_mcp_windows(result)


async def take_screenshot(server, label: str, out_dir: str, project_name: str, window_id: Optional[str] = None) -> None:
    env = f"PROJECT_NAME={project_name}\nTASK_ID={label}\nREASON={label}\n"
    if window_id:
        env += f"WINDOW_ID={window_id}\n"
    result = await server.execute_bash(script=env + _TAKE_SCREENSHOT_SCRIPT, command_timeout=20.0)
    if result.exit_code != 0:
        logger.warning(
            f"Screenshot [{label}]: script failed (exit_code={result.exit_code}, "
            f"stdout={result.stdout}, stderr={result.stderr})"
        )
        return
    lines = nonempty_lines(result.stdout)
    if not lines:
        logger.warning(f"Screenshot [{label}]: empty response")
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
                logger.info(f"Screenshot [{label}] saved: {path}")
                return
        logger.warning(f"Screenshot [{label}]: no image in response: {lines[-1][:400]}")
    except Exception as e:
        logger.error(f"Screenshot [{label}]: error: {e}")


async def dismiss_modal_dialogs(server) -> bool:
    """Click through modal dialogs blocking the IDE EDT using xdotool.

    Finds dialog windows (visible, no projectName) and clicks at y=89% (button
    row) across three x positions (25%, 50%, 75%).  Falls back to Escape when
    no dialog window with explicit bounds is found.

    Returns True if a modal dialog was detected.
    """
    parsed = await list_windows(server)
    if not parsed.windows or not any(w.modal_dialog_showing for w in parsed.windows):
        return False

    dialog_windows = [w for w in parsed.windows if not w.project_name and w.bounds]
    if dialog_windows:
        for dialog in dialog_windows:
            b = dialog.bounds
            btn_y = b.y + int(b.height * 0.89)
            for x_pct in (0.25, 0.50, 0.75):
                btn_x = b.x + int(b.width * x_pct)
                await server.execute_bash(
                    script=f"xdotool mousemove {btn_x} {btn_y} click 1",
                    command_timeout=5.0,
                )
                await asyncio.sleep(0.3)
    else:
        await server.execute_bash(script="xdotool key --clearmodifiers Escape", command_timeout=5.0)

    return True


async def take_screenshots_all_windows(server, label: str, out_dir: str) -> None:
    """List all open IDE windows and take a screenshot of each one."""
    parsed = await list_windows(server)
    if not parsed.windows:
        logger.warning(f"Screenshots [{label}]: no windows reported")
        return
    logger.info(f"Screenshots [{label}]: {len(parsed.windows)} window(s) found")
    for i, window in enumerate(parsed.windows):
        if not window.project_name:
            logger.debug(f"Screenshots [{label}]: skipping window {i} (no projectName — likely a dialog)")
            continue
        win_label = f"{label}_w{i}_{window.project_name}"
        await take_screenshot(
            server, win_label, out_dir, project_name=window.project_name, window_id=window.window_id or window.id
        )
