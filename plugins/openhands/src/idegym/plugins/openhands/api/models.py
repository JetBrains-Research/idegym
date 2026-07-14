"""Shared Pydantic request/response models — the stable contract for REST, MCP, and the client.

Every field here is versioned via :data:`idegym.plugins.openhands.api.names.API_VERSION`. Request models
use ``extra="forbid"`` so a misspelled field is rejected rather than silently ignored; response
models carry forward-compatible ``metadata``/``structured`` dicts.
"""

from datetime import datetime
from enum import StrEnum
from typing import Any, Optional

from idegym.plugins.openhands.api.names import API_VERSION
from pydantic import BaseModel, ConfigDict, Field


class TerminalBackend(StrEnum):
    """Selectable terminal execution backends."""

    TMUX = "tmux"
    SUBPROCESS = "subprocess"


class CallStatus(StrEnum):
    """Result status for a tool/terminal call."""

    COMPLETED = "completed"
    RUNNING = "running"
    INTERRUPTED = "interrupted"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    LOST = "lost"


class TerminalState(StrEnum):
    """Lifecycle state of a terminal handle."""

    READY = "ready"
    RUNNING = "running"
    EXITED = "exited"
    LOST = "lost"
    CLOSING = "closing"
    CLOSED = "closed"


class SupportStatus(StrEnum):
    """Per-tool capability status."""

    ENABLED = "enabled"
    DISABLED_BY_PROFILE = "disabled_by_profile"
    MISSING_DEPENDENCY = "missing_dependency"
    UNSUPPORTED_REQUIRES_AGENT = "unsupported_requires_agent"
    NOT_A_CALLABLE_TOOL = "not_a_callable_tool"
    ADAPTER_INCOMPATIBLE = "adapter_incompatible_with_pinned_version"


class Profile(StrEnum):
    """Tool exposure profile."""

    CORE = "core"
    FULL = "full"
    CUSTOM = "custom"


class ContentType(StrEnum):
    TEXT = "text"
    IMAGE = "image"
    RESOURCE_LINK = "resource_link"


class _Strict(BaseModel):
    """Base for request models: reject unknown fields to catch typos."""

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Content, artifacts, context
# ---------------------------------------------------------------------------


class ContentBlock(BaseModel):
    """A single block of result content (text, image, or resource link)."""

    type: ContentType = ContentType.TEXT
    text: Optional[str] = None
    # Base64 payload for image content.
    data: Optional[str] = None
    mime_type: Optional[str] = None
    # Retrieval URL for a resource link (e.g. an artifact download route).
    uri: Optional[str] = None

    @classmethod
    def of_text(cls, text: str) -> "ContentBlock":
        return cls(type=ContentType.TEXT, text=text)


class ArtifactDescriptor(BaseModel):
    """Metadata for an oversized output saved to the artifact store."""

    artifact_id: str
    media_type: str = "text/plain"
    size_bytes: int = 0
    filename: Optional[str] = None
    # Public retrieval path under ``/api/openhands/artifacts/{artifact_id}``.
    url: Optional[str] = None
    created_at: Optional[datetime] = None


class ToolCallContext(_Strict):
    """Transport context carried alongside a tool's action arguments."""

    terminal_id: Optional[str] = None
    browser_id: Optional[str] = None
    request_id: Optional[str] = None


class ToolCallRequest(_Strict):
    """Generic tool-call body used by ``POST /api/openhands/call``."""

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    context: ToolCallContext = Field(default_factory=ToolCallContext)


class ToolActionRequest(_Strict):
    """Per-tool route body: action ``arguments`` plus transport ``context``."""

    arguments: dict[str, Any] = Field(default_factory=dict)
    context: ToolCallContext = Field(default_factory=ToolCallContext)


class ToolCallResult(BaseModel):
    """Stable result envelope shared across all transports."""

    call_id: str
    tool: str
    status: CallStatus = CallStatus.COMPLETED
    is_error: bool = False
    content: list[ContentBlock] = Field(default_factory=list)
    structured: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None

    def text(self) -> str:
        """Concatenate text content blocks (convenience for callers)."""
        return "".join(b.text or "" for b in self.content if b.type == ContentType.TEXT)


# ---------------------------------------------------------------------------
# Capability / discovery
# ---------------------------------------------------------------------------


class ToolCapability(BaseModel):
    """Explicit capability record for one OpenHands tool family/tool."""

    name: str
    family: str
    status: SupportStatus
    reason: Optional[str] = None
    required_extras: list[str] = Field(default_factory=list)
    required_binaries: list[str] = Field(default_factory=list)
    state_scope: str = "stateless"
    read_only: bool = False
    destructive: bool = False
    idempotent: bool = False
    open_world: bool = False
    rest_route: Optional[str] = None
    mcp_name: Optional[str] = None
    client_method: Optional[str] = None


class ToolDescriptor(BaseModel):
    """An enabled, callable tool with its schemas."""

    name: str
    family: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: Optional[dict[str, Any]] = None
    annotations: dict[str, Any] = Field(default_factory=dict)


class ToolSchemaResponse(BaseModel):
    api_version: str = API_VERSION
    name: str
    family: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: Optional[dict[str, Any]] = None
    annotations: dict[str, Any] = Field(default_factory=dict)


class TerminalBackendStatus(BaseModel):
    """Availability/probe result for one terminal backend."""

    backend: TerminalBackend
    available: bool
    enabled: bool
    version: Optional[str] = None
    detail: Optional[str] = None


class BackendConfigView(BaseModel):
    """The configured default + allowed backends and their probe status."""

    default: TerminalBackend
    allowed: list[TerminalBackend]
    statuses: list[TerminalBackendStatus] = Field(default_factory=list)


class Diagnostics(BaseModel):
    """Diagnostics payload embedded in capabilities/health."""

    plugin_version: str = ""
    openhands_sdk_version: Optional[str] = None
    openhands_tools_version: Optional[str] = None
    profile: Profile = Profile.CORE
    catalog_summary: dict[str, int] = Field(default_factory=dict)
    browser_available: bool = False
    workspace_root: str = ""
    state_dir: str = ""
    output_dir: str = ""
    environment_generation: int = 0


class CapabilityResponse(BaseModel):
    api_version: str = API_VERSION
    profile: Profile = Profile.CORE
    capabilities: list[ToolCapability] = Field(default_factory=list)
    backends: BackendConfigView
    diagnostics: Diagnostics


class HealthResponse(BaseModel):
    api_version: str = API_VERSION
    live: bool = True
    ready: bool = False
    checks: dict[str, bool] = Field(default_factory=dict)
    backends: BackendConfigView
    environment_generation: int = 0
    detail: Optional[str] = None


# ---------------------------------------------------------------------------
# Terminals
# ---------------------------------------------------------------------------


class TerminalCreateRequest(_Strict):
    """Body for ``POST /api/openhands/terminals``."""

    backend: Optional[TerminalBackend] = None
    name: Optional[str] = None
    cwd: Optional[str] = None
    env: dict[str, str] = Field(default_factory=dict)
    no_change_timeout: Optional[float] = None
    cols: Optional[int] = None
    rows: Optional[int] = None


class TerminalDescriptor(BaseModel):
    """External view of a terminal handle. Sensitive backend metadata is redacted."""

    terminal_id: str
    name: Optional[str] = None
    backend: TerminalBackend
    generation: int
    workspace_root: str
    initial_cwd: str
    state: TerminalState
    created_at: datetime
    last_activity_at: datetime
    last_exit_code: Optional[int] = None
    last_working_dir: Optional[str] = None
    capture_supported: bool = True
    environment_id: str
    is_default: bool = False


class TerminalExecuteRequest(_Strict):
    """Body for ``.../execute`` and the canonical ``terminal`` tool."""

    command: str
    timeout: Optional[float] = None
    # When true, the text is sent as input to the current foreground command (compat with the
    # canonical OpenHands ``terminal`` tool's ``is_input`` field).
    is_input: bool = False
    # When true, destroy and recreate the backend before running the command.
    reset: bool = False
    request_id: Optional[str] = None


class TerminalInputRequest(_Strict):
    """Body for ``.../input``: text or a special key (e.g. ``C-c``, ``C-d``)."""

    text: str
    timeout: Optional[float] = None
    request_id: Optional[str] = None


class TerminalPollRequest(_Strict):
    timeout: Optional[float] = None


class TerminalResult(BaseModel):
    """Result of a terminal operation."""

    call_id: str
    terminal_id: str
    backend: TerminalBackend
    generation: int
    state: TerminalState
    status: CallStatus
    is_error: bool = False
    output: str = ""
    running: bool = False
    exit_code: Optional[int] = None
    working_dir: Optional[str] = None
    content: list[ContentBlock] = Field(default_factory=list)
    artifacts: list[ArtifactDescriptor] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Browser (models present; adapters deferred to the full profile)
# ---------------------------------------------------------------------------


class BrowserCreateRequest(_Strict):
    name: Optional[str] = None


class BrowserDescriptor(BaseModel):
    browser_id: str
    name: Optional[str] = None
    state: str = "ready"
    is_default: bool = False
    created_at: datetime


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class ResetResponse(BaseModel):
    reset: bool = True
    reason: str = ""
    environment_generation: int = 0
    terminated_terminals: int = 0
    terminated_browsers: int = 0
