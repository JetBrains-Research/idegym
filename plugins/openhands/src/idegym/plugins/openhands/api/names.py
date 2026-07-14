"""Canonical, stable string constants shared across REST, MCP, and the client.

Route paths, the MCP namespace, and canonical tool names are defined here once so the three
transports cannot drift out of sync.
"""

from enum import StrEnum

# API contract version. Bumped when request/response models change incompatibly.
API_VERSION = "1"

# Public IdeGYM route prefix. Mounted by the server plugin under the ``/api`` base, giving
# ``/api/openhands/...`` in the running container.
PUBLIC_PREFIX = "/openhands"

# Internal loopback service path prefix.
INTERNAL_PREFIX = "/v1"

# MCP namespace under which the plugin's upstream tools are mounted in the IdeGYM gateway.
MCP_NAMESPACE = "openhands"

# Config filename (without extension) written to ``/etc/idegym/mcp-upstreams.d/`` — must equal
# the image plugin's registered type name so the gateway mounts it under ``MCP_NAMESPACE``.
PLUGIN_NAME = "openhands"


class ToolFamily(StrEnum):
    """OpenHands upstream tool families."""

    TERMINAL = "terminal"
    FILE_EDITOR = "file_editor"
    APPLY_PATCH = "apply_patch"
    GREP = "grep"
    GLOB = "glob"
    PLANNING_FILE_EDITOR = "planning_file_editor"
    TASK_TRACKER = "task_tracker"
    GEMINI = "gemini"
    BROWSER = "browser_use"
    TASK = "task"
    WORKFLOW = "workflow"
    TOM_CONSULT = "tom_consult"
    DELEGATE = "delegate"
    PRESET = "preset"
    UTILS = "utils"


class ToolName(StrEnum):
    """Canonical tool names. Identical on all three surfaces."""

    TERMINAL = "terminal"
    FILE_EDITOR = "file_editor"
    APPLY_PATCH = "apply_patch"
    GREP = "grep"
    GLOB = "glob"
    PLANNING_FILE_EDITOR = "planning_file_editor"
    TASK_TRACKER = "task_tracker"
    READ_FILE = "read_file"
    WRITE_FILE = "write_file"
    EDIT = "edit"
    LIST_DIRECTORY = "list_directory"
