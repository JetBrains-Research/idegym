"""Single compatibility module for all version-specific OpenHands construction.

Nothing else in the package imports ``openhands`` directly. Everything here is lazy and guarded so
the package imports cleanly when ``openhands-sdk`` / ``openhands-tools`` are absent (dev checkout and
the client-only path). When OpenHands *is* installed — inside the container image — these helpers
reuse OpenHands' own terminal sessions, tool executors, tool definitions, and MCP schema generation
instead of reimplementing any of them — which is the whole point of the plugin.

Verified against ``openhands-sdk`` / ``openhands-tools`` == 1.36.0 (see ``COMPATIBILITY.md``). The
``openhands`` compatibility test re-verifies these construction paths where
OpenHands is importable and is skipped otherwise. Keep every brittle upstream detail in this file.
"""

import importlib
import importlib.metadata
import os
import pkgutil
from types import SimpleNamespace
from typing import Any, Optional, Protocol, runtime_checkable

# Pinned, compatible versions. Bump together with COMPATIBILITY.md and the image plugin default.
PINNED_OPENHANDS_SDK = "1.36.0"
PINNED_OPENHANDS_TOOLS = "1.36.0"


@runtime_checkable
class OpenHandsTool(Protocol):
    """The subset of the OpenHands ``ToolDefinition`` surface this plugin invokes.

    Typing the tool against this Protocol instead of ``Any`` gives a type-checker/IDE a compile-time
    contract for every OpenHands tool call the plugin makes (dispatch, schema, annotations) rather
    than opaque dynamic attribute access. The real SDK cannot be imported at type-check time — its
    dependency tree conflicts with the IdeGYM environment (why the service runs in its own venv; see
    ``COMPATIBILITY.md``) — so this Protocol captures the contract locally, and the ``openhands``
    compatibility test re-verifies it against the real SDK at runtime.
    """

    name: str
    description: str
    annotations: Any

    def action_from_arguments(self, arguments: dict[str, Any]) -> Any:
        """Build the tool's typed action from raw MCP arguments."""

    async def acall(self, action: Any, conversation: Optional[Any] = None) -> Any:
        """Execute the tool for ``action`` and return its observation."""

    def to_mcp_tool(self) -> dict[str, Any]:
        """Return the MCP tool descriptor (name/description/inputSchema/annotations)."""


# Top-level tool families present under ``openhands.tools`` in the pinned revision. The catalog
# audit test fails if the installed set differs.
KNOWN_TOOL_FAMILIES = (
    "apply_patch",
    "browser_use",
    "delegate",
    "file_editor",
    "gemini",
    "glob",
    "grep",
    "planning_file_editor",
    "preset",
    "task",
    "task_tracker",
    "terminal",
    "tom_consult",
    "utils",
    "workflow",
)


def _try_version(dist: str) -> Optional[str]:
    try:
        return importlib.metadata.version(dist)
    except importlib.metadata.PackageNotFoundError:
        return None


def openhands_versions() -> dict[str, Optional[str]]:
    return {"openhands-sdk": _try_version("openhands-sdk"), "openhands-tools": _try_version("openhands-tools")}


def openhands_available() -> bool:
    try:
        importlib.import_module("openhands.tools")
        return True
    except Exception:
        return False


def list_tool_family_modules() -> list[str]:
    """Return top-level submodule names under ``openhands.tools`` (for the catalog audit)."""
    package = importlib.import_module("openhands.tools")
    return sorted(m.name for m in pkgutil.iter_modules(package.__path__))


# ---------------------------------------------------------------------------
# Terminal: one retained pinned session per handle
# ---------------------------------------------------------------------------


def build_terminal_session(
    *,
    work_dir: str,
    terminal_type: str,
    env: Optional[dict[str, str]],
    username: Optional[str] = None,
    no_change_timeout_seconds: Optional[int] = None,
) -> Any:
    """Create and initialise ONE retained OpenHands terminal session pinned to a pane/process.

    ``create_terminal_session`` returns a single ``TerminalSession`` (the ``TmuxPanePool`` logic
    lives only in ``TerminalExecutor``), so the handle keeps stable pane/process affinity for its
    lifetime — never a pooled checkout.
    """
    terminal = importlib.import_module("openhands.tools.terminal")
    session = terminal.create_terminal_session(
        work_dir=work_dir,
        username=username,
        no_change_timeout_seconds=no_change_timeout_seconds,
        terminal_type=terminal_type,
        env=env,
    )
    session.initialize()
    return session


def terminal_action(
    command: str, *, is_input: bool = False, timeout: Optional[float] = None, reset: bool = False
) -> Any:
    terminal = importlib.import_module("openhands.tools.terminal")
    return terminal.TerminalAction(command=command, is_input=is_input, timeout=timeout, reset=reset)


def observation_text(obs: Any) -> str:
    """Concatenate the text of an OpenHands observation's content blocks."""
    parts = []
    for block in getattr(obs, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    return "".join(parts)


def observation_images(obs: Any) -> list[str]:
    """Return base64 image payloads from an OpenHands observation's content blocks."""
    images = []
    for block in getattr(obs, "content", []) or []:
        data = getattr(block, "image_urls", None) or getattr(block, "image_url", None)
        if isinstance(data, list):
            images.extend(str(d) for d in data)
        elif isinstance(data, str):
            images.append(data)
    return images


# ---------------------------------------------------------------------------
# Tools: reuse OpenHands executors + ToolDefinition, no LLM/agent
# ---------------------------------------------------------------------------


def _agentless_conv_state(working_dir: str, persistence_dir: str, env_persistence_dir: str) -> Any:
    """A narrow, agent-free compatibility context.

    It carries only the workspace and persistence paths OpenHands tool factories legitimately read.
    It has no ``agent`` and no ``llm``; factories that would inspect agent/LLM attributes are
    constructed directly instead (see ``_build_file_editor``).
    """
    os.makedirs(persistence_dir, exist_ok=True)
    os.makedirs(env_persistence_dir, exist_ok=True)
    return SimpleNamespace(
        workspace=SimpleNamespace(working_dir=working_dir),
        persistence_dir=persistence_dir,
        env_observation_persistence_dir=env_persistence_dir,
    )


def _build_file_editor(working_dir: str) -> list[OpenHandsTool]:
    """Construct ``file_editor`` directly (executor + definition), avoiding any fake agent.

    ``FileEditorTool.create`` reads ``conv_state.agent.llm.vision_is_active()`` only to toggle a
    cosmetic image-viewing line in the description. We replicate the real construction without an
    agent.
    """
    fe = importlib.import_module("openhands.tools.file_editor")
    impl = importlib.import_module("openhands.tools.file_editor.impl")
    annotations = importlib.import_module("openhands.sdk.tool").ToolAnnotations
    executor = impl.FileEditorExecutor(workspace_root=working_dir)
    description = getattr(fe, "TOOL_DESCRIPTION", "String-replacement based file editor.")
    return [
        fe.FileEditorTool(
            action_type=fe.FileEditorAction,
            observation_type=fe.FileEditorObservation,
            description=f"{description}\n\nYour current working directory is: {working_dir}",
            annotations=annotations(
                title="file_editor",
                readOnlyHint=False,
                destructiveHint=True,
                idempotentHint=False,
                openWorldHint=False,
            ),
            executor=executor,
        )
    ]


def build_family_tools(
    family: str,
    *,
    working_dir: str,
    persistence_dir: str,
    env_persistence_dir: str,
) -> list[OpenHandsTool]:
    """Return the agentless ``ToolDefinition`` instances a family contributes.

    Most families are built via their ``create(conv_state)`` classmethod with the narrow
    workspace/persistence shim. ``file_editor`` is built directly to avoid touching agent/LLM
    attributes. Returns an empty list for families that expose no standalone tool here.
    """
    if family == "file_editor":
        return _build_file_editor(working_dir)

    module = importlib.import_module(f"openhands.tools.{family}")
    shim = _agentless_conv_state(working_dir, persistence_dir, env_persistence_dir)
    tools: list[OpenHandsTool] = []
    for attr in dir(module):
        cls = getattr(module, attr)
        # Only concrete tool classes defined in this family module (skip the imported SDK base).
        if not isinstance(cls, type) or not attr.endswith("Tool"):
            continue
        if not getattr(cls, "__module__", "").startswith(f"openhands.tools.{family}"):
            continue
        create = getattr(cls, "create", None)
        if create is None:
            continue
        tools.extend(create(shim))
    return tools


def tool_mcp_schema(tool: OpenHandsTool) -> dict[str, Any]:
    """Return the MCP tool descriptor (name/description/inputSchema/annotations/outputSchema)."""
    return tool.to_mcp_tool()


def tool_annotations(tool: OpenHandsTool) -> dict[str, bool]:
    """Extract OpenHands annotation hints as plain bools."""
    ann = getattr(tool, "annotations", None)
    return {
        "read_only": bool(getattr(ann, "readOnlyHint", False)),
        "destructive": bool(getattr(ann, "destructiveHint", False)),
        "idempotent": bool(getattr(ann, "idempotentHint", False)),
        "open_world": bool(getattr(ann, "openWorldHint", False)),
    }
